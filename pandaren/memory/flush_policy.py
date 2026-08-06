"""pandaren/memory/flush_policy.py — 异步批量写入策略

AsyncBatchFlushPolicy 是 FlushPolicy 协议的内置默认实现，
提供 coalesce 窗口合并 + buffer 溢出保护的异步批量写入能力。
"""

from __future__ import annotations

import asyncio
import logging

from .models import MessageDict
from .protocols import RawLogBackend
from .constants import DEFAULT_FLUSH_COALESCE_MS, DEFAULT_FLUSH_BUFFER_MAX_ENTRIES

logger = logging.getLogger("pandaren.memory")


class AsyncBatchFlushPolicy:
    """异步批量写入策略，实现 FlushPolicy 协议。

    - coalesce_ms：批量合并窗口（默认 100ms）
    - buffer_max_entries：缓冲区条数上限（默认 DEFAULT_FLUSH_BUFFER_MAX_ENTRIES）

    当 buffer 条数达到上限时，立即触发写入（溢出保护）。
    每个 session_id key 独立维护 buffer 和 backend 引用，
    flush_all 时各 key 使用各自入队时绑定的 backend，不存在跨 backend 污染。
    """

    def __init__(
        self,
        coalesce_ms: int = DEFAULT_FLUSH_COALESCE_MS,
        buffer_max_entries: int = DEFAULT_FLUSH_BUFFER_MAX_ENTRIES,
    ) -> None:
        self._coalesce_ms = coalesce_ms
        self._buffer_max_entries = buffer_max_entries
        # session_id → (list[(MessageDict, run_id, step)], RawLogBackend)
        # 每条 buffer 项随消息一起携带 run_id/step，保证批量落盘时 key join 不丢失。
        self._buffers: dict[str, tuple[list[tuple[MessageDict, str, "int | None"]], RawLogBackend]] = {}
        self._flush_tasks: dict[str, asyncio.Task] = {}

    async def enqueue(
        self,
        message: MessageDict,
        session_id: str,
        backend: RawLogBackend,
        run_id: str = "",
        step: int | None = None,
    ) -> None:
        """将消息加入写入队列，可能触发批量写。"""
        key = session_id
        if key not in self._buffers:
            self._buffers[key] = ([], backend)

        messages, stored_backend = self._buffers[key]
        messages.append((message, run_id, step))

        # 溢出保护：buffer 条数超过上限立即写入
        if len(messages) >= self._buffer_max_entries:
            await self._flush_key(key)
            return

        # 取消旧的 coalesce task，重新调度
        if key in self._flush_tasks and not self._flush_tasks[key].done():
            self._flush_tasks[key].cancel()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 非异步上下文：同步写入
            await self._flush_key(key)
            return

        self._flush_tasks[key] = loop.create_task(
            self._delayed_flush(key)
        )

    async def _delayed_flush(self, key: str) -> None:
        """等待 coalesce_ms 后批量写入。"""
        try:
            await asyncio.sleep(self._coalesce_ms / 1000.0)
            await self._flush_key(key)
        except asyncio.CancelledError:
            pass  # 被新的 enqueue 取消，忽略

    async def _flush_key(self, key: str) -> None:
        """将指定 key 的缓冲区写入其绑定的后端。"""
        session_id = key
        entry = self._buffers.pop(key, None)
        if entry is None:
            return
        messages, backend = entry
        if not messages:
            return
        for msg, run_id, step in messages:
            try:
                backend.append_raw_message(
                    msg, session_id=session_id, run_id=run_id, step=step,
                )
            except Exception as exc:
                logger.warning(
                    "AsyncBatchFlushPolicy._flush_key failed "
                    "(session_id=%s): %s",
                    session_id, exc,
                )

    async def flush(
        self,
        session_id: str,
        backend: RawLogBackend,
        *,
        flush_all: bool = False,
    ) -> None:
        """强制写入缓冲消息。

        Args:
            flush_all: 若为 True，flush 所有 key 的缓冲区（各用各自绑定的 backend）；
                       否则仅 flush 指定 session_id 的缓冲区。
            backend:   flush_all=False 时若该 key 尚无缓冲区，此参数无效；
                       flush_all=True 时忽略此参数，各 key 使用入队时绑定的 backend。
        """
        if flush_all:
            # 取消所有 pending flush tasks
            for task in list(self._flush_tasks.values()):
                if not task.done():
                    task.cancel()
            # flush 所有 key，各用各自绑定的 backend
            for key in list(self._buffers.keys()):
                await self._flush_key(key)
        else:
            key = session_id
            if key in self._flush_tasks and not self._flush_tasks[key].done():
                self._flush_tasks[key].cancel()
            await self._flush_key(key)
