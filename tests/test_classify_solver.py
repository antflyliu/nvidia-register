from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import captcha as captcha_mod
from captcha import ClassifySolver, reset_captcha_state


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
    """_solve_rounds 多轮循环逻辑（对齐库 for cid in range(crumb_count) 模型）。

    hCaptcha 多轮：单次 /getcaptcha/ 下发 tasklist，长度=轮数。库在单次挑战内
    for cid 跑完所有轮（每轮取图→判图→回填→提交，不等 pass），所有轮提交后
    统一查 pass。_solve_rounds 对齐此模型：_run_crumbs 跑 crumb_n 轮 →
    _wait_token_brief 等一次 pass → 没拿到 refresh 重试。
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

    def _challenge(self) -> dict:
        return {
            "captcha_type": "drag", "queries": ["fake_b64"], "question": "Drag",
            "bbox_x": 100.0, "bbox_y": 100.0, "grid_w": 500, "grid_h": 469,
        }

    def test_multi_round_2_crumbs_then_pass(self) -> None:
        """2 轮挑战：_run_crumbs 跑 2 轮（apply_answer×2）→ 等 pass 1 次 → 拿 token。"""
        solver = self._make_solver()
        solver._click_checkbox = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        solver._capture_challenge = mock.AsyncMock(return_value=self._challenge())  # type: ignore[method-assign]
        solver._read_crumb_count = mock.AsyncMock(return_value=2)  # type: ignore[method-assign]
        solver._classify = mock.Mock(return_value=[[10, 20, 30, 40]])  # type: ignore[method-assign]
        solver._apply_answer = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        solver._wait_token_brief = mock.AsyncMock(return_value="fake-token-2443")  # type: ignore[method-assign]
        solver._inject_hcaptcha_token = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        with mock.patch.object(captcha_mod, "_is_register_button_enabled",
                               new=mock.AsyncMock(side_effect=[True])):
            page = FakePage()
            ok = asyncio.run(solver.solve(page))
        self.assertTrue(ok)
        # 2 轮：_apply_answer 调 2 次，_wait_token_brief 只调 1 次（所有轮提交后统一等）
        self.assertEqual(solver._apply_answer.await_count, 2)
        self.assertEqual(solver._wait_token_brief.await_count, 1)
        solver._inject_hcaptcha_token.assert_awaited_once_with(page, "fake-token-2443")

    def test_single_round_1_crumb_then_pass(self) -> None:
        """单轮挑战：crumb_count=1 → _run_crumbs 跑 1 轮 → 等 pass → 拿 token。"""
        solver = self._make_solver()
        solver._click_checkbox = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        solver._capture_challenge = mock.AsyncMock(return_value=self._challenge())  # type: ignore[method-assign]
        solver._read_crumb_count = mock.AsyncMock(return_value=1)  # type: ignore[method-assign]
        solver._classify = mock.Mock(return_value=[[10, 20, 30, 40]])  # type: ignore[method-assign]
        solver._apply_answer = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        solver._wait_token_brief = mock.AsyncMock(return_value="fake-token-2443")  # type: ignore[method-assign]
        solver._inject_hcaptcha_token = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        with mock.patch.object(captcha_mod, "_is_register_button_enabled",
                               new=mock.AsyncMock(side_effect=[True])):
            ok = asyncio.run(solver.solve(FakePage()))
        self.assertTrue(ok)
        # 单轮：_apply_answer 和 _wait_token_brief 各调 1 次
        self.assertEqual(solver._apply_answer.await_count, 1)
        self.assertEqual(solver._wait_token_brief.await_count, 1)

    def test_no_pass_triggers_fallback(self) -> None:
        """所有 attempt 都未 pass → _solve_rounds 返回 None → fallback local。"""
        solver = self._make_solver()
        solver._click_checkbox = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        solver._capture_challenge = mock.AsyncMock(return_value=self._challenge())  # type: ignore[method-assign]
        solver._read_crumb_count = mock.AsyncMock(return_value=1)  # type: ignore[method-assign]
        solver._classify = mock.Mock(return_value=[[10, 20, 30, 40]])  # type: ignore[method-assign]
        solver._apply_answer = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        solver._wait_token_brief = mock.AsyncMock(return_value=None)  # type: ignore[method-assign]
        solver._refresh_challenge = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        fallback = mock.AsyncMock(return_value=True)
        solver._fallback_local = fallback  # type: ignore[method-assign]
        ok = asyncio.run(solver.solve(FakePage()))
        self.assertTrue(ok)
        # 跑满 _MAX_CHALLENGE_ROUNDS 次 attempt 后 fallback
        from captcha import _MAX_CHALLENGE_ROUNDS
        self.assertEqual(solver._wait_token_brief.await_count, _MAX_CHALLENGE_ROUNDS)
        # attempt 2+ 每轮都 refresh（attempt 1 不 refresh），共 _MAX-1 次
        self.assertEqual(solver._refresh_challenge.await_count, _MAX_CHALLENGE_ROUNDS - 1)
        fallback.assert_awaited_once()

    def test_attempt2_calls_real_refresh(self) -> None:
        """attempt 1 no pass → attempt 2 前调 _refresh_challenge（真换题，非空循环）。"""
        solver = self._make_solver()
        solver._click_checkbox = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        solver._capture_challenge = mock.AsyncMock(return_value=self._challenge())  # type: ignore[method-assign]
        solver._read_crumb_count = mock.AsyncMock(return_value=1)  # type: ignore[method-assign]
        solver._classify = mock.Mock(return_value=[[10, 20, 30, 40]])  # type: ignore[method-assign]
        solver._apply_answer = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        # attempt 1 no pass，attempt 2 pass
        solver._wait_token_brief = mock.AsyncMock(side_effect=[None, "fake-token-2443"])  # type: ignore[method-assign]
        solver._refresh_challenge = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        solver._inject_hcaptcha_token = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        # 模拟 attempt 1 已捕获 challenge.js（_capture_challenge 依赖它判类型）
        solver._captured_challenge_js = "https://x/challenge/image_label_area_select/challenge.js"
        with mock.patch.object(captcha_mod, "_is_register_button_enabled",
                               new=mock.AsyncMock(side_effect=[True])):
            ok = asyncio.run(solver.solve(FakePage()))
        self.assertTrue(ok)
        # attempt 1 不 refresh，attempt 2 前 refresh 1 次
        solver._refresh_challenge.assert_awaited_once()
        # refresh 后不重置 _captured_challenge_js（复用 attempt 1 类型，真机实测
        # refresh 同类型换题不重新加载 challenge.js）
        self.assertIsNotNone(solver._captured_challenge_js)

    def test_capture_none_after_refresh_retries(self) -> None:
        """attempt 2+ refresh 后 _capture_challenge 返回 None（类型未知）→ continue 重试，不退出。"""
        solver = self._make_solver()
        solver._click_checkbox = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        # attempt 1 取图成功但 no pass；attempt 2 refresh 后取图 None（类型未知）；
        # attempt 3 取图成功且 pass
        solver._capture_challenge = mock.AsyncMock(
            side_effect=[self._challenge(), None, self._challenge()])  # type: ignore[method-assign]
        solver._read_crumb_count = mock.AsyncMock(return_value=1)  # type: ignore[method-assign]
        solver._classify = mock.Mock(return_value=[[10, 20, 30, 40]])  # type: ignore[method-assign]
        solver._apply_answer = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        solver._wait_token_brief = mock.AsyncMock(side_effect=[None, "fake-token-2443"])  # type: ignore[method-assign]
        solver._refresh_challenge = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        solver._inject_hcaptcha_token = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
        with mock.patch.object(captcha_mod, "_is_register_button_enabled",
                               new=mock.AsyncMock(side_effect=[True])):
            ok = asyncio.run(solver.solve(FakePage()))
        self.assertTrue(ok)
        # attempt 2 capture None 被 continue 跳过（不 apply_answer 不 wait_token），
        # attempt 3 正常走完拿 token：apply_answer 2 次（attempt 1 + 3），wait 2 次
        self.assertEqual(solver._apply_answer.await_count, 2)
        self.assertEqual(solver._wait_token_brief.await_count, 2)
        # refresh 调 2 次（attempt 2 和 attempt 3 前）
        self.assertEqual(solver._refresh_challenge.await_count, 2)


class ClassifySolverNoFallbackByDefaultTests(unittest.TestCase):
    """mode=classify 默认不应在判图失败时打 /createTask 开浏览器。

    回归：camoufox-turnstile 服务端 /v1/classify 是纯 LLM 判图不开浏览器，
    但旧 ClassifySolver 在判图失败时 fallback 到 LocalSolver（打 /createTask
    HCaptchaTaskProxyless）会触发服务端 CamoufoxHCaptchaSolver 开 camoufox
    浏览器，违背 classify 模式"无浏览器"初衷。新默认 classify_fallback_local=False
    时 _fallback_local 直接返回 False 不打 createTask。详见
    memory/classify-fallback-opens-browser.md。
    """
    def setUp(self) -> None:
        reset_captcha_state()

    def test_default_no_fallback_does_not_call_local_solver(self) -> None:
        solver = ClassifySolver(
            api_url="http://127.0.0.1:5072",
            poll_interval_seconds=0,
            timeout_seconds=5,
            local_solver_url="http://127.0.0.1:5072",
            # 新默认：classify_fallback_local=False
            fallback_local=False,
        )
        self.assertFalse(solver._fallback_local_enabled)
        # 把 _click_checkbox mock 成失败 → solve() 第 0 步就 fallback_local
        solver._click_checkbox = mock.AsyncMock(return_value=False)  # type: ignore[method-assign]
        # LocalSolver.solve 绝不能被调；若被调说明 fallback 打了 createTask
        local_solve = mock.AsyncMock(return_value=True)
        with mock.patch.object(captcha_mod, "LocalSolver") as LocalCls:
            LocalCls.return_value.solve = local_solve
            ok = asyncio.run(solver.solve(FakePage()))
        # 默认不 fallback：返回 False，账号失败，但不开浏览器
        self.assertFalse(ok)
        local_solve.assert_not_awaited()
        self.assertEqual(LocalCls.call_count, 0)

    def test_fallback_enabled_preserves_old_behavior(self) -> None:
        """显式 classify_fallback_local=True 仍走 LocalSolver（向后兼容）。"""
        solver = ClassifySolver(
            api_url="http://127.0.0.1:5072",
            poll_interval_seconds=0,
            timeout_seconds=5,
            local_solver_url="http://127.0.0.1:5072",
            fallback_local=True,
        )
        self.assertTrue(solver._fallback_local_enabled)
        solver._click_checkbox = mock.AsyncMock(return_value=False)  # type: ignore[method-assign]
        local_solve = mock.AsyncMock(return_value=True)
        with mock.patch.object(captcha_mod, "LocalSolver") as LocalCls:
            LocalCls.return_value.solve = local_solve
            ok = asyncio.run(solver.solve(FakePage()))
        self.assertTrue(ok)
        local_solve.assert_awaited_once()


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
