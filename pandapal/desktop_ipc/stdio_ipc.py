"""pandapal.desktop_ipc.stdio_ipc — IPC 传输壳（入站归一化改造后瘦身为纯 gate）。

★ 入站归一化改造：
  之前：gate 兼任「连接管理 + 消息分类 + 业务分发」——190 行 if-else 直通分派 +
        15 个 setter/_on_* + _build_inbound_message（方言翻译）。
  之后：gate 只负责连接管理（stdin 读行 / JSON 解析 / 大小上限 / PING 心跳），
        业务消息一律交给 InboundPipeline.handle()：
          - 方言翻译 → IpcInboundAdapter（inbound_adapter.py）
          - 分类分发 → InboundDispatcher（dispatch/dispatcher.py）

设计约束：
- BL1: 唯一职责是"stdin JSON 解析 → pipeline.handle" + "启动 IpcStdoutTransport" + PING 自答
- O3: 所有异常内部消化，O3 Never Throw
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from typing import Any

from pandapal.desktop_ipc.ipc_transport import IpcStdoutTransport
from pandapal.desktop_ipc.message_codec import IpcMessageType
from pandapal.dispatch.pipeline import InboundPipeline

from pandapal.degradation import DegradationEvent, report_degradation

logger = logging.getLogger(__name__)


class StdioIpcServer:
    """IPC 传输壳（纯 gate：连接管理，不做业务分发）。

    唯一职责：
    1. 启动 IpcStdoutTransport（出站由 Transport 负责）
    2. 从 stdin 读 JSON Lines → 大小/JSON/dict 检查 → PING 自答 → pipeline.handle(data)
    """

    def __init__(
        self,
        transport: IpcStdoutTransport | None = None,
        channel_id: str = "__desktop_ipc__",
        user_id: str = "",
        max_payload_bytes: int = 1 * 1024 * 1024,
        shutdown_event: asyncio.Event | None = None,
    ) -> None:
        self._transport = transport or IpcStdoutTransport()
        self._channel_id = channel_id
        self._user_id = user_id
        self._max_payload_bytes = max_payload_bytes
        # 5.2: stdin EOF 时 set 此 event（外部 await 触发进程退出）
        self._shutdown_event = shutdown_event
        self._pipeline: InboundPipeline | None = None
        self._running = False
        self._stdin_task: asyncio.Task | None = None

    def set_inbound_pipeline(self, pipeline: InboundPipeline) -> None:
        """注入入站管道（adapter + dispatcher 绑定胶水，组合根在 app.py）。"""
        if pipeline is None:
            raise ValueError("pipeline cannot be None")
        self._pipeline = pipeline

    async def start(self) -> None:
        """启动 IPC server。"""
        if self._running:
            return
        await self._transport.start()
        self._running = True
        self._stdin_task = asyncio.create_task(self._stdin_loop())
        logger.info("StdioIpcServer started (channel=%s, user=%s)",
                    self._channel_id, self._user_id)

    async def stop(self) -> None:
        """停止 IPC server。"""
        self._running = False
        if self._stdin_task:
            self._stdin_task.cancel()
            try:
                await self._stdin_task
            except asyncio.CancelledError:
                pass
            self._stdin_task = None
        await self._transport.stop()
        logger.info("StdioIpcServer stopped")

    @property
    def transport(self) -> IpcStdoutTransport:
        return self._transport

    # ── stdin 循环 ─────────────────────────────────────────────
    async def _stdin_loop(self) -> None:
        """从 stdin 读 JSON Lines 循环。"""
        # Windows: 确保 stdin 使用 UTF-8 编码读取。
        # Tauri sidecar 通过管道写入的是 UTF-8 字节流，
        # 但 Python 在 Windows 上默认使用系统 ANSI 代码页（如 CP936），
        # 会导致中文被错误解码，产生孤立的 surrogate 字符。
        if sys.platform == "win32" and hasattr(sys.stdin, "reconfigure"):
            try:
                sys.stdin.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
            except Exception:
                # reconfigure 失败会导致中文乱码，best-effort 但绝不静默（O2）。
                report_degradation(
                    DegradationEvent.STDIN_RECONFIGURE_FAILED,
                    category="exception_swallowed", source="stdio_ipc.serve_stdin",
                    exc_info=True,
                )

        loop = asyncio.get_running_loop()
        while self._running:
            try:
                raw = await loop.run_in_executor(None, sys.stdin.readline)
            except Exception as e:
                logger.warning("StdioIPC stdin read error: %s", e)
                await asyncio.sleep(0.1)
                continue
            if not raw:
                # EOF
                logger.info("StdioIPC stdin EOF")
                self._running = False
                # 5.2: 通知外部进程退出
                if self._shutdown_event is not None:
                    self._shutdown_event.set()
                break
            line = raw.strip()
            if not line:
                continue
            await self._handle_line(line)

    async def _handle_line(self, raw: str) -> None:
        """处理一行 stdin 输入：连接管理检查后，业务消息交 InboundPipeline。"""
        # 大小限制
        if len(raw) > self._max_payload_bytes:
            logger.warning("StdioIPC: payload too large, size=%d", len(raw))
            return

        try:
            data: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("StdioIPC recv: invalid JSON: %s", e)
            return

        if not isinstance(data, dict):
            logger.warning("StdioIPC recv: not a dict: %r", data)
            return

        # 心跳（连接层）：PING 直接回 PONG，绝不进入业务管道。
        #   保留现行 uuid 兜底，不得退化为空串。
        if data.get("type") == IpcMessageType.PING:
            self._transport.write_raw({
                "type": IpcMessageType.PONG,
                "msg_id": data.get("msg_id") or str(uuid.uuid4()),
            })
            return

        # 业务消息 → 统一入站管道（normalize → dispatch）
        if self._pipeline is None:
            logger.warning("StdioIPC: no inbound pipeline set, dropping type=%r",
                           data.get("type"))
            return
        await self._pipeline.handle(data)
