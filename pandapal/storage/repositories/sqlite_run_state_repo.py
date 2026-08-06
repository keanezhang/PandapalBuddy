"""Run State Repository 实现。

用于 HITL pause/resume 场景。
BL7 约束：delete_run_state 必须在 Agent 恢复后立即调用，防止二次恢复。
"""

from __future__ import annotations

import logging

import aiosqlite

from pandapal.storage.repositories._sqlite_base import BaseRepository

logger = logging.getLogger(__name__)


class RunStateRepository(BaseRepository):
    """HITL 运行状态持久化操作。"""

    def __init__(self, conn: aiosqlite.Connection, timeout: float = 5.0) -> None:
        super().__init__(conn, timeout)

    async def save_run_state(
        self, session_id: str, run_id: str, serialized_state: bytes
    ) -> None:
        """保存 Agent 运行状态快照（UPSERT）。"""
        now = self._now_iso()
        await self._execute(
            "INSERT OR REPLACE INTO run_states "
            "(session_id, run_id, serialized_state, created_at) "
            "VALUES (?, ?, ?, ?)",
            (session_id, run_id, serialized_state, now),
            operation="save_run_state",
        )
        await self._commit()

    async def get_run_state(
        self, session_id: str, run_id: str
    ) -> bytes | None:
        """获取 Agent 运行状态快照。不存在返回 None。"""
        row = await self._fetchone(
            "SELECT serialized_state FROM run_states "
            "WHERE session_id = ? AND run_id = ?",
            (session_id, run_id),
            operation="get_run_state",
        )
        if row is None:
            return None
        return row[0]

    async def get_pending_run_id(self, session_id: str) -> str | None:
        """查询该 session 是否存在待审批的 RunState，返回最新的 run_id（无则 None）。

        用于 HITL 会话锁：新消息到达时先调此方法，若有 pending 则拒绝执行新 run。
        """
        row = await self._fetchone(
            "SELECT run_id FROM run_states "
            "WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
            operation="get_pending_run_id",
        )
        return row[0] if row else None

    async def get_run_state_by_run_id(
        self, run_id: str
    ) -> tuple[str, bytes] | None:
        """按 run_id 查找运行状态（不依赖 session_id）。

        ⚠️ 危险：本方法绕过 session_id 复合键，是跨会话隔离的一道缺口。
        绝不可在 HITL/Interaction/Plan 的 resume 恢复路径里用它来「兜底」定位 run_state
        —— 那会让入站 session_id 错误时在错误会话里恢复执行（跨会话污染）。
        resume 路径必须用 get_run_state(session_id, run_id) 复合键读取，读不到就安全报错。
        本方法仅限「按 run_id 清理孤儿状态」等与恢复执行无关的运维场景；当前无生产调用。

        Returns:
            (session_id, serialized_state) 或 None（不存在）。
        """
        row = await self._fetchone(
            "SELECT session_id, serialized_state FROM run_states WHERE run_id = ?",
            (run_id,),
            operation="get_run_state_by_run_id",
        )
        if row is None:
            return None
        return row[0], row[1]

    async def delete_run_state(self, session_id: str, run_id: str) -> None:
        """删除运行状态（幂等）。

        BL7: 必须在 Agent 恢复执行后立即调用，防止 RunState 被二次恢复。
        """
        await self._execute(
            "DELETE FROM run_states WHERE session_id = ? AND run_id = ?",
            (session_id, run_id),
            operation="delete_run_state",
        )
        await self._commit()

    async def delete_all_for_session(self, session_id: str) -> int:
        """删除指定 session 的所有 run_state（幂等）。

        SessionListManager.soft_delete_session 用：删除会话时把 HITL 快照一起清。
        返回被删除行数。
        """
        cursor = await self._execute(
            "DELETE FROM run_states WHERE session_id = ?",
            (session_id,),
            operation="delete_all_for_session",
        )
        await self._commit()
        return cursor.rowcount or 0

    async def cleanup_orphans(self) -> list[str]:
        """清理所有孤儿 RunState（sidecar 启动时调用）。

        判断依据：sidecar 进程刚启动时，内存中无任何活跃 Agent 执行，
        此时存储中残留的 RunState 都是上一轮进程崩溃/重启遗留的孤儿，
        无法被自然恢复，必须清理以避免 session 永久锁死。

        Returns:
            被清理的 run_id 列表。
        """
        rows = await self._fetchall(
            "SELECT run_id, session_id FROM run_states",
            (),
            operation="cleanup_orphans",
        )
        cleaned: list[str] = []
        for row in rows:
            run_id, session_id = row[0], row[1]
            await self._execute(
                "DELETE FROM run_states WHERE run_id = ?",
                (run_id,),
                operation="cleanup_orphans_delete",
            )
            cleaned.append(run_id)
            logger.warning(
                "[RunStateRepo] Orphan cleanup: run_id=%s, session=%s (deleted at startup)",
                run_id, session_id,
            )

        if cleaned:
            await self._commit()
            logger.info(
                "[RunStateRepo] Orphan cleanup done: %d run_state(s) removed",
                len(cleaned),
            )
        return cleaned

    async def cleanup_expired_run_states(self, ttl_seconds: int) -> list[str]:
        """清理超过 TTL 的 RunState，返回被清理的 run_id 列表。"""
        rows = await self._fetchall(
            "SELECT run_id, session_id FROM run_states "
            "WHERE datetime(created_at) < datetime('now', ?)",
            (f"-{ttl_seconds} seconds",),
            operation="cleanup_expired_run_states",
        )
        cleaned: list[str] = []
        for row in rows:
            run_id, session_id = row[0], row[1]
            await self._execute(
                "DELETE FROM run_states WHERE run_id = ?",
                (run_id,),
                operation="cleanup_expired_delete",
            )
            cleaned.append(run_id)
            logger.info(
                "[RunStateRepo] TTL expired: run_id=%s session=%s",
                run_id, session_id,
            )
        if cleaned:
            await self._commit()
        return cleaned

    async def list_all_run_states(
        self,
    ) -> list[tuple[str, str, bytes]]:
        """列出全部 RunState，返回 [(session_id, run_id, serialized_state), ...]。
        用于启动时恢复 pending interactions。
        """
        rows = await self._fetchall(
            "SELECT session_id, run_id, serialized_state FROM run_states",
            (),
            operation="list_all_run_states",
        )
        return [(row[0], row[1], row[2]) for row in rows]
