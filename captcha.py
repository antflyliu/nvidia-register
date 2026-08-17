from __future__ import annotations

import asyncio
import base64
import json
import math
import random
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse, parse_qs

import requests
from playwright.async_api import Page

from config import CaptchaConfig


class CaptchaSolver(Protocol):
    async def solve(self, page: Page) -> bool:
        ...


class ManualCaptchaSolver:
    async def solve(self, page: Page) -> bool:
        print("\n[2/4] Please solve the hCaptcha manually...")
        for i in range(120):
            if await _is_register_button_enabled(page):
                print(f"  hCaptcha solved ({i}s)")
                return True
            await asyncio.sleep(1)
        print("  hCaptcha timeout")
        return False


@dataclass(frozen=True)
class YesCaptchaSolver:
    client_key: str
    api_url: str
    poll_interval_seconds: int
    timeout_seconds: int

    async def solve(self, page: Page) -> bool:
        print("\n[2/4] Solving hCaptcha with YesCaptcha...")
        site_key = await _get_site_key(page)
        if not site_key:
            print("  hCaptcha sitekey not found")
            return False

        task_id = self._create_task(page.url, site_key)
        token = self._poll_task_result(task_id)
        if not token:
            return False

        await _inject_hcaptcha_token(page, token)
        for i in range(20):
            if await _is_register_button_enabled(page):
                print(f"  hCaptcha solved by YesCaptcha ({i}s)")
                return True
            await asyncio.sleep(1)
        print("  hCaptcha token injected, but #register_button stayed disabled")
        return False

    def _create_task(self, website_url: str, website_key: str) -> str:
        response = requests.post(
            f"{self.api_url}/createTask",
            json={
                "clientKey": self.client_key,
                "task": {
                    "type": "HCaptchaTaskProxyless",
                    "websiteURL": website_url,
                    "websiteKey": website_key,
                },
            },
            timeout=30,
        )
        data = response.json()
        if data.get("errorId"):
            raise RuntimeError(f"YesCaptcha createTask failed: {data}")
        task_id = data.get("taskId")
        if not task_id:
            raise RuntimeError(f"YesCaptcha createTask missing taskId: {data}")
        return str(task_id)

    def _poll_task_result(self, task_id: str) -> str | None:
        deadline = time.time() + self.timeout_seconds
        while time.time() < deadline:
            try:
                response = requests.post(
                    f"{self.api_url}/getTaskResult",
                    json={"clientKey": self.client_key, "taskId": task_id},
                    timeout=30,
                )
                data = response.json()
            except requests.RequestException as exc:
                # 网络抖动不该让整个任务失败，继续轮询到超时为止
                print(f"  YesCaptcha getTaskResult network error: {exc}")
                time.sleep(self.poll_interval_seconds)
                continue

            if data.get("errorId"):
                print(f"  YesCaptcha getTaskResult failed: {data}")
                return None
            if data.get("status") == "ready":
                solution = data.get("solution") or {}
                return solution.get("gRecaptchaResponse") or solution.get("token")
            time.sleep(self.poll_interval_seconds)
        print("  YesCaptcha timeout")
        return None


@dataclass(frozen=True)
class CaptchaRunSolver:
    token: str
    api_url: str
    poll_interval_seconds: int
    timeout_seconds: int

    async def solve(self, page: Page) -> bool:
        print("\n[2/4] Solving hCaptcha with CaptchaRun...")
        site_key = await _get_site_key(page)
        if not site_key:
            print("  hCaptcha sitekey not found")
            return False

        user_agent = await page.evaluate("() => navigator.userAgent")
        task_id, token = self._create_task(page.url, site_key, user_agent)
        if task_id and not token:
            token = self._poll_task_result(task_id)
        if not token:
            return False

        await _inject_hcaptcha_token(page, token)
        for i in range(20):
            if await _is_register_button_enabled(page):
                print(f"  hCaptcha solved by CaptchaRun ({i}s)")
                return True
            await asyncio.sleep(1)
        print("  hCaptcha token injected, but #register_button stayed disabled")
        return False

    def _create_task(self, website_url: str, website_key: str, user_agent: str) -> tuple[str | None, str | None]:
        response = requests.post(
            f"{self.api_url}/v2/tasks",
            headers=self._headers(),
            json={
                "captchaType": "HCaptcha",
                "siteKey": website_key,
                "siteReferer": _site_referer(website_url),
                "userAgent": user_agent,
                "fallbackToActualUA": True,
            },
            timeout=30,
        )
        data = _response_json(response)
        if not response.ok:
            raise RuntimeError(f"CaptchaRun create task failed: {data}")
        task_id = data.get("taskId")
        result = data.get("result") or {}
        token = _extract_hcaptcha_token(result)
        if not task_id and not token:
            raise RuntimeError(f"CaptchaRun create task missing taskId/result: {data}")
        return str(task_id) if task_id else None, token

    def _poll_task_result(self, task_id: str) -> str | None:
        deadline = time.time() + self.timeout_seconds
        while time.time() < deadline:
            response = requests.get(
                f"{self.api_url}/v2/tasks/{task_id}",
                headers=self._headers(content_type=False),
                timeout=30,
            )
            data = _response_json(response)
            if not response.ok:
                print(f"  CaptchaRun get task result failed: {data}")
                return None

            status = str(data.get("status", "")).lower()
            if status == "success":
                return _extract_hcaptcha_token(data.get("response") or data.get("result") or {})
            if status == "fail":
                print(f"  CaptchaRun failed: {data.get('reason') or data}")
                return None
            time.sleep(self.poll_interval_seconds)
        print("  CaptchaRun timeout")
        return None

    def _headers(self, content_type: bool = True) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if content_type:
            headers["Content-Type"] = "application/json"
        return headers


