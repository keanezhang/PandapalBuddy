"""Session Repository 实现。

D3: 提供 batch 方法 find_sessions_by_user（避免 N+1）。
I3: save 操作使用 UPSERT（幂等）。

v003 扩展（SessionListManager 支持）：
- 新增字段：title / preview / message_count / is_empty / is_deleted /
            updated_at / group_id
- 新增方法：list_visible_sessions / soft_delete_session / hard_delete_empty_sessions /
            find_current_empty_session / update_session_meta /
            count_visible_sessions / find_oldest_visible

⚠️ 语义共存：SessionManager 用 last_active（消息超时），SessionListManager
   用 created_at（列表排序）。updated_at 与 last_active 写入路径同步以防漂移。
"""

from __future__ import annotations

from datetime import datetime

import aiosqlite

from pandapal.storage.models import Session
from pandapal.storage.repositories._sqlite_base import BaseRepository


class SessionRepository(BaseRepository):
    """会话持久化操作。"""

    def __init__(self, conn: aiosqlite.Connection, timeout: float = 5.0) -> None:
        super().__init__(conn, timeout)

    # ── 全字段选择列表（内部复用，避免 SELECT * 与列漂移）──
    _SESSION_COLUMNS = (
        "session_id, user_id, device_id, last_active, created_at, "
        "title, preview, message_count, is_empty, is_deleted, "
        "updated_at, group_id"
    )

    async def find_session(self, session_id: str) -> Session | None:
        """按 session_id 查找会话。不存在返回 None。"""
        row = await self._fetchone(
            f"SELECT {self._SESSION_COLUMNS} FROM sessions WHERE session_id = ?",
            (session_id,),
            operation="find_session",
        )
        if row is None:
            return None
        return self._row_to_model(row)

    async def find_sessions_by_user(self, user_id: str) -> list[Session]:
        """按 user_id 批量查找所有会话（D3 No N+1）。"""
        rows = await self._fetchall(
            f"SELECT {self._SESSION_COLUMNS} FROM sessions "
            f"WHERE user_id = ? ORDER BY last_active DESC",
            (user_id,),
            operation="find_sessions_by_user",
        )
        return [self._row_to_model(row) for row in rows]

    async def save_session(self, session: Session) -> None:
        """保存会话（UPSERT by session_id，幂等）。

        写入时 updated_at 同步 last_active（防漂移）。
        """
        updated_at = session.updated_at or session.last_active
        await self._execute(
            "INSERT OR REPLACE INTO sessions "
            "(session_id, user_id, device_id, last_active, created_at, "
            " title, preview, message_count, is_empty, is_deleted, "
            " updated_at, group_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session.session_id,
                session.user_id,
                session.device_id,
                self._to_iso(session.last_active),
                self._to_iso(session.created_at),
                session.title,
                session.preview,
                session.message_count,
                1 if session.is_empty else 0,
                1 if session.is_deleted else 0,
                self._to_iso(updated_at),
                session.group_id,
            ),
            operation="save_session",
        )
        await self._commit()

    async def update_session_last_active(
        self, session_id: str, timestamp: datetime
    ) -> None:
        """更新会话最后活跃时间（高频操作）。

        同步更新 updated_at 防漂移（BL2/BL3：语义分层但存储同源）。
        """
        iso = timestamp.isoformat()
        await self._execute(
            "UPDATE sessions SET last_active = ?, updated_at = ? "
            "WHERE session_id = ?",
            (iso, iso, session_id),
            operation="update_session_last_active",
        )
        await self._commit()

    async def delete_expired_sessions(self, before: datetime) -> int:
        """删除过期会话（batch cleanup）。返回删除行数。

        SessionManager 使用；只按 last_active 过滤，忽略 UI 元数据。
        """
        cursor = await self._execute(
            "DELETE FROM sessions WHERE last_active < ?",
            (before.isoformat(),),
            operation="delete_expired_sessions",
        )
        await self._commit()
        return cursor.rowcount  # type: ignore[return-value]

    async def delete_session(self, session_id: str) -> None:
        """硬删除指定会话（幂等，不存在也不报错）。

        SessionManager 使用；SessionListManager 走 soft_delete_session。
        """
        await self._execute(
            "DELETE FROM sessions WHERE session_id = ?",
            (session_id,),
            operation="delete_session",
        )
        await self._commit()

    # ═════════════════════════════════════════════════════════════════
    # SessionListManager 支持方法（v003）
    # ═════════════════════════════════════════════════════════════════

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
        offset = max(0, (page - 1) * limit)
        args: list = [user_id]
        group_filter = ""
        if group_id is None:
            pass  # 不过滤
        elif group_id == "":
            group_filter = " AND group_id IS NULL"
        else:
            group_filter = " AND group_id = ?"
            args.append(group_id)

        sql = (
            f"SELECT {self._SESSION_COLUMNS} FROM sessions "
            f"WHERE user_id = ? AND is_empty = 0 AND is_deleted = 0"
            f"{group_filter} "
            f"ORDER BY created_at DESC "
            f"LIMIT ? OFFSET ?"
        )
        args.extend([limit + 1, offset])
        rows = await self._fetchall(
            sql,
            tuple(args),
            operation="list_visible_sessions",
        )
        has_more = len(rows) > limit
        rows_take = rows[:limit]
        return [self._row_to_model(r) for r in rows_take], has_more

    async def list_visible_sessions_by_ids(
        self,
        user_id: str,
        session_ids: list[str],
        page: int,
        limit: int,
    ) -> tuple[list[Session], bool]:
        """按 session_id 列表精准查可见会话（正向记录快路径）。

        用于「加载某分组」：先用 group 的正向记录拿 id 列表，再按 id 精准查，
        避免全表按 group_id 过滤。

        Args:
            session_ids: 组内会话 id 列表（正向记录）
            page / limit: 分页

        Returns:
            (sessions, has_more)
        """
        if not session_ids:
            return [], False
        offset = max(0, (page - 1) * limit)
        placeholders = ", ".join("?" for _ in session_ids)
        sql = (
            f"SELECT {self._SESSION_COLUMNS} FROM sessions "
            f"WHERE user_id = ? AND is_empty = 0 AND is_deleted = 0 "
            f"AND session_id IN ({placeholders}) "
            f"ORDER BY created_at DESC LIMIT ? OFFSET ?"
        )
        args: list = [user_id, *session_ids, limit + 1, offset]
        rows = await self._fetchall(
            sql,
            tuple(args),
            operation="list_visible_sessions_by_ids",
        )
        has_more = len(rows) > limit
        return [self._row_to_model(r) for r in rows[:limit]], has_more

    async def search_by_title(
        self, user_id: str, query: str, limit: int = 15
    ) -> list[Session]:
        """按标题关键词模糊搜索可见会话（命令面板 ⌘K）。

        LIKE 匹配，转义 % _ \\ 通配符防注入；仅 is_empty=0 AND is_deleted=0。
        排序：created_at DESC（创建时间倒序）。
        """
        q = query.strip()
        if not q:
            return []
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        rows = await self._fetchall(
            f"SELECT {self._SESSION_COLUMNS} FROM sessions "
            f"WHERE user_id = ? AND is_empty = 0 AND is_deleted = 0 "
            f"AND title LIKE ? ESCAPE '\\' "
            f"ORDER BY created_at DESC "
            f"LIMIT ?",
            (user_id, pattern, limit),
            operation="search_by_title",
        )
        return [self._row_to_model(row) for row in rows]

    async def find_current_empty_session(
        self, user_id: str
    ) -> Session | None:
        """查询用户当前的空会话（节流复用用）。

        WHERE is_empty=1 AND is_deleted=0 LIMIT 1
        """
        row = await self._fetchone(
            f"SELECT {self._SESSION_COLUMNS} FROM sessions "
            f"WHERE user_id = ? AND is_empty = 1 AND is_deleted = 0 "
            f"LIMIT 1",
            (user_id,),
            operation="find_current_empty_session",
        )
        if row is None:
            return None
        return self._row_to_model(row)

    async def count_visible_sessions(self, user_id: str) -> int:
        """统计用户可见会话数（is_empty=0 AND is_deleted=0）。"""
        row = await self._fetchone(
            "SELECT COUNT(*) FROM sessions "
            "WHERE user_id = ? AND is_empty = 0 AND is_deleted = 0",
            (user_id,),
            operation="count_visible_sessions",
        )
        return int(row[0]) if row else 0

    async def find_oldest_visible(
        self, user_id: str, exclude_session_id: str | None = None
    ) -> Session | None:
        """查找最旧的可见会话（用于 evict_oldest）。

        排序：updated_at ASC, created_at ASC
        WHERE is_empty=0 AND is_deleted=0 AND session_id != exclude_session_id
        """
        args: list = [user_id]
        exclude_sql = ""
        if exclude_session_id:
            exclude_sql = " AND session_id != ?"
            args.append(exclude_session_id)
        row = await self._fetchone(
            f"SELECT {self._SESSION_COLUMNS} FROM sessions "
            f"WHERE user_id = ? AND is_empty = 0 AND is_deleted = 0"
            f"{exclude_sql} "
            f"ORDER BY updated_at ASC, created_at ASC LIMIT 1",
            tuple(args),
            operation="find_oldest_visible",
        )
        if row is None:
            return None
        return self._row_to_model(row)

    async def soft_delete_session(self, session_id: str) -> bool:
        """软删除：is_deleted=1。返回是否命中一行。

        幂等：多次调用不报错，行数只在首次为 1。
        """
        cursor = await self._execute(
            "UPDATE sessions SET is_deleted = 1, updated_at = ? "
            "WHERE session_id = ? AND is_deleted = 0",
            (self._now_iso(), session_id),
            operation="soft_delete_session",
        )
        await self._commit()
        return (cursor.rowcount or 0) > 0

    async def hard_delete_empty_sessions(self, user_id: str) -> int:
        """硬删除用户所有 is_empty=1 的会话（startup 清 is_empty 遗留）。

        Returns:
            实际删除的行数
        """
        cursor = await self._execute(
            "DELETE FROM sessions WHERE user_id = ? AND is_empty = 1",
            (user_id,),
            operation="hard_delete_empty_sessions",
        )
        await self._commit()
        return cursor.rowcount or 0

    async def update_session_meta(
        self,
        session_id: str,
        *,
        title: str | None = None,
        preview: str | None = None,
        message_count: int | None = None,
        is_empty: bool | None = None,
        group_id: str | None = None,
        group_id_touched: bool = False,
        touch_updated_at: bool = True,
    ) -> bool:
        """通用元数据更新（只更传入的非 None 字段）。

        Args:
            group_id_touched: True 时即使 group_id=None 也 SET group_id=NULL；
                              False 时 group_id 参数被忽略（用于跳过分组更新）
            touch_updated_at: True 时同时更新 updated_at + last_active

        Returns:
            是否命中一行
        """
        fields: list[str] = []
        args: list = []
        if title is not None:
            fields.append("title = ?")
            args.append(title)
        if preview is not None:
            fields.append("preview = ?")
            args.append(preview)
        if message_count is not None:
            fields.append("message_count = ?")
            args.append(message_count)
        if is_empty is not None:
            fields.append("is_empty = ?")
            args.append(1 if is_empty else 0)
        if group_id_touched:
            fields.append("group_id = ?")
            args.append(group_id)
        if touch_updated_at:
            now = self._now_iso()
            fields.append("updated_at = ?")
            fields.append("last_active = ?")
            args.append(now)
            args.append(now)
        if not fields:
            return False
        args.append(session_id)
        cursor = await self._execute(
            f"UPDATE sessions SET {', '.join(fields)} WHERE session_id = ?",
            tuple(args),
            operation="update_session_meta",
        )
        await self._commit()
        return (cursor.rowcount or 0) > 0

    async def increment_message_count(
        self, session_id: str, delta: int
    ) -> None:
        """原子增加 message_count 并刷 updated_at。"""
        now = self._now_iso()
        await self._execute(
            "UPDATE sessions "
            "SET message_count = message_count + ?, "
            "updated_at = ?, last_active = ? "
            "WHERE session_id = ?",
            (delta, now, now, session_id),
            operation="increment_message_count",
        )
        await self._commit()

    async def clear_group_id_for_group(self, group_id: str) -> int:
        """delete_group 时把关联会话的 group_id 置 NULL。返回受影响行数。"""
        cursor = await self._execute(
            "UPDATE sessions SET group_id = NULL WHERE group_id = ?",
            (group_id,),
            operation="clear_group_id_for_group",
        )
        await self._commit()
        return cursor.rowcount or 0

    # ═════════════════════════════════════════════════════════════════
    # 内部转换
    # ═════════════════════════════════════════════════════════════════

    @staticmethod
    def _row_to_model(row: tuple) -> Session:
        """将数据库行转换为 Session 模型。"""
        updated_at_raw = row[10] if len(row) > 10 else None
        return Session(
            session_id=row[0],
            user_id=row[1],
            device_id=row[2],
            last_active=BaseRepository._from_iso(row[3]),  # type: ignore[arg-type]
            created_at=BaseRepository._from_iso(row[4]),   # type: ignore[arg-type]
            title=row[5] if len(row) > 5 else "",
            preview=row[6] if len(row) > 6 else "",
            message_count=int(row[7]) if len(row) > 7 else 0,
            is_empty=bool(row[8]) if len(row) > 8 else True,
            is_deleted=bool(row[9]) if len(row) > 9 else False,
            updated_at=(
                BaseRepository._from_iso(updated_at_raw)
                if updated_at_raw else None
            ),
            group_id=row[11] if len(row) > 11 else None,
        )
