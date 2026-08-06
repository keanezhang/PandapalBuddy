"""XiaoZhi Bridge 测试（Relay 侧，无真实设备/ASR/TTS）。

验证点：
- WebSocket hello 握手流程
- 协议版本校验
- 音频帧缓冲 + ASR 转发
- Agent 回复 → TTS → 设备
- 多渠道路由（server.py 集成）
- 配置加载
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pandapal_relay.xiaozhi.asr import (
    ASRProvider,
    DashScopeASRProvider,
    MockASRProvider,
    QwenRealtimeASRProvider,
    WhisperASRProvider,
    create_asr_provider,
)
from pandapal_relay.xiaozhi.models import (
    SUPPORTED_PROTOCOL_VERSIONS,
    DeviceCapabilities,
    XiaoZhiDeviceSession,
    XiaoZhiMessageType,
)
from pandapal_relay.xiaozhi.tts import (
    DashScopeTTSProvider,
    EdgeTTSProvider,
    MockTTSProvider,
    QwenRealtimeTTSProvider,
    TTSProvider,
    create_tts_provider,
)
from pandapal_relay.xiaozhi_bridge import (
    _active_devices,
    _handle_audio_frame,
    _handle_json_message,
    _handle_listen,
    _process_voice_input,
    _send_tts_to_device,
    get_connected_xiaozhi_devices,
    get_device_count,
    handle_agent_reply_for_xiaozhi,
    init_xiaozhi_bridge,
)
from pandapal_relay.config import RelayConfig


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_active_devices():
    """每个测试前后清理全局设备状态。"""
    _active_devices.clear()
    yield
    _active_devices.clear()


def _make_session(
    device_id: str = "test-device-01",
    is_recording: bool = False,
) -> XiaoZhiDeviceSession:
    """创建一个测试用的设备会话。"""
    from datetime import datetime, timezone

    ws = AsyncMock()
    ws.send_text = AsyncMock()
    ws.send_bytes = AsyncMock()
    ws.close = AsyncMock()

    session = XiaoZhiDeviceSession(
        device_id=device_id,
        session_id=f"xiaozhi-{device_id}",
        websocket=ws,
        capabilities=DeviceCapabilities(
            supports_mcp=False,
            audio_format="opus",
            input_sample_rate=16000,
            output_sample_rate=24000,
            frame_duration_ms=60,
        ),
        connected_at=datetime.now(timezone.utc),
        last_active_at=datetime.now(timezone.utc),
        is_recording=is_recording,
    )
    return session


# ──────────────────────────────────────────────
# Models Tests
# ──────────────────────────────────────────────


def test_supported_protocol_versions():
    """协议版本集合包含 v3。"""
    assert 3 in SUPPORTED_PROTOCOL_VERSIONS
    assert 2 not in SUPPORTED_PROTOCOL_VERSIONS


def test_device_capabilities_frozen():
    """DeviceCapabilities 是不可变 dataclass。"""
    cap = DeviceCapabilities(supports_mcp=True)
    with pytest.raises(Exception):  # FrozenInstanceError
        cap.supports_mcp = False  # type: ignore


def test_message_type_values():
    """消息类型枚举值正确。"""
    assert XiaoZhiMessageType.HELLO == "hello"
    assert XiaoZhiMessageType.LISTEN == "listen"
    assert XiaoZhiMessageType.TTS == "tts"
    assert XiaoZhiMessageType.ABORT == "abort"
    assert XiaoZhiMessageType.GOODBYE == "goodbye"


# ──────────────────────────────────────────────
# ASR Provider Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mock_asr_returns_placeholder():
    """Mock ASR 返回占位文字。"""
    asr = MockASRProvider()
    result = await asr.transcribe(b"fake_audio_data", 16000)
    assert "ASR mock" in result
    assert "15" in result  # len(b"fake_audio_data") = 15


def test_create_asr_provider_mock():
    """无配置时回退到 Mock。"""
    provider = create_asr_provider("mock")
    assert isinstance(provider, MockASRProvider)


def test_create_asr_provider_dashscope():
    """有 API Key 时创建 DashScope ASR。"""
    provider = create_asr_provider("dashscope", "test-key")
    assert isinstance(provider, DashScopeASRProvider)


def test_create_asr_provider_qwen_realtime():
    """有 API Key 时创建 Qwen Realtime ASR。"""
    provider = create_asr_provider("qwen_realtime", "test-key")
    assert isinstance(provider, QwenRealtimeASRProvider)


def test_create_asr_provider_whisper():
    """有 API Key 时创建 Whisper ASR。"""
    provider = create_asr_provider("whisper", "test-key")
    assert isinstance(provider, WhisperASRProvider)


def test_create_asr_provider_fallback():
    """无效 provider 名称回退到 Mock。"""
    provider = create_asr_provider("unknown_provider", "key")
    assert isinstance(provider, MockASRProvider)


def test_create_asr_provider_no_key():
    """有 provider 但无 API Key 回退到 Mock。"""
    provider = create_asr_provider("dashscope", "")
    assert isinstance(provider, MockASRProvider)


# ──────────────────────────────────────────────
# TTS Provider Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mock_tts_returns_empty():
    """Mock TTS 返回空音频。"""
    tts = MockTTSProvider()
    result = await tts.synthesize("你好")
    assert result == b""


def test_create_tts_provider_mock():
    """无配置时回退到 Mock。"""
    provider = create_tts_provider("mock")
    assert isinstance(provider, MockTTSProvider)


def test_create_tts_provider_edge_tts():
    """edge_tts 不需要 API Key。"""
    provider = create_tts_provider("edge_tts")
    assert isinstance(provider, EdgeTTSProvider)


def test_create_tts_provider_dashscope():
    """有 API Key 时创建 DashScope TTS。"""
    provider = create_tts_provider("dashscope", "test-key")
    assert isinstance(provider, DashScopeTTSProvider)


def test_create_tts_provider_qwen_realtime():
    """有 API Key 时创建 Qwen Realtime TTS。"""
    provider = create_tts_provider("qwen_realtime", "test-key")
    assert isinstance(provider, QwenRealtimeTTSProvider)


def test_create_tts_provider_fallback():
    """无效 provider 名称回退到 Mock。"""
    provider = create_tts_provider("unknown", "key")
    assert isinstance(provider, MockTTSProvider)


# ──────────────────────────────────────────────
# Audio Frame Handling Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audio_frame_buffered_when_recording():
    """录音中时音频帧被缓冲（解析 BinaryProtocol3 头部）。"""
    session = _make_session(is_recording=True)
    assert len(session.audio_buffer) == 0

    # 构造 BinaryProtocol3 帧: type=0(OPUS), reserved=0, payload_size, payload
    import struct
    payload1 = b"opus_frame_1"
    frame1 = struct.pack("<BBH", 0, 0, len(payload1)) + payload1
    payload2 = b"opus_frame_2"
    frame2 = struct.pack("<BBH", 0, 0, len(payload2)) + payload2

    await _handle_audio_frame(session, frame1)
    await _handle_audio_frame(session, frame2)

    assert len(session.audio_buffer) == 2
    assert session.audio_buffer[0] == payload1
    assert session.audio_buffer[1] == payload2


@pytest.mark.asyncio
async def test_audio_frame_ignored_when_not_recording():
    """非录音状态时音频帧被忽略。"""
    session = _make_session(is_recording=False)

    await _handle_audio_frame(session, b"frame_1")

    assert len(session.audio_buffer) == 0


# ──────────────────────────────────────────────
# Listen Control Message Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_listen_start():
    """listen start 开启录音状态。"""
    session = _make_session()
    session.audio_buffer = [b"old_data"]

    await _handle_listen(session, {"state": "start"})

    assert session.is_recording is True
    assert len(session.audio_buffer) == 0  # 旧数据被清除


@pytest.mark.asyncio
async def test_listen_stop_triggers_asr():
    """listen stop 触发 ASR 处理。"""
    session = _make_session(is_recording=True)
    session.audio_buffer = [b"frame_1", b"frame_2"]

    with patch("pandapal_relay.xiaozhi_bridge.asyncio.create_task") as mock_task:
        await _handle_listen(session, {"state": "stop"})

    assert session.is_recording is False
    assert len(session.audio_buffer) == 0
    mock_task.assert_called_once()  # _process_voice_input 被调度


@pytest.mark.asyncio
async def test_listen_stop_no_audio_no_task():
    """没有音频数据时不触发 ASR。"""
    session = _make_session(is_recording=True)
    # audio_buffer 为空

    with patch("pandapal_relay.xiaozhi_bridge.asyncio.create_task") as mock_task:
        await _handle_listen(session, {"state": "stop"})

    mock_task.assert_not_called()


# ──────────────────────────────────────────────
# Voice Input Processing Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_voice_input_success():
    """语音输入正常流程：Opus 解码 → ASR → STT 显示 → 转发 Agent。"""
    session = _make_session()
    _active_devices[session.device_id] = session

    mock_forward = AsyncMock()

    with patch("pandapal_relay.xiaozhi_bridge._asr_provider") as mock_asr, \
         patch("pandapal_relay.xiaozhi_bridge._forward_to_agent_fn", mock_forward):
        mock_asr.transcribe = AsyncMock(return_value="你好世界")
        await _process_voice_input(session, [b"opus_frame_1", b"opus_frame_2"])

    # 验证 STT 结果发给设备
    session.websocket.send_text.assert_called()
    stt_call = session.websocket.send_text.call_args_list[0]
    stt_msg = json.loads(stt_call[0][0])
    assert stt_msg["type"] == "stt"
    assert stt_msg["text"] == "你好世界"

    # 验证转发给 Agent
    mock_forward.assert_called_once()
    call_kwargs = mock_forward.call_args[1]
    assert call_kwargs["user_id"] == session.device_id
    assert call_kwargs["content"] == "你好世界"
    assert call_kwargs["source_channel_id"] == f"xiaozhi:{session.device_id}"


@pytest.mark.asyncio
async def test_process_voice_input_empty_asr():
    """ASR 返回空文字时不转发。"""
    session = _make_session()

    mock_forward = AsyncMock()

    with patch("pandapal_relay.xiaozhi_bridge._asr_provider") as mock_asr, \
         patch("pandapal_relay.xiaozhi_bridge._forward_to_agent_fn", mock_forward):
        mock_asr.transcribe = AsyncMock(return_value="")
        await _process_voice_input(session, [b"opus_frame"])

    mock_forward.assert_not_called()


# ──────────────────────────────────────────────
# Agent Reply Handler Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_agent_reply_xiaozhi_target():
    """Agent 回复路由到 XiaoZhi 设备。"""
    session = _make_session(device_id="dev123")
    _active_devices["dev123"] = session

    frame = {
        "type": "message",
        "target_channel_ids": ["xiaozhi:dev123"],
        "payload": {"content": "回复内容", "user_id": "dev123"},
    }

    with patch("pandapal_relay.xiaozhi_bridge._send_tts_to_device") as mock_tts:
        # _send_tts_to_device 是 asyncio.create_task 调用的，这里 mock 它
        with patch("pandapal_relay.xiaozhi_bridge.asyncio.create_task") as mock_task:
            result = await handle_agent_reply_for_xiaozhi(frame)

    assert result is True
    mock_task.assert_called_once()


@pytest.mark.asyncio
async def test_handle_agent_reply_origin_claim():
    """渠道策略分发下 target_channel_ids 为空，按 origin 前缀认领（重构核心场景）。"""
    session = _make_session(device_id="dev123")
    _active_devices["dev123"] = session

    frame = {
        "type": "message",
        "origin_channel_id": "xiaozhi:dev123",
        "target_channel_ids": [],
        "payload": {"content": "回复内容", "user_id": "dev123"},
    }

    with patch("pandapal_relay.xiaozhi_bridge._send_tts_to_device"):
        with patch("pandapal_relay.xiaozhi_bridge.asyncio.create_task") as mock_task:
            result = await handle_agent_reply_for_xiaozhi(frame)

    assert result is True
    mock_task.assert_called_once()


@pytest.mark.asyncio
async def test_handle_agent_reply_origin_non_xiaozhi():
    """origin 非 xiaozhi 前缀（如桌面 IPC）且 target 无 xiaozhi → 不认领。"""
    session = _make_session(device_id="dev123")
    _active_devices["dev123"] = session

    frame = {
        "type": "message",
        "origin_channel_id": "__desktop_ipc__",
        "target_channel_ids": [],
        "payload": {"content": "桌面回复", "user_id": "u1"},
    }

    result = await handle_agent_reply_for_xiaozhi(frame)
    assert result is False


@pytest.mark.asyncio
async def test_handle_agent_reply_non_xiaozhi():
    """非 XiaoZhi 目标返回 False。"""
    frame = {
        "type": "message",
        "target_channel_ids": ["wecom:user123"],
        "payload": {"content": "回复", "user_id": "user123"},
    }

    result = await handle_agent_reply_for_xiaozhi(frame)
    assert result is False


@pytest.mark.asyncio
async def test_handle_agent_reply_device_offline():
    """设备离线时返回 False。"""
    frame = {
        "type": "message",
        "target_channel_ids": ["xiaozhi:offline_device"],
        "payload": {"content": "回复", "user_id": "offline_device"},
    }

    result = await handle_agent_reply_for_xiaozhi(frame)
    assert result is False


# ──────────────────────────────────────────────
# TTS Send to Device Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_tts_to_device():
    """TTS 发送完整序列：sentence_start → start → audio → stop。"""
    session = _make_session()

    with patch("pandapal_relay.xiaozhi_bridge._tts_provider") as mock_tts:
        mock_tts.synthesize = AsyncMock(return_value=b"fake_opus_audio")
        await _send_tts_to_device(session, "你好！")

    ws = session.websocket
    calls = ws.send_text.call_args_list

    # 验证消息序列
    assert len(calls) >= 3
    msg1 = json.loads(calls[0][0][0])
    assert msg1["type"] == "tts"
    assert msg1["state"] == "sentence_start"
    assert msg1["text"] == "你好！"

    msg2 = json.loads(calls[1][0][0])
    assert msg2["type"] == "tts"
    assert msg2["state"] == "start"

    msg3 = json.loads(calls[2][0][0])
    assert msg3["type"] == "tts"
    assert msg3["state"] == "stop"

    # 验证音频数据
    ws.send_bytes.assert_called_once_with(b"fake_opus_audio")


@pytest.mark.asyncio
async def test_send_tts_empty_audio():
    """TTS 合成空音频时只发控制消息。"""
    session = _make_session()

    with patch("pandapal_relay.xiaozhi_bridge._tts_provider") as mock_tts:
        mock_tts.synthesize = AsyncMock(return_value=b"")
        await _send_tts_to_device(session, "text")

    ws = session.websocket
    ws.send_bytes.assert_not_called()  # 无音频帧


# ──────────────────────────────────────────────
# JSON Message Dispatch Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_json_listen_message():
    """JSON listen 消息正确分发。"""
    session = _make_session()

    msg = json.dumps({"type": "listen", "state": "start"})
    await _handle_json_message(session, msg)

    assert session.is_recording is True


@pytest.mark.asyncio
async def test_handle_json_goodbye():
    """goodbye 消息触发 WebSocket 关闭。"""
    session = _make_session()

    msg = json.dumps({"type": "goodbye"})
    await _handle_json_message(session, msg)

    session.websocket.close.assert_called_once()


@pytest.mark.asyncio
async def test_handle_json_invalid():
    """无效 JSON 静默忽略。"""
    session = _make_session()

    # 不应抛异常
    await _handle_json_message(session, "not valid json {{{")


# ──────────────────────────────────────────────
# Utility Tests
# ──────────────────────────────────────────────


def test_get_connected_devices_empty():
    """无设备时返回空列表。"""
    assert get_connected_xiaozhi_devices() == []
    assert get_device_count() == 0


def test_get_connected_devices_with_sessions():
    """有设备时返回正确列表。"""
    session1 = _make_session(device_id="dev1")
    session2 = _make_session(device_id="dev2")
    _active_devices["dev1"] = session1
    _active_devices["dev2"] = session2

    devices = get_connected_xiaozhi_devices()
    assert len(devices) == 2
    assert "dev1" in devices
    assert "dev2" in devices
    assert get_device_count() == 2


# ──────────────────────────────────────────────
# Config Tests
# ──────────────────────────────────────────────


def test_relay_config_xiaozhi_defaults():
    """XiaoZhi 配置默认值正确。"""
    config = RelayConfig()
    assert config.xiaozhi_enabled is False
    assert config.xiaozhi_asr_provider == "mock"
    assert config.xiaozhi_asr_api_key == ""
    assert config.xiaozhi_tts_provider == "mock"
    assert config.xiaozhi_tts_api_key == ""


def test_relay_config_from_env_xiaozhi(monkeypatch):
    """XiaoZhi 配置从环境变量读取。"""
    monkeypatch.setenv("XIAOZHI_ENABLED", "true")
    monkeypatch.setenv("XIAOZHI_ASR_PROVIDER", "dashscope")
    monkeypatch.setenv("XIAOZHI_ASR_API_KEY", "sk-asr-test")
    monkeypatch.setenv("XIAOZHI_TTS_PROVIDER", "edge_tts")
    monkeypatch.setenv("XIAOZHI_TTS_API_KEY", "")

    # WeCom 现为可选渠道（未配置 WECOM_CORP_ID 即禁用），无需再提供 wecom 字段
    config = RelayConfig.from_env()
    assert config.xiaozhi_enabled is True
    assert config.xiaozhi_asr_provider == "dashscope"
    assert config.xiaozhi_asr_api_key == "sk-asr-test"
    assert config.xiaozhi_tts_provider == "edge_tts"


def test_relay_config_xiaozhi_enabled_variants(monkeypatch):
    """xiaozhi_enabled 支持多种 truthy 值。"""
    for val in ("true", "True", "1", "yes"):
        monkeypatch.setenv("XIAOZHI_ENABLED", val)
        config = RelayConfig.from_env()
        assert config.xiaozhi_enabled is True, f"Failed for {val}"

    for val in ("false", "0", "no", ""):
        monkeypatch.setenv("XIAOZHI_ENABLED", val)
        config = RelayConfig.from_env()
        assert config.xiaozhi_enabled is False, f"Failed for {val}"


# ──────────────────────────────────────────────
# WeCom 可选渠道 validate() Tests
# ──────────────────────────────────────────────


def test_relay_config_validate_wecom_disabled(monkeypatch):
    """未配置 WECOM_CORP_ID → wecom 禁用，validate 仅要求 AUTH_JWT_SECRET。"""
    for var in ("WECOM_CORP_ID", "WECOM_AGENT_ID", "WECOM_APP_SECRET",
                "WECOM_TOKEN", "WECOM_AES_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AUTH_JWT_SECRET", "secret")

    config = RelayConfig.from_env()
    assert config.wecom_enabled is False
    assert config.validate() == []


def test_relay_config_validate_wecom_enabled_requires_all_fields(monkeypatch):
    """配置 WECOM_CORP_ID 即启用 → 其余 wecom 字段 fail-closed 全部必填。"""
    monkeypatch.setenv("AUTH_JWT_SECRET", "secret")
    monkeypatch.setenv("WECOM_CORP_ID", "corp")
    for var in ("WECOM_AGENT_ID", "WECOM_APP_SECRET", "WECOM_TOKEN", "WECOM_AES_KEY"):
        monkeypatch.delenv(var, raising=False)

    config = RelayConfig.from_env()
    assert config.wecom_enabled is True
    missing = config.validate()
    assert "WECOM_AGENT_ID" in missing
    assert "WECOM_APP_SECRET" in missing
    assert "WECOM_TOKEN" in missing
    assert "WECOM_AES_KEY" in missing


def test_relay_config_validate_wecom_missing_agent_id_only(monkeypatch):
    """★ 回归：仅缺 WECOM_AGENT_ID 也必须 fail-fast（gettoken 自检用不到 agent_id，
    漏校验会导致启动通过后每条消息发送都失败）。"""
    monkeypatch.setenv("AUTH_JWT_SECRET", "secret")
    monkeypatch.setenv("WECOM_CORP_ID", "corp")
    monkeypatch.delenv("WECOM_AGENT_ID", raising=False)
    monkeypatch.setenv("WECOM_APP_SECRET", "secret")
    monkeypatch.setenv("WECOM_TOKEN", "token")
    monkeypatch.setenv("WECOM_AES_KEY", "aeskey")

    assert RelayConfig.from_env().validate() == ["WECOM_AGENT_ID"]


# ──────────────────────────────────────────────
# Init Bridge Tests
# ──────────────────────────────────────────────


def test_init_xiaozhi_bridge():
    """初始化后 providers 正确设置。"""
    import pandapal_relay.xiaozhi_bridge as bridge

    mock_forward = AsyncMock()
    init_xiaozhi_bridge(
        asr_provider_name="mock",
        tts_provider_name="mock",
        forward_to_agent=mock_forward,
    )

    assert isinstance(bridge._asr_provider, MockASRProvider)
    assert isinstance(bridge._tts_provider, MockTTSProvider)
    assert bridge._forward_to_agent_fn is mock_forward


# ──────────────────────────────────────────────
# Server Multi-Channel Routing Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_server_reply_handler_chain():
    """多渠道路由：优先匹配的处理器终止链。"""
    from pandapal_relay import server

    # 保存原状态
    orig_handlers = server._reply_handlers[:]
    orig_fallback = server._fallback_reply_handler

    try:
        server._reply_handlers.clear()
        server._fallback_reply_handler = None

        handler_a = AsyncMock(return_value=False)
        handler_b = AsyncMock(return_value=True)
        handler_c = AsyncMock(return_value=False)

        server.register_reply_handler(handler_a)
        server.register_reply_handler(handler_b)
        server.register_reply_handler(handler_c)

        frame = {"type": "message", "payload": {"content": "test"}}
        await server._handle_agent_message(json.dumps(frame))

        handler_a.assert_called_once()
        handler_b.assert_called_once()
        handler_c.assert_not_called()  # b 已处理，c 不调用

    finally:
        server._reply_handlers = orig_handlers
        server._fallback_reply_handler = orig_fallback


@pytest.mark.asyncio
async def test_server_fallback_handler():
    """所有渠道处理器未处理时调用兜底处理器。"""
    from pandapal_relay import server

    orig_handlers = server._reply_handlers[:]
    orig_fallback = server._fallback_reply_handler

    try:
        server._reply_handlers.clear()
        server._fallback_reply_handler = None

        handler_a = AsyncMock(return_value=False)
        fallback = AsyncMock()

        server.register_reply_handler(handler_a)
        server.register_agent_reply_handler(fallback)

        frame = {"type": "message", "payload": {"content": "test"}}
        await server._handle_agent_message(json.dumps(frame))

        handler_a.assert_called_once()
        fallback.assert_called_once()

    finally:
        server._reply_handlers = orig_handlers
        server._fallback_reply_handler = orig_fallback
