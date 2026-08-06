"""XiaoZhi TTS — 云端语音合成。

将文字转为音频推送给设备。当前支持：
- Qwen3 TTS Realtime（阿里云百炼 WebSocket 实时合成，输出 Opus/PCM）
- Edge-TTS（微软免费 TTS，无需 API Key）
- DashScope CosyVoice（阿里云 HTTP）
- Mock（测试用）
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import AsyncIterator, Protocol

logger = logging.getLogger(__name__)


class TTSProvider(Protocol):
    """TTS 提供者接口。"""

    async def synthesize(self, text: str) -> bytes:
        """将文字合成为音频数据（PCM 或 Opus 完整数据）。"""
        ...


class TTSStreamProvider(Protocol):
    """流式 TTS 提供者接口（逐帧返回 Opus 数据）。"""

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        """将文字合成为音频，流式逐块返回。"""
        ...


class MockTTSProvider:
    """Mock TTS（测试用，返回空音频）。"""

    async def synthesize(self, text: str) -> bytes:
        return b""  # 空音频，设备只显示文字

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        """Mock 流式合成，不产生任何音频。"""
        # 空的 async generator
        if False:
            yield b""  # type: ignore  # make this an async generator


class QwenRealtimeTTSProvider:
    """阿里云 Qwen3-TTS-Flash-Realtime（WebSocket 实时流式合成）。

    使用 DashScope Realtime WebSocket API（官方文档 2026-05）：
    - 端点: wss://dashscope.aliyuncs.com/api-ws/v1/realtime（model 通过构造函数配置）
    - 认证: Authorization: Bearer <api_key>
    - 模式: commit（客户端手动提交触发合成，适合对话场景）
    - 输出格式: opus（直接推给小智设备，无需再编码）

    协议流程（commit 模式）：
    1. 建立 WebSocket 连接
    2. 等待 session.created
    3. 发送 session.update 配置（voice, response_format, mode 等）
    4. 等待 session.updated
    5. 发送 input_text_buffer.append（文本）
    6. 发送 input_text_buffer.commit 触发合成
    7. 接收 response.audio.delta（base64 编码的音频增量）
    8. 接收 response.done（当前句子合成完毕）
    9. 发送 session.finish 结束
    """

    def __init__(
        self,
        api_key: str,
        model: str = "qwen3-tts-flash-realtime",
        voice: str = "Cherry",
        sample_rate: int = 24000,
        output_format: str = "pcm",
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._sample_rate = sample_rate
        self._output_format = output_format
        # model 必须放在 URL query 参数中（和 ASR 一致）
        self._ws_url = f"wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model={model}"

    async def synthesize(self, text: str) -> bytes:
        """一次性合成：将全部文本合成后返回完整音频数据。

        Args:
            text: 待合成的文本。

        Returns:
            音频数据（Opus 或 PCM 格式，取决于 output_format 配置）。
        """
        if not text.strip():
            return b""

        chunks: list[bytes] = []
        async for chunk in self.synthesize_stream(text):
            chunks.append(chunk)
        return b"".join(chunks)

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        """流式合成：逐块 yield 音频数据。

        每个 yield 的 chunk 是一段 Opus/PCM 音频数据，可直接推送给设备。

        Args:
            text: 待合成的文本。

        Yields:
            音频数据块。
        """
        if not text.strip():
            return

        try:
            import websockets
        except ImportError:
            logger.error("websockets not installed, cannot use QwenRealtimeTTSProvider")
            return

        try:
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "OpenAI-Beta": "realtime=v1",
            }

            async with websockets.connect(
                self._ws_url,
                additional_headers=headers,
                close_timeout=10,
            ) as ws:
                # Step 1: 等待 session.created
                await self._wait_for_event(ws, "session.created", timeout=5.0)
                logger.debug("TTS: session.created received")

                # Step 2: 发送 session.update
                session_update = {
                    "type": "session.update",
                    "event_id": f"event_{int(time.time() * 1000)}",
                    "session": {
                        "mode": "commit",
                        "voice": self._voice,
                        "response_format": self._output_format,
                        "sample_rate": self._sample_rate,
                    },
                }
                await ws.send(json.dumps(session_update))
                logger.debug("TTS: session.update sent")

                # 等待 session.updated
                await self._wait_for_event(ws, "session.updated", timeout=5.0)
                logger.debug("TTS: session.updated received")

                # Step 3: 发送文本
                append_msg = {
                    "type": "input_text_buffer.append",
                    "event_id": f"event_{int(time.time() * 1000)}",
                    "text": text,
                }
                await ws.send(json.dumps(append_msg))
                logger.debug("TTS: text appended (%d chars)", len(text))

                # Step 4: commit 触发合成
                commit_msg = {
                    "type": "input_text_buffer.commit",
                    "event_id": f"event_{int(time.time() * 1000)}",
                }
                await ws.send(json.dumps(commit_msg))
                logger.debug("TTS: commit sent")

                # Step 5: 接收音频流
                async for chunk in self._receive_audio_stream(ws, timeout=30.0):
                    yield chunk

                # Step 6: 结束会话
                finish_msg = {
                    "type": "session.finish",
                    "event_id": f"event_{int(time.time() * 1000)}",
                }
                await ws.send(json.dumps(finish_msg))
                logger.debug("TTS: session.finish sent")

        except asyncio.TimeoutError:
            logger.error("Qwen TTS timeout for text: %s", text[:50])
        except Exception as e:
            logger.error("Qwen Realtime TTS error: %s", e)

    async def _wait_for_event(self, ws, event_type: str, timeout: float = 5.0) -> dict:
        """等待指定类型的服务端事件（跳过其他事件）。"""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError(f"TTS: Waiting for {event_type}")
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            if isinstance(raw, bytes):
                continue  # 不应该出现二进制帧，但安全跳过
            event = json.loads(raw)
            ev_type = event.get("type", "")
            logger.debug("TTS event (waiting for %s): got %s", event_type, ev_type)
            if ev_type == event_type:
                return event
            if ev_type == "error":
                raise RuntimeError(f"TTS server error: {event}")

    async def _receive_audio_stream(self, ws, timeout: float = 30.0) -> AsyncIterator[bytes]:
        """接收音频增量数据，直到 response.done。

        官方协议：音频通过 response.audio.delta 事件的 delta 字段（base64）传输。
        """
        deadline = asyncio.get_event_loop().time() + timeout

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                logger.warning("TTS: audio receive timeout")
                break

            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                logger.warning("TTS: recv timeout while waiting for audio")
                break

            if isinstance(raw, bytes):
                # 不应该出现，但以防万一也作为音频处理
                yield raw
                continue

            event = json.loads(raw)
            event_type = event.get("type", "")

            if event_type == "response.audio.delta":
                # 音频增量：base64 解码
                delta_b64 = event.get("delta", "")
                if delta_b64:
                    audio_bytes = base64.b64decode(delta_b64)
                    yield audio_bytes

            elif event_type == "response.audio.done":
                # 音频生成完成
                logger.debug("TTS: response.audio.done")

            elif event_type == "response.done":
                # 当前响应完成
                logger.debug("TTS: response.done")
                break

            elif event_type == "session.finished":
                logger.debug("TTS: session.finished")
                break

            elif event_type == "error":
                logger.error("TTS error event: %s", event)
                break

            # 其他事件（response.created, response.output_item.added 等）跳过


class EdgeTTSProvider:
    """微软 Edge-TTS（免费，无需 API Key）。输出 MP3 格式。"""

    def __init__(self, voice: str = "zh-CN-XiaoxiaoNeural") -> None:
        self._voice = voice

    async def synthesize(self, text: str) -> bytes:
        """使用 edge-tts 合成语音。"""
        try:
            import edge_tts
            import io

            communicate = edge_tts.Communicate(text, self._voice)
            audio_data = io.BytesIO()

            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_data.write(chunk["data"])

            return audio_data.getvalue()

        except ImportError:
            logger.warning("edge-tts not installed, returning empty audio")
            return b""
        except Exception as e:
            logger.error("Edge-TTS error: %s", e)
            return b""


class DashScopeTTSProvider:
    """阿里云 DashScope CosyVoice TTS（HTTP）。"""

    def __init__(self, api_key: str, voice: str = "longxiaochun") -> None:
        self._api_key = api_key
        self._voice = voice

    async def synthesize(self, text: str) -> bytes:
        """调用 DashScope TTS API。"""
        try:
            import httpx

            url = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/synthesis"
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": "cosyvoice-v1",
                "input": {"text": text},
                "parameters": {"voice": self._voice, "format": "mp3"},
            }

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 200:
                    return resp.content
                else:
                    logger.error("DashScope TTS failed: %d", resp.status_code)
                    return b""
        except Exception as e:
            logger.error("DashScope TTS error: %s", e)
            return b""


def create_tts_provider(provider: str, api_key: str = "") -> QwenRealtimeTTSProvider | EdgeTTSProvider | DashScopeTTSProvider | MockTTSProvider:
    """工厂方法：根据配置创建 TTS Provider。

    provider 支持：
    - "qwen_realtime" 或以 "qwen3-tts" 开头的模型名
    - "edge_tts"
    - "dashscope"
    - 其他 → MockTTSProvider
    """
    if (provider == "qwen_realtime" or provider.startswith("qwen3-tts")) and api_key:
        model = provider if provider.startswith("qwen3-tts") else "qwen3-tts-flash-realtime"
        return QwenRealtimeTTSProvider(api_key, model=model)
    elif provider == "edge_tts":
        return EdgeTTSProvider()
    elif provider == "dashscope" and api_key:
        return DashScopeTTSProvider(api_key)
    else:
        logger.warning("Using MockTTSProvider (no valid TTS config)")
        return MockTTSProvider()
