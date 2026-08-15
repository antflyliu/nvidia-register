from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import captcha as captcha_mod
from captcha import LocalSolver, reset_captcha_state


class FakePage:
    """最小 Page 替身：只暴露 solve 用到的 url / evaluate / context.cookies。"""

    def __init__(self, url: str = "https://login.nvgs.nvidia.com/v1/create-account") -> None:
        self.url = url
        self._button_enabled = False
        self._injected: list[str] = []
        self.context = mock.Mock()
        self.context.cookies = mock.AsyncMock(return_value=[{"name": "session", "value": "abc", "domain": "login.nvgs.nvidia.com", "path": "/"}])

    async def evaluate(self, expr: str, *args: Any) -> Any:
        if "navigator.userAgent" in expr:
            return "Mozilla/5.0 (fake)"
        if "register_button" in expr:
            return self._button_enabled
        if "__hCaptchaCallback" in expr:
            # _inject_hcaptcha_token 的 evaluate：记录注入的 token 并返回
            token = args[0] if args else ""
            self._injected.append(token)
            return {"callbackInvoked": True, "textareaCount": 1}
        return None

    def enable_button(self) -> None:
        self._button_enabled = True


def _fake_response(payload: dict[str, Any]) -> mock.Mock:
    resp = mock.Mock()
    resp.json.return_value = payload
    return resp


class LocalSolverCreateTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_captcha_state()
        self.solver = LocalSolver(
            api_url="http://127.0.0.1:5072",
            poll_interval_seconds=0,
            timeout_seconds=5,
        )

    def test_create_task_sends_hcaptcha_proxyless_shape(self) -> None:
        with mock.patch("captcha.requests.post", return_value=_fake_response({"errorId": 0, "taskId": "t1"})) as post:
            task_id = self.solver._create_task(
                "https://login.nvgs.nvidia.com/v1/create-account",
                "sitekey-abc",
                "Mozilla/5.0 (fake)",
            )
        self.assertEqual(task_id, "t1")
        post.assert_called_once()
        url, kwargs = post.call_args
        self.assertEqual(url[0], "http://127.0.0.1:5072/createTask")
        body = kwargs["json"]
        task = body["task"]
        self.assertEqual(task["type"], "HCaptchaTaskProxyless")
        self.assertEqual(task["websiteURL"], "https://login.nvgs.nvidia.com/v1/create-account")
        self.assertEqual(task["websiteKey"], "sitekey-abc")
        self.assertEqual(task["userAgent"], "Mozilla/5.0 (fake)")
        # 本地服务不校验 clientKey，LocalSolver 不应发送
        self.assertNotIn("clientKey", body)
        # 无 cookie 时不发送 cookies 字段
        self.assertNotIn("cookies", task)

    def test_create_task_sends_cookies_when_provided(self) -> None:
        cookies = [{"name": "session", "value": "abc", "domain": "login.nvgs.nvidia.com", "path": "/"}]
        with mock.patch("captcha.requests.post", return_value=_fake_response({"errorId": 0, "taskId": "t1"})) as post:
            self.solver._create_task("https://login.nvgs.nvidia.com/v1/create-account", "sk", "ua", cookies)
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["task"]["cookies"], cookies)

    def test_create_task_raises_on_error_id(self) -> None:
        with mock.patch(
            "captcha.requests.post",
            return_value=_fake_response({"errorId": 1, "errorCode": "ERROR_BAD_REQUEST", "errorDescription": "websiteURL required"}),
        ):
            with self.assertRaises(RuntimeError):
                self.solver._create_task("https://x", "sk", "ua")

    def test_create_task_raises_on_missing_task_id(self) -> None:
        with mock.patch("captcha.requests.post", return_value=_fake_response({"errorId": 0})):
            with self.assertRaises(RuntimeError):
                self.solver._create_task("https://x", "sk", "ua")


class LocalSolverPollTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_captcha_state()
        self.solver = LocalSolver(
            api_url="http://127.0.0.1:5072",
            poll_interval_seconds=0,
            timeout_seconds=5,
        )

    def test_poll_returns_token_from_solution(self) -> None:
        with mock.patch(
            "captcha.requests.post",
            return_value=_fake_response({"errorId": 0, "status": "ready", "solution": {"token": "P1_abc"}}),
        ):
            self.assertEqual(self.solver._poll_task_result("t1"), "P1_abc")

    def test_poll_returns_grecaptcha_response_fallback(self) -> None:
        with mock.patch(
            "captcha.requests.post",
            return_value=_fake_response({"errorId": 0, "status": "ready", "solution": {"gRecaptchaResponse": "P1_xyz"}}),
        ):
            self.assertEqual(self.solver._poll_task_result("t1"), "P1_xyz")

    def test_poll_returns_none_on_error_id(self) -> None:
        with mock.patch(
            "captcha.requests.post",
            return_value=_fake_response({"errorId": 1, "errorCode": "ERROR_SOLVER", "errorDescription": "boom"}),
        ):
            self.assertIsNone(self.solver._poll_task_result("t1"))

    def test_poll_returns_none_on_timeout(self) -> None:
        # 一直返回 processing，直到超时
        with mock.patch(
            "captcha.requests.post",
            return_value=_fake_response({"errorId": 0, "status": "processing"}),
        ):
            self.assertIsNone(self.solver._poll_task_result("t1"))

    def test_poll_retries_after_network_error(self) -> None:
        # 第一次网络异常，第二次成功
        import requests

        responses = [
            requests.RequestException("net down"),
            _fake_response({"errorId": 0, "status": "ready", "solution": {"token": "P1_ok"}}),
        ]
        with mock.patch("captcha.requests.post", side_effect=responses):
            self.assertEqual(self.solver._poll_task_result("t1"), "P1_ok")


class LocalSolverSolveTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_captcha_state()
        self.solver = LocalSolver(
            api_url="http://127.0.0.1:5072",
            poll_interval_seconds=0,
            timeout_seconds=5,
        )

    def test_solve_success_injects_token_and_enables_button(self) -> None:
        captcha_mod._captured_sitekey = "sitekey-abc"
        page = FakePage()

        def _post(url: str, **kwargs: Any) -> mock.Mock:
            if url.endswith("/createTask"):
                return _fake_response({"errorId": 0, "taskId": "t1"})
            return _fake_response({"errorId": 0, "status": "ready", "solution": {"token": "P1_ok"}})

        with mock.patch("captcha.requests.post", side_effect=_post):
            # 注入后按钮 enable
            async def _run() -> bool:
                result = await self.solver.solve(page)
                return result

            import asyncio

            # 让按钮在注入后 enable
            original_inject = captcha_mod._inject_hcaptcha_token

            async def _inject_and_enable(p, token):
                ok = await original_inject(p, token)
                p.enable_button()
                return ok

            with mock.patch("captcha._inject_hcaptcha_token", side_effect=_inject_and_enable):
                result = asyncio.run(_run())

        self.assertTrue(result)
        self.assertEqual(page._injected, ["P1_ok"])

    def test_solve_returns_false_when_sitekey_missing(self) -> None:
        reset_captcha_state()  # 无 sitekey
        page = FakePage()
        with mock.patch("captcha.requests.post") as post:
            import asyncio

            result = asyncio.run(self.solver.solve(page))
        self.assertFalse(result)
        post.assert_not_called()

    def test_solve_raises_when_create_task_fails(self) -> None:
        # 与 YesCaptchaSolver 一致：_create_task 失败时异常向上传播，
        # 由 main._solve_captcha_and_submit 捕获并视为该账号失败。
        captcha_mod._captured_sitekey = "sitekey-abc"
        page = FakePage()
        with mock.patch(
            "captcha.requests.post",
            return_value=_fake_response({"errorId": 1, "errorCode": "ERROR_SOLVER", "errorDescription": "boom"}),
        ):
            import asyncio

            with self.assertRaises(RuntimeError):
                asyncio.run(self.solver.solve(page))

    def test_solve_returns_false_when_token_injected_but_button_stays_disabled(self) -> None:
        captcha_mod._captured_sitekey = "sitekey-abc"
        page = FakePage()  # 按钮一直 disabled

        def _post(url: str, **kwargs: Any) -> mock.Mock:
            if url.endswith("/createTask"):
                return _fake_response({"errorId": 0, "taskId": "t1"})
            return _fake_response({"errorId": 0, "status": "ready", "solution": {"token": "P1_ok"}})

        with mock.patch("captcha.requests.post", side_effect=_post):
            import asyncio

            result = asyncio.run(self.solver.solve(page))
        self.assertFalse(result)


class LocalSolverUnreachableTests(unittest.TestCase):
    """服务不可达时给出清晰可操作的错误（而非晦涩的 urllib3 连接错误）。"""

    def setUp(self) -> None:
        reset_captcha_state()
        self.solver = LocalSolver(
            api_url="http://127.0.0.1:5072",
            poll_interval_seconds=0,
            timeout_seconds=5,
        )

    def test_create_task_raises_actionable_error_when_service_unreachable(self) -> None:
        import requests

        with mock.patch(
            "captcha.requests.post",
            side_effect=requests.exceptions.ConnectionError(
                "Max retries exceeded with url: /createTask "
                "(Caused by NewConnectionError(...): Failed to establish a new connection: "
                "[WinError 10061] 由于目标计算机积极拒绝，无法连接。)"
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.solver._create_task(
                    "https://login.nvgs.nvidia.com/v1/create-account", "sk", "ua"
                )
        msg = str(ctx.exception)
        # 清晰可操作：包含服务地址 + 不可达 + 修复指引
        self.assertIn("127.0.0.1:5072", msg)
        self.assertIn("unreachable", msg)
        self.assertIn("camoufox-turnstile", msg)
        self.assertIn("solver_hcaptcha", msg)

    def test_create_task_raises_actionable_error_on_timeout(self) -> None:
        import requests

        with mock.patch(
            "captcha.requests.post",
            side_effect=requests.exceptions.ConnectTimeout("timed out"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.solver._create_task("https://x", "sk", "ua")
        self.assertIn("unreachable", str(ctx.exception))

    def test_poll_keeps_retrying_on_network_error(self) -> None:
        # 轮询阶段网络抖动仍应继续重试（不因单次抖动失败），与既有行为一致
        import requests

        responses = [
            requests.exceptions.ConnectionError("net down"),
            _fake_response({"errorId": 0, "status": "ready", "solution": {"token": "P1_ok"}}),
        ]
        with mock.patch("captcha.requests.post", side_effect=responses):
            self.assertEqual(self.solver._poll_task_result("t1"), "P1_ok")


if __name__ == "__main__":
    unittest.main()
