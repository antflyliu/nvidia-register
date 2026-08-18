from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as main_mod
from main import _SELECT_ACCOUNT_RELOAD_AFTER, finalize_and_create_key


class _FakePage:
    """最小 fake，URL 始终落在 select-account，模拟卡加载白屏。"""

    def __init__(self) -> None:
        # URL 含 select-account 但 DOM 未渲染（_create_org 会返回 False）
        self.url = "https://cloudaccounts.nvidia.com/select-account"


class SelectAccountRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """select-account 页卡加载时不应死循环到阶段C超时，应 reload 恢复并最终建 key。

    Bug: select-account 连续填组织名失败（表单未渲染）时旧代码丢 _create_org 返回值、
    盲 sleep(4) 循环，一直打「创建组织页：填组织名...」直到阶段C 300s 超时。修复后：
    连续失败到 _SELECT_ACCOUNT_RELOAD_AFTER 触发 _recover_from_navigation_error
    （reload），恢复后能拿到 orgName 建 key。
    """

    async def test_consecutive_create_org_failures_trigger_reload_then_key(self) -> None:
        page = _FakePage()
        cfg = mock.MagicMock()

        # _get_org_name：失败阶段返回 None（session 未就绪），reload 后返回 orgName
        # _create_org：前 N 次返回 False（表单未渲染），reload 后不再被调（直接建 key）
        get_org = mock.AsyncMock(side_effect=[None, None, None, None, "my-org"])
        create_org = mock.AsyncMock(return_value=False)  # 表单一直卡住
        create_key = mock.AsyncMock(return_value="nvapi-fake-key")
        recover = mock.AsyncMock(return_value=True)

        with mock.patch.object(main_mod, "_get_org_name", get_org), \
             mock.patch.object(main_mod, "_create_org", create_org), \
             mock.patch.object(main_mod, "_create_key_in_browser", create_key), \
             mock.patch.object(main_mod, "_recover_from_navigation_error", recover), \
             mock.patch.object(main_mod, "asyncio", wraps=asyncio) as aio:
            aio.sleep = mock.AsyncMock()  # 跳过所有 sleep，加速测试
            api_key = await finalize_and_create_key(page, cfg)

        self.assertEqual(api_key, "nvapi-fake-key")
        # 关键契约：连续失败触发了 reload（_recover_from_navigation_error 被调至少 1 次）
        self.assertGreaterEqual(recover.await_count, 1)
        # _create_org 失败次数应达到阈值（即不是失败 1 次就 reload，也未在 reload 后继续盲填）
        self.assertGreaterEqual(create_org.await_count, _SELECT_ACCOUNT_RELOAD_AFTER)
        # 最终拿到了 orgName 并建 key（证明流程接回，没卡在死循环/超时）
        create_key.assert_awaited_once_with(page, "my-org", cfg)

    async def test_under_threshold_keeps_retrying_without_reload(self) -> None:
        """连续失败未达阈值时不 reload，仅等待重试（避免抖动误触发刷新）。"""
        page = _FakePage()
        cfg = mock.MagicMock()

        # 前 2 次 _create_org 失败未达阈值（阈值=3）；第 3 轮 _get_org_name 先就绪，
        # 状态机直接建 key 退出，不再调 _create_org。全程不触发 reload。
        # 状态机每轮先查 _get_org_name，其 side_effect 第 3 次=orgName 即在此轮退出。
        get_org = mock.AsyncMock(side_effect=[None, None, "my-org"])
        create_org = mock.AsyncMock(side_effect=[False, False])
        create_key = mock.AsyncMock(return_value="nvapi-fake-key")
        recover = mock.AsyncMock(return_value=True)

        with mock.patch.object(main_mod, "_get_org_name", get_org), \
             mock.patch.object(main_mod, "_create_org", create_org), \
             mock.patch.object(main_mod, "_create_key_in_browser", create_key), \
             mock.patch.object(main_mod, "_recover_from_navigation_error", recover), \
             mock.patch.object(main_mod, "asyncio", wraps=asyncio) as aio:
            aio.sleep = mock.AsyncMock()
            api_key = await finalize_and_create_key(page, cfg)

        self.assertEqual(api_key, "nvapi-fake-key")
        # 仅前 2 次失败未达阈值（阈值=3），第 3 轮 session 就绪直接建 key — 全程不 reload
        self.assertEqual(create_org.await_count, 2)
        recover.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
