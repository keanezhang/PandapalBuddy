"""WeCom Bridge origin 白名单测试（2026-06 渠道策略重构）。

验证 _handle_agent_reply 的投递门禁：
- 放行：origin ∈ {None, "", "wecom"}（全局事件 + wecom 自有事件）
- 拒收：origin ∈ {"__desktop_ipc__", "xiaozhi:{device}", ...}（其他渠道不串话）
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pandapal_relay import wecom_bridge


def _make_frame(origin: str | None) -> dict:
    return {
        "type": "message",
        "msg_id": "test-msg-1",
        "event_type": "reply_end",
        "origin_channel_id": origin,
        "target_channel_ids": [],
        "payload": {"content": "回复内容", "user_id": "u1"},
    }


@pytest.fixture
def _mock_transport():
    transport = MagicMock()
    transport._user_id = "u1"
    transport.send = AsyncMock()
    with patch.object(wecom_bridge, "_transport", transport):
        yield transport


@pytest.mark.asyncio
async def test_whitelist_allows_wecom_origin(_mock_transport):
    """origin='wecom' 放行投递。"""
    await wecom_bridge._handle_agent_reply(_make_frame("wecom"))
    _mock_transport.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_whitelist_allows_global_event(_mock_transport):
    """origin=None（全局事件，如定时任务）放行投递。"""
    await wecom_bridge._handle_agent_reply(_make_frame(None))
    _mock_transport.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_whitelist_rejects_desktop_origin(_mock_transport):
    """origin='__desktop_ipc__' 拒收——桌面会话不得串到企微。"""
    await wecom_bridge._handle_agent_reply(_make_frame("__desktop_ipc__"))
    _mock_transport.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_whitelist_rejects_xiaozhi_origin(_mock_transport):
    """origin='xiaozhi:{device}' 拒收——音箱事件归 xiaozhi_bridge，错投企微即事故。"""
    await wecom_bridge._handle_agent_reply(_make_frame("xiaozhi:dev123"))
    _mock_transport.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_hitl_bridge_target_still_skipped(_mock_transport):
    """回归：__hitl_bridge__ 内部目标仍然跳过（不受白名单影响）。"""
    frame = _make_frame("wecom")
    frame["target_channel_ids"] = ["__hitl_bridge__"]
    await wecom_bridge._handle_agent_reply(frame)
    _mock_transport.send.assert_not_awaited()
