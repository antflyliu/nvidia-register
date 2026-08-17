#!/usr/bin/env python3
"""prototype/diag_challenge_js.py — 诊断 challenge.js 捕获失败。

对比两种 camoufox 启动方式在真实 NVIDIA 页面的 hCaptcha 网络请求捕获情况：
  方式 A：AsyncCamoufox() context manager（step1b 方式，已验证能捕获）
  方式 B：AsyncNewBrowser(p, ...) 复用外层 async_playwright()（main.py 方式）

打印所有 hCaptcha 相关网络请求 URL，定位 challenge.js 是否加载、URL 格式、
page.on("response") 是否在两种方式下行为一致。

用法：
  set NV_EMAIL=you@example.com
  set NV_PASSWORD=SomePass123
  set DIAG_MODE=A   # 或 B，默认 A
  python prototype/diag_challenge_js.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import async_playwright
from camoufox.async_api import AsyncNewBrowser, AsyncCamoufox
from camoufox.addons import DefaultAddons

import main as main_mod
from captcha import ClassifySolver, reset_captcha_state, start_capturing_sitekey
from config import load_config
from email_providers import build_email_provider
from passwords import generate_password

# 优先用环境变量（调试指定邮箱），否则用 config email_provider 自动创建临时邮箱
NV_EMAIL = os.environ.get("NV_EMAIL", "")
NV_PASSWORD = os.environ.get("NV_PASSWORD", "")
DIAG_MODE = os.environ.get("DIAG_MODE", "A").strip().upper()
CLASSIFY_URL = os.environ.get("CLASSIFY_URL", "http://127.0.0.1:5072").rstrip("/")

# 收集所有 hCaptcha 相关网络请求
_hcaptcha_requests: list[str] = []
_captured_challenge_js: str | None = None


def _make_on_response(label: str):
    def _on_response(resp):
        global _captured_challenge_js
        url = resp.url or ""
        # 只关心 hCaptcha 相关
        if "hcaptcha.com" not in url:
            return
        _hcaptcha_requests.append(url)
        # challenge.js 捕获（对齐 captcha.py 的 _on_response 条件）
        if not _captured_challenge_js:
            if "/challenge/" in url and url.endswith("/challenge.js"):
                _captured_challenge_js = url
                print(f"  [{label}] ★ captured challenge.js: {url}")
            else:
                # 打印所有非 challenge.js 的 hcaptcha 请求（前 30 个）
                if len(_hcaptcha_requests) <= 30:
                    short = url.split("?")[0][-80:]
                    print(f"  [{label}] hcaptcha resp: ...{short}")
    return _on_response


def _make_on_response_async(label: str):
    """async 版 _on_response，完全对齐 captcha.py:730 的 _on_response 逻辑。
    测试 async 回调是否影响 challenge.js 捕获。"""
    async def _on_response(resp):
        global _captured_challenge_js
        url = resp.url or ""
        if "hcaptcha.com" not in url:
            return
        _hcaptcha_requests.append(url)
        # challenge.js（类型识别）—— 对齐 captcha.py:733-735
        if not _captured_challenge_js:
            if "/challenge/" in url and url.endswith("/challenge.js"):
                _captured_challenge_js = url
                print(f"  [{label}] ★ captured challenge.js: {url}")
            else:
                if len(_hcaptcha_requests) <= 30:
                    short = url.split("?")[0][-80:]
                    print(f"  [{label}] hcaptcha resp: ...{short}")
        # /getcaptcha/ /checkcaptcha/ pass 检测——对齐 captcha.py:740-750
        if "/getcaptcha/" in url or "/checkcaptcha/" in url:
            try:
                ctype = resp.headers.get("content-type", "")
                if "json" in ctype:
                    data = await resp.json()
                    if data.get("pass"):
                        print(f"  [{label}] ★★ pass response: {data.get('generated_pass_UUID','')[:40]}")
            except Exception as exc:
                print(f"  [{label}] resp.json() error: {exc}")
    return _on_response


async def _navigate_to_password_filled(page) -> bool:
    """复用 main.py 真实链路到「密码已填、hCaptcha widget 就绪」。"""
    print("  [diag] goto build.nvidia.com ...")
    await page.goto("https://build.nvidia.com/", wait_until="domcontentloaded", timeout=90000)
    await main_mod._accept_cookie_banner(page)
    print("  [diag] open signin modal ...")
    if not await main_mod._open_signin_modal(page):
        print("  [diag] Login button / signin modal not found")
        return False
    start_capturing_sitekey(page)
    await main_mod._ensure_hcaptcha_hook(page)
    print("  [diag] submit email → create-account page ...")
    if not await main_mod._submit_email_step(page, NV_EMAIL):
        return False
    print("  [diag] wait password field ...")
    if not await main_mod._wait_for_password_field(page):
        print("  [diag] password field never appeared")
        return False
    if not await main_mod._fill_visible_password_fields(page, NV_PASSWORD):
        print("  [diag] failed to fill password")
        return False
    try:
        checkbox = page.locator("#stay_signin_checkbox_v2-input")
        if await checkbox.count() > 0 and not await checkbox.is_checked():
            await checkbox.check()
    except Exception:
        pass
    print("  [diag] password filled, hCaptcha widget ready")
    return True


async def _run_mode(label: str, launch_coro):
    """通用流程：启动浏览器 → 导航 → 挂监听 → 点 checkbox → 打印结果。"""
    global _hcaptcha_requests, _captured_challenge_js
    _hcaptcha_requests = []
    _captured_challenge_js = None

    reset_captcha_state()
    solver = ClassifySolver(api_url=CLASSIFY_URL, timeout_seconds=60, humanize=False)

    browser = await launch_coro
    page = await browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        if not await _navigate_to_password_filled(page):
            print(f"\n[{label}] 导航失败")
            await browser.close()
            return

        # 挂网络监听（在点 checkbox 前）
        page.on("response", _make_on_response(label))
        print(f"\n[{label} === 点 checkbox ===")

        t0 = time.monotonic()
        clicked = await solver._click_checkbox(page)
        print(f"  [{label}] _click_checkbox = {clicked} ({time.monotonic()-t0:.1f}s)")
        if not clicked:
            print(f"  [{label}] checkbox 点击失败")
            return

        # 等 challenge.js 捕获（最长 10s，对齐 _capture_challenge）
        print(f"  [{label}] waiting challenge.js ...")
        js_deadline = time.time() + 10
        while time.time() < js_deadline and not _captured_challenge_js:
            await asyncio.sleep(0.3)
        print(f"  [{label}] challenge.js = {_captured_challenge_js!r}")
        print(f"  [{label}] 共捕获 {len(_hcaptcha_requests)} 个 hcaptcha 请求")
        if _hcaptcha_requests:
            print(f"  [{label}] 最后 5 个 hcaptcha 请求:")
            for u in _hcaptcha_requests[-5:]:
                print(f"      {u.split('?')[0][-100:]}")
    finally:
        try:
            await browser.close()
        except Exception:
            pass


async def main() -> int:
    global NV_EMAIL, NV_PASSWORD
    print("=" * 60)
    print(f"  diag_challenge_js.py — DIAG_MODE={DIAG_MODE}")
    print(f"  CLASSIFY_URL={CLASSIFY_URL}")
    print("=" * 60)

    # 环境变量未设则用 config email_provider 自动创建临时邮箱（对齐 main.py 流程）
    if not NV_EMAIL or not NV_PASSWORD:
        print("\n[diag] NV_EMAIL/NV_PASSWORD 未设，用 config email_provider 自动创建")
        cfg = load_config()
        email_provider = build_email_provider(cfg)
        inbox_name = "nv" + str(int(time.time()))[-8:]
        inbox = email_provider.create_inbox(inbox_name)
        NV_EMAIL = inbox.address
        NV_PASSWORD = generate_password(12)
        print(f"[diag] Email: {NV_EMAIL}")
        print(f"[diag] Password: {NV_PASSWORD}")

    if DIAG_MODE == "A":
        # 方式 A：AsyncCamoufox context manager（step1b 方式）
        print("\n[mode A] AsyncCamoufox() context manager")
        async with AsyncCamoufox(headless=False, humanize=True, exclude_addons=[DefaultAddons.UBO]) as browser:
            # _run_mode 内会 close browser，但 context manager 退出时再 close 无害
            # 这里直接用 browser，不调 _run_mode 的 launch
            global _hcaptcha_requests, _captured_challenge_js
            _hcaptcha_requests = []
            _captured_challenge_js = None
            reset_captcha_state()
            solver = ClassifySolver(api_url=CLASSIFY_URL, timeout_seconds=60, humanize=False)
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            try:
                if not await _navigate_to_password_filled(page):
                    print("\n[mode A] 导航失败")
                    return 2
                page.on("response", _make_on_response("A"))
                print("\n[mode A] === 点 checkbox ===")
                t0 = time.monotonic()
                clicked = await solver._click_checkbox(page)
                print(f"  [A] _click_checkbox = {clicked} ({time.monotonic()-t0:.1f}s)")
                if clicked:
                    print("  [A] waiting challenge.js ...")
                    js_deadline = time.time() + 10
                    while time.time() < js_deadline and not _captured_challenge_js:
                        await asyncio.sleep(0.3)
                    print(f"  [A] challenge.js = {_captured_challenge_js!r}")
                    print(f"  [A] 共捕获 {len(_hcaptcha_requests)} 个 hcaptcha 请求")
                    for u in _hcaptcha_requests[-5:]:
                        print(f"      {u.split('?')[0][-100:]}")
            finally:
                pass  # context manager 退出会 close
        return 0

    if DIAG_MODE == "B":
        # 方式 B：AsyncNewBrowser 复用外层 async_playwright()（main.py 方式）
        print("\n[mode B] AsyncNewBrowser(p, ...) 复用外层 async_playwright()")
        async with async_playwright() as p:
            browser = await AsyncNewBrowser(
                p, headless=False, humanize=True, exclude_addons=[DefaultAddons.UBO],
            )
            await _run_mode("B", _already_launched(browser))
        return 0

    if DIAG_MODE == "C":
        # 方式 C：AsyncCamoufox + 只靠 solver._click_checkbox 内部 async handler
        # 完全复现 main.py 的 handler 挂载方式（不额外挂监听器），测 AsyncCamoufox 下
        # captcha.py:730 async def _on_response 能否捕获 challenge.js
        print("\n[mode C] AsyncCamoufox + 只靠 solver 内部 async handler（复现 main.py）")
        async with AsyncCamoufox(headless=False, humanize=True, exclude_addons=[DefaultAddons.UBO]) as browser:
            reset_captcha_state()
            solver = ClassifySolver(api_url=CLASSIFY_URL, timeout_seconds=60, humanize=False)
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            try:
                if not await _navigate_to_password_filled(page):
                    print("\n[mode C] 导航失败")
                    return 2
                print("\n[mode C] === solver._click_checkbox（内部挂 async handler）===")
                t0 = time.monotonic()
                clicked = await solver._click_checkbox(page)
                print(f"  [C] _click_checkbox = {clicked} ({time.monotonic()-t0:.1f}s)")
                if clicked:
                    print("  [C] waiting challenge.js ...")
                    js_deadline = time.time() + 10
                    while time.time() < js_deadline and not solver._captured_challenge_js:
                        await asyncio.sleep(0.3)
                    print(f"  [C] solver._captured_challenge_js = {solver._captured_challenge_js!r}")
            finally:
                pass
        return 0

    if DIAG_MODE == "D":
        # 方式 D：AsyncNewBrowser + 只靠 solver 内部 async handler（完全复现 main.py）
        print("\n[mode D] AsyncNewBrowser + 只靠 solver 内部 async handler（完全复现 main.py）")
        async with async_playwright() as p:
            browser = await AsyncNewBrowser(
                p, headless=False, humanize=True, exclude_addons=[DefaultAddons.UBO],
            )
            reset_captcha_state()
            solver = ClassifySolver(api_url=CLASSIFY_URL, timeout_seconds=60, humanize=False)
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            try:
                if not await _navigate_to_password_filled(page):
                    print("\n[mode D] 导航失败")
                    return 2
                print("\n[mode D] === solver._click_checkbox（内部挂 async handler）===")
                t0 = time.monotonic()
                clicked = await solver._click_checkbox(page)
                print(f"  [D] _click_checkbox = {clicked} ({time.monotonic()-t0:.1f}s)")
                if clicked:
                    print("  [D] waiting challenge.js ...")
                    js_deadline = time.time() + 10
                    while time.time() < js_deadline and not solver._captured_challenge_js:
                        await asyncio.sleep(0.3)
                    print(f"  [D] solver._captured_challenge_js = {solver._captured_challenge_js!r}")
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass
        return 0

    print(f"未知 DIAG_MODE={DIAG_MODE}，用 A / B / C / D")
    return 1


async def _already_launched(browser):
    """_run_mode 期望 launch_coro 是 awaitable，这里 browser 已启动，包一层。"""
    return browser


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
