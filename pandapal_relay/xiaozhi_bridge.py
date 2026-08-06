"""XiaoZhi Bridge — Relay 侧 XiaoZhi 设备接入。

架构定位：与 WeCom Bridge 对称设计。
- WeCom Bridge: HTTP 回调 → 解密 → 文字 → forward_to_agent()
- XiaoZhi Bridge: WebSocket → 音频帧 → ASR → 文字 → forward_to_agent()

设备连接后的消息流：
  设备 → Relay /xiaozhi/ws → ASR → 统一帧格式 → Agent
  Agent → 回复文字 → TTS → Opus 帧 → 设备
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .xiaozhi.asr import ASRProvider, MockASRProvider, create_asr_provider
from .xiaozhi.models import (
    SUPPORTED_PROTOCOL_VERSIONS,
    DeviceCapabilities,
    XiaoZhiDeviceSession,
    XiaoZhiMessageType,
)
from .xiaozhi.tts import TTSProvider, MockTTSProvider, create_tts_provider

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Module-level state
# ──────────────────────────────────────────────

_active_devices: dict[str, XiaoZhiDeviceSession] = {}
_asr_provider: ASRProvider = MockASRProvider()
_tts_provider: TTSProvider = MockTTSProvider()
_forward_to_agent_fn: Any = None  # 由 init 时注入

# FastAPI Router
xiaozhi_router = APIRouter(tags=["xiaozhi"])


# ──────────────────────────────────────────────
# Initialization
# ──────────────────────────────────────────────


def init_xiaozhi_bridge(
    asr_provider_name: str = "mock",
    asr_api_key: str = "",
    tts_provider_name: str = "mock",
    tts_api_key: str = "",
    forward_to_agent: Any = None,
) -> None:
    """初始化 XiaoZhi Bridge（由 run_relay.py 调用）。"""
    global _asr_provider, _tts_provider, _forward_to_agent_fn

    _asr_provider = create_asr_provider(asr_provider_name, asr_api_key)
    _tts_provider = create_tts_provider(tts_provider_name, tts_api_key)
    _forward_to_agent_fn = forward_to_agent

    logger.info(
        "XiaoZhi Bridge initialized (asr=%s, tts=%s)",
        asr_provider_name, tts_provider_name,
    )


# ──────────────────────────────────────────────
# WebSocket Endpoint
# ──────────────────────────────────────────────


@xiaozhi_router.websocket("/xiaozhi/ws")
async def xiaozhi_ws_endpoint(websocket: WebSocket) -> None:
    """XiaoZhi 设备 WebSocket 连接入口。"""
    await websocket.accept()

    device_id: str | None = None

    try:
        # Step 1: 等待 hello 握手
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
        hello_payload = json.loads(raw)

        if hello_payload.get("type") != XiaoZhiMessageType.HELLO:
            await websocket.close(code=4001, reason="Expected hello message")
            return

        # Step 2: 验证协议版本
        version = hello_payload.get("version", 0)
        if version not in SUPPORTED_PROTOCOL_VERSIONS:
            await websocket.send_text(json.dumps({
                "type": "hello", "error": "unsupported_version",
            }))
            await websocket.close(code=4002, reason="Unsupported version")
            return

        # Step 3: 创建会话
        device_id = str(uuid.uuid4())[:12]
        session_id = f"xiaozhi-{device_id}"
        now = datetime.now(timezone.utc)

        features = hello_payload.get("features", {})
        audio_params = hello_payload.get("audio_params", {})

        session = XiaoZhiDeviceSession(
            device_id=device_id,
            session_id=session_id,
            websocket=websocket,
            capabilities=DeviceCapabilities(
                supports_mcp=features.get("mcp", False),
                audio_format=audio_params.get("format", "opus"),
                input_sample_rate=audio_params.get("sample_rate", 16000),
                output_sample_rate=24000,
                frame_duration_ms=audio_params.get("frame_duration", 60),
            ),
            connected_at=now,
            last_active_at=now,
        )
        _active_devices[device_id] = session

        # Step 4: 回复 hello
        await websocket.send_text(json.dumps({
            "type": "hello",
            "transport": "websocket",
            "session_id": session_id,
            "audio_params": {"sample_rate": 24000, "frame_duration": 60},
        }))

        logger.info("XiaoZhi device connected: %s", device_id)

        # Step 5: 消息接收循环
        while True:
            message = await websocket.receive()

            if message.get("type") == "websocket.disconnect":
                break

            # 二进制 = 音频帧
            if "bytes" in message and message["bytes"]:
                await _handle_audio_frame(session, message["bytes"])
            # 文本 = JSON 控制消息
            elif "text" in message and message["text"]:
                await _handle_json_message(session, message["text"])

    except WebSocketDisconnect:
        pass
    except asyncio.TimeoutError:
        logger.warning("XiaoZhi hello timeout")
    except Exception as e:
        logger.error("XiaoZhi connection error: %s", e)
    finally:
        if device_id:
            _active_devices.pop(device_id, None)
            logger.info("XiaoZhi device disconnected: %s", device_id)


# ──────────────────────────────────────────────
# Agent Reply Handler (注册到 relay server)
# ──────────────────────────────────────────────


async def handle_agent_reply_for_xiaozhi(frame: dict[str, Any]) -> bool:
    """处理 Agent 回复（如果目标是 XiaoZhi 设备）。

    仅对语音相关的回复类型进行 TTS 合成：
    - LLM_TOKEN / REASONING_TOKEN / REPLY_END — 流式文本内容
    - AGENT_REPLY — 非流式完整回复
    - APPROVAL_RESULT — 审批结果，可读

    以下类型不触发 TTS（即使内容非空）：
    - ERROR / TOOL_START / TOOL_END — 不应以语音播报
    - HITL_REQUEST / INTERACTION_REQUEST — 审批和问卷不应语音播报
    - PLAN_APPROVAL_REQUEST / PERMISSION_DENIED / AGENT_HALTED — 系统级消息
    - TASK_NOTIFICATION / QUICK_APP_DATA — 数据推送不应语音播报

    历史问题（D6 修复）：原实现只判断 `if session and content`，
    未按 event_type 过滤，导致 ERROR/TOOL_END 等非语音事件也被 TTS 播报。

    Returns:
        True if handled (target is XiaoZhi device and content is speech-relevant),
        False otherwise.
    """
    # 只处理语音相关的事件类型
    event_type = frame.get("event_type", "")
    if event_type not in _XIAOZHI_SPEECH_EVENT_TYPES:
        return False

    target_channels = frame.get("target_channel_ids", [])
    payload = frame.get("payload", {})
    content = payload.get("content", "")

    # ★ 认领依据（2026-06 渠道策略重构）：
    #   主依据 = origin_channel_id 前缀 "xiaozhi:"（pandapal 渠道策略分发下，
    #     出站帧的 target_channel_ids 仅 R0 指名时才有，正常路径为空——
    #     xiaozhi 发起的 run 回复帧 origin 必为 "xiaozhi:{device_id}"）。
    #   兼容 = 旧帧 target_channel_ids 中带 "xiaozhi:" 前缀的渠道。
    origin = frame.get("origin_channel_id") or payload.get("origin_channel_id") or ""
    candidate_ids: list[str] = []
    if isinstance(origin, str) and origin.startswith("xiaozhi:"):
        candidate_ids.append(origin)
    candidate_ids.extend(
        cid for cid in target_channels
        if isinstance(cid, str) and cid.startswith("xiaozhi:")
    )

    for channel_id in candidate_ids:
        device_id = channel_id.replace("xiaozhi:", "", 1)
        session = _active_devices.get(device_id)
        if session and content:
            asyncio.create_task(_send_tts_to_device(session, content))
            return True

    return False


# XiaoZhi TTS 适用的事件类型（按 event_type 过滤）
_XIAOZHI_SPEECH_EVENT_TYPES = frozenset([
    "llm_token",           # 流式文本 token
    "llm_reasoning_token", # 流式推理 token
    "reply_end",           # 流式结束
    "agent_reply",         # 非流式完整回复
    "approval_result",     # 审批结果
])


# ──────────────────────────────────────────────
# Internal Handlers
# ──────────────────────────────────────────────


async def _handle_audio_frame(session: XiaoZhiDeviceSession, data: bytes) -> None:
    """接收音频帧并缓冲。

    设备发送的二进制帧格式（BinaryProtocol3）：
    - byte 0: type (0=OPUS, 1=JSON)
    - byte 1: reserved
    - byte 2-3: payload_size (uint16 little-endian)
    - byte 4+: payload (Opus 编码的音频数据)
    """
    session.last_active_at = datetime.now(timezone.utc)

    if session.is_recording:
        # 解析 BinaryProtocol3 头部，提取 Opus payload
        if len(data) > 4:
            frame_type = data[0]
            if frame_type == 0:  # OPUS audio
                opus_payload = data[4:]
                session.audio_buffer.append(opus_payload)
        else:
            # 无头部的裸数据（兼容）
            session.audio_buffer.append(data)


async def _handle_json_message(session: XiaoZhiDeviceSession, raw: str) -> None:
    """处理 JSON 控制消息。"""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return

    session.last_active_at = datetime.now(timezone.utc)
    msg_type = payload.get("type", "")

    if msg_type == XiaoZhiMessageType.LISTEN:
        await _handle_listen(session, payload)
    elif msg_type == XiaoZhiMessageType.ABORT:
        logger.debug("TTS abort from device: %s", session.device_id)
    elif msg_type == XiaoZhiMessageType.GOODBYE:
        try:
            await session.websocket.close()
        except Exception:
            pass


async def _handle_listen(session: XiaoZhiDeviceSession, payload: dict) -> None:
    """处理录音控制消息。"""
    state = payload.get("state", "")

    if state == "start":
        session.is_recording = True
        session.audio_buffer.clear()
        logger.debug("Recording started: %s", session.device_id)

    elif state == "stop":
        session.is_recording = False
        logger.debug("Recording stopped: %s", session.device_id)

        # 有音频数据 → Opus 解码 → ASR → 转发给 Agent
        if session.audio_buffer:
            opus_frames = list(session.audio_buffer)
            session.audio_buffer.clear()
            asyncio.create_task(_process_voice_input(session, opus_frames))

    elif state == "detect":
        logger.debug("Wake word detected: %s", session.device_id)


def _decode_opus_frames(audio_data: bytes, sample_rate: int = 16000) -> bytes:
    """将多个 Opus 帧拼接后解码为 PCM 16bit。

    注意：audio_data 是由 b"".join(session.audio_buffer) 生成的，
    但 session.audio_buffer 中每个元素是一个独立的 Opus packet。
    这个函数接收的实际上已经被 join 了，无法区分帧边界。

    正确做法：在调用处传入 frames list，逐帧解码。
    这里作为兜底，直接将原始数据传给 ASR（Qwen ASR 支持 Opus 输入）。

    Args:
        audio_data: Opus 编码数据。
        sample_rate: 目标采样率。

    Returns:
        PCM bytes 或原始 Opus bytes（如无解码器）。
    """
    # Qwen Realtime ASR 接收 PCM，但如果无法解码 Opus 则传原始数据
    # 真正的解码需要逐帧处理（见 _decode_opus_frame_list）
    return audio_data


def _decode_opus_frame_list(
    frames: list[bytes], sample_rate: int = 16000, frame_duration_ms: int = 60
) -> bytes:
    """逐帧解码 Opus → PCM 16bit little-endian。

    Args:
        frames: Opus 帧列表（每帧是独立的 Opus packet）。
        sample_rate: 采样率（16000）。
        frame_duration_ms: 每帧时长（60ms）。

    Returns:
        拼接后的 PCM 16bit bytes。
    """
    try:
        import opuslib
        decoder = opuslib.Decoder(sample_rate, 1)  # mono
        frame_size = int(sample_rate * frame_duration_ms / 1000)  # 960 samples per 60ms

        pcm_chunks: list[bytes] = []
        for frame in frames:
            try:
                pcm = decoder.decode(frame, frame_size)
                pcm_chunks.append(pcm)
            except Exception as e:
                logger.debug("Skip bad opus frame: %s", e)

        return b"".join(pcm_chunks)

    except ImportError:
        logger.debug("opuslib not installed, returning concatenated raw frames")
        return b"".join(frames)
    except Exception as e:
        logger.warning("Opus decode list error: %s", e)
        return b"".join(frames)


async def _process_voice_input(
    session: XiaoZhiDeviceSession, opus_frames: list[bytes]
) -> None:
    """Opus 帧列表 → PCM 解码 → ASR → 转发给 Agent（异步处理）。

    设备发送的是 Opus 编码帧（16kHz, mono, 60ms/frame）。
    ASR 需要 PCM 16bit 16kHz mono 数据。
    """
    # Step 0: Opus 逐帧解码 → PCM
    pcm_data = _decode_opus_frame_list(
        opus_frames,
        sample_rate=session.capabilities.input_sample_rate,
        frame_duration_ms=session.capabilities.frame_duration_ms,
    )

    if not pcm_data:
        logger.warning("Opus decode empty for device %s", session.device_id)
        return

    # Step 1: ASR 语音识别（输入 PCM 数据）
    text = await _asr_provider.transcribe(pcm_data, session.capabilities.input_sample_rate)

    if not text:
        logger.warning("ASR returned empty text for device %s", session.device_id)
        return

    # Step 2: 向设备发送 STT 识别结果（显示在屏幕）
    try:
        await session.websocket.send_text(json.dumps({
            "type": "stt", "text": text,
        }))
    except Exception:
        pass

    # Step 3: 转发给 Agent（统一帧格式，与 WeCom 一致）
    if _forward_to_agent_fn:
        msg_id = str(uuid.uuid4())
        source_channel = f"xiaozhi:{session.device_id}"
        await _forward_to_agent_fn(
            user_id=session.device_id,
            content=text,
            msg_id=msg_id,
            source_channel_id=source_channel,
            # 发起方 mint 于连接建立时（f"xiaozhi-{device_id}"），此处只透传
            session_id=session.session_id,
        )
        logger.info(
            "Voice forwarded to agent: device=%s, text=%s",
            session.device_id, text[:50],
        )


async def _send_tts_to_device(
    session: XiaoZhiDeviceSession, text: str
) -> None:
    """Agent 回复 → TTS → 推送到设备。"""
    try:
        ws = session.websocket

        # Step 1: 发送文字显示
        await ws.send_text(json.dumps({
            "type": "tts", "state": "sentence_start", "text": text,
        }))

        # Step 2: TTS 合成
        audio_data = await _tts_provider.synthesize(text)

        # Step 3: 发送音频
        await ws.send_text(json.dumps({"type": "tts", "state": "start"}))

        if audio_data:
            # 发送音频帧（简化：整块发送）
            await ws.send_bytes(audio_data)

        await ws.send_text(json.dumps({"type": "tts", "state": "stop"}))

    except Exception as e:
        logger.error("TTS send failed: device=%s, error=%s", session.device_id, e)


# ──────────────────────────────────────────────
# Utility
# ──────────────────────────────────────────────


def get_connected_xiaozhi_devices() -> list[str]:
    """获取已连接的 XiaoZhi 设备 ID 列表。"""
    return list(_active_devices.keys())


def get_device_count() -> int:
    """获取连接设备数。"""
    return len(_active_devices)
