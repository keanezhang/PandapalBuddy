"""MCP 服务器配置的持久化（``{USER_DATA_DIR}/mcp/servers.toml``）。

职责边界：本模块只管「配置文件的读 / 写 / 原子替换」，不碰连接与工具注册
（那是 :mod:`pandapal.mcp.manager` 的事）。

文件结构::

    [[servers]]
    name = "fs"
    transport = "stdio"
    command = "npx"
    args = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]

设计约定（对齐 `pandapal/local/TEST_RULE.md` 与健壮性契约 §九）：

- **读取缺失不算错**：文件不存在 → 返回空表（首次运行是常态）。
- **单条非法只跳过该条**：一条坏配置不应吞掉整份文件；跳过时经
  :func:`pandapal.degradation.report_degradation` 留痕。
- **写入原子**：临时文件 + ``os.replace``，绝不产生半成品
  （范式同 ``pandapal/config/llm/credentials_store.py:310``）。
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from pandapal.degradation import report_degradation
from pandaren.mcp.config import McpConfigError, McpServerConfig

logger = logging.getLogger(__name__)

# ── 契约字符串（跨 Python/TOML 键）────────────────────────────────────────
_SERVERS_KEY = "servers"

# 降级 event_code（走 pandapal.degradation 统一通道作主键聚合）
DEGRADE_CONFIG_ITEM_INVALID = "mcp.config_item_invalid"
DEGRADE_CONFIG_PARSE_FAILED = "mcp.config_parse_failed"

# TOML bare key：仅 [A-Za-z0-9_-] 可裸写，其余需引号包裹。
_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class McpConfigStore:
    """``servers.toml`` 的读写门面（无状态，可多次实例化）。"""

    def __init__(self, toml_path: Path | str) -> None:
        self._path = Path(toml_path)

    @property
    def path(self) -> Path:
        """配置文件路径（供日志 / 测试断言）。"""
        return self._path

    # ══════════════════════════════════════════════════════════════════════
    # 读
    # ══════════════════════════════════════════════════════════════════════

    def load_all(self) -> list[McpServerConfig]:
        """读取全部 server 配置。

        - 文件不存在 → ``[]``（不算错）。
        - TOML 解析失败 → ``[]`` + 留痕（不崩）。
        - 单条 ``from_mapping`` 失败 → 跳过该条 + 留痕，其余保留。
        """
        raw = self._read_raw()
        items = raw.get(_SERVERS_KEY, [])
        if not isinstance(items, list):
            report_degradation(
                DEGRADE_CONFIG_ITEM_INVALID,
                category="id",
                source="mcp.config_store.load_all",
                expected="list",
                fallback=type(items).__name__,
            )
            logger.warning(
                "MCP 配置文件 %s 的 [[%s]] 不是数组，忽略：%r",
                self._path, _SERVERS_KEY, type(items).__name__,
            )
            return []

        configs: list[McpServerConfig] = []
        for index, item in enumerate(items):
            try:
                configs.append(McpServerConfig.from_mapping(item))
            except (McpConfigError, TypeError) as exc:
                report_degradation(
                    DEGRADE_CONFIG_ITEM_INVALID,
                    category="id",
                    source="mcp.config_store.load_all",
                    exc_info=True,
                )
                logger.warning(
                    "跳过第 %d 条非法 MCP server 配置（%s）: %s",
                    index, self._path, exc,
                )
        return configs

    # ══════════════════════════════════════════════════════════════════════
    # 写
    # ══════════════════════════════════════════════════════════════════════

    def save(self, cfg: McpServerConfig) -> None:
        """按 ``name`` upsert 单条配置并原子落盘。

        校验不通过（``McpConfigError``）直接抛出，**不落盘**——调用方
        （:class:`pandapal.mcp.manager.McpManager`）负责转成 ERROR 事件。
        """
        cfg.validate()
        ordered: dict[str, McpServerConfig] = {c.name: c for c in self.load_all()}
        ordered[cfg.name] = cfg
        self._write_atomic(self._serialize(list(ordered.values())))

    def delete(self, name: str) -> None:
        """删除指定 ``name`` 的配置（幂等：不存在也照写）并原子落盘。"""
        remaining = [c for c in self.load_all() if c.name != name]
        self._write_atomic(self._serialize(remaining))

    # ══════════════════════════════════════════════════════════════════════
    # Private：文件 I/O
    # ══════════════════════════════════════════════════════════════════════

    def _read_raw(self) -> dict[str, Any]:
        """读取并解析 TOML；缺失 / 解析失败返回空 dict。"""
        if not self._path.exists():
            return {}
        try:
            with open(self._path, "rb") as f:
                data = tomllib.load(f)
        except Exception as exc:  # noqa: BLE001 —— 读取边界：坏文件不应导致崩溃
            report_degradation(
                DEGRADE_CONFIG_PARSE_FAILED,
                category="id",
                source="mcp.config_store._read_raw",
                exc_info=True,
            )
            logger.warning("无法解析 MCP 配置文件 %s: %s", self._path, exc)
            return {}
        return data if isinstance(data, dict) else {}

    def _write_atomic(self, content: str) -> None:
        """原子写入：写临时文件 → ``os.replace``，保证不产生半成品。"""
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            suffix=".toml",
            prefix=".mcp_servers_tmp_",
            dir=str(parent),
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, str(self._path))
            logger.info("MCP 配置已保存到 %s", self._path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ══════════════════════════════════════════════════════════════════════
    # Private：TOML 序列化
    # ══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _serialize(cfgs: list[McpServerConfig]) -> str:
        """把配置列表渲染为 TOML 文本（仅输出非空可选键）。"""
        lines: list[str] = [
            "# MCP 服务器配置 —— 由 PandaPal 自动生成，请通过 UI 编辑。",
            "",
        ]
        for cfg in cfgs:
            lines.append(f"[[{_SERVERS_KEY}]]")
            for key, value in cfg.to_dict().items():
                if value is None or value == [] or value == {}:
                    continue
                lines.append(f"{key} = {_toml_value(value)}")
            lines.append("")
        return "\n".join(lines)


def _toml_key(key: str) -> str:
    """dict 内联表的键：非 bare key 时加引号。"""
    if _BARE_KEY_RE.match(key):
        return key
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_value(value: Any) -> str:
    """把 Python 标量 / 列表 / 内联表渲染成 TOML 字面量。"""
    # bool 是 int 子类，必须先判 bool。
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(
            f"{_toml_key(str(k))} = {_toml_value(v)}" for k, v in value.items()
        )
        return f"{{ {inner} }}" if inner else "{}"
    raise TypeError(f"无法序列化为 TOML 的值类型：{type(value).__name__}")


__all__ = ["McpConfigStore", "DEGRADE_CONFIG_ITEM_INVALID", "DEGRADE_CONFIG_PARSE_FAILED"]
