"""XiaoZhi Relay 侧数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


# 支持的协议版本
SUPPORTED_PROTOCOL_VERSIONS = frozenset({3})


class XiaoZhiMessageType(str, Enum):
    """XiaoZhi 协议消息类型。"""

    HELLO = "hello"
    LISTEN = "listen"
    TTS = "tts"
    STT = "stt"
    LLM = "llm"
    MCP = "mcp"
    ABORT = "abort"
    GOODBYE = "goodbye"


@dataclass(frozen=True)
class DeviceCapabilities:
    """设备能力（从 hello 消息解析）。"""

    supports_mcp: bool = False
    audio_format: str = "opus"
    input_sample_rate: int = 16000
    output_sample_rate: int = 24000
    frame_duration_ms: int = 60


@dataclass
class XiaoZhiDeviceSession:
    """设备连接会话（Relay 侧维护）。"""

    device_id: str
    session_id: str
    websocket: Any  # FastAPI WebSocket
    capabilities: DeviceCapabilities
    connected_at: datetime
    last_active_at: datetime
    audio_buffer: list[bytes] = field(default_factory=list)
    is_recording: bool = False
