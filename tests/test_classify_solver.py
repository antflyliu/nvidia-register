from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import captcha as captcha_mod
from captcha import ClassifySolver, LocalSolver, reset_captcha_state


class FakePage:
    def __init__(self) -> None:
        self.url = "https://login.nvgs.nvidia.com/v1/create-account"


def _make_solver() -> ClassifySolver:
    return ClassifySolver(
        api_url="http://127.0.0.1:5072",
        poll_interval_seconds=0,
        timeout_seconds=5,
        local_solver_url="http://127.0.0.1:5072",
    )


class ClassifySolverFallbackTests(unittest.TestCase):
    """3 个 fallback 触发点（§9.5）：drag / classify 返回 None / apply_answer 失败。"""

    def setUp(self) -> None:
        reset_captcha_state()

    def _run_with_fallback_mock(self, solver: ClassifySolver, fallback_ret: bool) -> mock.Mock:
        """把 _fallback_local 换成 AsyncMock，返回 fallback_ret。

        同时 mock _click_checkbox 返回 True——solve() 第 0 步点 checkbox 弹挑战框，
        fallback 测试不验证 checkbox 点击（那是 Step 1 真机验证），跳过它直接到
        _capture_challenge 之后的 fallback 触发点。
        """
        fallback = mock.AsyncMock(return_value=fallback_ret)
        solver._fallback_local = fallback  # type: ignore[method-assign]
        solver._click_checkbox = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        return fallback

    def test_drag_triggers_fallback(self) -> None:
        solver = _make_solver()
        solver._capture_challenge = mock.AsyncMock(return_value={"captcha_type": "drag"})  # type: ignore[method-assign]
        fallback = self._run_with_fallback_mock(solver, True)
        page = FakePage()

        ok = asyncio.run(solver.solve(page))

        self.assertTrue(ok)
        fallback.assert_awaited_once()

    def test_classify_unsupported_triggers_fallback(self) -> None:
        solver = _make_solver()
        solver._capture_challenge = mock.AsyncMock(return_value={"captcha_type": "grid"})  # type: ignore[method-assign]
        solver._classify = mock.Mock(return_value=None)  # type: ignore[method-assign]
        fallback = self._run_with_fallback_mock(solver, True)
        page = FakePage()

        ok = asyncio.run(solver.solve(page))

        self.assertTrue(ok)
        fallback.assert_awaited_once()

    def test_apply_answer_failure_triggers_fallback(self) -> None:
        solver = _make_solver()
        solver._capture_challenge = mock.AsyncMock(return_value={"captcha_type": "grid"})  # type: ignore[method-assign]
        solver._classify = mock.Mock(return_value=["2", "6", "9"])  # type: ignore[method-assign]
        solver._apply_answer = mock.AsyncMock(return_value=False)  # type: ignore[method-assign]
        fallback = self._run_with_fallback_mock(solver, True)
        page = FakePage()

        ok = asyncio.run(solver.solve(page))

        self.assertTrue(ok)
        fallback.assert_awaited_once()


class ClassifySolverBuildTests(unittest.TestCase):
    def test_build_classify_solver_routes_to_ClassifySolver(self) -> None:
        from config import CaptchaConfig
        cfg = CaptchaConfig(
            mode="classify",
            yescaptcha_client_key=None,
            yescaptcha_api_url="https://api.yescaptcha.com",
            captcharun_token=None,
            captcharun_api_url="https://api.captcha-run.com",
            local_solver_url="http://127.0.0.1:5072",
            classify_solver_url="http://127.0.0.1:5072",
            classify_humanize=True,
            poll_interval_seconds=1,
            timeout_seconds=30,
        )
        solver = captcha_mod.build_captcha_solver(cfg)
        self.assertIsInstance(solver, ClassifySolver)

    def test_classify_missing_url_raises(self) -> None:
        from config import CaptchaConfig
        cfg = CaptchaConfig(
            mode="classify",
            yescaptcha_client_key=None,
            yescaptcha_api_url="https://api.yescaptcha.com",
            captcharun_token=None,
            captcharun_api_url="https://api.captcha-run.com",
            local_solver_url="",
            classify_solver_url="",
            classify_humanize=True,
            poll_interval_seconds=1,
            timeout_seconds=30,
        )
        with self.assertRaises(ValueError):
            captcha_mod.build_captcha_solver(cfg)


if __name__ == "__main__":
    unittest.main()
