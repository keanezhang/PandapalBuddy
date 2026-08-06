"""Avatar Config Repository 实现。

简单的 UPSERT by user_id 模式。
"""

from __future__ import annotations

import aiosqlite

from pandapal.storage.models import AvatarConfig
from pandapal.storage.repositories._sqlite_base import BaseRepository


class AvatarConfigRepository(BaseRepository):
    """Avatar 配置持久化操作。"""

    def __init__(self, conn: aiosqlite.Connection, timeout: float = 5.0) -> None:
        super().__init__(conn, timeout)

    async def load_avatar_config(self, user_id: str) -> AvatarConfig | None:
        """加载用户的 Avatar 配置。不存在返回 None。"""
        row = await self._fetchone(
            "SELECT user_id, character_name, animation_list_json, "
            "state_animation_map_json, updated_at "
            "FROM avatar_configs WHERE user_id = ?",
            (user_id,),
            operation="load_avatar_config",
        )
        if row is None:
            return None
        return self._row_to_model(row)

    async def save_avatar_config(self, config: AvatarConfig) -> None:
        """保存 Avatar 配置（UPSERT by user_id，幂等）。"""
        now = self._to_iso(config.updated_at) or self._now_iso()
        await self._execute(
            "INSERT OR REPLACE INTO avatar_configs "
            "(user_id, character_name, animation_list_json, "
            "state_animation_map_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                config.user_id,
                config.character_name,
                config.animation_list_json,
                config.state_animation_map_json,
                now,
            ),
            operation="save_avatar_config",
        )
        await self._commit()

    @staticmethod
    def _row_to_model(row: tuple) -> AvatarConfig:
        return AvatarConfig(
            user_id=row[0],
            character_name=row[1],
            animation_list_json=row[2],
            state_animation_map_json=row[3],
            updated_at=BaseRepository._from_iso(row[4]),
        )
