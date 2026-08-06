"""Session Group Repository 实现。

分组是 UI 会话的自定义分类，1:1 绑定 sessions.group_id。
UNIQUE(user_id, name) 由 DDL 层保证防重名。

设计约束：
- D2: 方法名反映业务意图（find_group_by_name / list_groups_by_user）
- I3: save/delete 幂等
"""

from __future__ import annotations

import sqlite3

import aiosqlite

from pandapal.storage.exceptions import StorageDuplicateError
from pandapal.storage.models import SessionGroup
from pandapal.storage.repositories._sqlite_base import BaseRepository


class SessionGroupRepository(BaseRepository):
    """会话分组持久化操作。"""

    _COLUMNS = "id, user_id, name, created_at"

    def __init__(self, conn: aiosqlite.Connection, timeout: float = 5.0) -> None:
        super().__init__(conn, timeout)

    async def create_group(self, group: SessionGroup) -> None:
        """新建分组（INSERT，重名抛 StorageDuplicateError）。"""
        try:
            await self._execute(
                "INSERT INTO session_groups (id, user_id, name, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    group.id,
                    group.user_id,
                    group.name,
                    self._to_iso(group.created_at),
                ),
                operation="create_group",
            )
            await self._commit()
        except sqlite3.IntegrityError as e:
            raise StorageDuplicateError("SessionGroup", group.name) from e

    async def find_group(self, group_id: str) -> SessionGroup | None:
        """按 group_id 查找。"""
        row = await self._fetchone(
            f"SELECT {self._COLUMNS} FROM session_groups WHERE id = ?",
            (group_id,),
            operation="find_group",
        )
        return self._row_to_model(row) if row else None

    async def find_group_by_name(
        self, user_id: str, name: str
    ) -> SessionGroup | None:
        """按 (user_id, name) 查找（用于重名检查）。"""
        row = await self._fetchone(
            f"SELECT {self._COLUMNS} FROM session_groups "
            f"WHERE user_id = ? AND name = ?",
            (user_id, name),
            operation="find_group_by_name",
        )
        return self._row_to_model(row) if row else None

    async def list_groups_by_user(self, user_id: str) -> list[SessionGroup]:
        """列出用户所有分组（按创建时间升序）。"""
        rows = await self._fetchall(
            f"SELECT {self._COLUMNS} FROM session_groups "
            f"WHERE user_id = ? ORDER BY created_at ASC",
            (user_id,),
            operation="list_groups_by_user",
        )
        return [self._row_to_model(r) for r in rows]

    async def count_groups_by_user(self, user_id: str) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) FROM session_groups WHERE user_id = ?",
            (user_id,),
            operation="count_groups_by_user",
        )
        return int(row[0]) if row else 0

    async def rename_group(self, group_id: str, new_name: str) -> bool:
        """重命名分组。重名抛 StorageDuplicateError；不存在返回 False。"""
        try:
            cursor = await self._execute(
                "UPDATE session_groups SET name = ? WHERE id = ?",
                (new_name, group_id),
                operation="rename_group",
            )
            await self._commit()
            return (cursor.rowcount or 0) > 0
        except sqlite3.IntegrityError as e:
            raise StorageDuplicateError("SessionGroup", new_name) from e

    async def delete_group(self, group_id: str) -> bool:
        """删除分组（幂等）。返回是否命中一行。"""
        cursor = await self._execute(
            "DELETE FROM session_groups WHERE id = ?",
            (group_id,),
            operation="delete_group",
        )
        await self._commit()
        return (cursor.rowcount or 0) > 0

    # ─────────────────────────────────────────────────────
    # 内部转换
    # ─────────────────────────────────────────────────────

    @staticmethod
    def _row_to_model(row: tuple) -> SessionGroup:
        return SessionGroup(
            id=row[0],
            user_id=row[1],
            name=row[2],
            created_at=BaseRepository._from_iso(row[3]),  # type: ignore[arg-type]
        )
