"""ConfigManager 测试。"""

from __future__ import annotations

import pytest

from pandapal.config.system.exceptions import (
    ConfigFileError,
    ConfigLoadError,
    ConfigValidationError,
)
from pandapal.config.system.manager import ConfigManager


@pytest.mark.asyncio
async def test_load_valid_config(valid_env_file):
    """有效环境文件可以正常加载。"""
    cm = ConfigManager(valid_env_file)
    await cm.load_config()

    sys_config = cm.get_system_config()
    assert sys_config.relay_url == "wss://relay.example.com/ws"
    assert sys_config.relay_auth_token == "test-token-123"
    assert sys_config.data_dir == "~/.pandapal"
    # 代码默认值
    assert sys_config.session_timeout_minutes == 60
    assert sys_config.hitl_timeout_seconds == 600
    assert sys_config.screen_control_enabled is False


@pytest.mark.asyncio
async def test_load_missing_env_file_raises(config_dir):
    """.env.development 不存在时抛出 ConfigFileError。"""
    cm = ConfigManager(config_dir)

    with pytest.raises(ConfigFileError):
        await cm.load_config()


@pytest.mark.asyncio
async def test_load_missing_required_fields_raises(tmp_path):
    """必填字段（relay_url、auth_token）缺失时抛出 ConfigLoadError。"""
    env_path = tmp_path / ".env.development"
    env_path.write_text(
        "PANDAPAL_DATA_DIR=~/.pandapal\n",
        encoding="utf-8",
    )

    cm = ConfigManager(str(tmp_path))

    with pytest.raises(ConfigLoadError) as exc_info:
        await cm.load_config()

    error_fields = [
        e.field_name for e in exc_info.value.errors
        if isinstance(e, ConfigValidationError)
    ]
    assert "relay_url" in error_fields
    assert "relay_auth_token" in error_fields


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
