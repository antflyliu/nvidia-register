#!/usr/bin/env python3
"""prototype/step1_classify_click_capture.py — 阶段 3 Step 1 验证。

验证 ClassifySolver 的「自动点 checkbox + 取图 + 判图」链路（不真点击提交，
不注入 token，不完成注册）。解决阶段 3 最大风险：checkbox 自动点击（prototype
真机验证 element.click 不可靠，此处用库的 page.mouse.click 视口坐标方式）。

复用 main.py 真实导航链路到「密码已填、hCaptcha widget 就绪」，然后调
ClassifySolver._click_checkbox + _capture_challenge + _classify + 可视化对照，
打印判图结果。不调 solve() 主流程（避免走 _apply_answer/_wait_hcaptcha_token stub
污染状态）。

用法（需本机已起 camoufox-turnstile 服务，且能连真实 NVIDIA 会话）：
  set NV_EMAIL=you@example.com
  set NV_PASSWORD=SomePass123
  set CLASSIFY_URL=http://127.0.0.1:5072
  python prototype/step1_classify_click_capture.py
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import sys
import time
from pathlib import Path

# 让 prototype/ 能 import 顶层 main/config/captcha
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import async_playwright

import main as main_mod
from captcha import ClassifySolver

CLASSIFY_URL = os.environ.get("CLASSIFY_URL", "http://127.0.0.1:5072").rstrip("/")
NV_EMAIL = os.environ.get("NV_EMAIL", "")
NV_PASSWORD = os.environ.get("NV_PASSWORD", "")
HOLD_SECONDS = 300


async def _hold(seconds: int, why: str) -> None:
    print(f"\n[step1] 浏览器保持打开 {seconds}s：{why}")
    try:
        await asyncio.sleep(seconds)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


def _annotate(png_b64: str, dtype: str, answer, out_path: Path) -> Path | None:
    """把答案画到截图上（grid 红框/point 红圈），存 PNG 供人眼对照。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("  PIL 不可用，跳过可视化")
        return None
    try:
        img = Image.open(io.BytesIO(base64.b64decode(png_b64))).convert("RGB")
        draw = ImageDraw.Draw(img)
        w, h = img.size
        try:
            font = ImageFont.truetype("arial.ttf", max(16, w // 30))
        except Exception:
            font = ImageFont.load_default()
        if dtype == "grid":
            cw, ch = w / 3, h / 3
            for n in answer or []:
                try:
                    idx = int(n) - 1
                except (ValueError, TypeError):
                    continue
                if not 0 <= idx <= 8:
                    continue
                row, col = idx // 3, idx % 3
                draw.rectangle([col * cw, row * ch, (col + 1) * cw, (row + 1) * ch],
                               outline=(255, 0, 0), width=max(3, w // 200))
                draw.text((col * cw + cw / 2 - 6, row * ch + ch / 2 - 8), str(n),
                          fill=(255, 0, 0), font=font)
        elif dtype == "point":
            r = max(12, w // 50)
            for i, pt in enumerate(answer or []):
                try:
                    x, y = int(pt[0]), int(pt[1])
                except (ValueError, TypeError, IndexError):
                    continue
                draw.ellipse([x - r, y - r, x + r, y + r], outline=(255, 0, 0),
                             width=max(3, w // 200))
                draw.line([x - r, y, x + r, y], fill=(255, 0, 0), width=2)
                draw.line([x, y - r, x, y + r], fill=(255, 0, 0), width=2)
                draw.text((x + r + 4, y - r), str(i + 1), fill=(255, 0, 0), font=font)
        else:
            return None
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(out_path))
        return out_path
    except Exception as exc:
        print(f"  可视化失败: {exc}")
        return None


async def _navigate_to_password_filled(page) -> bool:
    """复用 main.py 真实链路到「密码已填、hCaptcha widget 就绪」。"""
    print("  [step1] goto build.nvidia.com ...")
    await page.goto("https://build.nvidia.com/", wait_until="domcontentloaded", timeout=90000)
    await main_mod._accept_cookie_banner(page)
    print("  [step1] open signin modal ...")
    if not await main_mod._open_signin_modal(page):
        print("  [step1] Login button / signin modal not found")
        return False
    import captcha as captcha_mod
    captcha_mod.start_capturing_sitekey(page)
    await main_mod._ensure_hcaptcha_hook(page)
    print("  [step1] submit email → create-account page ...")
    if not await main_mod._submit_email_step(page, NV_EMAIL):
        return False
    print("  [step1] wait password field ...")
    if not await main_mod._wait_for_password_field(page):
        print("  [step1] password field never appeared")
        return False
    if not await main_mod._fill_visible_password_fields(page, NV_PASSWORD):
        print("  [step1] failed to fill password")
        return False
    # 保持登录勾选框（main.py:430 范式，用 .check() 不是 .click()）
    try:
        checkbox = page.locator("#stay_signin_checkbox_v2-input")
        if await checkbox.count() > 0 and not await checkbox.is_checked():
            await checkbox.check()
    except Exception:
        pass
    print("  [step1] password filled, hCaptcha widget ready")
    return True


async def main() -> int:
    print("=" * 60)
    print("  prototype/step1_classify_click_capture.py — 阶段 3 Step 1")
    print(f"  classify_url = {CLASSIFY_URL}")
    print("  验证：自动点 checkbox + 取图 + 判图（不点击提交）")
    print("=" * 60)

    if not NV_EMAIL or not NV_PASSWORD:
        print("\n[step1] NV_EMAIL / NV_PASSWORD 未设置")
        return 1

    solver = ClassifySolver(api_url=CLASSIFY_URL, timeout_seconds=60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        try:
            if not await _navigate_to_password_filled(page):
                print("\n[step1] 导航失败")
                await _hold(HOLD_SECONDS, "导航失败，可 F12 看当前页面")
                return 2

            # Step 1 核心：自动点 checkbox
            print("\n  [step1] === Step 1: 自动点 checkbox ===")
            t0 = time.monotonic()
            clicked = await solver._click_checkbox(page)
            print(f"  [step1] _click_checkbox = {clicked} "
                  f"({(time.monotonic() - t0):.1f}s)")
            if not clicked:
                print("\n[step1] ❌ 自动点 checkbox 失败（最大风险点）")
                await _hold(HOLD_SECONDS, "checkbox 点击失败，可 F12 抓 checkbox iframe")
                return 3
            print("  [step1] ✅ 自动点 checkbox 成功，挑战框已弹出")

            # 取图
            print("\n  [step1] === 取图 ===")
            challenge = await solver._capture_challenge(page)
            if challenge is None:
                print("\n[step1] ❌ 取图失败")
                await _hold(HOLD_SECONDS, "取图失败，可 F12 抓 challenge iframe")
                return 4
            print(f"  [step1] type={challenge['captcha_type']} "
                  f"js={challenge['challenge_js_type']!r} "
                  f"q={challenge['question']!r} "
                  f"size={challenge['grid_w']}x{challenge['grid_h']}")

            # drag/bbox 不判图
            if challenge["captcha_type"] in ("drag", "bbox", "unknown"):
                print(f"\n[step1] ⏭️  type={challenge['captcha_type']} 非 grid/point，不判图")
                await _hold(HOLD_SECONDS, "非 grid/point 挑战，人眼对照挑战框")
                return 0

            # 判图
            print("\n  [step1] === 判图 ===")
            t0 = time.monotonic()
            answer = solver._classify(challenge)
            print(f"  [step1] answer = {answer} ({(time.monotonic() - t0):.1f}s)")
            if answer is None:
                print("\n[step1] ❌ 判图失败")
                await _hold(HOLD_SECONDS, "判图失败")
                return 5
            print("  [step1] ✅ 判图成功")

            # 可视化对照
            out_path = Path(__file__).resolve().parent / "out" / (
                "step1_%s_%d.png" % (challenge["captcha_type"], int(time.time()))
            )
            saved = _annotate(challenge["queries"][0], challenge["captcha_type"], answer, out_path)
            if saved:
                print(f"\n  [step1] 🖼️  可视化对照图：{saved}")
                print("       打开该文件，红框/红圈=判图答案，对照原图判断是否命中。")

            print("\n[step1] ✅✅ Step 1 验证通过：自动点 checkbox + 取图 + 判图链路通")
            print("       （未点击提交、未注入 token、未完成注册）")
            await _hold(HOLD_SECONDS, "Step 1 完成，人眼对照可视化图 + 浏览器挑战框")
            return 0
        finally:
            await browser.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