@dataclass(frozen=True)
class LocalSolver:
    """调本地 camoufox-turnstile 服务求解 hCaptcha（YesCaptcha 兼容协议）。

    与 YesCaptchaSolver 走同一套 createTask/getTaskResult 协议，但指向本地
    camoufox-turnstile 服务（默认 http://127.0.0.1:5072）。服务端不校验
    clientKey、忽略 websiteKey（sitekey 由服务从页面 DOM 推导），并接受
    userAgent 用于设置求解浏览器的 UA。nvidia-register 已走到密码环节
    （login.nvgs.nvidia.com，hCaptcha 已出现），这里把 page.url 作为
    websiteURL 交给服务，服务开独立 Camoufox 浏览器去该页求解。
    """

    api_url: str
    poll_interval_seconds: int
    timeout_seconds: int

    async def solve(self, page: Page) -> bool:
        print("\n[2/4] Solving hCaptcha with local camoufox-turnstile...")
        site_key = await _get_site_key(page)
        if not site_key:
            print("  hCaptcha sitekey not found")
            return False

        user_agent = await page.evaluate("() => navigator.userAgent")
        # 提取当前页面会话 cookie。NVIDIA 的 hCaptcha 只在携带会话 cookie 时
        # 才加载（login.nvgs.nvidia.com），服务用这些 cookie 开浏览器才能渲染挑战。
        cookies = await page.context.cookies()
        # 本地化日志：每次 createTask 都打印一条，带上 websiteURL 片段，便于
        # 与服务端 `hcaptcha solve.start cr=N` 日志对账「一个账号的发起了几次求解」。
        print(f"  [local-solver] createTask websiteURL={page.url[:80]} "
              f"sitekey={site_key[:12]}... cookies={len(cookies)}")
        task_id = self._create_task(page.url, site_key, user_agent, cookies)
        print(f"  [local-solver] taskId={task_id} 开始轮询")
        token = self._poll_task_result(task_id)
        if not token:
            print("  [local-solver] 轮询结束，未拿到 token")
            return False
        print(f"  [local-solver] 拿到 token len={len(str(token))}，注入页面")

        await _inject_hcaptcha_token(page, token)
        for i in range(20):
            if await _is_register_button_enabled(page):
                print(f"  hCaptcha solved by local solver ({i}s)")
                return True
            await asyncio.sleep(1)
        print("  hCaptcha token injected, but #register_button stayed disabled")
        return False

    def _unreachable(self, path: str, exc: Exception) -> RuntimeError:
        """构造服务不可达时的清晰可操作错误。

        本地 camoufox-turnstile 服务是自托管控制面，连接失败几乎总是
        「服务没启动 / 端口不对 / 求解器配置成 mock」这类可修复问题。
        把晦涩的 urllib3 `Max retries exceeded` 包装成带服务地址和修复
        指引的 RuntimeError，避免用户对着连接错误无从下手。
        """
        return RuntimeError(
            f"local solver service unreachable at {self.api_url}{path}: {exc}\n"
            "请确认 camoufox-turnstile 服务已启动并监听该端口，且其 "
            "solver_hcaptcha 配置为 camoufox（真实求解）而非 mock。"
        )

    def _create_task(
        self,
        website_url: str,
        website_key: str,
        user_agent: str,
        cookies: list[dict[str, Any]] | None = None,
    ) -> str:
        task: dict[str, Any] = {
            "type": "HCaptchaTaskProxyless",
            "websiteURL": website_url,
            "websiteKey": website_key,
            "userAgent": user_agent,
        }
        if cookies:
            task["cookies"] = cookies
        try:
            response = requests.post(
                f"{self.api_url}/createTask",
                json={"task": task},
                timeout=30,
            )
            data = response.json()
        except requests.RequestException as exc:
            raise self._unreachable("/createTask", exc) from exc
        if data.get("errorId"):
            raise RuntimeError(f"local solver createTask failed: {data}")
        task_id = data.get("taskId")
        if not task_id:
            raise RuntimeError(f"local solver createTask missing taskId: {data}")
        return str(task_id)

    def _poll_task_result(self, task_id: str) -> str | None:
        deadline = time.time() + self.timeout_seconds
        while time.time() < deadline:
            try:
                response = requests.post(
                    f"{self.api_url}/getTaskResult",
                    json={"taskId": task_id},
                    timeout=30,
                )
                data = response.json()
            except requests.RequestException as exc:
                # 网络抖动不该让整个任务失败，继续轮询到超时为止；但首次
                # 连接失败（服务不可达）给出清晰提示，避免反复打印晦涩错误。
                print(f"  local solver getTaskResult network error: {exc}")
                time.sleep(self.poll_interval_seconds)
                continue

            if data.get("errorId"):
                print(f"  local solver getTaskResult failed: {data}")
                return None
            if data.get("status") == "ready":
                solution = data.get("solution") or {}
                return solution.get("gRecaptchaResponse") or solution.get("token")
            time.sleep(self.poll_interval_seconds)
        print("  local solver timeout")
        return None


# ---------------------------------------------------------------------------
#  sitekey 捕获（render=explicit 模式下 DOM 无 sitekey，只能从网络请求获取）
# ---------------------------------------------------------------------------

_captured_sitekey: str | None = None


def reset_captcha_state() -> None:
    """重置模块级缓存，供批量注册时每个新账号使用。"""
    global _captured_sitekey
    _captured_sitekey = None


def start_capturing_sitekey(page: Page) -> None:
    """注册网络请求监听器，从 checksiteconfig 请求中捕获 hCaptcha sitekey。

    必须在 create-account 页加载前调用。
    """
    def _on_request(req):
        global _captured_sitekey
        if _captured_sitekey:
            return
        url = req.url
        if "checksiteconfig" in url and "sitekey=" in url:
            try:
                sk = parse_qs(urlparse(url).query).get("sitekey", [None])[0]
                if sk:
                    _captured_sitekey = sk
                    print(f"  sitekey captured: {sk}")
            except Exception:
                pass

    page.on("request", _on_request)


async def _get_site_key(page: Page) -> str | None:
    """获取 sitekey（仅从网络请求缓存中读取）。"""
    if _captured_sitekey:
        return _captured_sitekey
    # 等待网络请求捕获（hCaptcha iframe 可能还在加载）
    for _ in range(30):
        if _captured_sitekey:
            return _captured_sitekey
        await asyncio.sleep(1)
    return None


# ---------------------------------------------------------------------------
#  token 注入（通过拦截的 Angular 回调直接触发 onSuccess）
# ---------------------------------------------------------------------------


