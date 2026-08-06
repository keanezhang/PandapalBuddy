"""XiaoZhi ASR — 云端语音识别。

将 Opus 音频帧转为文字。当前支持：
- Qwen3 ASR Realtime（阿里云百炼 WebSocket 实时识别）
- DashScope Paraformer（阿里云 HTTP 接口）
- OpenAI Whisper API（兜底）
- Mock（测试用）
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class ASRProvider(Protocol):
    """ASR 提供者接口。"""

    async def transcribe(self, audio_data: bytes, sample_rate: int = 16000) -> str:
        """将音频数据转为文字。"""
        ...


class MockASRProvider:
    """Mock ASR（测试用，直接返回占位文字）。"""

    async def transcribe(self, audio_data: bytes, sample_rate: int = 16000) -> str:
        return f"[ASR mock: {len(audio_data)} bytes audio]"


class QwenRealtimeASRProvider:
    """阿里云 Qwen3-ASR-Flash-Realtime（WebSocket 实时流式识别）。

    使用 DashScope Realtime WebSocket API（官方文档 2026-05）：
    - 端点: wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=<model>
    - 模型: qwen3-asr-flash-realtime
    - 认证: Authorization: Bearer <api_key> + OpenAI-Beta: realtime=v1
    - 音频格式: PCM 16kHz 16bit mono（从设备收到的 Opus 需先解码）
    - 消息字段: "type"（非 "event"）

    工作流程（Manual 模式，客户端控制断句）：
    1. 建立 WebSocket 连接（URL 带 model 参数，Header 带 OpenAI-Beta）
    2. 等待 session.created
    3. 发送 session.update 配置（turn_detection=null 为 Manual 模式）
    4. 循环发送 input_audio_buffer.append（Base64 编码的 PCM 数据，每块 ~3200B）
    5. 发送 input_audio_buffer.commit 触发识别
    6. 接收 conversation.item.input_audio_transcription.completed 事件
    7. 发送 session.finish 结束
    """

    def __init__(
        self,
        api_key: str,
        model: str = "qwen3-asr-flash-realtime",
        language: str = "zh",
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._language = language
        # 官方文档：endpoint 为 /api-ws/v1/realtime，model 通过 URL query 传递
        self._ws_url = f"wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model={model}"

    async def transcribe(self, audio_data: bytes, sample_rate: int = 16000) -> str:
        """通过 WebSocket Realtime API 进行语音识别。

        Args:
            audio_data: PCM 格式的音频数据（16kHz 16bit mono）。
            sample_rate: 采样率（默认 16000）。

        Returns:
            识别出的文字；失败时返回空字符串。
        """
        try:
            import websockets
        except ImportError:
            logger.error("websockets not installed, cannot use QwenRealtimeASRProvider")
            return ""

        if not audio_data or len(audio_data) < 100:
            logger.debug("Audio data too short (%d bytes), skip ASR", len(audio_data))
            return ""

        try:
            # 连接 WebSocket（必须带 OpenAI-Beta header）
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "OpenAI-Beta": "realtime=v1",
            }

            async with websockets.connect(
                self._ws_url,
                additional_headers=headers,
                close_timeout=10,
            ) as ws:
                # 等待服务端初始 session.created 事件
                await self._wait_for_event(ws, "session.created", timeout=5.0)
                logger.debug("ASR: session.created received")

                # Step 1: 发送 session.update — Manual 模式（turn_detection=null）
                session_update = {
                    "type": "session.update",
                    "session": {
                        "modalities": ["text"],
                        "input_audio_format": "pcm",
                        "sample_rate": sample_rate,
                        "input_audio_transcription": {
                            "language": self._language,
                        },
                        "turn_detection": None,  # Manual 模式：客户端手动 commit
                    },
                }
                await ws.send(json.dumps(session_update))
                logger.debug("ASR: session.update sent")

                # 等待 session.updated 确认
                await self._wait_for_event(ws, "session.updated", timeout=5.0)
                logger.debug("ASR: session.updated received")

                # Step 2: 分块发送音频数据（每块约 3200 bytes PCM = 0.1s）
                # Base64 编码后每块约 4267 字符
                raw_chunk_size = 3200  # PCM bytes per chunk
                for i in range(0, len(audio_data), raw_chunk_size):
                    chunk = audio_data[i:i + raw_chunk_size]
                    chunk_b64 = base64.b64encode(chunk).decode("ascii")
                    append_msg = {
                        "type": "input_audio_buffer.append",
                        "audio": chunk_b64,
                    }
                    await ws.send(json.dumps(append_msg))

                logger.debug("ASR: sent %d audio chunks", (len(audio_data) + raw_chunk_size - 1) // raw_chunk_size)

                # Step 3: 提交音频，触发识别（Manual 模式必须）
                commit_msg = {"type": "input_audio_buffer.commit"}
                await ws.send(json.dumps(commit_msg))
                logger.debug("ASR: commit sent")

                # Step 4: 等待识别结果
                text = await self._wait_for_transcription(ws, timeout=15.0)

                # Step 5: 结束会话
                finish_msg = {"type": "session.finish"}
                await ws.send(json.dumps(finish_msg))

                return text

        except asyncio.TimeoutError:
            logger.error("Qwen ASR timeout")
            return ""
        except Exception as e:
            logger.error("Qwen Realtime ASR error: %s", e)
            return ""

    async def _wait_for_event(self, ws, event_type: str, timeout: float = 5.0) -> dict:
        """等待指定类型的服务端事件。"""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError(f"Waiting for {event_type}")
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            event = json.loads(raw)
            ev_type = event.get("type", "")
            logger.debug("ASR event: %s", ev_type)
            if ev_type == event_type:
                return event
            if ev_type == "error":
                raise RuntimeError(f"ASR server error: {event}")
            # 继续等待

    async def _wait_for_transcription(self, ws, timeout: float = 15.0) -> str:
        """等待转录完成事件，收集文字结果。"""
        deadline = asyncio.get_event_loop().time() + timeout
        text_parts: list[str] = []

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break

            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break

            event = json.loads(raw)
            event_type = event.get("type", "")
            logger.debug("ASR transcription event: %s", event_type)

            # 最终识别结果
            if event_type == "conversation.item.input_audio_transcription.completed":
                transcript = event.get("transcript", "")
                if transcript:
                    text_parts.append(transcript)
                break

            # 中间识别结果（流式文本）
            elif event_type == "conversation.item.input_audio_transcription.text":
                text = event.get("text", "")
                # text 事件是中间结果，不累加（completed 才是最终的）
                logger.debug("ASR intermediate: %s", text)

            # 错误
            elif event_type == "error":
                logger.error("ASR error event: %s", event)
                break

            # input_audio_buffer.committed / speech_started / speech_stopped 等跳过
            elif event_type == "session.finished":
                break

        return "".join(text_parts)


class DashScopeASRProvider:
    """阿里云 DashScope Paraformer ASR（HTTP 接口）。"""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def transcribe(self, audio_data: bytes, sample_rate: int = 16000) -> str:
        """调用 DashScope ASR API。"""
        try:
            import httpx

            # DashScope Paraformer 实时语音识别 API
            url = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription"
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/octet-stream",
                "X-DashScope-AudioFormat": "opus",
                "X-DashScope-SampleRate": str(sample_rate),
            }

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, content=audio_data, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("output", {}).get("text", "")
                else:
                    logger.error("DashScope ASR failed: %d %s", resp.status_code, resp.text[:200])
                    return ""
        except Exception as e:
            logger.error("ASR transcribe error: %s", e)
            return ""


class WhisperASRProvider:
    """OpenAI Whisper API ASR。"""

    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1") -> None:
        self._api_key = api_key
        self._base_url = base_url

    async def transcribe(self, audio_data: bytes, sample_rate: int = 16000) -> str:
        """调用 Whisper API。"""
        try:
            import httpx

            url = f"{self._base_url}/audio/transcriptions"
            headers = {"Authorization": f"Bearer {self._api_key}"}

            # Whisper API 需要文件格式
            files = {"file": ("audio.opus", audio_data, "audio/opus")}
            data = {"model": "whisper-1", "language": "zh"}

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, headers=headers, files=files, data=data)
                if resp.status_code == 200:
                    return resp.json().get("text", "")
                else:
                    logger.error("Whisper ASR failed: %d", resp.status_code)
                    return ""
        except Exception as e:
            logger.error("Whisper ASR error: %s", e)
            return ""


def create_asr_provider(provider: str, api_key: str = "") -> ASRProvider:
    """工厂方法：根据配置创建 ASR Provider。

    provider 支持：
    - "qwen_realtime" 或以 "qwen3-asr" 开头的模型名
    - "dashscope"
    - "whisper"
    - 其他 → MockASRProvider
    """
    if (provider == "qwen_realtime" or provider.startswith("qwen3-asr")) and api_key:
        model = provider if provider.startswith("qwen3-asr") else "qwen3-asr-flash-realtime"
        return QwenRealtimeASRProvider(api_key, model=model)
    elif provider == "dashscope" and api_key:
        return DashScopeASRProvider(api_key)
    elif provider == "whisper" and api_key:
        return WhisperASRProvider(api_key)
    else:
        logger.warning("Using MockASRProvider (no valid ASR config)")
        return MockASRProvider()

