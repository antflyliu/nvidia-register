#!/usr/bin/env python3
"""prototype/step2_drag_apply_verify.py — 阶段 3 Step 2 验证（drag 回填半验证）。

在 step1b（判图准确率已验证）基础上，验证 drag 回填的坐标映射 + 拖拽动作：
判图 → _apply_drag 执行拖拽 → 暂停（不点 submit，不完成注册）。

人眼确认：形状是否被拖到轮廓里。
- 拖拽到位 + 形状贴合轮廓 → 坐标映射正确，_human_drag 动作 OK
- 拖拽位置偏 / 没拖动 → 坐标映射或 _human_drag 有 bug

只验证 drag（hCaptcha 对 camoufox 优先下发 drag）。grid/point 回填验证留后续。
不点 .button-submit，不等 token，不注入，不完成注册——纯回填动作验证。

用法（需本机已起 camoufox-turnstile 服务，且能连真实 NVIDIA 会话）：
  set NV_EMAIL=you@example.com
  set NV_PASSWORD=SomePass123
  set CLASSIFY_URL=http://127.0.0.1:5072
  set HUMANIZE=false
  python prototype/step2_drag_apply_verify.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# 让 prototype/ 能 import 顶层 main/config/captcha
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from camoufox.async_api import AsyncCamoufox

import main as main_mod
from captcha import ClassifySolver

CLASSIFY_URL = os.environ.get("CLASSIFY_URL", "http://127.0.0.1:5072").rstrip("/")
NV_EMAIL = os.environ.get("NV_EMAIL", "")
NV_PASSWORD = os.environ.get("NV_PASSWORD", "")
HOLD_SECONDS = 300

# camoufox humanize 控制（同 step1b）。
_hum_raw = os.environ.get("HUMANIZE", "true").strip().lower()
if _hum_raw in ("false", "0", "no", "off"):
    HUMANIZE: bool | float = False
elif _hum_raw.replace(".", "", 1).isdigit():
    HUMANIZE = float(_hum_raw)
else:
    HUMANIZE = True


async def _hold(seconds: int, why: str) -> None:
    print(f"\n[step2] 浏览器保持打开 {seconds}s：{why}")
    try:
        await asyncio.sleep(seconds)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


async def _navigate_to_password_filled(page) -> bool:
    """复用 main.py 真实链路到「密码已填、hCaptcha widget 就绪」（同 step1b）。"""
    print("  [step2] goto build.nvidia.com ...")
    await page.goto("https://build.nvidia.com/", wait_until="domcontentloaded", timeout=90000)
    await main_mod._accept_cookie_banner(page)
    print("  [step2] open signin modal ...")
    if not await main_mod._open_signin_modal(page):
        print("  [step2] Login button / signin modal not found")
        return False
    import captcha as captcha_mod
    captcha_mod.start_capturing_sitekey(page)
    await main_mod._ensure_hcaptcha_hook(page)
    print("  [step2] submit email → create-account page ...")
    if not await main_mod._submit_email_step(page, NV_EMAIL):
        return False
    print("  [step2] wait password field ...")
    if not await main_mod._wait_for_password_field(page):
        print("  [step2] password field never appeared")
        return False
    if not await main_mod._fill_visible_password_fields(page, NV_PASSWORD):
        print("  [step2] failed to fill password")
        return False
    try:
        checkbox = page.locator("#stay_signin_checkbox_v2-input")
        if await checkbox.count() > 0 and not await checkbox.is_checked():
            await checkbox.check()
    except Exception:
        pass
    print("  [step2] password filled, hCaptcha widget ready")
    return True


async def main() -> int:
    print("=" * 60)
    print("  prototype/step2_drag_apply_verify.py — 阶段 3 Step 2")
    print(f"  classify_url = {CLASSIFY_URL}")
    print("  验证：drag 判图 + 回填拖拽（不点提交，不完成注册）")
    print("=" * 60)

    if not NV_EMAIL or not NV_PASSWORD:
        print("\n[step2] NV_EMAIL / NV_PASSWORD 未设置")
        return 1

    solver = ClassifySolver(api_url=CLASSIFY_URL, timeout_seconds=60, humanize=False)

    print(f"  [step2] camoufox humanize = {HUMANIZE}")
    async with AsyncCamoufox(headless=False, humanize=HUMANIZE) as browser:
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        try:
            if not await _navigate_to_password_filled(page):
                print("\n[step2] 导航失败")
                await _hold(HOLD_SECONDS, "导航失败，可 F12 看当前页面")
                return 2

            # 1) 点 checkbox 弹挑战框
            print("\n  [step2] === 点 checkbox ===")
            t0 = time.monotonic()
            clicked = await solver._click_checkbox(page)
            print(f"  [step2] _click_checkbox = {clicked} ({(time.monotonic() - t0):.1f}s)")
            if not clicked:
                print("\n[step2] ❌ 点 checkbox 失败")
                await _hold(HOLD_SECONDS, "checkbox 点击失败")
                return 3

            # 2) 取图
            print("\n  [step2] === 取图 ===")
            challenge = await solver._capture_challenge(page)
            if challenge is None:
                print("\n[step2] ❌ 取图失败")
                await _hold(HOLD_SECONDS, "取图失败")
                return 4
            print(f"  [step2] type={challenge['captcha_type']} "
                  f"js={challenge['challenge_js_type']!r} "
                  f"q={challenge['question']!r} "
                  f"size={challenge['grid_w']}x{challenge['grid_h']} "
                  f"bbox=({challenge['bbox_x']:.0f},{challenge['bbox_y']:.0f})")

            # 非 drag：本脚本只验证 drag 回填，其他类型提示后退出
            if challenge["captcha_type"] != "drag":
                print(f"\n[step2] ⏭️  type={challenge['captcha_type']} 非 drag，"
                      f"本脚本只验证 drag 回填。重跑直到拿到 drag 挑战。")
                await _hold(HOLD_SECONDS, "非 drag 挑战，人眼对照")
                return 0

            # 3) 判图
            print("\n  [step2] === 判图 ===")
            t0 = time.monotonic()
            answer = solver._classify(challenge)
            print(f"  [step2] answer = {answer} ({(time.monotonic() - t0):.1f}s)")
            if answer is None:
                print("\n[step2] ❌ 判图失败")
                await _hold(HOLD_SECONDS, "判图失败")
                return 5

            # 4) 回填拖拽（只拖拽，不点 submit）—— 半验证核心
            print("\n  [step2] === 回填拖拽（不点提交）===")
            bbox_x = float(challenge.get("bbox_x", 0.0))
            bbox_y = float(challenge.get("bbox_y", 0.0))
            print(f"  [step2] bbox=({bbox_x:.0f},{bbox_y:.0f}) "
                  f"size={challenge['grid_w']}x{challenge['grid_h']}")
            t0 = time.monotonic()
            try:
                await solver._apply_drag(page, answer, bbox_x, bbox_y)
                print(f"  [step2] _apply_drag 完成 ({(time.monotonic() - t0):.1f}s)")
            except Exception as exc:
                print(f"\n[step2] ❌ _apply_drag 失败: {exc}")
                await _hold(HOLD_SECONDS, "拖拽失败，人眼查看挑战框状态")
                return 6

            print("\n[step2] ✅ 拖拽已执行（未点提交）")
            print("       人眼确认：形状是否被拖到轮廓里？")
            print("       - 贴合 → 坐标映射 + _human_drag OK，可进 Step 3（完整注册）")
            print("       - 偏移/没动 → 坐标映射或拖拽动作有 bug，需调试")
            await _hold(HOLD_SECONDS, "拖拽完成，人眼确认形状位置（未提交）")
            return 0
        finally:
            try:
                await browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