async def _inject_hcaptcha_token(page: Page, token: str) -> bool:
    """调用拦截的 __hCaptchaCallback 触发 Angular onSuccess，使 #register_button enable。

    回调由 main.py 的 _ensure_hcaptcha_hook 通过 addInitScript 在
    hcaptcha.render 调用时捕获到 window.__hCaptchaCallback。

    同时把 token 写进 h-captcha-response 隐藏域并置 __hCaptchaInjectedToken 标记，
    后者会让 hook 屏蔽掉 hCaptcha 组件自身失败触发的 expired/error 回调，
    避免刚注入的 token 又被 Angular 清掉。
    """
    result = await page.evaluate(
        r"""(token) => {
            window.__hCaptchaInjectedToken = token;

            // 隐藏域：部分实现在提交时直接读表单值
            let textareaCount = 0;
            for (const name of ['h-captcha-response', 'g-recaptcha-response']) {
                for (const field of document.querySelectorAll(`textarea[name="${name}"]`)) {
                    field.value = token;
                    field.dispatchEvent(new Event('input', {bubbles: true}));
                    field.dispatchEvent(new Event('change', {bubbles: true}));
                    textareaCount += 1;
                }
            }

            let callbackInvoked = false;
            if (typeof window.__hCaptchaCallback === 'function') {
                window.__hCaptchaCallback(token);
                callbackInvoked = true;
            }
            return {callbackInvoked, textareaCount};
        }""",
        token,
    )
    callback_invoked = bool(result.get("callbackInvoked"))
    print(f"  token injected (callback={callback_invoked}, textarea={result.get('textareaCount')})")
    if not callback_invoked:
        print("  WARNING: __hCaptchaCallback 未捕获到，Angular 可能收不到 token")
    return callback_invoked


# ---------------------------------------------------------------------------
#  辅助
# ---------------------------------------------------------------------------


async def _is_register_button_enabled(page: Page) -> bool:
    """检查 #register_button 是否 enabled（hCaptcha 通过后按钮才会 enable）。"""
    result = await page.evaluate(
        """() => {
            const btn = document.querySelector('#register_button');
            return btn ? !btn.disabled : false;
        }"""
    )
    return bool(result)


