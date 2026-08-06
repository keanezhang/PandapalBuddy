"""Markdown RunState Repository 实现（异步接口）。

用于 HITL pause/resume 场景。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from pandapal.storage.repositories._markdown_base import MarkdownBaseRepository

logger = logging.getLogger(__name__)

# RunState 最大存活时间（秒）：超过此时间视为过期，自动清理
# 防止因审批响应丢失、进程崩溃等原因导致 session 永久锁死
_RUN_STATE_TTL_SECONDS = 30 * 60  # 30 分钟


class MarkdownRunStateRepository(MarkdownBaseRepository):
    """Markdown 运行状态持久化操作（异步接口）。"""

    def __init__(self, base_dir: str, timeout: float = 5.0) -> None:
        super().__init__(base_dir, "run_states", timeout, session_partitioned=True)

    # ──────────────────────────────────────────────
    # CRUD 操作（异步接口，与 SQLite 版本一致）
    # ──────────────────────────────────────────────

    async def save_run_state(
        self, session_id: str, run_id: str, serialized_state: bytes
    ) -> None:
        """保存 Agent 运行状态快照（UPSERT）。"""
        file_path = self._partition_path(session_id, run_id)
        data = {
            "session_id": session_id,
            "run_id": run_id,
            "serialized_state": serialized_state.hex(),  # bytes → hex 字符串
            "created_at": self._now_iso(),
        }
        title = f"Run State: {run_id}"
        await self._write_entity(file_path, data, title)

    async def get_run_state(
        self, session_id: str, run_id: str
    ) -> bytes | None:
        """获取 Agent 运行状态快照。不存在返回 None。"""
        file_path = self._partition_path(session_id, run_id)
        data = await self._read_entity(file_path)

        if data is None:
            return None

        # 校验 session_id 是否匹配（分区路径已隔离，此处为双保险）
        if data.get("session_id") != session_id:
            return None

        # hex 字符串 → bytes
        state_hex = data.get("serialized_state")
        if state_hex:
            return bytes.fromhex(state_hex)
        return None

    async def get_pending_run_id(self, session_id: str) -> str | None:
        """查询该 session 是否存在待审批的 RunState，返回最新的 run_id（无则 None）。

        用于 HITL 会话锁：新消息到达时先调此方法，若有 pending 则拒绝执行新 run。

        安全机制：内置 TTL 过期自动清理（_RUN_STATE_TTL_SECONDS）。
        若 RunState 存活超过 TTL（因审批响应丢失、进程崩溃、用户放弃等），
        自动删除该 RunState 并放行新消息，避免 session 永久锁死。
        """
        all_entities = await self._list_entities()

        now = datetime.now(timezone.utc)

        # 过滤出该 session 的 run_states
        matching = []
        for e in all_entities:
            if e.get("session_id") != session_id or not e.get("serialized_state"):
                continue

            # TTL 检查：过期则自动清理
            created_at_str = e.get("created_at", "")
            if created_at_str:
                try:
                    created_at = datetime.fromisoformat(created_at_str)
                    age_seconds = (now - created_at).total_seconds()
                    if age_seconds > _RUN_STATE_TTL_SECONDS:
                        run_id = e.get("run_id", "")
                        logger.warning(
                            "[RunStateRepo] TTL expired: run_id=%s, age=%.0fs > %ds, "
                            "auto-deleting to unblock session=%s",
                            run_id, age_seconds, _RUN_STATE_TTL_SECONDS, session_id,
                        )
                        # 异步删除过期文件
                        file_path = self._partition_path(e.get("session_id", ""), run_id)
                        await self._delete_entity(file_path)
                        continue
                except (ValueError, TypeError):
                    pass  # 解析失败则不过期，保守处理

            matching.append(e)

        if not matching:
            return None

        # 按 created_at 倒序排序，取最新的
        matching.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return matching[0].get("run_id")

    async def get_run_state_by_run_id(
        self, run_id: str
    ) -> tuple[str, bytes] | None:
        """按 run_id 查找运行状态（不依赖 session_id）。

        用于 hitl_decision 中 session_id 为空或不匹配时的降级路径。
        """
        file_path = await self._find_path_by_id(run_id)
        if file_path is None:
            return None
        data = await self._read_entity(file_path)

        if data is None:
            return None

        session_id = data.get("session_id")
        state_hex = data.get("serialized_state")

        if session_id and state_hex:
            return session_id, bytes.fromhex(state_hex)
        return None

    async def delete_run_state(self, session_id: str, run_id: str) -> None:
        """删除运行状态（幂等）。

        BL7: 必须在 Agent 恢复执行后立即调用，防止 RunState 被二次恢复。
        """
        file_path = self._partition_path(session_id, run_id)
        # 校验 session_id 是否匹配（安全校验）
        data = await self._read_entity(file_path)
        if data and data.get("session_id") == session_id:
            await self._delete_entity(file_path)

    async def delete_all_for_session(self, session_id: str) -> int:
        """删除指定 session 的所有 run_state（幂等）。

        SessionListManager.soft_delete_session 用：删除会话时把 HITL 快照一起清。
        返回被删除的条数。
        """
        all_entities = await self._list_entities()
        deleted = 0
        for e in all_entities:
            if e.get("session_id") != session_id:
                continue
            run_id = e.get("run_id", "")
            if not run_id:
                continue
            file_path = self._partition_path(session_id, run_id)
            await self._delete_entity(file_path)
            deleted += 1
        return deleted

    async def cleanup_orphans(self) -> list[str]:
        """清理所有孤儿 RunState（sidecar 启动时调用）。"""
        all_entities = await self._list_entities()
        cleaned: list[str] = []

        for e in all_entities:
            run_id = e.get("run_id", "")
            session_id = e.get("session_id", "")
            if not run_id:
                continue
            file_path = self._partition_path(session_id, run_id)
            await self._delete_entity(file_path)
            cleaned.append(run_id)
            logger.warning(
                "[RunStateRepo] Orphan cleanup: run_id=%s, session=%s (deleted at startup)",
                run_id, session_id,
            )

        if cleaned:
            logger.info(
                "[RunStateRepo] Orphan cleanup done: %d run_state(s) removed",
                len(cleaned),
            )
        return cleaned

    async def cleanup_expired_run_states(self, ttl_seconds: int) -> list[str]:
        """清理超过 TTL 的 RunState，返回被清理的 run_id 列表。"""
        from datetime import datetime, timezone

        all_entities = await self._list_entities()
        now = datetime.now(timezone.utc)
        cleaned: list[str] = []

        for e in all_entities:
            run_id = e.get("run_id", "")
            if not run_id:
                continue
            created_at_str = e.get("created_at", "")
            if not created_at_str:
                continue
            try:
                created_at = datetime.fromisoformat(created_at_str)
                age = (now - created_at).total_seconds()
                if age > ttl_seconds:
                    file_path = self._partition_path(e.get("session_id", ""), run_id)
                    await self._delete_entity(file_path)
                    cleaned.append(run_id)
                    logger.info(
                        "[RunStateRepo] TTL expired: run_id=%s age=%.0fs",
                        run_id, age,
                    )
            except (ValueError, TypeError):
                pass

        if cleaned:
            logger.info(
                "[RunStateRepo] TTL cleanup done: %d expired run_state(s)",
                len(cleaned),
            )
        return cleaned

    async def list_all_run_states(
        self,
    ) -> list[tuple[str, str, bytes]]:
        """列出全部 RunState，返回 [(session_id, run_id, serialized_state), ...]。"""
        all_entities = await self._list_entities()
        result: list[tuple[str, str, bytes]] = []
        for e in all_entities:
            session_id = e.get("session_id", "")
            run_id = e.get("run_id", "")
            state_hex = e.get("serialized_state", "")
            if run_id and state_hex:
                result.append((session_id, run_id, bytes.fromhex(state_hex)))
        return result
