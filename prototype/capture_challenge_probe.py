#!/usr/bin/env python3
"""prototype/capture_challenge_probe.py — 阶段 2 客户端取图原型（非生产）。

目的（no-rerender-end-to-end.md §4 阶段 2 验证门）：
  确认客户端浏览器能稳定进跨域 frame=challenge iframe，取出整块九宫格图 +
  问句，调本机 camoufox-turnstile 阶段 1 的 /v1/classify，打印返回对照
  真实挑战——暴露 §7 风险点（canvas vs img DOM、坐标换算、判图命中率）。

**非生产路径**：不接 build_captcha_solver、不动 captcha.py 的 ClassifySolver、
不点回填、不注入 token、不完成注册。只做 "导航到 hCaptcha 挑战框 → 取图 →
判图 → 打印"，便于人眼对照。

实现策略：复用 main.py 的真实导航链路（build.nvidia.com → cookie → Login 弹窗
→ 填邮箱 → Next → 等密码框 → 填密码/确认密码 → 勾选保持登录），停在"密码已填、
hCaptcha widget 已就绪"这步。之后由本脚本接管：点 #register_button 触发
hCaptcha → 等 checkbox iframe → 点 checkbox → 等 challenge iframe 渲染 →
取图 + 判图。

为什么点 #register_button 但不完成注册：NVIDIA create-account 页的 hCaptcha
挑战框只有在点 #register_button 后才会渲染（组件先拦截表单提交，弹 widget）。
挑战框渲染后我们**只取图判图、不注入 token、不再点提交**，因此注册不会完成。

约束（已由服务端 app/classify.py 确认）：
  - /v1/classify 的 _decode_b64_png 强制 PNG 签名 → 截图必须 .screenshot()（PNG）
  - grid 吃 1 张整块九宫格图（非 9 张分图）→ 截 challenge-view 整块
  - 判图基准：服务端 1 起数（grid answer 是 ["2","6","9"]）
  - 端口默认 5072

用法（需本机已起 camoufox-turnstile 服务，且能连真实 NVIDIA 会话）：
  set NV_EMAIL=you@example.com
  set NV_PASSWORD=SomePass123
  set CLASSIFY_URL=http://127.0.0.1:5072   (可选，默认 5072)
  python prototype/capture_challenge_probe.py

注意：该脚本会真实走 NVIDIA 邮箱+密码 + 点 #register_button 以触发 hCaptcha
挑战框，但**不会**注入 token、不会再次提交，注册不会完成，停在取图+判图打印。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

# 让 prototype/ 能 import 顶层 main/config/captcha（cwd 在 nvidia-register 根）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# 复用 main.py 的真实导航链路（goto → cookie → login → email → password）
import main as main_mod

CLASSIFY_URL = os.environ.get("CLASSIFY_URL", "http://127.0.0.1:5072").rstrip("/")
NV_EMAIL = os.environ.get("NV_EMAIL", "")
NV_PASSWORD = os.environ.get("NV_PASSWORD", "")

# hcaptcha-challenger 的同款匹配规则：iframe src 含 newassets.hcaptcha.com + frame=challenge
CHECKBOX_FRAME_HINT = "newassets.hcaptcha.com"
CHALLENGE_FRAME_HINT = "newassets.hcaptcha.com"
CHALLENGE_VIEW_SEL = ".challenge-view"
TASK_IMAGE_SEL = ".task-image"
# 真机 DOM 诊断确认：point 问句在 .challenge-prompt（纯挑战问句，如
# "Find all animals the given number of times"）。旧值 .prompt-rich-text 是早先
# 猜测，库实际从 hCaptcha JS payload 取问句（requester_question 字段）不靠 DOM。
# .challenge-header 含问句+报告文字拼接，.challenge-report 是报告链接，均非纯问句。
QUESTION_SEL = ".challenge-prompt"

# challenge.js 子路径 → 服务端支持的 captcha_type（实测三种，来自用户真机网络请求）。
# 这是区分 hCaptcha 挑战类型的唯一可靠信号——所有类型的 challenge iframe src 完全
# 一样（host + frame=challenge），只有 challenge.js 子路径不同。
#   image_label_binary       → grid  (9 张图，ImageClassifier，服务端 ✅)
#   image_label_area_select  → point (1 张图，SpatialPointReasoner，服务端 ✅)
#   image_drag_drop          → drag  (2 张图，服务端 _SUPPORTED 外 → fallback local)
_CHALLENGE_JS_TYPE_MAP = {
    "image_label_binary": "grid",
    "image_label_area_select": "point",
    "image_drag_drop": "drag",
}

WAIT_IFRAME_SEC = 30
WAIT_SCREENSHOT_SEC = 5
# 失败/取图后浏览器保持打开的时间，供手动 F12 抓 iframe src、人眼对照挑战框。
HOLD_SECONDS = 300  # 5 分钟
# 路线 A：等用户手动点 checkbox 弹出挑战框的最长时间（3 分钟够手动操作）
MANUAL_CLICK_WAIT_SEC = 180

# 模块级：网络监听捕获到的 challenge.js URL（点 checkbox 后 hCaptcha 才加载它）。
# 形如 https://newassets.hcaptcha.com/captcha/v1/<hash>/challenge/image_label_binary/challenge.js
_captured_challenge_js: str | None = None


async def _hold(seconds: int, why: str) -> None:
    """失败/成功后浏览器保持打开，供手动 F12 排查。"""
    print(f"\n[probe] 浏览器保持打开 {seconds}s 供手动 F12 排查：{why}")
    print("       （Ctrl+C 可提前退出）")
    try:
        await asyncio.sleep(seconds)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


def _b64png(bytes_: bytes) -> str:
    """bytes → base64（无 data: 前缀，对齐服务端 _decode_b64_png 期望）。"""
    return base64.b64encode(bytes_).decode("ascii")


def _classify_whole_grid(grid_png_b64: str, question: str) -> dict:
    """POST /v1/classify，captcha_type=grid，1 张整块九宫格图。"""
    return _classify("grid", grid_png_b64, question)


def _classify(captcha_type: str, png_b64: str, question: str) -> dict:
    """POST /v1/classify。grid/point 都吃 1 张整块 challenge-view 图（探针+真机实测）。

    grid → ImageClassifier（1 张整块九宫格图）
    point → SpatialPointReasoner（1 张原图 + 服务端内部生成网格图）
    """
    resp = requests.post(
        f"{CLASSIFY_URL}/v1/classify",
        json={
            "captchaType": "HCaptchaClassification",
            "captcha_type": captcha_type,
            "question": question,
            "queries": [png_b64],
        },
        timeout=60,
    )
    try:
        return resp.json()
    except ValueError:
        return {"_http_status": resp.status_code, "_text": resp.text}


def _annotate_answer(png_b64: str, dtype: str, answer, out_path: Path) -> Path | None:
    """把服务端答案画到截图上，存 PNG，供人眼对照坐标是否命中。

    grid：answer 是 1 起数序号（如 ["2","6","9"]），在 3x3 对应格子中心画红框+序号。
    point：answer 是 [[x,y]] 像素坐标（基于截图尺寸），在对应位置画红圈+十字+序号。
    返回保存路径；PIL 不可用或画图失败返回 None（不阻断主流程）。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("  [probe] PIL 不可用，跳过可视化")
        return None
    try:
        raw = base64.b64decode(png_b64)
        import io
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        draw = ImageDraw.Draw(img)
        w, h = img.size
        # 字体：用默认（跨平台稳），size 按图大小缩放
        try:
            font = ImageFont.truetype("arial.ttf", max(16, w // 30))
        except Exception:
            font = ImageFont.load_default()

        if dtype == "grid":
            # 3x3 九宫格，序号 1-9 → 格子中心。answer 是 1 起数序号字符串。
            cw, ch = w / 3, h / 3
            for i, n in enumerate(answer or []):
                try:
                    idx = int(n) - 1
                except (ValueError, TypeError):
                    continue
                if not 0 <= idx <= 8:
                    continue
                row, col = idx // 3, idx % 3
                cx, cy = col * cw + cw / 2, row * ch + ch / 2
                # 红框 + 序号标签
                x0, y0 = col * cw, row * ch
                x1, y1 = (col + 1) * cw, (row + 1) * ch
                draw.rectangle([x0, y0, x1, y1], outline=(255, 0, 0), width=max(3, w // 200))
                label = str(n)
                draw.text((cx - 6, cy - 8), label, fill=(255, 0, 0), font=font)
        elif dtype == "point":
            # answer 是 [[x,y]] 像素坐标（基于截图尺寸），直接画
            r = max(12, w // 50)
            for i, pt in enumerate(answer or []):
                try:
                    x, y = int(pt[0]), int(pt[1])
                except (ValueError, TypeError, IndexError):
                    continue
                # 红圈 + 十字 + 序号
                draw.ellipse([x - r, y - r, x + r, y + r], outline=(255, 0, 0), width=max(3, w // 200))
                draw.line([x - r, y, x + r, y], fill=(255, 0, 0), width=2)
                draw.line([x, y - r, x, y + r], fill=(255, 0, 0), width=2)
                draw.text((x + r + 4, y - r), str(i + 1), fill=(255, 0, 0), font=font)
        else:
            return None
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(out_path))
        return out_path
    except Exception as exc:
        print(f"  [probe] 可视化失败: {exc}")
        return None


async def _navigate_to_password_filled(page) -> bool:
    """走 main.py 真实链路到"密码已填、hCaptcha widget 就绪"这步。

    复用 main.py：goto build.nvidia.com → _accept_cookie_banner → _open_signin_modal
    → _submit_email_step → _wait_for_password_field → _fill_visible_password_fields
    → 勾 #stay_signin_checkbox_v2-input。随后由调用方点 #register_button 触发 hCaptcha。
    """
    print("  [probe] goto build.nvidia.com ...")
    await page.goto("https://build.nvidia.com/", wait_until="domcontentloaded", timeout=90000)
    await main_mod._accept_cookie_banner(page)

    print("  [probe] open signin modal ...")
    if not await main_mod._open_signin_modal(page):
        print("  [probe] Login button / signin modal not found")
        await main_mod._print_clickable_snapshot(page)
        return False

    # sitekey 捕获 + hCaptcha render 拦截 hook 必须在表单提交前就位
    import captcha as captcha_mod
    captcha_mod.start_capturing_sitekey(page)
    await main_mod._ensure_hcaptcha_hook(page)

    # 挂网络监听捕获 challenge.js URL——点 checkbox 后 hCaptcha 才加载 challenge.js，
    # 其子路径（image_label_binary/area_select/drag_drop）是区分挑战类型的唯一可靠
    # 信号（所有类型 challenge iframe src 完全一样）。
    global _captured_challenge_js
    _captured_challenge_js = None

    def _on_response(resp):
        global _captured_challenge_js
        if _captured_challenge_js:
            return
        url = resp.url or ""
        # 形如 .../challenge/<type>/challenge.js
        if "/challenge/" in url and url.endswith("/challenge.js"):
            _captured_challenge_js = url
            print(f"  [probe] captured challenge.js: {url.split('/challenge/')[-1]}")

    page.on("response", _on_response)

    print("  [probe] submit email → create-account page ...")
    if not await main_mod._submit_email_step(page, NV_EMAIL):
        print("  [probe] email step failed")
        await main_mod._print_clickable_snapshot(page)
        return False

    print("  [probe] wait password field ...")
    if not await main_mod._wait_for_password_field(page):
        print("  [probe] password field never appeared")
        await main_mod._print_clickable_snapshot(page)
        return False
    if not await main_mod._fill_visible_password_fields(page, NV_PASSWORD):
        print("  [probe] failed to fill password")
        return False

    # 保持登录勾选框（main.py:430 范式，用 .check() 不是 .click()）
    try:
        checkbox = page.locator("#stay_signin_checkbox_v2-input")
        if await checkbox.count() > 0 and not await checkbox.is_checked():
            await checkbox.check()
    except Exception:
        pass
    print("  [probe] password filled, ready to trigger hCaptcha")
    return True


async def _trigger_hcaptcha_challenge(page) -> bool:
    """等 hCaptcha widget 加载 → 提示手动点 checkbox → 等挑战框。

    真实流程（用户实测纠正）：hCaptcha widget 在页面渲染成功后就会自动加载
    （checkbox iframe 直接出现），**并非**要点 #register_button 才触发。
    #register_button 只在极端情况下（widget 没自动显示）才需点击兜底。

    故：①密码填好后直接等 checkbox iframe（widget 自动加载）；
    ②若等不到，才点 #register_button 兜底触发；
    ③提示用户手动点 checkbox，长轮询等 challenge iframe 出现。
    """
    # ①直接等 checkbox iframe——页面渲染成功 hCaptcha widget 就自动加载了
    print("  [probe] waiting hCaptcha checkbox iframe (widget 自动加载) ...")
    checkbox_found = True
    try:
        await page.wait_for_selector(
            f"iframe[src*='{CHECKBOX_FRAME_HINT}'][src*='frame=checkbox']",
            timeout=20000,
        )
    except PlaywrightTimeout:
        checkbox_found = False
        print("  [probe] checkbox iframe 未自动出现，尝试点 #register_button 兜底触发 ...")
        # ②兜底：极端情况下 widget 没自动显示，点 #register_button 触发
        register_btn = page.locator("#register_button")
        try:
            await register_btn.wait_for(state="visible", timeout=10000)
            await register_btn.click(timeout=10000)
            print("  [probe] clicked #register_button (兜底触发)")
        except Exception as exc:
            print(f"  [probe] #register_button 兜底点击失败: {exc}")
        # 再等一次 checkbox iframe
        try:
            await page.wait_for_selector(
                f"iframe[src*='{CHECKBOX_FRAME_HINT}'][src*='frame=checkbox']",
                timeout=15000,
            )
            checkbox_found = True
        except PlaywrightTimeout:
            # checkbox iframe 仍没出现，但 challenge.js 已加载说明挑战框可能已直接弹出
            if _captured_challenge_js:
                print("  [probe] checkbox iframe 未现但 challenge.js 已加载，直接等挑战框")
                checkbox_found = True
            else:
                print("  [probe] hCaptcha checkbox iframe not found (可能无挑战框直接过)")
                return False

    # 路线 A（半自动）：不自动点 checkbox——hCaptcha 的 #checkbox 被 div.check
    # 遮罩拦截 pointer events + frame detach，自动点击不可靠（实测多次失败）。
    # 原型阶段目标是验证"取图+判图"能力，不是"自动点 checkbox"。改为停下来
    # 等用户手动点 checkbox，脚本长轮询检测 challenge iframe 出现即自动接管。
    print("\n" + "=" * 60)
    print("  👉 请在浏览器里手动点击 hCaptcha 的复选框（checkbox）")
    print("     点出挑战框后脚本会自动检测并接管取图判图。")
    print(f"     （最长等待 {MANUAL_CLICK_WAIT_SEC}s）")
    print("=" * 60 + "\n")

    # 长轮询等 challenge iframe 出现（用户手动点 checkbox 后它才出现）
    deadline = time.time() + MANUAL_CLICK_WAIT_SEC
    while time.time() < deadline:
        for fr in page.frames:
            src = fr.url or ""
            if CHALLENGE_FRAME_HINT in src and "frame=challenge" in src:
                print("  [probe] ✅ 检测到 challenge iframe，自动接管取图...")
                await asyncio.sleep(2)  # 给挑战框内容渲染时间
                return True
        await asyncio.sleep(0.5)
    print("  [probe] 等待手动点 checkbox 超时（%ds 内未出现 challenge iframe）" % MANUAL_CLICK_WAIT_SEC)
    return False


async def _find_challenge_frame(page):
    """扁平扫描 page.frames，找含 newassets.hcaptcha.com 且 frame=challenge 的 frame。"""
    deadline = time.time() + WAIT_IFRAME_SEC
    while time.time() < deadline:
        for fr in page.frames:
            src = fr.url or ""
            if CHALLENGE_FRAME_HINT in src and "frame=challenge" in src:
                return fr
        await asyncio.sleep(0.5)
    return None


async def _wait_images_loaded(fr, detected_type: str) -> None:
    """等挑战图片真正加载渲染（challenge.js 加载完成后图片才开始加载）。

    关键：challenge-view 容器出现 ≠ 图片加载完。必须等 <img> 的 naturalWidth>0
    （浏览器已拿到图片字节并解码），否则 screenshot 截到空白/占位框。

    - grid：等 9 个 task-image 且每个内 <img> naturalWidth>0
    - point/drag：等挑战大图 <img> naturalWidth>0（task-image 结构不同，直接扫所有 img）
    - unknown：退化为等任意 <img> naturalWidth>0
    超时 15s 不报错（继续截图，由人眼/服务端判定），只打印告警。
    """
    deadline = time.time() + 15
    if detected_type == "grid":
        print("  [probe] waiting 9 task-image <img> loaded ...")
        while time.time() < deadline:
            done = await fr.evaluate(
                """() => {
                    const tis = [...document.querySelectorAll('.task-image')];
                    if (tis.length < 9) return {n: tis.length, ready: false};
                    const imgs = tis.map(t => t.querySelector('img')).filter(Boolean);
                    if (imgs.length < 9) return {n: imgs.length, ready: false};
                    const ready = imgs.every(i => i.complete && i.naturalWidth > 0);
                    return {n: imgs.length, ready};
                }"""
            )
            if done and done.get("ready"):
                print(f"  [probe] 9 张 task-image 图片已加载完成")
                return
            await asyncio.sleep(0.3)
        print(f"  [probe] ⚠️ grid 图片 15s 未全部加载完，继续截图（可能截到半渲染）")
        return

    # point/drag/unknown：先查 <img>（grid 外可能仍有 img），无 img 则查 <canvas>
    # （真机诊断：point 挑战图渲染在 <canvas w=1000 h=940>，无 <img>）。
    # canvas 是 JS 绘制的，无 load 事件/naturalWidth，故等 canvas 存在 + 尺寸>0，
    # 再 settle 1s 让绘制完成（hCaptcha 画挑战图是同步绘制，1s 足够）。
    print(f"  [probe] waiting challenge image loaded (type={detected_type}) ...")
    found_canvas = False
    while time.time() < deadline:
        state = await fr.evaluate(
            """() => {
                const cv = document.querySelector('.challenge-view');
                if (!cv) return {kind: 'none'};
                const imgs = [...cv.querySelectorAll('img')];
                if (imgs.length) {
                    return {kind: 'img', ready: imgs.every(i => i.complete && i.naturalWidth > 0)};
                }
                const canvases = [...cv.querySelectorAll('canvas')];
                if (canvases.length) {
                    const c = canvases[0];
                    return {kind: 'canvas', ready: c.width > 0 && c.height > 0};
                }
                return {kind: 'none'};
            }"""
        )
        kind = (state or {}).get("kind")
        if kind == "img" and state.get("ready"):
            print(f"  [probe] 挑战图片已加载完成（{detected_type}, img）")
            return
        if kind == "canvas" and state.get("ready"):
            found_canvas = True
            break
        await asyncio.sleep(0.3)
    if found_canvas:
        # canvas 尺寸就绪后给绘制一拍时间（hCaptcha 同步绘制，1s 足够）
        await asyncio.sleep(1.0)
        print(f"  [probe] 挑战 canvas 已就绪（{detected_type}）")
        return
    print(f"  [probe] ⚠️ {detected_type} 图片 15s 未加载完，继续截图（可能截到半渲染）")
    # 诊断：打印 challenge-view 内 DOM 概要（img/canvas/背景图），定位真实图片结构
    try:
        diag = await fr.evaluate(
            """() => {
                const cv = document.querySelector('.challenge-view');
                if (!cv) return {err: 'no challenge-view'};
                const imgs = [...cv.querySelectorAll('img')];
                const canvases = [...cv.querySelectorAll('canvas')];
                const taskImages = [...cv.querySelectorAll('.task-image')];
                const bgEls = [...cv.querySelectorAll('*')].filter(e => {
                    const s = getComputedStyle(e).backgroundImage;
                    return s && s !== 'none';
                }).map(e => ({tag: e.tagName, cls: e.className, bg: getComputedStyle(e).backgroundImage.slice(0,60)}));
                return {
                    imgCount: imgs.length,
                    imgInfo: imgs.map(i => ({src: (i.src||'').slice(0,50), natW: i.naturalWidth, complete: i.complete})),
                    canvasCount: canvases.length,
                    canvasInfo: canvases.map(c => ({w: c.width, h: c.height})),
                    taskImageCount: taskImages.length,
                    bgCount: bgEls.length,
                    bgSample: bgEls.slice(0, 3),
                };
            }"""
        )
        print("  [probe] challenge-view DOM 诊断:")
        print(f"    {json.dumps(diag, ensure_ascii=False)[:500]}")
    except Exception as exc:
        print(f"  [probe] DOM 诊断失败: {exc}")


async def _capture_challenge(page) -> dict | None:
    """取图 + 问句 + 自适应判类型。

    返回 {png_b64, question, grid_w, grid_h, n_task_images, detected_type} 或 None。
    detected_type: 'grid'(9 个 task-image，调 /v1/classify) / 'drag'(无九宫格，
    属服务端不支持的类型，只打印不判图) / 'unknown'。
    """
    fr = await _find_challenge_frame(page)
    if fr is None:
        print("  [probe] challenge iframe not found within %ds" % WAIT_IFRAME_SEC)
        # 打印所有 hcaptcha 相关 iframe src 供人眼诊断（grid vs drag frame hint 差异）
        try:
            iframes = await page.evaluate(
                """() => [...document.querySelectorAll('iframe')]
                    .map(f => f.src).filter(s => s && s.includes('hcaptcha'))"""
            )
            print("  [probe] 现有 hcaptcha iframe src 列表:")
            for s in iframes or []:
                print(f"      {s[:160]}")
            if not iframes:
                print("      (无——可能 checkbox 未点成功，挑战框未弹出)")
        except Exception:
            pass
        return None

    # === 时序修正（关键）：challenge.js 加载完成 → 才加载挑战图片 → 图片渲染到 DOM。
    # 故必须先等 challenge.js 捕获并判类型，再等 challenge-view/图片渲染，最后截图。
    # 旧顺序（先等 challenge-view 再等 challenge.js）会导致：challenge.js 还没到就
    # 取图，图片未加载 → 截到空白；且类型判错（误判 drag）。

    # 1) 先等 challenge.js 捕获（最长 10s）。网络监听通常已捕获，此处兜底等晚到响应。
    if not _captured_challenge_js:
        print("  [probe] challenge.js 尚未捕获，等待网络响应 ...")
        js_deadline = time.time() + 10
        while time.time() < js_deadline and not _captured_challenge_js:
            await asyncio.sleep(0.3)
        if _captured_challenge_js:
            print("  [probe] challenge.js 已捕获（等待后到达）")
        else:
            print("  [probe] challenge.js 10s 内未捕获")

    # 2) 判类型：优先 challenge.js 子路径（唯一可靠信号）。
    detected_type = "unknown"
    challenge_js_type = None
    if _captured_challenge_js:
        # URL 形如 .../challenge/image_label_binary/challenge.js
        tail = _captured_challenge_js.split("/challenge/")[-1]  # image_label_binary/challenge.js
        challenge_js_type = tail.rsplit("/challenge.js", 1)[0]  # image_label_binary
        detected_type = _CHALLENGE_JS_TYPE_MAP.get(challenge_js_type, "unknown")

    # DOM 兜底：网络监听没拿到时，从 challenge iframe 内 <script> 读
    if challenge_js_type is None:
        try:
            js_in_dom = await fr.evaluate(
                """() => {
                    const s = document.querySelector('script[src*="/challenge/"][src$="/challenge.js"]');
                    return s ? s.src : null;
                }"""
            )
            if js_in_dom:
                tail = js_in_dom.split("/challenge/")[-1]
                challenge_js_type = tail.rsplit("/challenge.js", 1)[0]
                detected_type = _CHALLENGE_JS_TYPE_MAP.get(challenge_js_type, "unknown")
                print(f"  [probe] challenge.js from DOM: {challenge_js_type}")
        except Exception as exc:
            print(f"  [probe] DOM script 读取失败: {exc}")

    print(f"  [probe] detected_type={detected_type} (challenge.js={challenge_js_type!r})")

    # 3) 等 challenge-view 容器渲染（所有类型共有，最长 15s）。
    print("  [probe] waiting challenge-view to render ...")
    cv = None
    cv_deadline = time.time() + 15
    while time.time() < cv_deadline:
        cv = await fr.query_selector(CHALLENGE_VIEW_SEL)
        if cv is not None:
            break
        await asyncio.sleep(0.3)
    if cv is None:
        print("  [probe] challenge-view 15s 内未渲染出来")
        return None
    print("  [probe] challenge-view 已渲染")

    # 4) 等图片真正加载渲染（challenge.js 之后图片才开始加载）。
    #    grid：等 9 个 task-image 且每个 <img> naturalWidth>0；
    #    point/drag：等挑战大图 <img> naturalWidth>0；
    #    unknown：退化为等任意 <img> naturalWidth>0。
    await _wait_images_loaded(fr, detected_type)

    # 5) settle：图片渲染后给一拍布局稳定时间再截图，避免截到半渲染。
    await asyncio.sleep(0.5)

    # task-image 数量作辅助记录（grid=9，point/drag 结构不同）
    n_task_images = await fr.eval_on_selector_all(TASK_IMAGE_SEL, "els => els.length") or 0

    # unknown 兜底：challenge.js 没拿到时用 task-image 数量粗判（不可靠，仅兜底）
    if detected_type == "unknown":
        if n_task_images == 9:
            detected_type = "grid"
        elif n_task_images == 0:
            detected_type = "drag"
    print(f"  [probe] final detected_type={detected_type} "
          f"(challenge.js={challenge_js_type!r}, n_task_images={n_task_images})")

    question_el = await fr.query_selector(QUESTION_SEL)
    question = (await question_el.text_content()) if question_el else ""

    # 诊断：question 取不到时，打印 challenge-view 内文本节点候选，定位真实问句 DOM。
    # .prompt-rich-text 是早先猜测的选择器，库实际从 hCaptcha JS payload 取问句
    # （requester_question 字段），不靠 DOM——故此处需真机 DOM 诊断校准选择器。
    if not (question or "").strip():
        try:
            q_diag = await fr.evaluate(
                """() => {
                    const cv = document.querySelector('.challenge-view');
                    if (!cv) return {err: 'no challenge-view'};
                    // 候选：所有含可见文本的元素（过滤空/短）
                    const cands = [...cv.querySelectorAll('*')]
                        .map(e => ({tag: e.tagName, cls: e.className,
                                    txt: (e.textContent||'').trim().slice(0,80)}))
                        .filter(c => c.txt && c.txt.length > 2 && c.txt.length < 120);
                    // 去重（父子含同文本）
                    const seen = new Set(); const out = [];
                    for (const c of cands) {
                        if (!seen.has(c.txt)) { seen.add(c.txt); out.push(c); }
                    }
                    return {candidates: out.slice(0, 10)};
                }"""
            )
            print("  [probe] question 取不到，challenge-view 文本候选:")
            print(f"    {json.dumps(q_diag, ensure_ascii=False)[:600]}")
        except Exception as exc:
            print(f"  [probe] question 诊断失败: {exc}")

    # 截整块 challenge-view（cv 已在上面轮询等到，直接复用）
    png = await cv.screenshot()  # Playwright 默认 PNG ✅ 满足服务端签名校验

    box = await cv.bounding_box()
    w, h = (int(box["width"]), int(box["height"])) if box else (-1, -1)

    return {
        "png_b64": _b64png(png),
        "question": (question or "").strip(),
        "grid_w": w,
        "grid_h": h,
        "n_task_images": int(n_task_images),
        "detected_type": detected_type,
        "challenge_js_type": challenge_js_type,
    }


async def main() -> int:
    print("=" * 60)
    print("  prototype/capture_challenge_probe.py — 阶段 2 取图原型")
    print(f"  classify_url = {CLASSIFY_URL}")
    print("=" * 60)

    if not NV_EMAIL or not NV_PASSWORD:
        print("\n[probe] NV_EMAIL / NV_PASSWORD 未设置，无法触发挑战。")
        return 1

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        page = await browser.new_page(viewport={"width": 1280, "height": 800})

        try:
            if not await _navigate_to_password_filled(page):
                print("\n[probe] 导航到 hCaptcha 就绪状态失败。")
                await _hold(HOLD_SECONDS, "导航失败，可 F12 看当前页面 DOM")
                return 2

            if not await _trigger_hcaptcha_challenge(page):
                print("\n[probe] 触发 hCaptcha 挑战框失败。")
                await _hold(HOLD_SECONDS, "触发失败，可 F12 抓 checkbox/challenge iframe src")
                return 3

            print("  [probe] capturing challenge ...")
            challenge = await _capture_challenge(page)
            if challenge is None:
                print("\n[probe] 取图失败。")
                await _hold(HOLD_SECONDS, "取图失败，可 F12 抓 challenge iframe src 对照 frame hint")
                return 4

            print("\n[probe] 取图结果:")
            print(f"  detected_type    = {challenge['detected_type']}")
            print(f"  challenge.js类型 = {challenge['challenge_js_type']!r}")
            print(f"  question        = {challenge['question']!r}")
            print(f"  grid_w x h      = {challenge['grid_w']} x {challenge['grid_h']}")
            print(f"  n_task_images   = {challenge['n_task_images']}")
            print(f"  png_b64 长度    = {len(challenge['png_b64'])}")

            dtype = challenge["detected_type"]
            if dtype in ("grid", "point"):
                # 服务端 _SUPPORTED = {grid, point}，二者都吃 1 张整块 challenge-view 图。
                print(f"\n[probe] POST {CLASSIFY_URL}/v1/classify (captcha_type={dtype}) ...")
                t0 = time.monotonic()
                resp = _classify(dtype, challenge["png_b64"], challenge["question"])
                print(f"  elapsed = {(time.monotonic() - t0) * 1000:.0f} ms")
                print("\n[probe] 服务端返回:")
                print(json.dumps(resp, ensure_ascii=False, indent=2))

                if resp.get("solved"):
                    ans = resp.get("answer")
                    if dtype == "grid":
                        print(f"\n[probe] ✅ grid 判图成功，answer = {ans}（1 起数序号）")
                        print("       请人眼对照浏览器九宫格，确认序号是否命中。")
                    else:
                        print(f"\n[probe] ✅ point 判图成功，answer = {ans}（[[x,y]] 原图像素坐标）")
                        print("       请人眼对照浏览器挑战图，确认坐标是否落在目标上。")
                    # 可视化：把答案画到截图上存文件，人眼直接看图判断坐标是否命中
                    out_path = Path(__file__).resolve().parent / "out" / (
                        "annotate_%s_%d.png" % (dtype, int(time.time()))
                    )
                    saved = _annotate_answer(challenge["png_b64"], dtype, ans, out_path)
                    if saved:
                        print(f"\n[probe] 🖼️  可视化对照图已保存：{saved}")
                        print("       打开该文件，红框/红圈=判图答案，对照原图判断是否命中。")
                else:
                    print("\n[probe] ❌ 判图失败 errorCode = %s" % resp.get("errorCode"))
            else:
                # drag/unknown：服务端 _SUPPORTED 外会拒（classify 仅 grid/point，
                # drag → fallback local，见 classify-endpoint-implementation.md §7 风险4）。
                print(f"\n[probe] ⏭️  detected_type={dtype} 非 grid/point，服务端会拒。")
                print("       不调 /v1/classify（避免被拒）。drag 应 fallback local 真机。")
                print("       截图已取，供人眼对照挑战结构。")

            print("\n[probe] 浏览器保持打开 5 分钟供人眼对照（不提交、不回填）...")
            await _hold(HOLD_SECONDS, "取图/判图完成，人眼对照答案或抓 iframe src")
            return 0
        finally:
            await browser.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
