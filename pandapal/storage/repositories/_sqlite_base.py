"""Repository 基类，提供 timeout 包装和通用工具方法。

设计约束：
- I5: 所有 aiosqlite 操作通过 asyncio.wait_for 包裹 query_timeout_s
- D1: Repository 接口使用业务语言，不暴露 sqlite3.Row
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

import aiosqlite

from pandapal.storage.exceptions import StorageTimeoutError


class BaseRepository:
    """异步 Repository 基类。

    所有数据库操作通过 _execute / _fetchone / _fetchall 执行，
    自动包裹 asyncio.wait_for 超时保护（I5）。
    """

    def __init__(self, conn: aiosqlite.Connection, timeout: float = 5.0) -> None:
        self._conn = conn
        self._timeout = timeout

    async def _execute(
        self, sql: str, params: tuple = (), *, operation: str = "execute"
    ) -> aiosqlite.Cursor:
        """执行 SQL 语句（带超时保护）。"""
        try:
            return await asyncio.wait_for(
                self._conn.execute(sql, params), timeout=self._timeout
            )
        except asyncio.TimeoutError as e:
            raise StorageTimeoutError(operation, self._timeout) from e

    async def _execute_insert(
        self, sql: str, params: tuple = (), *, operation: str = "insert"
    ) -> int:
        """执行 INSERT 并返回 lastrowid。"""
        cursor = await self._execute(sql, params, operation=operation)
        return cursor.lastrowid  # type: ignore[return-value]

    async def _fetchone(
        self, sql: str, params: tuple = (), *, operation: str = "fetchone"
    ) -> sqlite3.Row | None:
        """查询单行（带超时保护）。

        Fix #5: 合并 execute + fetchone 到单次 wait_for，
        避免第一次成功第二次超时时 cursor 泄露。
        """
        async def _exec_and_fetch():
            cursor = await self._conn.execute(sql, params)
            return await cursor.fetchone()

        try:
            return await asyncio.wait_for(
                _exec_and_fetch(), timeout=self._timeout
            )
        except asyncio.TimeoutError as e:
            raise StorageTimeoutError(operation, self._timeout) from e

    async def _fetchall(
        self, sql: str, params: tuple = (), *, operation: str = "fetchall"
    ) -> list[sqlite3.Row]:
        """查询多行（带超时保护）。

        Fix #5: 合并 execute + fetchall 到单次 wait_for。
        """
        async def _exec_and_fetch():
            cursor = await self._conn.execute(sql, params)
            return await cursor.fetchall()

        try:
            return await asyncio.wait_for(
                _exec_and_fetch(), timeout=self._timeout
            )
        except asyncio.TimeoutError as e:
            raise StorageTimeoutError(operation, self._timeout) from e

    async def _commit(self) -> None:
        """提交事务（带超时保护）。"""
        try:
            await asyncio.wait_for(
                self._conn.commit(), timeout=self._timeout
            )
        except asyncio.TimeoutError as e:
            raise StorageTimeoutError("commit", self._timeout) from e

    # ──────────────────────────────────────────────
    # 工具方法
    # ──────────────────────────────────────────────

    @staticmethod
    def _now_iso() -> str:
        """返回当前 UTC 时间的 ISO 格式字符串。"""
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _to_iso(dt: datetime | None) -> str | None:
        """datetime 转 ISO 字符串。"""
        if dt is None:
            return None
        return dt.isoformat()

    @staticmethod
    def _from_iso(value: str | None) -> datetime | None:
        """ISO 字符串转 datetime。"""
        if value is None:
            return None
        return datetime.fromisoformat(value)
