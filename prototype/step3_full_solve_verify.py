#!/usr/bin/env python3
"""prototype/step3_full_solve_verify.py — 阶段 3 Step 3 完整链路验证。

在 step2（drag 回填半验证通过）基础上，跑完整 solver.solve()：
点 checkbox → 取图 → 判图 → 回填 → 点 submit → 等 /getcaptcha/ pass →
注入 token → 注册按钮 enable → 点 #register_button → 等注册接口受理。

到「register accepted」即完整链路验证通过（hCaptcha token 有效，注册接口
受理）。不继续走验证码/组织创建（与 hCaptcha 无关）。

用 camoufox（HUMANIZE=false），复用 step1b/step2 已验证的浏览器路径。
main.py 生产用普通 chromium，是独立兼容性问题，本脚本不覆盖。

用法（需本机已起 camoufox-turnstile 服务，且能连真实 NVIDIA 会话）：
  set NV_EMAIL=you@example.com
  set NV_PASSWORD=SomePass123
  set CLASSIFY_URL=http://127.0.0.1:5072
  set HUMANIZE=false
  python prototype/step3_full_solve_verify.py
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

# camoufox humanize 控制（同 step1b/step2）。
_hum_raw = os.environ.get("HUMANIZE", "true").strip().lower()
if _hum_raw in ("false", "0", "no", "off"):
    HUMANIZE: bool | float = False
elif _hum_raw.replace(".", "", 1).isdigit():
    HUMANIZE = float(_hum_raw)
else:
    HUMANIZE = True


async def _hold(seconds: int, why: str) -> None:
    print(f"\n[step3] 浏览器保持打开 {seconds}s：{why}")
    try:
        await asyncio.sleep(seconds)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


async def _navigate_to_password_filled(page) -> bool:
    """复用 main.py 真实链路到「密码已填、hCaptcha widget 就绪」（同 step1b/step2）。"""
    print("  [step3] goto build.nvidia.com ...")
    await page.goto("https://build.nvidia.com/", wait_until="domcontentloaded", timeout=90000)
    await main_mod._accept_cookie_banner(page)
    print("  [step3] open signin modal ...")
    if not await main_mod._open_signin_modal(page):
        print("  [step3] Login button / signin modal not found")
        return False
    import captcha as captcha_mod
    captcha_mod.start_capturing_sitekey(page)
    await main_mod._ensure_hcaptcha_hook(page)
    print("  [step3] submit email → create-account page ...")
    if not await main_mod._submit_email_step(page, NV_EMAIL):
        return False
    print("  [step3] wait password field ...")
    if not await main_mod._wait_for_password_field(page):
        print("  [step3] password field never appeared")
        return False
    if not await main_mod._fill_visible_password_fields(page, NV_PASSWORD):
        print("  [step3] failed to fill password")
        return False
    try:
        checkbox = page.locator("#stay_signin_checkbox_v2-input")
        if await checkbox.count() > 0 and not await checkbox.is_checked():
            await checkbox.check()
    except Exception:
        pass
    print("  [step3] password filled, hCaptcha widget ready")
    return True


async def main() -> int:
    print("=" * 60)
    print("  prototype/step3_full_solve_verify.py — 阶段 3 Step 3")
    print(f"  classify_url = {CLASSIFY_URL}")
    print("  验证：完整 solver.solve() + 点注册按钮（真完成注册受理）")
    print("=" * 60)

    if not NV_EMAIL or not NV_PASSWORD:
        print("\n[step3] NV_EMAIL / NV_PASSWORD 未设置")
        return 1

    solver = ClassifySolver(api_url=CLASSIFY_URL, timeout_seconds=60, humanize=False)

    print(f"  [step3] camoufox humanize = {HUMANIZE}")
    async with AsyncCamoufox(headless=False, humanize=HUMANIZE) as browser:
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        try:
            if not await _navigate_to_password_filled(page):
                print("\n[step3] 导航失败")
                await _hold(HOLD_SECONDS, "导航失败，可 F12 看当前页面")
                return 2

            # 1) 完整 solve：点 checkbox → 取图 → 判图 → 回填 → 提交 → 等 token → 注入
            print("\n  [step3] === solver.solve() 完整链路 ===")
            t0 = time.monotonic()
            solved = await solver.solve(page)
            print(f"  [step3] solver.solve() = {solved} "
                  f"({(time.monotonic() - t0):.1f}s)")
            if not solved:
                print("\n[step3] ❌ solver.solve() 失败（hCaptcha 未通过或 token 未拿到）")
                await _hold(HOLD_SECONDS, "solve 失败，人眼查看挑战框状态")
                return 3
            print("  [step3] ✅ hCaptcha 通过，token 已注入，注册按钮应已 enable")

            # 2) 点 #register_button 提交注册，等注册接口受理
            print("\n  [step3] === 点 #register_button 提交注册 ===")
            t0 = time.monotonic()
            register_result = await main_mod._click_register_and_wait_result(page)
            print(f"  [step3] register_result = {register_result!r} "
                  f"({(time.monotonic() - t0):.1f}s)")

            if register_result == "accepted":
                print("\n[step3] ✅✅✅ 完整链路验证通过！")
                print("  hCaptcha：checkbox → 取图 → 判图 → 回填 → 提交 → token → 注入 → 注册受理")
                print("  （未继续走验证码/组织创建，与 hCaptcha 无关）")
                await _hold(HOLD_SECONDS, "完整链路通过，人眼确认注册受理")
                return 0
            elif register_result == "email_exists":
                print("\n[step3] ⚠️  邮箱已注册（email_exists）——hCaptcha 链路本身通过，"
                      "但该邮箱已存在。换新邮箱重跑可验证 accepted。")
                await _hold(HOLD_SECONDS, "邮箱已注册，hCaptcha 链路已通过")
                return 0  # hCaptcha 链路通过，email_exists 不是 hCaptcha 问题
            else:
                print(f"\n[step3] ❌ 注册未受理（result={register_result}）")
                print("  可能原因：token 无效 / 注册接口拒 / 按钮未触发")
                await _hold(HOLD_SECONDS, "注册未受理，人眼查看页面状态")
                return 4
        finally:
            try:
                await browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
