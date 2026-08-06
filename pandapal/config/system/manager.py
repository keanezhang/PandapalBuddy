"""ConfigManager — 配置管理核心类。

职责：
- 加载 .env.{env} 文件 → 从 os.environ 构建 SystemConfig（frozen）
- 校验配置完整性（ConfigValidationError 列表）

设计约束：
- I1 (Fail Fast): load_config() 校验不通过则立即抛出
- I4 (Externalized Config): 返回 frozen 对象，敏感字段 masked
- BL1 (Single Responsibility): 只管系统配置（.env 文件），不涉及用户配置
- BL5 (Semantic Exceptions): ConfigValidationError 含 suggestion
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from pandapal.config.system.exceptions import (
    ConfigFileError,
    ConfigLoadError,
    ConfigValidationError,
)
from pandapal.config.system.models import SystemConfig

logger = logging.getLogger(__name__)


class ConfigManager:
    """配置管理器（管理 .env.development → SystemConfig）。

    使用方式：
        config_manager = ConfigManager("/path/to/config/dir")
        await config_manager.load_config()
        sys_config = config_manager.get_system_config()
    """

    def __init__(self, config_dir: str) -> None:
        self._config_dir = Path(config_dir).expanduser()

        # 系统配置缓存（从环境变量构建，frozen 对象）
        self._system_config: SystemConfig | None = None

        # 并发控制
        self._lock = asyncio.Lock()

    # ──────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────

    async def load_config(self) -> None:
        """加载 .env.development → SystemConfig（I1 Fail Fast）。

        Raises:
            ConfigFileError: .env.development 文件不存在。
            ConfigLoadError: 配置校验不通过（含所有字段错误）。
        """
        async with self._lock:
            env_file = self._config_dir / ".env.development"
            if not env_file.exists():
                raise ConfigFileError(
                    str(env_file),
                    f"环境文件不存在：{env_file}\n"
                    f"请创建 .env.development 并配置必填项",
                )

            try:
                from dotenv import load_dotenv
                load_dotenv(env_file)
            except ImportError:
                pass  # dotenv 不可用时依赖外部注入的环境变量

            sys_config = self._build_from_env()
            errors = self._validate_system_config(sys_config)
            if errors:
                raise ConfigLoadError(errors)

            self._system_config = sys_config

        logger.info("Config loaded successfully (dir=%s)", self._config_dir)

    async def reload_config(self) -> None:
        """热重载 .env.development（override=True 覆盖已有值），失败则回滚。"""
        async with self._lock:
            old = self._system_config
            try:
                env_file = self._config_dir / ".env.development"
                if not env_file.exists():
                    raise ConfigFileError(str(env_file), "环境文件不存在")
                from dotenv import load_dotenv
                load_dotenv(env_file, override=True)
                self._system_config = self._build_from_env()
            except Exception:
                self._system_config = old
                raise

    def get_system_config(self) -> SystemConfig:
        """获取系统配置（frozen）。未初始化时 raise RuntimeError。"""
        if self._system_config is None:
            raise RuntimeError(
                "ConfigManager 未初始化，请先调用 load_config()"
            )
        return self._system_config

    # ──────────────────────────────────────────────
    # Private Methods - 配置加载
    # ──────────────────────────────────────────────

    def _build_from_env(self) -> SystemConfig:
        """从 os.environ 构建 SystemConfig。

        只有 PANDAPAL_DATA_DIR / PANDAPAL_RELAY_URL / PANDAPAL_RELAY_AUTH_TOKEN
        从环境变量读取，其余字段使用代码默认值。
        """
        # 存储模式：未识别的值回落 "markdown"（向后兼容 + Fail-Safe，不因拼错停机）
        storage_mode = os.environ.get("PANDAPAL_STORAGE_MODE", "markdown").strip().lower()
        if storage_mode not in ("markdown", "sqlite"):
            logger.warning(
                "PANDAPAL_STORAGE_MODE=%r 非法（仅支持 markdown/sqlite），回落 markdown",
                storage_mode,
            )
            storage_mode = "markdown"

        return SystemConfig(
            relay_url=os.environ.get("PANDAPAL_RELAY_URL", ""),
            relay_auth_token=os.environ.get("PANDAPAL_RELAY_AUTH_TOKEN", ""),
            data_dir=os.environ.get("PANDAPAL_DATA_DIR", ""),
            storage_mode=storage_mode,
            # 以下使用代码默认值，不需要环境变量配置
        )

    def _validate_system_config(
        self, config: SystemConfig
    ) -> list[ConfigValidationError]:
        """校验 SystemConfig，返回所有错误。"""
        errors: list[ConfigValidationError] = []

        # relay_url: 必填，wss:// 开头
        if not config.relay_url:
            errors.append(ConfigValidationError(
                field_name="relay_url",
                value="",
                reason="relay_url 不能为空",
                suggestion="应为 wss:// 开头的 WebSocket 地址，示例：wss://relay.example.com/ws",
            ))
        elif not config.relay_url.startswith("wss://"):
            errors.append(ConfigValidationError(
                field_name="relay_url",
                value=self._sanitize_url_for_display(config.relay_url),
                reason="relay_url 格式错误",
                suggestion="应为 wss:// 开头的 WebSocket 地址，示例：wss://relay.example.com/ws",
            ))

        # relay_auth_token: 必填
        if not config.relay_auth_token:
            errors.append(ConfigValidationError(
                field_name="relay_auth_token",
                value="***",
                reason="relay_auth_token 不能为空",
                suggestion="请从管理后台获取 relay_auth_token",
            ))

        return errors

    @staticmethod
    def _sanitize_url_for_display(url: str) -> str:
        """脱敏 URL，隐藏嵌入的 userinfo（user:password@host）。"""
        try:
            parsed = urlparse(url)
            if parsed.username or parsed.password:
                safe = f"{parsed.scheme}://***@{parsed.hostname or ''}"
                if parsed.port:
                    safe += f":{parsed.port}"
                safe += parsed.path
                return safe
            return url[:50] + ("..." if len(url) > 50 else "")
        except Exception:
            return "<invalid URL>"