def _site_referer(website_url: str) -> str:
    parsed = urlparse(website_url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/"
    return website_url


def _response_json(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {"status_code": response.status_code, "text": response.text}
    return data if isinstance(data, dict) else {"data": data}


def _extract_hcaptcha_token(data: dict[str, Any]) -> str | None:
    token = data.get("gRecaptchaResponse") or data.get("token")
    return str(token) if token else None


# ---------------------------------------------------------------------------
#  ClassifySolver — 客户端取图 → 自建 /v1/classify 判图 → 客户端回填
#  与 LocalSolver.solve 完全相同的接口
#  drag/bbox 或服务端 ERROR_UNSUPPORTED_CHALLENGE 时自动 fallback local
# ---------------------------------------------------------------------------

# hCaptcha 挑战框 DOM 选择器（真机诊断确认，与 prototype 一致）。
# 所有类型 challenge iframe src 相同（newassets.hcaptcha.com + frame=challenge），
# 只有 challenge.js 子路径不同——是区分类型的唯一可靠信号。
_CHALLENGE_FRAME_HINT = "newassets.hcaptcha.com"
_CHALLENGE_VIEW_SEL = ".challenge-view"
_TASK_IMAGE_SEL = ".task-image"
# 问句在 .challenge-prompt（纯挑战问句），非 .challenge-header（含报告文字拼接）。
_QUESTION_SEL = ".challenge-prompt"
# challenge.js 子路径 → captcha_type（实测三种）。
_CHALLENGE_JS_TYPE_MAP = {
    "image_label_binary": "grid",
    "image_label_area_select": "point",
    "image_drag_drop": "drag",
}
# checkbox iframe XPath（对齐 hcaptcha-challenger 库 click_checkbox）。
_CHECKBOX_IFRAME_XPATH = (
    "//iframe[starts-with(@src,'https://newassets.hcaptcha.com/captcha/v1/') "
    "and contains(@src, 'frame=checkbox')]"
)
# 提交按钮（对齐库 challenger.py:641）。
_SUBMIT_BUTTON_XPATH = "//div[@class='button-submit button']"

# 等待 iframe/渲染/图片的超时（秒）。
_WAIT_IFRAME_SEC = 30
_WAIT_RENDER_SEC = 15
_WAIT_IMAGE_SEC = 15
# 多轮挑战最大轮数（对齐库 MAX_CRUMB_COUNT 默认 2，留余量覆盖 3 轮 + 失败重试）。
_MAX_CHALLENGE_ROUNDS = 4


def _bezier_trajectory(start: tuple[float, float], end: tuple[float, float],
                       steps: int = 25) -> list[tuple[float, float]]:
    """二次贝塞尔曲线鼠标轨迹（对齐库 _generate_bezier_trajectory）。

    普通 chromium 缺 camoufox 的 humanize，page.mouse.move 直线一步到位是机器人
    特征，hCaptcha 会识别并拒给挑战（display-error）。用贝塞尔曲线 + 随机控制点
    模拟真人鼠标弧线轨迹。
    """
    points = []
    distance = math.sqrt((end[0] - start[0]) ** 2 + (end[1] - start[1]) ** 2)
    offset_factor = min(0.3, max(0.1, distance / 1000))
    mid_x = (start[0] + end[0]) / 2
    mid_y = (start[1] + end[1]) / 2
    control_x = mid_x + random.uniform(-1, 1) * distance * offset_factor
    control_y = mid_y + random.uniform(-1, 1) * distance * offset_factor
    for i in range(steps + 1):
        t = i / steps
        x = (1 - t) ** 2 * start[0] + 2 * (1 - t) * t * control_x + t ** 2 * end[0]
        y = (1 - t) ** 2 * start[1] + 2 * (1 - t) * t * control_y + t ** 2 * end[1]
        points.append((x, y))
    return points


def _dynamic_delays(steps: int, base_delay: float = 8) -> list[float]:
    """动态延迟（对齐库 _generate_dynamic_delays）：两端慢中间快，加 ±10% 随机。"""
    delays = []
    for i in range(steps + 1):
        progress = i / steps
        if progress < 0.5:
            factor = 2 * progress * progress
        else:
            progress -= 1
            factor = 1 - (-2 * progress * progress)
        delay_factor = 1.5 - 0.9 * factor
        delays.append(base_delay * delay_factor * random.uniform(0.9, 1.1))
    return delays


class ClassifySolver:
    """hCaptcha classify 求解器。

    流程：客户端浏览器内取图 → POST /v1/classify → 回填点击提交。
    检测到 drag/bbox 或服务端返回 unsupported 时自动 fallback 到 LocalSolver。
    """

    def __init__(
        self,
        api_url: str,
        poll_interval_seconds: int = 1,
        timeout_seconds: int = 30,
        local_solver_url: str | None = None,
        humanize: bool = True,
    ):
        self.api_url = api_url.rstrip("/")
        self.poll_interval_seconds = poll_interval_seconds
        self.timeout_seconds = timeout_seconds
        self.local_solver_url = local_solver_url or api_url
        # checkbox 点击是否加贝塞尔真人轨迹。普通 chromium 需 True（绕过 hCaptcha
        # 自动化检测）；camoufox(humanize=True) 浏览器内核已真人化，设 False 直接
        # 点击更快（camoufox 文档建议关自定义贝塞尔）。
        self._humanize = bool(humanize)
        # 网络监听捕获的 challenge.js URL（点 checkbox 后 hCaptcha 才加载）。
        self._captured_challenge_js: str | None = None

    async def solve(self, page: Page) -> bool:
        """与 LocalSolver.solve 完全相同的接口。"""
        print("\n[2/4] Solving hCaptcha with classify solver...")

        # 0) 点 checkbox 弹出挑战框（ClassifySolver 独有；现有 solver 不点，靠打码平台云端点）
        if not await self._click_checkbox(page):
            print("  failed to trigger challenge via checkbox, fallback local")
            return await self._fallback_local(page)

        # 1-4) 多轮挑战循环：hCaptcha drag/point/grid 可能要求连续通过多轮
        # （库 MAX_CRUMB_COUNT 默认 2）。每轮：取图→判图→回填→提交，提交后等
        # /getcaptcha/ 或 /checkcaptcha/ 的 pass 响应。pass 则拿到 token 退出；
        # 未 pass 则 hCaptcha 刷新下一轮挑战，继续循环。
        token = await self._solve_rounds(page)
        if not token:
            print("  classify solver failed all rounds, fallback local")
            return await self._fallback_local(page)

        # 5) 注入 token，检查注册按钮使能
        await self._inject_hcaptcha_token(page, token)
        for i in range(20):
            if await _is_register_button_enabled(page):
                print(f"  hCaptcha solved by classify solver ({i}s)")
                return True
            await asyncio.sleep(1)

        print("  hCaptcha token injected, but #register_button stayed disabled")
        return False

    async def _solve_rounds(self, page: Page) -> str | None:
        """多轮挑战循环（对齐库 challenge_image_drag_drop 的 for cid in range(crumb_count)）。

        hCaptcha 多轮机制：单次 /getcaptcha/ 下发 tasklist，长度=轮数。库在单次
        挑战内 for cid 循环跑完所有轮——每轮取图→判图→回填→提交，循环内**不查
        pass**，所有轮提交后才在 wait_for_challenge 查 pass。本方法对齐此模型：

        1. round 1 取图后读 crumb_count（DOM .Crumb 数），确定本轮挑战的轮数
        2. 循环 crumb_count 轮：取图→判图→回填→提交（不等 pass）
        3. 所有轮提交后统一等 pass token
        4. 没拿到 pass → refresh 重试整个流程（对齐库 RETRY_ON_FAILURE）

        crumb_count 读不到（DOM 未渲染）时退化为单轮，提交后等 pass，没 pass
        则重试（覆盖 NVIDIA 单轮场景）。
        """
        deadline = time.time() + self.timeout_seconds
        for attempt in range(1, _MAX_CHALLENGE_ROUNDS + 1):
            if time.time() > deadline:
                print("  [classify] rounds timeout")
                return None

            # round 1 取图 + 读 crumb_count
            if attempt > 1:
                await asyncio.sleep(1.5)  # 等刷新后新挑战渲染
                self._captured_challenge_js = None
            challenge = await self._capture_challenge(page)
            if challenge is None:
                print("  no challenge frame surfaced")
                return None

            crumb_n = await self._read_crumb_count(page)
            print(f"  [classify] attempt {attempt}/{_MAX_CHALLENGE_ROUNDS} "
                  f"crumb_count={crumb_n} ({'多轮' if crumb_n > 1 else '单轮'})")

            # bbox 不支持
            if challenge.get("captcha_type") == "bbox":
                print("  bbox unsupported by classify")
                return None

            # 循环 crumb_n 轮：取图→判图→回填→提交（不等 pass，对齐库 for cid）
            round_ok = await self._run_crumbs(page, challenge, crumb_n)
            if not round_ok:
                return None  # 某轮判图/回填失败，fallback

            # 所有轮提交后统一等 pass token
            token = await self._wait_token_brief()
            if token:
                return token
            print("  [classify] no pass after all crumbs, refresh and retry")

        print("  [classify] exhausted all attempts without pass")
        return None

    async def _run_crumbs(self, page: Page, first_challenge: dict, crumb_n: int) -> bool:
        """跑 crumb_n 轮挑战（对齐库 for cid in range(crumb_count)）。

        每轮：取图→判图→回填→提交，循环内不等 pass。第 1 轮复用已取的
        first_challenge，第 2 轮起重新取图（每轮挑战图不同）。
        """
        challenge = first_challenge
        for cid in range(crumb_n):
            print(f"  [classify] === crumb {cid + 1}/{crumb_n} ===")
            # 第 2 轮起重新取图（等下一轮挑战渲染）
            if cid > 0:
                await asyncio.sleep(1.5)
                # 不重置 _captured_challenge_js：多轮挑战类型不变（同一
                # challenge.js），hCaptcha 多轮是同挑战框内切换内容不重新加载
                # challenge.js。重置会导致 _capture_challenge 等 10s 超时，
                # challenge.js=None 走 DOM 兜底误判类型（如 point 误判 drag）。
                challenge = await self._capture_challenge(page)
                if challenge is None:
                    print("  no challenge frame for crumb %d" % (cid + 1))
                    return False
                if challenge.get("captcha_type") == "bbox":
                    print("  bbox unsupported by classify")
                    return False

            # 判图
            answer = self._classify(challenge)
            if answer is None:
                print("  classify returned unsupported or failed")
                return False
            print(f"  [classify] answer = {answer}")

            # 回填 + 提交
            ok = await self._apply_answer(page, challenge, answer)
            if not ok:
                print("  apply_answer failed")
                return False
        return True

    async def _read_crumb_count(self, page: Page) -> int:
        """读 hCaptcha 挑战框 .Crumb 元素数（多轮指示器，对齐库 check_crumb_count）。

        hCaptcha 用 .Crumb 元素指示挑战轮数（像分页器圆点）。单轮=1 个或无，
        多轮=2+ 个。库 check_crumb_count（challenger.py:417-430）同逻辑。
        诊断用：确认 hCaptcha 实际给几轮，判断多轮循环是否可实测。
        """
        try:
            fr = await self._find_challenge_frame(page)
            if fr is None:
                return 1
            count = await fr.locator("//div[@class='Crumb']").count()
            return count if count else 1
        except Exception:
            return 1

    async def _wait_token_brief(self, timeout: float = 15.0) -> str | None:
        """短等 pass token（单轮提交后）。pass → 返回 token；超时 → None（继续下一轮）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._captured_token:
                print(f"  [classify] hCaptcha pass, token len={len(self._captured_token)}")
                return self._captured_token
            await asyncio.sleep(0.5)
        return None

    async def _click_checkbox(self, page: Page) -> bool:
        """点 hCaptcha checkbox 弹出挑战框（ClassifySolver 独有，现有 solver 不需要）。

        对齐 hcaptcha-challenger 库 click_checkbox：frame_locator(checkbox iframe)
        .locator('#checkbox') + bounding_box() + page.mouse.click(视口坐标中心)。
        库用 page.mouse.click（视口坐标）而非 element.click()（DOM 点击），可绕过
        hCaptcha 的 div.check pointer-events 拦截（prototype 真机验证 element.click
        不可靠：Frame was detached / subtree intercepts pointer events）。

        同时挂 page.on('response') 捕获 challenge.js URL——点 checkbox 后 hCaptcha
        才加载 challenge.js，其子路径是区分挑战类型的唯一可靠信号。

        返回 True 若挑战 iframe 在 _WAIT_IFRAME_SEC 内出现；否则 False。
        """
        # 挂网络监听捕获 challenge.js（点 checkbox 后才加载）+ hCaptcha 验证 token
        self._captured_challenge_js = None
        self._captured_token: str | None = None

        async def _on_response(resp):
            url = resp.url or ""
            # challenge.js（类型识别）
            if not self._captured_challenge_js:
                if "/challenge/" in url and url.endswith("/challenge.js"):
                    self._captured_challenge_js = url
            # hCaptcha 验证通过 → /getcaptcha/ 或 /checkcaptcha/ 响应 {pass:true,
            # generated_pass_UUID:...}。对齐库 challenger.py:723-734 + 783-788
            # （两个端点都可能返回 pass 结果）。token 只取首个 pass 响应。
            if not self._captured_token and ("/getcaptcha/" in url or "/checkcaptcha/" in url):
                try:
                    ctype = resp.headers.get("content-type", "")
                    if "json" in ctype:
                        data = await resp.json()
                        if data.get("pass"):
                            tok = data.get("generated_pass_UUID") or ""
                            if tok:
                                self._captured_token = tok
                except Exception:
                    pass  # 非 JSON 或解析失败，忽略（库同样吞异常）

        page.on("response", _on_response)

        # 点 checkbox：库的方式，视口坐标 mouse.click
        try:
            checkbox_frame = page.frame_locator(_CHECKBOX_IFRAME_XPATH)
            checkbox_el = checkbox_frame.locator("//div[@id='checkbox']")
            # frame_locator.locator 的 bounding_box 需通过 page 级 locator
            # 用 page.locator 组合 XPath 定位 iframe 内 checkbox
            await self._click_checkbox_center(page)
        except Exception as exc:
            print(f"  [classify] click checkbox failed: {exc}")
            return False

        # 等挑战 iframe 出现
        fr = await self._find_challenge_frame(page)
        if fr is None:
            print("  [classify] challenge iframe not found after clicking checkbox")
            return False
        print("  [classify] challenge iframe detected")
        return True

    async def _click_checkbox_center(self, page: Page) -> None:
        """真人化点击 checkbox 中心（贝塞尔轨迹 + 动态延迟，绕过 hCaptcha 自动化检测）。

        先等 checkbox iframe 出现，取其内 #checkbox 的 bounding_box，用 _human_click
        点中心。普通 chromium 缺 camoufox humanize，裸 page.mouse.move/click 是直线
        瞬移机器人特征，hCaptcha 识别后弹挑战框但 display-error 不给挑战内容。
        """
        deadline = time.time() + _WAIT_IFRAME_SEC
        while time.time() < deadline:
            iframe_el = page.locator(_CHECKBOX_IFRAME_XPATH).first
            try:
                if await iframe_el.count() == 0:
                    await asyncio.sleep(0.3)
                    continue
            except Exception:
                await asyncio.sleep(0.3)
                continue
            checkbox = page.frame_locator(_CHECKBOX_IFRAME_XPATH).locator("//div[@id='checkbox']")
            try:
                box = await checkbox.bounding_box()
            except Exception:
                await asyncio.sleep(0.3)
                continue
            if box is None:
                await asyncio.sleep(0.3)
                continue
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            # 点击前随机停顿（真人点前会犹豫），避免点完 widget 就点的机器时序
            await asyncio.sleep(random.uniform(0.3, 0.8))
            await self._human_click(page, cx, cy, bezier=self._humanize)
            print(f"  [classify] human-clicked checkbox at ({cx:.0f},{cy:.0f}) "
                  f"(bezier={self._humanize})")
            return
        raise RuntimeError("checkbox iframe not found within %ds" % _WAIT_IFRAME_SEC)

    async def _human_click(self, page: Page, x: float, y: float, *,
                           bezier: bool = True) -> None:
        """点击目标，可选贝塞尔轨迹真人化。

        bezier=True（默认，普通 chromium 用）：贝塞尔轨迹移动 + 动态延迟 + 点击，
        模拟真人鼠标。普通 chromium 缺 camoufox humanize，裸 page.mouse.click 是
        机器人特征，hCaptcha 会拒。
        bezier=False（camoufox 用）：直接 page.mouse.click 一步到位。camoufox 的
        humanize=True 已在浏览器内核层处理真人化（isTrusted=true + 真实轨迹），
        再加贝塞尔是多余且拖慢（camoufox 文档建议关自定义贝塞尔）。
        """
        if not bezier:
            await page.mouse.move(x, y)
            await page.mouse.click(x, y, delay=random.randint(80, 200))
            return
        # 贝塞尔轨迹：随机起点 → 曲线移动 → 动态延迟 → 点击
        start_x = x + random.uniform(-150, 150)
        start_y = y + random.uniform(-150, 150)
        steps = random.randint(20, 30)
        traj = _bezier_trajectory((start_x, start_y), (x, y), steps)
        delays = _dynamic_delays(steps, base_delay=8)
        await page.mouse.move(start_x, start_y)
        for (px, py), d in zip(traj, delays):
            await page.mouse.move(px, py)
            await asyncio.sleep(d / 1000)
        # 到位后短暂停顿再点（真人移到位会看一眼再点）
        await asyncio.sleep(random.uniform(0.1, 0.25))
        await page.mouse.click(x, y, delay=random.randint(80, 200))

    async def _human_drag(self, page: Page, sx: float, sy: float,
                          ex: float, ey: float, *, bezier: bool = True) -> None:
        """拖拽（drag 挑战回填），对齐库 _perform_drag_drop（challenger.py:518-574）。

        bezier=True（普通 chromium 用）：移到起点 → 按下前犹豫 → down → 贝塞尔轨迹
        移动（末段加噪声）→ 精确收尾 → 释放前停顿 → up。连续移动必须贝塞尔，直线瞬移
        是机器人特征，hCaptcha 会拒。
        bezier=False（camoufox 用）：move→down→move→up 一步到位，camoufox humanize
        已在内核层处理真人化。
        """
        if not bezier:
            await page.mouse.move(sx, sy)
            await page.mouse.down()
            await page.mouse.move(ex, ey)
            await page.mouse.up()
            return
        # 移到起点
        await page.mouse.move(sx, sy)
        # 按下前犹豫（真人反应时间）
        await asyncio.sleep(random.uniform(0.05, 0.15))
        await page.mouse.down()
        # 贝塞尔轨迹拖拽（复用 _bezier_trajectory + _dynamic_delays）
        steps = 25
        traj = _bezier_trajectory((sx, sy), (ex, ey), steps)
        delays = _dynamic_delays(steps, base_delay=15)
        for i, ((cx, cy), d) in enumerate(zip(traj, delays)):
            # 末段加微调噪声（对齐库：最后 30% 加噪声，最后 10% 更大）
            if i > steps * 0.7:
                noise = 0.5 if i > steps * 0.9 else 0.2
                cx += random.uniform(-noise, noise)
                cy += random.uniform(-noise, noise)
            await page.mouse.move(cx, cy)
            await asyncio.sleep(d / 1000)
        # 精确收尾到终点
        await page.mouse.move(ex, ey)
        # 释放前停顿（真人精度调整）
        await asyncio.sleep(random.uniform(0.05, 0.1))
        await page.mouse.up()
        # 拖拽间隔
        await asyncio.sleep(random.uniform(0.08, 0.12))

    async def _find_challenge_frame(self, page: Page):
        """扁平扫描 page.frames，找含 newassets.hcaptcha.com 且 frame=challenge 的 frame。"""
        deadline = time.time() + _WAIT_IFRAME_SEC
        while time.time() < deadline:
            for fr in page.frames:
                src = fr.url or ""
                if _CHALLENGE_FRAME_HINT in src and "frame=challenge" in src:
                    return fr
            await asyncio.sleep(0.5)
        return None

    async def _dump_hcaptcha_iframes(self, page: Page) -> None:
        """打印所有 hcaptcha iframe src，诊断 challenge iframe 是否存在/消失。"""
        try:
            iframes = await page.evaluate(
                """() => [...document.querySelectorAll('iframe')]
                    .map(f => f.src).filter(s => s && s.includes('hcaptcha'))"""
            )
            print("  [classify] 现有 hcaptcha iframe src:")
            for s in iframes or []:
                print(f"      {s[:160]}")
            if not iframes:
                print("      (无——checkbox 点击后挑战框可能未弹出或已消失)")
        except Exception as exc:
            print(f"  [classify] iframe 诊断失败: {exc}")

    async def _wait_images_loaded(self, fr, detected_type: str) -> None:
        """等挑战图片加载（grid 等task-image img，point 等canvas尺寸>0+settle）。

        真机诊断：point 挑战图渲染在 <canvas>（无 <img>），故先查 img 无则查 canvas。
        """
        deadline = time.time() + _WAIT_IMAGE_SEC
        if detected_type == "grid":
            while time.time() < deadline:
                done = await fr.evaluate(
                    """() => {
                        const tis = [...document.querySelectorAll('.task-image')];
                        if (tis.length < 9) return {ready: false};
                        const imgs = tis.map(t => t.querySelector('img')).filter(Boolean);
                        if (imgs.length < 9) return {ready: false};
                        return {ready: imgs.every(i => i.complete && i.naturalWidth > 0)};
                    }"""
                )
                if done and done.get("ready"):
                    return
                await asyncio.sleep(0.3)
            return
        # point/drag/unknown：先 img 无则 canvas
        found_canvas = False
        while time.time() < deadline:
            state = await fr.evaluate(
                """() => {
                    const cv = document.querySelector('.challenge-view');
                    if (!cv) return {kind: 'none'};
                    const imgs = [...cv.querySelectorAll('img')];
                    if (imgs.length) return {kind: 'img', ready: imgs.every(i => i.complete && i.naturalWidth > 0)};
                    const canvases = [...cv.querySelectorAll('canvas')];
                    if (canvases.length) return {kind: 'canvas', ready: canvases[0].width > 0 && canvases[0].height > 0};
                    return {kind: 'none'};
                }"""
            )
            kind = (state or {}).get("kind")
            if kind == "img" and state.get("ready"):
                return
            if kind == "canvas" and state.get("ready"):
                found_canvas = True
                break
            await asyncio.sleep(0.3)
        if found_canvas:
            await asyncio.sleep(1.0)  # canvas 同步绘制，1s settle
            return

    async def _capture_challenge(self, page: Page) -> dict[str, Any] | None:
        """客户端取图 + 问句 + 自适应判类型（prototype 验证逻辑搬入）。

        时序（关键）：等 challenge.js → 判类型 → 等 challenge-view → 等图片 → 截图。
        返回 {captcha_type, queries:[png_b64], question, detected_type, challenge_js_type,
              grid_w, grid_h, n_task_images} 或 None。
        """
        fr = await self._find_challenge_frame(page)
        if fr is None:
            print("  [classify] challenge iframe not found")
            await self._dump_hcaptcha_iframes(page)
            return None

        # 1) 等 challenge.js 捕获（最长 10s）
        if not self._captured_challenge_js:
            print("  [classify] waiting challenge.js ...")
            js_deadline = time.time() + 10
            while time.time() < js_deadline and not self._captured_challenge_js:
                await asyncio.sleep(0.3)
            print(f"  [classify] challenge.js = {self._captured_challenge_js!r}")

        # 2) 判类型
        detected_type = "unknown"
        challenge_js_type = None
        if self._captured_challenge_js:
            tail = self._captured_challenge_js.split("/challenge/")[-1]
            challenge_js_type = tail.rsplit("/challenge.js", 1)[0]
            detected_type = _CHALLENGE_JS_TYPE_MAP.get(challenge_js_type, "unknown")
        # DOM 兜底
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
            except Exception:
                pass

        # 3) 等 challenge-view 渲染
        cv = None
        cv_deadline = time.time() + _WAIT_RENDER_SEC
        while time.time() < cv_deadline:
            cv = await fr.query_selector(_CHALLENGE_VIEW_SEL)
            if cv is not None:
                break
            await asyncio.sleep(0.3)
        if cv is None:
            print("  [classify] challenge-view not rendered")
            # 诊断：打印 challenge iframe 内 DOM 概要 + error-text，定位是
            # hCaptcha 报错（自动化检测）还是选择器不对。
            try:
                dom = await fr.evaluate(
                    """() => {
                        const body = document.body;
                        if (!body) return {err: 'no body'};
                        const errEl = document.querySelector('.error-text, .display-error');
                        return {
                            url: location.href.slice(0, 120),
                            readyState: document.readyState,
                            bodyClasses: body.className,
                            childTags: [...body.children].map(c => c.tagName + '.' + (c.className||'').slice(0,40)),
                            hasChallengeView: !!document.querySelector('.challenge-view'),
                            hasError: !!document.querySelector('.display-error, .error-text'),
                            errorText: errEl ? (errEl.textContent||'').trim().slice(0,200) : null,
                            allDivs: [...document.querySelectorAll('div')].slice(0,15).map(d => (d.className||'').slice(0,40)).filter(Boolean),
                        };
                    }"""
                )
                print(f"  [classify] challenge iframe DOM: {json.dumps(dom, ensure_ascii=False)[:500]}")
            except Exception as exc:
                print(f"  [classify] DOM 诊断失败: {exc}")
            return None

        # 4) 等图片加载
        await self._wait_images_loaded(fr, detected_type)
        await asyncio.sleep(0.5)  # settle

        # 5) task-image 数量 + unknown 兜底
        n_task_images = await fr.eval_on_selector_all(_TASK_IMAGE_SEL, "els => els.length") or 0
        if detected_type == "unknown":
            if n_task_images == 9:
                detected_type = "grid"
            elif n_task_images == 0:
                detected_type = "drag"

        # 6) 取问句
        question_el = await fr.query_selector(_QUESTION_SEL)
        question = (await question_el.text_content()) if question_el else ""
        question = (question or "").strip()

        # 7) 截 challenge-view 整块
        png = await cv.screenshot()
        # bbox 用 Locator 方式取（与 _click_checkbox 一致，返回主框架视口坐标，
        # 比 ElementHandle.bounding_box() 在 iframe 内更可靠）。drag/point 回填
        # 路 B：服务端返回截图内像素坐标，客户端 +bbox_x/+bbox_y 映射到视口。
        cv_locator = fr.locator(_CHALLENGE_VIEW_SEL)
        box = await cv_locator.bounding_box()
        if box:
            w, h = int(box["width"]), int(box["height"])
            bbox_x, bbox_y = box["x"], box["y"]
        else:
            w, h = -1, -1
            bbox_x, bbox_y = 0.0, 0.0

        print(f"  [classify] type={detected_type} js={challenge_js_type!r} "
              f"q={question!r} size={w}x{h} task_images={n_task_images}")
        return {
            "captcha_type": detected_type,
            "queries": [base64.b64encode(png).decode("ascii")],
            "question": question,
            "detected_type": detected_type,
            "challenge_js_type": challenge_js_type,
            "grid_w": w,
            "grid_h": h,
            "n_task_images": n_task_images,
            "bbox_x": bbox_x,
            "bbox_y": bbox_y,
        }

    def _classify(self, challenge: dict[str, Any]) -> list | None:
        """POST /v1/classify；返回 answer 或 None（服务端拒/失败）。"""
        try:
            resp = requests.post(
                f"{self.api_url}/v1/classify",
                json={
                    "captchaType": "HCaptchaClassification",
                    "captcha_type": challenge.get("captcha_type"),
                    "question": challenge.get("question", ""),
                    "queries": challenge.get("queries", []),
                },
                timeout=self.timeout_seconds,
            )
            data = _response_json(resp)
            if not data.get("solved"):
                return None
            return data.get("answer")
        except Exception as exc:
            print(f"  classify request failed: {exc}")
            return None

    async def _apply_answer(self, page: Page, challenge: dict[str, Any],
                            answer: list | list[list]) -> bool:
        """客户端回填点击/拖拽 + 点提交按钮。

        按 captcha_type 把服务端答案映射到视口坐标，用 _human_click/_human_drag
        执行，最后点 .button-submit 提交。坐标映射（路 B）：服务端返回 challenge-view
        截图内像素坐标，客户端 +bbox_x/+bbox_y 映射到视口。
        """
        ctype = challenge.get("captcha_type")
        bbox_x = float(challenge.get("bbox_x", 0.0))
        bbox_y = float(challenge.get("bbox_y", 0.0))
        w = challenge.get("grid_w") or 0
        h = challenge.get("grid_h") or 0

        try:
            if ctype == "grid":
                await self._apply_grid(page, answer, bbox_x, bbox_y, w, h)
            elif ctype == "point":
                await self._apply_point(page, answer, bbox_x, bbox_y)
            elif ctype == "drag":
                await self._apply_drag(page, answer, bbox_x, bbox_y)
            else:
                print(f"  [classify] unknown captcha_type for apply: {ctype}")
                return False
        except Exception as exc:
            print(f"  [classify] apply answer failed: {exc}")
            return False

        # 点提交按钮（对齐库 challenger.py:642-643）
        return await self._click_submit(page)

    async def _apply_grid(self, page: Page, answer: list, bbox_x: float,
                          bbox_y: float, w: int, h: int) -> None:
        """grid：1 起数序号 → 九宫格每格中心视口坐标 → 逐格 click。

        answer=["2","6","9"]，序号 n 的格：col=(n-1)%3, row=(n-1)//3，
        中心视口坐标 = bbox_x + (col+0.5)*cell_w, bbox_y + (row+0.5)*cell_h。
        """
        if w <= 0 or h <= 0:
            raise RuntimeError("grid size unknown (w=%s h=%s)" % (w, h))
        cell_w, cell_h = w / 3, h / 3
        for n in answer:
            try:
                idx = int(n) - 1
            except (ValueError, TypeError):
                continue
            if not 0 <= idx <= 8:
                continue
            row, col = idx // 3, idx % 3
            cx = bbox_x + (col + 0.5) * cell_w
            cy = bbox_y + (row + 0.5) * cell_h
            print(f"  [classify] grid click #{n} at ({cx:.0f},{cy:.0f})")
            await self._human_click(page, cx, cy, bezier=self._humanize)
            await asyncio.sleep(random.uniform(0.3, 0.6))

    async def _apply_point(self, page: Page, answer: list, bbox_x: float,
                            bbox_y: float) -> None:
        """point：[[x,y]] 原图像素 → +bbox 映射视口 → click。"""
        for pt in answer:
            try:
                px, py = int(pt[0]), int(pt[1])
            except (ValueError, TypeError, IndexError):
                continue
            vx, vy = bbox_x + px, bbox_y + py
            print(f"  [classify] point click at ({vx:.0f},{vy:.0f})")
            await self._human_click(page, vx, vy, bezier=self._humanize)
            await asyncio.sleep(random.uniform(0.3, 0.6))

    async def _apply_drag(self, page: Page, answer: list, bbox_x: float,
                          bbox_y: float) -> None:
        """drag：[[sx,sy,ex,ey]] 原图像素 → +bbox 映射视口 → _human_drag。"""
        for seg in answer:
            try:
                sx, sy, ex, ey = int(seg[0]), int(seg[1]), int(seg[2]), int(seg[3])
            except (ValueError, TypeError, IndexError):
                continue
            vsx, vsy = bbox_x + sx, bbox_y + sy
            vex, vey = bbox_x + ex, bbox_y + ey
            print(f"  [classify] drag ({vsx:.0f},{vsy:.0f}) -> ({vex:.0f},{vey:.0f})")
            await self._human_drag(page, vsx, vsy, vex, vey, bezier=self._humanize)

    async def _click_submit(self, page: Page) -> bool:
        """点 challenge iframe 内 .button-submit 提交（对齐库 challenger.py:642）。"""
        fr = await self._find_challenge_frame(page)
        if fr is None:
            print("  [classify] challenge frame gone, cannot submit")
            return False
        try:
            btn = fr.locator(_SUBMIT_BUTTON_XPATH)
            if await btn.count() == 0:
                print("  [classify] submit button not found")
                return False
            box = await btn.bounding_box()
            if box is None:
                print("  [classify] submit button not visible")
                return False
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            await asyncio.sleep(random.uniform(0.3, 0.8))
            await self._human_click(page, cx, cy, bezier=self._humanize)
            print(f"  [classify] submit clicked at ({cx:.0f},{cy:.0f})")
            return True
        except Exception as exc:
            print(f"  [classify] submit failed: {exc}")
            return False

    async def _inject_hcaptcha_token(self, page: Page, token: str):
        """注入 token 到 textarea[name="h-captcha-response"]（已有实现，直接复用）。"""
        await _inject_hcaptcha_token(page, token)

    async def _fallback_local(self, page: Page) -> bool:
        """委托 LocalSolver 真机兜底（零重复逻辑）。"""
        print("  falling back to local solver (real browser)")
        local = LocalSolver(
            api_url=self.local_solver_url,
            poll_interval_seconds=self.poll_interval_seconds,
            timeout_seconds=self.timeout_seconds,
        )
        return await local.solve(page)


def build_captcha_solver(config: CaptchaConfig) -> CaptchaSolver:
    if config.mode == "manual":
        return ManualCaptchaSolver()
    if config.mode == "yescaptcha":
        if not config.yescaptcha_client_key:
            raise ValueError("yescaptcha_client_key is required")
        return YesCaptchaSolver(
            client_key=config.yescaptcha_client_key,
            api_url=config.yescaptcha_api_url,
            poll_interval_seconds=config.poll_interval_seconds,
            timeout_seconds=config.timeout_seconds,
        )
    if config.mode == "captcharun":
        if not config.captcharun_token:
            raise ValueError("captcharun_token is required")
        return CaptchaRunSolver(
            token=config.captcharun_token,
            api_url=config.captcharun_api_url,
            poll_interval_seconds=config.poll_interval_seconds,
            timeout_seconds=config.timeout_seconds,
        )
    if config.mode == "local":
        if not config.local_solver_url:
            raise ValueError("local_solver_url is required")
        return LocalSolver(
            api_url=config.local_solver_url,
            poll_interval_seconds=config.poll_interval_seconds,
            timeout_seconds=config.timeout_seconds,
        )
    if config.mode == "classify":
        if not config.classify_solver_url:
            raise ValueError("classify_solver_url is required")
        return ClassifySolver(
            api_url=config.classify_solver_url,
            poll_interval_seconds=config.poll_interval_seconds,
            timeout_seconds=config.timeout_seconds,
            local_solver_url=config.local_solver_url or config.classify_solver_url,
            humanize=config.classify_humanize,
        )
    raise ValueError(f"Unsupported captcha mode: {config.mode}")
