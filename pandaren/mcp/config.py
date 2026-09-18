"""pandaren/mcp/config.py — MCP 服务器配置模型与校验（fail-fast）。

设计依据：docs/design/mcp-capability.md（§6.1 config.py、§10 失效处理）。

职责边界：
  - 只做「配置数据结构 + 校验」，**不做 IO**（读写 TOML 属应用层
    pandapal/mcp/config_store.py）。
  - **不 import mcp SDK**：本模块可被无 mcp 依赖的环境直接 import / 单测。

降级红线（见项目 §九）：
  transport / command / url 属「决策/身份类」字段，缺失即 McpConfigError（fail-fast），
  绝不回落默认值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from ..tool.types import ToolTier

#: 连接握手 / 单次工具调用的默认超时（秒）。
DEFAULT_CONNECT_TIMEOUT = 20.0
DEFAULT_CALL_TIMEOUT = 60.0


class McpError(Exception):
    """MCP 相关错误基类。"""


class McpConfigError(McpError, ValueError):
    """MCP 配置非法（fail-fast，绝不用默认值糊过去）。"""


class McpConnectError(McpError):
    """MCP 服务器连接 / 握手失败。"""


class McpTransport(str, Enum):
    """MCP 传输方式。"""

    STDIO = "stdio"
    HTTP = "http"


class McpServerStatus(str, Enum):
    """MCP 服务器运行时状态。"""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


# 构造 McpServerConfig 时允许的键（其余视为拼写错误，fail-fast）。
_KNOWN_KEYS: frozenset[str] = frozenset({
    "name", "transport", "enabled", "tier",
    "command", "args", "env", "cwd",
    "url", "headers",
    "connect_timeout", "call_timeout",
    "high_risk_tools", "safe_tools",
})


def _slugify(name: str) -> str:
    """把服务器名转为工具名安全片段（空白 → 下划线）。

    非 ASCII 字符不做替换——Tool 名允许非 ASCII，LLM 侧由
    ``to_safe_name_parts`` 统一 hash 化（见 pandaren/tool/safe_name.py）。
    """
    cleaned = "".join(ch if not ch.isspace() else "_" for ch in name.strip())
    return cleaned or "server"


@dataclass(frozen=True)
class McpServerConfig:
    """单个 MCP 服务器的配置（创建即校验，frozen 不可变）。

    stdio 传输使用 ``command`` / ``args`` / ``env`` / ``cwd``；
    http 传输使用 ``url`` / ``headers``。
    两组字段按 transport 二选一，多余字段被忽略但不报错（便于配置模板复用）。
    """

    name: str
    transport: McpTransport
    enabled: bool = True
    tier: ToolTier = ToolTier.DEFERRED
    command: str | None = None
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    # ── 超时 ──
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    call_timeout: float = DEFAULT_CALL_TIMEOUT

    # ── sensitivity 配置覆盖（最高优先级，见 tool_adapter._classify）──
    high_risk_tools: tuple[str, ...] = ()
    safe_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # transport：接受 str（TOML/IPC 传入），非法值 fail-fast。
        # 注意 McpTransport 是 str 子类，必须先判 isinstance(..., McpTransport)。
        if not isinstance(self.transport, McpTransport):
            try:
                object.__setattr__(self, "transport", McpTransport(self.transport))
            except (ValueError, TypeError):
                raise McpConfigError(
                    f"MCP server '{self.name}' 的 transport 非法：{self.transport!r}，"
                    f"有效值 {[t.value for t in McpTransport]}"
                ) from None

        # tier：接受 ToolTier / int / str（"deferred" / "DEFERRED" / 2）。
        tier = self.tier
        if not isinstance(tier, ToolTier):
            object.__setattr__(self, "tier", _coerce_tier(self.name, tier))

        # 序列字段规范化为 tuple，避免 list 冻结后仍可变。
        for attr in ("args", "high_risk_tools", "safe_tools"):
            val = getattr(self, attr)
            if not isinstance(val, tuple):
                object.__setattr__(self, attr, tuple(val))

        self.validate()

    # ── 校验 ──────────────────────────────────────────────

    def validate(self) -> None:
        """显式校验（幂等）。构造时已自动调用，供反序列化后再核对。"""
        if not isinstance(self.name, str) or not self.name.strip():
            raise McpConfigError("MCP server name 不能为空")

        if not isinstance(self.enabled, bool):
            raise McpConfigError(f"MCP server '{self.name}' 的 enabled 必须是 bool")
        if not self.enabled:
            # 未启用：不校验传输细节（配置可留待启用时补全）。
            return

        if self.transport is McpTransport.STDIO:
            if not isinstance(self.command, str) or not self.command.strip():
                raise McpConfigError(
                    f"MCP server '{self.name}'（stdio）缺少 command"
                )
        else:  # HTTP
            if not isinstance(self.url, str) or not self.url.strip():
                raise McpConfigError(
                    f"MCP server '{self.name}'（http）缺少 url"
                )
            if not (self.url.startswith("http://") or self.url.startswith("https://")):
                raise McpConfigError(
                    f"MCP server '{self.name}' 的 url 必须以 http:// 或 https:// 开头，"
                    f"当前：{self.url}"
                )

        if self.connect_timeout <= 0:
            raise McpConfigError(
                f"MCP server '{self.name}' 的 connect_timeout 必须 > 0"
            )
        if self.call_timeout <= 0:
            raise McpConfigError(
                f"MCP server '{self.name}' 的 call_timeout 必须 > 0"
            )

    # ── 派生 ──────────────────────────────────────────────

    @property
    def slug(self) -> str:
        """工具名安全片段（作 ``name`` 前缀）。"""
        return _slugify(self.name)

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 友好 dict（供 IPC / TOML 写出）。"""
        return {
            "name": self.name,
            "transport": self.transport.value,
            "enabled": self.enabled,
            "tier": self.tier.name.lower(),
            "command": self.command,
            "args": list(self.args),
            "env": dict(self.env),
            "cwd": self.cwd,
            "url": self.url,
            "headers": dict(self.headers),
            "connect_timeout": self.connect_timeout,
            "call_timeout": self.call_timeout,
            "high_risk_tools": list(self.high_risk_tools),
            "safe_tools": list(self.safe_tools),
        }

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "McpServerConfig":
        """从 dict 构造（缺失必填项 fail-fast；未知键视为拼写错误）。"""
        if not isinstance(mapping, Mapping):
            raise McpConfigError(
                f"McpServerConfig.from_mapping 期望 Mapping，收到 {type(mapping).__name__}"
            )
        unknown = set(mapping) - _KNOWN_KEYS
        if unknown:
            raise McpConfigError(f"MCP server 配置含未知字段：{sorted(unknown)}")
        if not mapping.get("name"):
            raise McpConfigError("MCP server 配置缺少 name")
        if not mapping.get("transport"):
            raise McpConfigError(
                f"MCP server '{mapping.get('name')}' 配置缺少 transport"
            )
        return cls(**dict(mapping))


def _coerce_tier(server_name: str, value: Any) -> ToolTier:
    """把配置里的 tier 归一化为 ToolTier（int / "deferred" / "DEFERRED"）。"""
    # bool 是 int 子类，必须先行拒绝（True/False 不应视作 1/0）。
    if isinstance(value, bool):
        raise McpConfigError(
            f"MCP server '{server_name}' 的 tier 类型非法：{type(value).__name__}"
        )
    if isinstance(value, int):
        try:
            return ToolTier(value)
        except ValueError:
            raise McpConfigError(
                f"MCP server '{server_name}' 的 tier 非法：{value}，"
                f"有效值 {[t.value for t in ToolTier]}"
            ) from None
    if isinstance(value, str):
        try:
            return ToolTier[value.strip().upper()]
        except KeyError:
            raise McpConfigError(
                f"MCP server '{server_name}' 的 tier 非法：{value!r}，"
                f"有效值 {[t.name.lower() for t in ToolTier]}"
            ) from None
    raise McpConfigError(
        f"MCP server '{server_name}' 的 tier 类型非法：{type(value).__name__}"
    )
