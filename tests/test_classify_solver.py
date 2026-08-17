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


class ClassifySolverMultiRoundTests(unittest.TestCase):
    """_solve_rounds 多轮循环逻辑（不依赖真机 hCaptcha）。

    hCaptcha 多轮机制：全部轮通过才返回 pass token。mock _capture_challenge
    返回多轮挑战 + _wait_token_brief 第 1 轮返回 None（未 pass）第 2 轮返回
    token（pass），验证循环推进 + 退出逻辑。
    """

    def setUp(self) -> None:
        reset_captcha_state()

    def _make_solver(self) -> ClassifySolver:
        return ClassifySolver(
            api_url="http://127.0.0.1:5072",
            poll_interval_seconds=0,
            timeout_seconds=30,
            local_solver_url="http://127.0.0.1:5072",
        )

    def test_multi_round_passes_on_round_2(self) -> None:
        """2 轮挑战：round 1 未 pass，round 2 pass → 拿 token，注入成功。"""
        solver = self._make_solver()
        solver._click_checkbox = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        # _capture_challenge 每轮返回 drag 挑战（含 bbox 字段）
        challenge = {
            "captcha_type": "drag",
            "queries": ["fake_b64"],
            "question": "Drag the shape",
            "bbox_x": 100.0, "bbox_y": 100.0,
            "grid_w": 500, "grid_h": 469,
        }
        solver._capture_challenge = mock.AsyncMock(return_value=challenge)  # type: ignore[method-assign]
        solver._read_crumb_count = mock.AsyncMock(return_value=2)  # type: ignore[method-assign]
        solver._classify = mock.Mock(return_value=[[10, 20, 30, 40]])  # type: ignore[method-assign]
        solver._apply_answer = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        # round 1 未 pass（None），round 2 pass（token）
        solver._wait_token_brief = mock.AsyncMock(side_effect=[None, "fake-token-2443"])  # type: ignore[method-assign]
        solver._inject_hcaptcha_token = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        # 注册按钮第 1 次检查就 enable
        with mock.patch.object(captcha_mod, "_is_register_button_enabled",
                               new=mock.AsyncMock(side_effect=[True])):
            page = FakePage()
            ok = asyncio.run(solver.solve(page))
        self.assertTrue(ok)
        # _apply_answer 被调 2 次（2 轮），_wait_token_brief 被调 2 次
        self.assertEqual(solver._apply_answer.await_count, 2)
        self.assertEqual(solver._wait_token_brief.await_count, 2)
        solver._inject_hcaptcha_token.assert_awaited_once_with(page, "fake-token-2443")

    def test_single_round_passes_on_round_1(self) -> None:
        """单轮挑战：round 1 就 pass → 不进 round 2。"""
        solver = self._make_solver()
        solver._click_checkbox = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        challenge = {
            "captcha_type": "drag", "queries": ["fake_b64"], "question": "Drag",
            "bbox_x": 100.0, "bbox_y": 100.0, "grid_w": 500, "grid_h": 469,
        }
        solver._capture_challenge = mock.AsyncMock(return_value=challenge)  # type: ignore[method-assign]
        solver._read_crumb_count = mock.AsyncMock(return_value=1)  # type: ignore[method-assign]
        solver._classify = mock.Mock(return_value=[[10, 20, 30, 40]])  # type: ignore[method-assign]
        solver._apply_answer = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        # round 1 直接 pass
        solver._wait_token_brief = mock.AsyncMock(return_value="fake-token-2443")  # type: ignore[method-assign]
        solver._inject_hcaptcha_token = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        with mock.patch.object(captcha_mod, "_is_register_button_enabled",
                               new=mock.AsyncMock(side_effect=[True])):
            ok = asyncio.run(solver.solve(FakePage()))
        self.assertTrue(ok)
        # 只 1 轮：_apply_answer 和 _wait_token_brief 各调 1 次
        self.assertEqual(solver._apply_answer.await_count, 1)
        self.assertEqual(solver._wait_token_brief.await_count, 1)

    def test_all_rounds_fail_triggers_fallback(self) -> None:
        """所有轮都未 pass → _solve_rounds 返回 None → fallback local。"""
        solver = self._make_solver()
        solver._click_checkbox = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        challenge = {"captcha_type": "drag", "queries": ["fake_b64"], "question": "Drag",
                      "bbox_x": 100.0, "bbox_y": 100.0, "grid_w": 500, "grid_h": 469}
        solver._capture_challenge = mock.AsyncMock(return_value=challenge)  # type: ignore[method-assign]
        solver._read_crumb_count = mock.AsyncMock(return_value=2)  # type: ignore[method-assign]
        solver._classify = mock.Mock(return_value=[[10, 20, 30, 40]])  # type: ignore[method-assign]
        solver._apply_answer = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        # 所有轮都未 pass
        solver._wait_token_brief = mock.AsyncMock(return_value=None)  # type: ignore[method-assign]
        fallback = mock.AsyncMock(return_value=True)
        solver._fallback_local = fallback  # type: ignore[method-assign]
        ok = asyncio.run(solver.solve(FakePage()))
        self.assertTrue(ok)
        # 跑满 _MAX_CHALLENGE_ROUNDS 轮后 fallback
        from captcha import _MAX_CHALLENGE_ROUNDS
        self.assertEqual(solver._apply_answer.await_count, _MAX_CHALLENGE_ROUNDS)
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
