"""Markdown Session Repository 实现。

使用 Markdown 文件存储 Session 数据，每个 Session 对应一个 .md 文件。
文件格式使用 YAML front matter 存储结构化数据。
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone
from typing import Any

from pandapal.storage.models import Session
from pandapal.storage.repositories._markdown_base import MarkdownBaseRepository

logger = logging.getLogger(__name__)


class MarkdownSessionRepository(MarkdownBaseRepository):
    """Markdown 会话持久化操作（异步接口，与 SQLite 版本一致）。"""

    def __init__(self, base_dir: str, timeout: float = 5.0) -> None:
        """
        Args:
            base_dir: Markdown 存储根目录
            timeout: 操作超时秒数（保留参数，兼容接口）
        """
        super().__init__(base_dir, "sessions", timeout)

    def _get_file_path(self, session_id: str) -> str:
        """获取新布局下的 session 元数据路径。

        新路径：{base_dir}/sessions/{sid}/session.md
        与 raw_log.md / working_memory.md / audit.md 并排，方便按 session
        打包、查看和删除。
        """
        safe_id = self._sanitize_id(session_id)
        return os.path.join(self._entity_dir, safe_id, "session.md")

    def _get_legacy_file_path(self, session_id: str) -> str:
        """旧布局：{base_dir}/sessions/{sid}.md。"""
        safe_id = self._sanitize_id(session_id)
        return os.path.join(self._entity_dir, f"{safe_id}.md")

    def _record_glob_patterns(self) -> list[str]:
        """session 记录文件清单：每 sid 一目录的 session.md + legacy 平铺 {sid}.md。

        覆盖基类默认（{entity_dir}/*.md），使索引只扫 session 元数据，
        不再误读同目录下的 raw_log.md / run_states / approvals 等附属大文件。
        """
        return [
            os.path.join(self._entity_dir, "*", "session.md"),
            os.path.join(self._entity_dir, "*.md"),
        ]

    async def _read_session_entity(self, session_id: str) -> dict[str, Any] | None:
        data = await self._read_entity(self._get_file_path(session_id))
        if data is not None:
            return data
        return await self._read_entity(self._get_legacy_file_path(session_id))

    async def _delete_legacy_session_file(self, session_id: str) -> None:
        legacy_path = self._get_legacy_file_path(session_id)
        if os.path.exists(legacy_path):
            await self._delete_entity(legacy_path)

    # ──────────────────────────────────────────────
    # CRUD 操作（异步接口）
    # ──────────────────────────────────────────────

    async def find_session(self, session_id: str) -> Session | None:
        """按 session_id 查找会话。不存在返回 None。"""
        data = await self._read_session_entity(session_id)

        if data is None:
            return None

        return self._dict_to_model(data)

    async def find_sessions_by_user(self, user_id: str) -> list[Session]:
        """按 user_id 批量查找所有会话。"""
        entities = await self._filter_entities(user_id=user_id)
        return [self._dict_to_model(data) for data in entities]

    async def save_session(self, session: Session) -> None:
        """保存会话（UPSERT by session_id，幂等）。"""
        file_path = self._get_file_path(session.session_id)

        # 构建存储数据
        data = {
            "session_id": session.session_id,
            "user_id": session.user_id,
            "device_id": session.device_id,
            "last_active": self._to_iso(session.last_active),
            "created_at": self._to_iso(session.created_at),
            "title": session.title,
            "preview": session.preview,
            "message_count": session.message_count,
            "is_empty": session.is_empty,
            "is_favorite": session.is_favorite,
            "is_deleted": session.is_deleted,
            "updated_at": self._to_iso(session.updated_at or session.last_active),
            "group_id": session.group_id,
        }

        # 写入 Markdown 文件
        title = f"Session: {session.session_id}"
        await self._write_entity(file_path, data, title)
        await self._delete_legacy_session_file(session.session_id)

    async def update_session_last_active(
        self, session_id: str, timestamp: datetime
    ) -> None:
        """更新会话最后活跃时间（高频操作，单字段更新）。"""
        file_path = self._get_file_path(session_id)
        data = await self._read_session_entity(session_id)

        if data is None:
            return  # 会话不存在，静默失败

        # 更新最后活跃时间
        data["last_active"] = timestamp.isoformat()

        # 写回文件
        title = f"Session: {session_id}"
        await self._write_entity(file_path, data, title)
        await self._delete_legacy_session_file(session_id)

    async def delete_expired_sessions(self, before: datetime) -> int:
        """删除过期会话（batch cleanup）。返回删除行数。"""
        deleted_count = 0

        # 走内存索引遍历所有会话（修复 _list_filenames 在新布局下失效的 bug：
        # 原实现用 os.listdir 平铺列出 .md，但 session.md 现在在 {sid}/ 子目录中）。
        # 时间比较解析为 datetime（naive 补 UTC），而非字符串比较——存储格式
        # 存在 _to_iso（本地时间）与 isoformat（带时区）两种，字符串比较会误判。
        for data in await self._list_entities():
            session_id = data.get("session_id", "")
            if not session_id:
                continue

            last_active = self._parse_datetime(data.get("last_active"))
            if last_active is None or last_active >= before:
                continue

            await self._delete_entity(self._get_file_path(session_id))
            await self._delete_legacy_session_file(session_id)
            deleted_count += 1

        return deleted_count

    async def delete_session(self, session_id: str) -> None:
        """删除指定会话（幂等，不存在也不报错）。"""
        file_path = self._get_file_path(session_id)
        await self._delete_entity(file_path)
        await self._delete_legacy_session_file(session_id)

    # ──────────────────────────────────────────────
    # v003 会话列表扩展方法（与 SQLite SessionRepository 接口一致）
    # ──────────────────────────────────────────────

    async def list_visible_sessions(
        self,
        user_id: str,
        group_id: str | None,
        page: int,
        limit: int,
    ) -> tuple[list[Session], bool]:
        """列出可见会话（is_empty=0 AND is_deleted=0）。

        排序：created_at DESC（创建时间倒序）
        分页：LIMIT limit+1（多取 1 条判断 has_more）

        Args:
            user_id: 用户 ID
            group_id: 分组过滤；None="all"（不过滤）；空字符串 ""=无分组
            page: 页码（1-based）
            limit: 每页大小

        Returns:
            (sessions, has_more)
        """
        # 加载用户所有实体
        entities = await self._filter_entities(user_id=user_id)

        # 过滤：可见且非空
        visible = [
            e for e in entities
            if not bool(e.get("is_empty", True))
            and not bool(e.get("is_deleted", False))
        ]

        # 按 group_id 过滤
        if group_id is not None:
            if group_id == "":
                visible = [e for e in visible if e.get("group_id") is None]
            else:
                visible = [e for e in visible if e.get("group_id") == group_id]

        # 排序：created_at DESC（创建时间倒序，缺失时排最后）
        visible.sort(key=lambda e: e.get("created_at") or "", reverse=True)

        # 分页
        offset = max(0, (page - 1) * limit)
        result = visible[offset:offset + limit + 1]
        has_more = len(result) > limit
        sessions = [self._dict_to_model(e) for e in result[:limit]]
        return sessions, has_more

    async def search_by_title(
        self, user_id: str, query: str, limit: int = 15
    ) -> list[Session]:
        """按标题关键词模糊搜索可见会话（命令面板 ⌘K）。

        与 SQLite 版语义对齐：仅 is_empty=0 AND is_deleted=0，
        标题子串（不区分大小写）命中；排序 created_at DESC（创建时间倒序）。
        """
        q = query.strip().lower()
        if not q:
            return []
        entities = await self._filter_entities(user_id=user_id)
        matched = [
            e for e in entities
            if not bool(e.get("is_empty", True))
            and not bool(e.get("is_deleted", False))
            and q in str(e.get("title", "")).lower()
        ]
        matched.sort(key=lambda e: e.get("created_at") or "", reverse=True)
        return [self._dict_to_model(e) for e in matched[:limit]]

    async def find_current_empty_session(self, user_id: str) -> Session | None:
        """查询用户当前的空会话（节流复用）。

        WHERE is_empty=1 AND is_deleted=0 LIMIT 1
        """
        entities = await self._filter_entities(
            user_id=user_id,
            is_empty=True,
            is_deleted=False,
        )
        if not entities:
            return None
        return self._dict_to_model(entities[0])

    async def hard_delete_empty_sessions(self, user_id: str) -> int:
        """硬删除用户所有 is_empty=1 的会话（startup 清遗留）。

        Returns:
            实际删除的文件数
        """
        entities = await self._filter_entities(user_id=user_id, is_empty=True)
        deleted_count = 0
        for e in entities:
            file_path = self._get_file_path(e["session_id"])
            try:
                deleted = await self._delete_entity(file_path)
                if deleted:
                    deleted_count += 1
            except Exception:
                logger.warning(
                    "Failed to delete empty session %s", e.get("session_id"),
                    exc_info=True,
                )
        return deleted_count

    async def count_visible_sessions(self, user_id: str) -> int:
        """统计用户可见会话数（is_empty=0 AND is_deleted=0）。"""
        entities = await self._filter_entities(user_id=user_id)
        visible = [
            e for e in entities
            if not bool(e.get("is_empty", True))
            and not bool(e.get("is_deleted", False))
        ]
        return len(visible)

    async def find_oldest_visible(
        self, user_id: str, exclude_session_id: str | None = None
    ) -> Session | None:
        """查找最旧的可见会话（用于 evict_oldest）。

        排序：updated_at ASC, created_at ASC
        WHERE is_empty=0 AND is_deleted=0 AND session_id != exclude_session_id
        """
        entities = await self._filter_entities(user_id=user_id)
        visible = [
            e for e in entities
            if not bool(e.get("is_empty", True))
            and not bool(e.get("is_deleted", False))
            and e.get("session_id") != exclude_session_id
        ]
        if not visible:
            return None
        # 按 updated_at ASC（最旧在前），取第一条
        visible.sort(key=lambda e: e.get("updated_at") or "")
        return self._dict_to_model(visible[0])

    async def soft_delete_session(self, session_id: str) -> bool:
        """软删除：is_deleted=1。返回是否命中。

        幂等：多次调用不报错。
        """
        file_path = self._get_file_path(session_id)
        data = await self._read_entity(file_path)
        if data is None:
            return False
        if bool(data.get("is_deleted", False)):
            return False  # 已删除，幂等
        data["is_deleted"] = True
        data["updated_at"] = self._to_iso(datetime.now(timezone.utc))
        await self._write_entity(file_path, data, f"Session: {session_id}")
        return True

    async def update_session_meta(
        self,
        session_id: str,
        *,
        title: str | None = None,
        preview: str | None = None,
        message_count: int | None = None,
        is_empty: bool | None = None,
        is_favorite: bool | None = None,
        group_id: str | None = None,
        group_id_touched: bool = False,
        touch_updated_at: bool = True,
    ) -> bool:
        """通用元数据更新（只更传入的非 None 字段）。

        Args:
            group_id_touched: True 时即使 group_id=None 也设置
            touch_updated_at: True 时同时刷新 updated_at + last_active

        Returns:
            是否命中
        """
        file_path = self._get_file_path(session_id)
        data = await self._read_entity(file_path)
        if data is None:
            return False

        if title is not None:
            data["title"] = title
        if preview is not None:
            data["preview"] = preview
        if message_count is not None:
            data["message_count"] = message_count
        if is_empty is not None:
            data["is_empty"] = is_empty
        if is_favorite is not None:
            data["is_favorite"] = is_favorite
        if group_id_touched:
            data["group_id"] = group_id
        if touch_updated_at:
            now = self._to_iso(datetime.now(timezone.utc))
            data["updated_at"] = now
            data["last_active"] = now

        await self._write_entity(file_path, data, f"Session: {session_id}")
        return True

    async def increment_message_count(
        self, session_id: str, delta: int
    ) -> None:
        """原子增加 message_count 并刷 updated_at。静默失败。"""
        file_path = self._get_file_path(session_id)
        data = await self._read_entity(file_path)
        if data is None:
            return
        data["message_count"] = int(data.get("message_count", 0) or 0) + delta
        now = self._to_iso(datetime.now(timezone.utc))
        data["updated_at"] = now
        data["last_active"] = now
        await self._write_entity(file_path, data, f"Session: {session_id}")

    async def clear_group_id_for_group(self, group_id: str) -> int:
        """delete_group 时把关联会话的 group_id 置 NULL。返回受影响行数。"""
        entities = await self._filter_entities(group_id=group_id)
        count = 0
        for e in entities:
            file_path = self._get_file_path(e["session_id"])
            data = await self._read_entity(file_path)
            if data is None:
                continue
            data["group_id"] = None
            data["updated_at"] = self._to_iso(datetime.now(timezone.utc))
            try:
                await self._write_entity(
                    file_path, data, f"Session: {e['session_id']}",
                )
                count += 1
            except Exception:
                logger.warning(
                    "Failed to clear group_id for session %s",
                    e.get("session_id"),
                    exc_info=True,
                )
        return count

    # ──────────────────────────────────────────────
    # 辅助方法
    # ──────────────────────────────────────────────

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        """解析时间字段为 offset-aware datetime。

        兼容两种存储格式：_to_iso 的本地时间（"%Y-%m-%d %H:%M:%S"）
        与 isoformat 的带时区 ISO 格式。

        naive 值（_to_iso 产出，语义是本地时间）用 astimezone() 补本地时区，
        而非补 UTC——补 UTC 会把本地时间误当 UTC，产生时区偏移，导致
        delete_expired_sessions 的 last_active < before 比较失真。
        """
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            try:
                dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                return None
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt

    @staticmethod
    def _dict_to_model(data: dict[str, Any]) -> Session:
        """将字典转换为 Session 模型。"""

        return Session(
            session_id=data.get("session_id", ""),
            user_id=data.get("user_id", ""),
            device_id=data.get("device_id"),
            last_active=MarkdownSessionRepository._parse_datetime(data.get("last_active")),
            created_at=MarkdownSessionRepository._parse_datetime(data.get("created_at")),
            title=data.get("title", "") or "",
            preview=data.get("preview", "") or "",
            message_count=int(data.get("message_count", 0) or 0),
            is_empty=bool(data.get("is_empty", True)),
            is_favorite=bool(data.get("is_favorite", False)),
            is_deleted=bool(data.get("is_deleted", False)),
            updated_at=MarkdownSessionRepository._parse_datetime(data.get("updated_at")),
            group_id=data.get("group_id"),
        )
