"""Markdown SessionGroup Repository 实现（异步接口）。

接口与 SQLiteSessionGroupRepository 完全对齐：
- create_group
- find_group
- find_group_by_name
- list_groups_by_user
- count_groups_by_user
- rename_group
- delete_group

SQLite 版靠 UNIQUE(user_id, name) DDL 防重名；Markdown 无 DDL 约束，
故在 create_group / rename_group 中以代码层查重，命中时抛
StorageDuplicateError，保持与 SQLite 版一致的语义（上层据此转
GroupNameConflict）。
"""

from __future__ import annotations

import logging
from typing import Any

from pandapal.storage.exceptions import StorageDuplicateError
from pandapal.storage.models import SessionGroup
from pandapal.storage.repositories._markdown_base import MarkdownBaseRepository

logger = logging.getLogger(__name__)


class MarkdownSessionGroupRepository(MarkdownBaseRepository):
    """会话分组持久化操作（Markdown 版）。"""

    def __init__(self, base_dir: str, timeout: float = 5.0) -> None:
        super().__init__(base_dir, "session_groups", timeout)

    # ──────────────────────────────────────────────
    # CRUD 操作（异步接口）
    # ──────────────────────────────────────────────

    async def create_group(self, group: SessionGroup) -> None:
        """新建分组（重名抛 StorageDuplicateError）。"""
        existing = await self.find_group_by_name(group.user_id, group.name)
        if existing is not None:
            raise StorageDuplicateError("SessionGroup", group.name)

        file_path = self._get_file_path(group.id)
        data = {
            "id": group.id,
            "user_id": group.user_id,
            "name": group.name,
            "created_at": self._to_iso(group.created_at) or self._now_iso(),
        }
        await self._write_entity(file_path, data, f"Group: {group.name}")

    async def find_group(self, group_id: str) -> SessionGroup | None:
        """按 group_id 查找。"""
        file_path = self._get_file_path(group_id)
        data = await self._read_entity(file_path)
        return self._dict_to_model(data) if data else None

    async def find_group_by_name(
        self, user_id: str, name: str
    ) -> SessionGroup | None:
        """按 (user_id, name) 查找（用于重名检查）。"""
        entities = await self._filter_entities(user_id=user_id, name=name)
        for data in entities:
            if data:
                return self._dict_to_model(data)
        return None

    async def list_groups_by_user(self, user_id: str) -> list[SessionGroup]:
        """列出用户所有分组（按创建时间升序）。"""
        entities = await self._filter_entities(user_id=user_id)
        entities.sort(key=lambda d: d.get("created_at") or "")
        return [self._dict_to_model(data) for data in entities if data]

    async def count_groups_by_user(self, user_id: str) -> int:
        entities = await self._filter_entities(user_id=user_id)
        return len(entities)

    async def rename_group(self, group_id: str, new_name: str) -> bool:
        """重命名分组。重名抛 StorageDuplicateError；不存在返回 False。"""
        file_path = self._get_file_path(group_id)
        data = await self._read_entity(file_path)
        if data is None:
            return False

        # 同用户下重名检查（排除自身）
        clash = await self.find_group_by_name(data.get("user_id", ""), new_name)
        if clash is not None and clash.id != group_id:
            raise StorageDuplicateError("SessionGroup", new_name)

        data["name"] = new_name
        await self._write_entity(file_path, data, f"Group: {new_name}")
        return True

    async def delete_group(self, group_id: str) -> bool:
        """删除分组（幂等）。返回是否命中一行。"""
        file_path = self._get_file_path(group_id)
        return await self._delete_entity(file_path)

    # ──────────────────────────────────────────────
    # 内部转换
    # ──────────────────────────────────────────────

    @staticmethod
    def _dict_to_model(data: dict[str, Any]) -> SessionGroup:
        return SessionGroup(
            id=data.get("id", ""),
            user_id=data.get("user_id", ""),
            name=data.get("name", ""),
            created_at=MarkdownBaseRepository._from_iso(data.get("created_at")),
        )
