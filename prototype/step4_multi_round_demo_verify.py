#!/usr/bin/env python3
"""prototype/step4_multi_round_demo_verify.py — 多轮挑战真机实测（hCaptcha demo）。

NVIDIA sitekey 只给单轮 drag，多轮逻辑（_solve_rounds round 2+）在该 sitekey
不触发。本脚本用 hCaptcha 官方 demo 页 + user_difficult sitekey 刷多轮挑战
（point 类型 100% 多轮，drag/grid 单轮），实测 _solve_rounds 多轮循环。

流程：camoufox 打开 demo 页 → 点 checkbox 弹挑战 → _solve_rounds 多轮循环
（取图→判图→回填→提交，crumb_count 轮）→ 等 pass token。到拿 token 即多轮
链路通过，不注入不检查注册按钮（demo 页无 #register_button）。

用法：
  set CLASSIFY_URL=http://127.0.0.1:5072
  set HUMANIZE=false
  set HC_DEMO_SITEKEY=3fac610f-4879-4fd5-919b-ca072a134a79
  python prototype/step4_multi_round_demo_verify.py
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

from captcha import ClassifySolver

CLASSIFY_URL = os.environ.get("CLASSIFY_URL", "http://127.0.0.1:5072").rstrip("/")
# hCaptcha demo + user_difficult sitekey（多轮概率高）。point 类型 100% 多轮。
HC_DEMO_SITEKEY = os.environ.get(
    "HC_DEMO_SITEKEY", "3fac610f-4879-4fd5-919b-ca072a134a79"
)
HOLD_SECONDS = 300

_hum_raw = os.environ.get("HUMANIZE", "true").strip().lower()
if _hum_raw in ("false", "0", "no", "off"):
    HUMANIZE: bool | float = False
elif _hum_raw.replace(".", "", 1).isdigit():
    HUMANIZE = float(_hum_raw)
else:
    HUMANIZE = True


async def _hold(seconds: int, why: str) -> None:
    print(f"\n[step4] 浏览器保持打开 {seconds}s：{why}")
    try:
        await asyncio.sleep(seconds)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


async def main() -> int:
    print("=" * 60)
    print("  prototype/step4_multi_round_demo_verify.py — 多轮真机实测")
    print(f"  classify_url = {CLASSIFY_URL}")
    print(f"  demo sitekey = {HC_DEMO_SITEKEY}")
    print("  验证：hCaptcha demo 多轮挑战（point 类型 100% 多轮）")
    print("=" * 60)

    solver = ClassifySolver(api_url=CLASSIFY_URL, timeout_seconds=90, humanize=False)

    demo_url = f"https://accounts.hcaptcha.com/demo?sitekey={HC_DEMO_SITEKEY}"
    print(f"  [step4] camoufox humanize = {HUMANIZE}")
    async with AsyncCamoufox(headless=False, humanize=HUMANIZE) as browser:
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        try:
            print(f"\n  [step4] goto {demo_url}")
            await page.goto(demo_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2)  # 等 hCaptcha widget 渲染

            # 1) 点 checkbox 弹挑战框
            print("\n  [step4] === 点 checkbox ===")
            t0 = time.monotonic()
            clicked = await solver._click_checkbox(page)
            print(f"  [step4] _click_checkbox = {clicked} ({(time.monotonic() - t0):.1f}s)")
            if not clicked:
                print("\n[step4] ❌ 点 checkbox 失败")
                await _hold(HOLD_SECONDS, "checkbox 点击失败")
                return 3

            # 2) 多轮循环：取图→判图→回填→提交（crumb_count 轮）→ 等 pass
            print("\n  [step4] === _solve_rounds 多轮循环 ===")
            t0 = time.monotonic()
            token = await solver._solve_rounds(page)
            print(f"  [step4] _solve_rounds = token?{'yes' if token else 'no'} "
                  f"({(time.monotonic() - t0):.1f}s)")
            if not token:
                print("\n[step4] ❌ 多轮循环未拿到 pass token")
                print("  可能：判图不准 / 回填位置偏 / hCaptcha 拒。人眼查看挑战框。")
                await _hold(HOLD_SECONDS, "多轮失败")
                return 4

            print(f"\n[step4] ✅✅ 多轮链路通过！token len={len(token)}")
            print("  _solve_rounds 多轮循环（crumb_count 轮取图判图回填提交）验证通过。")
            print("  （demo 页无 #register_button，不注入不检查注册按钮）")
            await _hold(HOLD_SECONDS, "多轮通过，人眼确认")
            return 0
        finally:
            try:
                await browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
