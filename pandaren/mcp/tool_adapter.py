"""pandaren/mcp/tool_adapter.py — MCP 工具 → pandaren Tool 适配。

设计依据：docs/design/mcp-capability.md（§4 D1/D2/D4/D5、§6.1 tool_adapter.py、§9.1）。

一个 ``McpToolDescriptor`` → 一个 ``Tool``：
  name        = f"{server.slug}__{mcp_tool}"   → full_name = f"mcp_{slug}__{mcp_tool}"
  executor    = 委托 ``McpSessionHandle.call_tool``
  policy      = 基于 sensitivity 分级（决定 read_only / is_reversible / audit / HITL）

Sensitivity 判定优先级（§9.1；2026-07 用户裁决「annotations 只升不降」，取代原「annotations 优先」）：
  1. 名称启发式（**下界**：变更类动词 → CRITICAL；只读类动词 → LOW；其余 → MEDIUM）
  2. MCP annotations **只升不降**（`destructiveHint` → 提升为 CRITICAL；`readOnlyHint`
     **不参与降级**——MCP server 是外部不可信来源，其自报「只读」不足以作为免审批
     依据；确有把握要降级请用 `safe_tools` 显式声明，可留痕可追溯）
  3. server 配置覆盖（high_risk_tools / safe_tools，最终裁定权）

为何名称启发式**不设 HIGH 档**：本项目 `pandapal/local/run_local.py` 设
``auto_confirm_high=True``（§2.4 / §9.1），即 HIGH 会被**自动放行**。若把
"write / create / update / 修改类"动词判为 HIGH，等于把有副作用的工具静默降级为
「无审批 + 无门禁」——正是项目 §九 明令禁止的静默降级。故变更类动词一律判
CRITICAL（强制 HITL，HC6）；用户确有把握要放行，用 ``safe_tools`` 显式降级
（可留痕、可追溯），而不是靠启发式猜。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..identity.models import TrustLevel
from ..tool.definition.tool import Tool
from ..tool.definition.tool_policy import ToolPolicy
from ..tool.definition.tool_result import ToolResult
from ..tool.types import SensitivityLevel
from .client import McpSessionHandle, McpToolDescriptor
from .config import McpServerConfig

logger = logging.getLogger("pandaren.mcp.tool_adapter")

#: MCP 工具默认输出上限（超长截断在 executor 层统一执行）。
MCP_TOOL_MAX_OUTPUT_BYTES = 128_000

#: 名称启发式词表（敏感度判定的**下界**；token 精确匹配，避免 "target" 命中 "get"）。
#: 变更 / 破坏 / 外发 / 执行类动词一律 CRITICAL（设计 §9.1 词表 + 同类收敛，见模块 docstring）。
_CRITICAL_KEYWORDS: frozenset[str] = frozenset({
    # 破坏 / 删除
    "delete", "remove", "rm", "drop", "kill", "terminate", "truncate",
    "purge", "destroy", "reset", "uninstall", "revoke", "revert",
    # 执行 / 提权
    "exec", "execute", "shell", "run", "cmd", "sudo", "chmod", "chown",
    # 写入 / 变更
    "write", "create", "update", "modify", "insert", "patch", "put", "post",
    "set", "add", "edit", "apply", "move", "rename", "mkdir", "touch",
    # 外发 / 部署 / 安装 / 付费
    "send", "deploy", "install", "upload", "publish", "grant", "payment", "pay",
})
_LOW_KEYWORDS: frozenset[str] = frozenset({
    "list", "get", "read", "search", "query", "fetch", "describe", "show",
    "info", "status", "count", "find", "lookup", "view", "head", "stat",
    "exists", "ping", "health", "version", "export", "dryrun",
})


def _tokenize(name: str) -> set[str]:
    """把工具名拆成小写 token 集合（支持 snake_case / kebab-case / camelCase）。"""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return {w.lower() for w in re.split(r"[^A-Za-z0-9]+", spaced) if w}


def _get_annotation(annotations: Any, *names: str) -> Any:
    """从 annotations（pydantic 对象或 dict）取字段，兼容 snake / camel 两种命名。"""
    if annotations is None:
        return None
    for name in names:
        if isinstance(annotations, dict):
            if name in annotations:
                return annotations[name]
        elif hasattr(annotations, name):
            return getattr(annotations, name)
    return None


class McpToolAdapter:
    """把某服务器的 MCP 工具描述适配为 pandaren ``Tool``。

    一个 adapter 绑定一个 (server_config, session_handle) 对。
    """

    def __init__(self, config: McpServerConfig, handle: McpSessionHandle) -> None:
        self._cfg = config
        self._handle = handle

    # ── 对外 ──────────────────────────────────────────────

    def adapt(self, descriptor: McpToolDescriptor) -> Tool:
        """构造一个可注册进 ToolRegistry 的 Tool。"""
        sensitivity = self.classify(descriptor)
        read_only = sensitivity == SensitivityLevel.LOW
        # CRITICAL 视为不可逆（不可逆高危 → 强制 HITL）；其余可逆。
        is_reversible = sensitivity != SensitivityLevel.CRITICAL

        full_tool_name = f"mcp_{self._cfg.slug}__{descriptor.name}"
        policy = ToolPolicy(
            sensitivity=sensitivity,
            audit_required=sensitivity >= SensitivityLevel.HIGH,
            is_reversible=is_reversible,
            is_idempotent=read_only,
            trust_level_required=TrustLevel.EXTERNAL,
            max_output_bytes=MCP_TOOL_MAX_OUTPUT_BYTES,
            read_only=read_only,
            # 不声明 sensitive_permission：MCP 工具的副作用无法可靠映射到
            # SensitivePermission 单一枚举（本地 stdio 可能是文件写/代码执行，
            # 远端 http 才是网络调用）。安全门由 sensitivity 驱动——CRITICAL
            # 强制 HITL（见 behavior 层），HIGH 受 auto_confirm_high 控制。
            sensitive_permission=None,
        )

        when_to_use = (
            descriptor.when_to_use
            or descriptor.description
            or f"调用 MCP 服务器 {self._cfg.name} 提供的 {descriptor.name} 工具"
        ).strip()

        return Tool(
            name=f"{self._cfg.slug}__{descriptor.name}",
            description=descriptor.description
            or f"MCP 工具 {descriptor.name}（来自 {self._cfg.name}）",
            executor=self._make_executor(descriptor, full_tool_name),
            policy=policy,
            input_schema=self._safe_schema(descriptor),
            tier=self._cfg.tier,
            when_to_use=when_to_use,
            namespace="mcp",
        )

    def classify(self, descriptor: McpToolDescriptor) -> SensitivityLevel:
        """判定工具敏感度（名称启发为下界 → annotations 只升不降 → 配置覆盖）。

        安全策略（2026-07 用户裁决，取代设计 §9.1 原「annotations 优先」）：
        MCP server 是外部不可信来源，其自报的 ``readOnlyHint`` **不得**作为免审批
        依据；因此 ``readOnlyHint=true`` 不参与降级。只有 ``destructiveHint=true``
        能把等级**提升**为 CRITICAL。名称启发式是下界，配置覆盖
        （``high_risk_tools`` / ``safe_tools``）是最终裁定权。
        """
        level = self._name_heuristic(descriptor.name)

        # annotations 只能升：destructiveHint 提升为 CRITICAL。
        if (
            _get_annotation(
                descriptor.annotations, "destructiveHint", "destructive_hint"
            )
            is True
        ):
            level = SensitivityLevel.CRITICAL

        # readOnlyHint 刻意不参与定级（见 docstring）；降级请走 safe_tools 显式声明。

        # 配置覆盖：最终裁定权（允许用户强改 annotations/启发式结论）。
        if descriptor.name in self._cfg.high_risk_tools:
            level = SensitivityLevel.CRITICAL
        elif descriptor.name in self._cfg.safe_tools:
            level = SensitivityLevel.LOW

        return level

    # ── 内部 ──────────────────────────────────────────────

    @staticmethod
    def _name_heuristic(name: str) -> SensitivityLevel:
        tokens = _tokenize(name)
        if tokens & _CRITICAL_KEYWORDS:
            return SensitivityLevel.CRITICAL
        if tokens & _LOW_KEYWORDS:
            return SensitivityLevel.LOW
        return SensitivityLevel.MEDIUM

    @staticmethod
    def _safe_schema(descriptor: McpToolDescriptor) -> dict[str, Any]:
        schema = descriptor.input_schema
        if isinstance(schema, dict) and schema.get("type"):
            return schema
        return {"type": "object", "properties": {}}

    def _make_executor(self, descriptor: McpToolDescriptor, full_tool_name: str):
        handle = self._handle
        server_name = self._cfg.name
        mcp_tool_name = descriptor.name

        async def _executor(ctx: Any, **kwargs: Any) -> ToolResult:
            # 入口取消检查点（引擎 P2 竞速已在边界兜底，这里做一次廉价的前置短路）。
            token = ctx.metadata.get("cancel_token") if ctx is not None else None
            if token is not None:
                token.raise_if_cancelled()

            result = await handle.call_tool(mcp_tool_name, kwargs)
            result.tool_name = full_tool_name
            if not result.success:
                logger.warning(
                    "MCP 工具 '%s'（server=%s）执行失败：%s",
                    mcp_tool_name, server_name, result.error,
                )
            return result

        return _executor
