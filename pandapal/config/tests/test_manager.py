"""ConfigManager 测试。"""

from __future__ import annotations

import pytest

from pandapal.config.system.exceptions import (
    ConfigFileError,
    ConfigValidationError,
)
from pandapal.config.system.manager import ConfigManager


@pytest.mark.asyncio
async def test_load_missing_env_file_raises(config_dir):
    """.env.development 不存在时抛出 ConfigFileError。"""
    cm = ConfigManager(config_dir)

    with pytest.raises(ConfigFileError):
        await cm.load_config()


@pytest.mark.asyncio
async def test_get_system_config_before_init_raises(config_dir):
    """未初始化时获取配置抛出 RuntimeError。"""
    cm = ConfigManager(config_dir)
    with pytest.raises(RuntimeError, match="未初始化"):
        cm.get_system_config()


# ──────────────────────────────────────────────
# load_config 失败后状态保持 None
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_failure_preserves_none_state(config_dir):
    """load_config 失败后 _system_config 仍为 None。"""
    cm = ConfigManager(config_dir)

    with pytest.raises(ConfigFileError):
        await cm.load_config()

    # 失败后 get_system_config 应仍 raise RuntimeError（不是返回空壳）
    with pytest.raises(RuntimeError, match="未初始化"):
        cm.get_system_config()


# ──────────────────────────────────────────────
# URL 脱敏
# ──────────────────────────────────────────────


def test_sanitize_url_hides_credentials():
    """URL 中嵌入的凭证被脱敏。"""
    sanitized = ConfigManager._sanitize_url_for_display(
        "wss://admin:secret@relay.example.com/ws"
    )
    assert "secret" not in sanitized
    assert "admin" not in sanitized
    assert "***" in sanitized

    # 普通 URL 不变
    normal = ConfigManager._sanitize_url_for_display("http://example.com/path")
    assert "example.com" in normal


# ──────────────────────────────────────────────
# ConfigValidationError repr 脱敏
# ──────────────────────────────────────────────


def test_config_validation_error_repr_masks_sensitive():
    """__repr__ 对敏感字段的 value 做 mask。"""
    error = ConfigValidationError(
        field_name="llm_api_key",
        value="sk-secret-1234567890",
        reason="test",
        suggestion="test",
    )
    repr_str = repr(error)
    assert "sk-secret" not in repr_str
    assert "***" in repr_str


def test_config_validation_error_repr_shows_non_sensitive():
    """__repr__ 对非敏感字段正常显示 value。"""
    error = ConfigValidationError(
        field_name="relay_url",
        value="http://wrong.com",
        reason="格式错误",
        suggestion="use wss://",
    )
    repr_str = repr(error)
    assert "http://wrong.com" in repr_str
