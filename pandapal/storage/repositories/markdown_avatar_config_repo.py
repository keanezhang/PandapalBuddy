"""Markdown AvatarConfig Repository 实现（异步接口）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pandapal.storage.models import AvatarConfig
from pandapal.storage.repositories._markdown_base import MarkdownBaseRepository


class MarkdownAvatarConfigRepository(MarkdownBaseRepository):
    """Markdown Avatar 配置持久化操作（异步接口）。"""

    def __init__(self, base_dir: str, timeout: float = 5.0) -> None:
        super().__init__(base_dir, "avatar_configs", timeout)

    # ──────────────────────────────────────────────
    # CRUD 操作（异步接口）
    # ──────────────────────────────────────────────

    async def save_avatar_config(self, config: AvatarConfig) -> None:
        """保存角色配置。"""
        file_path = self._get_file_path(config.avatar_id)
        data = {
            "avatar_id": config.avatar_id,
            "user_id": config.user_id,
            "name": config.name,
            "system_prompt": config.system_prompt,
            "tools_json": config.tools_json,
            "model_config_json": config.model_config_json,
            "created_at": self._to_iso(config.created_at) or self._now_iso(),
            "is_active": config.is_active,
        }
        title = f"Avatar: {config.name}"
        await self._write_entity(file_path, data, title)

    async def find_avatar_config(self, avatar_id: str) -> AvatarConfig | None:
        """按 avatar_id 查找角色配置。"""
        file_path = self._get_file_path(avatar_id)
        data = await self._read_entity(file_path)
        return self._dict_to_model(data) if data else None

    async def find_avatar_configs_by_user(self, user_id: str) -> list[AvatarConfig]:
        """按 user_id 查找所有角色配置。"""
        entities = await self._filter_entities(user_id=user_id)
        return [self._dict_to_model(data) for data in entities if data]

    async def delete_avatar_config(self, avatar_id: str) -> None:
        """删除角色配置。"""
        file_path = self._get_file_path(avatar_id)
        await self._delete_entity(file_path)

    # ──────────────────────────────────────────────
    # 辅助方法
    # ──────────────────────────────────────────────

    @staticmethod
    def _dict_to_model(data: dict[str, Any]) -> AvatarConfig:
        """将字典转换为 AvatarConfig 模型。"""

        def parse_datetime(value):
            if not value:
                return None
            try:
                return datetime.fromisoformat(value)
            except (ValueError, TypeError):
                return None

        return AvatarConfig(
            avatar_id=data.get("avatar_id", ""),
            user_id=data.get("user_id", ""),
            name=data.get("name", ""),
            system_prompt=data.get("system_prompt", ""),
            tools_json=data.get("tools_json"),
            model_config_json=data.get("model_config_json"),
            created_at=parse_datetime(data.get("created_at")),
            is_active=data.get("is_active", True),
        )
