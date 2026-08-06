"""AvatarConfigRepository 测试。"""

from __future__ import annotations

import pytest

from pandapal.storage.models import AvatarConfig


@pytest.mark.asyncio
async def test_save_and_load(memory_storage):
    """保存并加载 Avatar 配置。"""
    repo = memory_storage.get_avatar_config_repo()
    config = AvatarConfig(
        user_id="u1",
        character_name="PandaPal",
        animation_list_json='["wave","smile","nod"]',
        state_animation_map_json='{"happy":"smile","thinking":"nod"}',
    )
    await repo.save_avatar_config(config)
    loaded = await repo.load_avatar_config("u1")

    assert loaded is not None
    assert loaded.character_name == "PandaPal"
    assert loaded.animation_list_json == '["wave","smile","nod"]'


@pytest.mark.asyncio
async def test_load_nonexistent_returns_none(memory_storage):
    """加载不存在的配置返回 None。"""
    repo = memory_storage.get_avatar_config_repo()
    assert await repo.load_avatar_config("nonexistent") is None


@pytest.mark.asyncio
async def test_upsert_overwrites(memory_storage):
    """UPSERT 覆盖已有配置。"""
    repo = memory_storage.get_avatar_config_repo()

    await repo.save_avatar_config(AvatarConfig(
        user_id="u1", character_name="OldName",
    ))
    await repo.save_avatar_config(AvatarConfig(
        user_id="u1", character_name="NewName",
    ))

    loaded = await repo.load_avatar_config("u1")
    assert loaded is not None
    assert loaded.character_name == "NewName"
