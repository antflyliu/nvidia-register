from __future__ import annotations

import asyncio
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
    ):
        self.api_url = api_url.rstrip("/")
        self.poll_interval_seconds = poll_interval_seconds
        self.timeout_seconds = timeout_seconds
        self.local_solver_url = local_solver_url or api_url

    async def solve(self, page: Page) -> bool:
        """与 LocalSolver.solve 完全相同的接口。"""
        print("\n[2/4] Solving hCaptcha with classify solver...")

        # 1) 取图 + 问句（客户端取图能力，阶段 2 实现）
        challenge = await self._capture_challenge(page)
        if challenge is None:
            print("  no challenge frame surfaced")
            return False

        # 2) drag/bbox 直接 fallback（stage1 不支持）
        if challenge.get("captcha_type") in ("drag", "bbox"):
            print(f"  {challenge['captcha_type']} unsupported by classify, fallback local")
            return await self._fallback_local(page)

        # 3) 调自建判图服务
        answer = self._classify(challenge)
        if answer is None:
            print("  classify returned unsupported or failed, fallback local")
            return await self._fallback_local(page)

        # 4) 客户端回填点击 + 提交（阶段 3 实现）
        ok = await self._apply_answer(page, challenge, answer)
        if not ok:
            print("  apply_answer failed, fallback local")
            return await self._fallback_local(page)

        # 5) 等 token 注入，检查按钮使能
        token = await self._wait_hcaptcha_token(page)
        if not token:
            return False

        await self._inject_hcaptcha_token(page, token)

        for i in range(20):
            if await _is_register_button_enabled(page):
                print(f"  hCaptcha solved by classify solver ({i}s)")
                return True
            await asyncio.sleep(1)

        print("  hCaptcha token injected, but #register_button stayed disabled")
        return False

    async def _capture_challenge(self, page: Page) -> dict[str, Any] | None:
        """客户端取图 + 问句（TODO: 阶段 2 实现）"""
        # TODO: 实现进 frame=challenge + task-image.screenshot() + question/anchor 提取
        # 目前直接返回模拟数据，便于后续开发
        return {
            "captcha_type": "grid",
            "queries": ["base64-placeholder-for-grid-image"],
            "question": "Click the 5th image",
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

    async def _apply_answer(self, page: Page, challenge: dict[str, Any], answer: list | list[list]) -> bool:
        """客户端回填点击提交（TODO: 阶段 3 实现）"""
        # TODO: 按 captcha_type 做坐标映射 + page.mouse.click + button-submit
        print(f"  [TODO] Applying answer: {answer} (stub)")
        return True

    async def _wait_hcaptcha_token(self, page: Page) -> str | None:
        """等待 /getcaptcha/ 返回 pass，提取 token。（TODO: 阶段 3 实现）"""
        print("  [TODO] Waiting for hCaptcha pass and token")
        return "stub-token-placeholder"

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
        )
    raise ValueError(f"Unsupported captcha mode: {config.mode}")
