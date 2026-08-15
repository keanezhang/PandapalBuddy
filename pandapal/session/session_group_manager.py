"""SessionGroupManager — 会话分组管理（v004）。

职责：
- 分组 CRUD（create/rename/delete/list）
- 1:1 分组关联（assign_to_group）
- 组内会话列表加载（list_group_sessions，走正向记录快路径）
- 正向记录维护（写穿透 + on_session_removed 回调）

与 SessionListManager 的关系：
- SessionListManager 管会话元数据 + 路由 + 容量
- SessionGroupManager 管分组 + 正向记录 + 组内会话列表
- 两者通过 on_session_removed 回调协作（会话删除时同步正向记录）

设计约束：
- 正向记录（group.session_ids）是读取索引，sessions.group_id 仍是反向真值
- 写穿透：assign_to_group / on_session_removed 同步维护正向记录
- 加载组内会话走 list_visible_sessions_by_ids（组内大小 << 总会话数时收益显著）
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol

from pandapal.broadcast.broadcaster import MessageBroadcast
from pandapal.events.normalized import NormalizedEvent
from pandapal.session.exceptions import (
    GroupNameConflict,
    GroupNameInvalid,
    GroupNotFoundError,
    GroupQuotaExceeded,
    InvalidPageSize,
    SessionNotFoundError,
)
from pandapal.session.session_list_manager import (
    DEFAULT_MAX_GROUPS,
    DEFAULT_PAGE_SIZE,
    MAX_GROUP_NAME_LENGTH,
    MAX_PAGE_SIZE,
    DefaultIdGenerator,
    SessionInfo,
    SystemClock,
    session_to_info,
)
from pandapal.storage.exceptions import StorageDuplicateError
from pandapal.storage.models import Session, SessionGroup

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════
# 依赖协议
# ═════════════════════════════════════════════════════════════

class _GroupIdGeneratorLike(Protocol):
    def new_group_id(self) -> str: ...


class _ClockLike(Protocol):
    def now(self) -> datetime: ...


class SessionGroupManager:
    """会话分组管理器（无状态 + 全局单例）。"""

    def __init__(
        self,
        session_repo: Any,          # SessionRepository
        group_repo: Any,            # SessionGroupRepository
        broadcast: MessageBroadcast,
        id_generator: _GroupIdGeneratorLike | None = None,
        clock: _ClockLike | None = None,
    ) -> None:
        if session_repo is None:
            raise ValueError("SessionGroupManager requires session_repo")
        if group_repo is None:
            raise ValueError("SessionGroupManager requires group_repo")
        if broadcast is None:
            raise ValueError("SessionGroupManager requires broadcast")

        self._session_repo = session_repo
        self._group_repo = group_repo
        self._broadcast = broadcast
        self._id_gen: _GroupIdGeneratorLike = id_generator or DefaultIdGenerator()
        self._clock: _ClockLike = clock or SystemClock()

    # ═══════════════════════════════════════════════════════════
    # 分组 CRUD
    # ═══════════════════════════════════════════════════════════

    async def create_group(self, user_id: str, name: str) -> str:
        name = (name or "").strip()
        if not name:
            raise GroupNameInvalid(name, "empty")
        if len(name) > MAX_GROUP_NAME_LENGTH:
            raise GroupNameInvalid(name, f"exceeds {MAX_GROUP_NAME_LENGTH} chars")

        count = await self._group_repo.count_groups_by_user(user_id)
        if count >= DEFAULT_MAX_GROUPS:
            raise GroupQuotaExceeded(user_id, count, DEFAULT_MAX_GROUPS)

        existing = await self._group_repo.find_group_by_name(user_id, name)
        if existing is not None:
            raise GroupNameConflict(user_id, name)

        group_id = self._id_gen.new_group_id()
        group = SessionGroup(
            id=group_id,
            user_id=user_id,
            name=name,
            created_at=self._clock.now(),
            session_ids=[],
        )
        try:
            await self._group_repo.create_group(group)
        except StorageDuplicateError:
            # 竞态：并发创建同名分组，转为语义化异常
            raise GroupNameConflict(user_id, name)

        await self._broadcast_group_list(user_id)
        return group_id

    async def rename_group(
        self, user_id: str, group_id: str, new_name: str
    ) -> None:
        new_name = (new_name or "").strip()
        if not new_name:
            raise GroupNameInvalid(new_name, "empty")
        if len(new_name) > MAX_GROUP_NAME_LENGTH:
            raise GroupNameInvalid(new_name, f"exceeds {MAX_GROUP_NAME_LENGTH} chars")

        group = await self._group_repo.find_group(group_id)
        if group is None or group.user_id != user_id:
            raise GroupNotFoundError(group_id)

        try:
            ok = await self._group_repo.rename_group(group_id, new_name)
        except StorageDuplicateError:
            raise GroupNameConflict(user_id, new_name)
        if not ok:
            raise GroupNotFoundError(group_id)

        await self._broadcast_group_list(user_id)

        # 逐个广播 SESSION_UPDATED，否则前端会话列表里的分组标签仍显示旧名。
        for sid in group.session_ids:
            s = await self._session_repo.find_session(sid)
            if s is not None and s.group_id == group_id and not s.is_deleted:
                await self._broadcast_session_updated(s, "group_changed")

    async def delete_group(self, user_id: str, group_id: str) -> None:
        """删除分组（不级联删除会话）。

        先把组内会话 group_id 置 NULL（会话保留，变为「无分组」），再删分组。
        级联软删除由上层 handler 负责（先拿 session_ids，逐个 soft_delete_session）。
        """
        group = await self._group_repo.find_group(group_id)
        if group is None or group.user_id != user_id:
            raise GroupNotFoundError(group_id)

        # 先解除关联（会话保留），并逐个广播 SESSION_UPDATED，
        # 否则前端会话列表会残留旧的 group_id/group_name（分组标签不消失）。
        affected: list[Session] = []
        for sid in group.session_ids:
            s = await self._session_repo.find_session(sid)
            if s is not None and not s.is_deleted:
                affected.append(s)

        await self._session_repo.clear_group_id_for_group(group_id)
        for s in affected:
            updated = await self._session_repo.find_session(s.session_id)
            if updated is not None:
                await self._broadcast_session_updated(updated, "group_changed")

        # 再删分组
        await self._group_repo.delete_group(group_id)
        await self._broadcast_group_list(user_id)

    async def list_groups(self, user_id: str) -> list[SessionGroup]:
        return await self._group_repo.list_groups_by_user(user_id)

    # ═══════════════════════════════════════════════════════════
    # 分组关联（1:1）
    # ═══════════════════════════════════════════════════════════

    async def assign_to_group(
        self, user_id: str, session_id: str, group_id: str | None
    ) -> None:
        """1:1 分组关联。group_id=None 表示解除关联。

        写穿透：同步维护旧组/新组的正向记录（session_ids）。
        """
        session = await self._session_repo.find_session(session_id)
        if session is None or session.is_deleted or session.user_id != user_id:
            raise SessionNotFoundError(session_id)
        if group_id is not None:
            g = await self._group_repo.find_group(group_id)
            if g is None or g.user_id != user_id:
                raise GroupNotFoundError(group_id)

        old_group_id = session.group_id
        await self._session_repo.update_session_meta(
            session_id,
            group_id=group_id,
            group_id_touched=True,
            touch_updated_at=False,
        )

        # 正向记录维护（旧组移除 + 新组加入）
        if old_group_id != group_id:
            if old_group_id is not None:
                await self._remove_session_from_group(old_group_id, session_id)
            if group_id is not None:
                await self._add_session_to_group(group_id, session_id)

        updated = await self._session_repo.find_session(session_id)
        if updated is not None:
            await self._broadcast_session_updated(updated, "group_changed")

    # ═══════════════════════════════════════════════════════════
    # 组内会话列表（正向记录快路径）
    # ═══════════════════════════════════════════════════════════

    async def list_group_sessions(
        self,
        user_id: str,
        group_id: str,
        page: int = 1,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> tuple[list[SessionInfo], bool]:
        """加载某分组内的会话列表（走正向记录，避免全表按 group_id 过滤）。"""
        if limit <= 0 or limit > MAX_PAGE_SIZE:
            raise InvalidPageSize(limit, MAX_PAGE_SIZE)

        group = await self._group_repo.find_group(group_id)
        if group is None or group.user_id != user_id:
            raise GroupNotFoundError(group_id)

        if group.session_ids:
            # 快路径：正向记录 + 按 id 精准查（脏 id 会被 user_id/可见性过滤自然剔除）
            sessions_raw, has_more = await self._session_repo.list_visible_sessions_by_ids(
                user_id=user_id,
                session_ids=group.session_ids,
                page=max(1, page),
                limit=limit,
            )
        else:
            # 兜底（升级后正向记录为空但组内可能有会话）：
            # 回退到 group_id 反查（慢路径），保证正确性优先；下次 backfill 修复正向记录。
            sessions_raw, has_more = await self._session_repo.list_visible_sessions(
                user_id=user_id,
                group_id=group_id,
                page=max(1, page),
                limit=limit,
            )

        group_map = {group.id: group.name}
        infos = [session_to_info(s, group_map) for s in sessions_raw]
        return infos, has_more

    async def list_group_session_ids(
        self, user_id: str, group_id: str
    ) -> list[str]:
        """返回组内会话 id 列表（供上层级联删除）。"""
        group = await self._group_repo.find_group(group_id)
        if group is None or group.user_id != user_id:
            raise GroupNotFoundError(group_id)
        return list(group.session_ids)

    async def backfill_forward_index(self, user_id: str) -> int:
        """幂等重建所有分组的正向记录（从 sessions.group_id 聚合）。

        用于启动兜底：升级后旧分组 session_ids 为空、或并发/历史 bug 造成漂移时，
        以 sessions.group_id（反向真值）为准覆写每个 group 的 session_ids。

        Returns:
            被修正的分组数量（漂移才重写，一致不动）。
        """
        groups = await self._group_repo.list_groups_by_user(user_id)
        if not groups:
            return 0

        # 聚合所有未删除且已分组的会话（group_id 非空）
        by_group: dict[str, list[str]] = {}
        for s in await self._session_repo.find_sessions_by_user(user_id):
            if s.group_id is not None and not s.is_deleted:
                by_group.setdefault(s.group_id, []).append(s.session_id)

        changed = 0
        for g in groups:
            desired = by_group.get(g.id, [])
            current = await self._group_repo.get_session_ids(g.id) or []
            if set(current) != set(desired):
                await self._group_repo.set_session_ids(g.id, desired)
                changed += 1
        if changed:
            logger.info(
                "[SessionGroup] backfill repaired %d group(s) for user=%s",
                changed, user_id,
            )
        return changed

    # ═══════════════════════════════════════════════════════════
    # 正向记录回调（供 SessionListManager 调用）
    # ═══════════════════════════════════════════════════════════

    async def on_session_removed(
        self, session_id: str, group_id: str | None
    ) -> None:
        """会话被软删除时，从所属组的正向记录移除该 session_id。"""
        if group_id is None:
            return
        try:
            await self._remove_session_from_group(group_id, session_id)
        except Exception as e:
            logger.warning(
                "[SessionGroup] on_session_removed failed session=%s group=%s: %s",
                session_id, group_id, e,
            )

    # ═══════════════════════════════════════════════════════════
    # 正向记录维护（内部）
    # ═══════════════════════════════════════════════════════════

    async def _add_session_to_group(self, group_id: str, session_id: str) -> None:
        ids = await self._group_repo.get_session_ids(group_id)
        if ids is None:
            return  # 组不存在，忽略（幂等）
        if session_id in ids:
            return
        ids.append(session_id)
        await self._group_repo.set_session_ids(group_id, ids)

    async def _remove_session_from_group(
        self, group_id: str, session_id: str
    ) -> None:
        ids = await self._group_repo.get_session_ids(group_id)
        if ids is None:
            return
        if session_id not in ids:
            return
        ids = [x for x in ids if x != session_id]
        await self._group_repo.set_session_ids(group_id, ids)

    # ═══════════════════════════════════════════════════════════
    # 广播辅助
    # ═══════════════════════════════════════════════════════════

    async def _broadcast_session_updated(
        self, session: Session, reason: str
    ) -> None:
        group_map: dict[str, str] = {}
        if session.group_id:
            try:
                g = await self._group_repo.find_group(session.group_id)
                if g is not None:
                    group_map[g.id] = g.name
            except Exception:
                pass
        info = session_to_info(session, group_map)
        try:
            await self._broadcast.send(
                NormalizedEvent.session_updated(
                    session_info=info.to_dict(),
                    reason=reason,
                ),
            )
        except Exception as e:
            logger.warning(
                "[SessionGroup] broadcast SESSION_UPDATED failed: %s", e,
            )

    async def _broadcast_group_list(self, user_id: str) -> None:
        try:
            groups = await self._group_repo.list_groups_by_user(user_id)
            payload = [
                {
                    "id": g.id,
                    "user_id": g.user_id,
                    "name": g.name,
                    "created_at": g.created_at.isoformat() if g.created_at else "",
                }
                for g in groups
            ]
            await self._broadcast.send(
                NormalizedEvent.session_group_list(groups=payload),
            )
        except Exception as e:
            logger.warning("[SessionGroup] broadcast group_list failed: %s", e)
