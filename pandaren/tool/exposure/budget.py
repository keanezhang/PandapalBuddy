"""pandaren/tool/exposure/budget.py — 工具 schema Token 预算控制。

从原 tool_budget.py 迁移。
"""

from __future__ import annotations

import json
import logging

from ..definition.tool_schema import ToolSchema
from ...constants import DEFAULT_CONTEXT_WINDOW, DEFAULT_TOOL_SCHEMA_RATIO

# token 估算属「计费/预算类」：兜底绝不静默（§九金额类留痕硬要求）——
# 估算失败回落 _FALLBACK_TOKEN_ESTIMATE 会影响工具暴露预算裁剪，必须 warning 留痕。
logger = logging.getLogger(__name__)

# 模块级默认值
DEFAULT_MAX_ALWAYS_COUNT: int = 15
DEFAULT_MAX_DISCOVERED: int = 20

_BYTES_PER_TOKEN: int = 4
_FALLBACK_TOKEN_ESTIMATE: int = 100


class ToolBudget:
    """工具 schema 的 token 预算管理。"""

    def __init__(
        self,
        budget_ratio: float = DEFAULT_TOOL_SCHEMA_RATIO,
        max_always_count: int = DEFAULT_MAX_ALWAYS_COUNT,
        max_discovered_per_session: int = DEFAULT_MAX_DISCOVERED,
    ) -> None:
        self.budget_ratio = budget_ratio
        self.max_always_count = max_always_count
        self.max_discovered_per_session = max_discovered_per_session

    def enforce(
        self,
        schemas: list[ToolSchema],
        *,
        tool_schema_tokens: int | None = None,
    ) -> list[ToolSchema]:
        """强制 token 预算。超出时裁剪 DEFERRED 已发现工具。

        裁剪策略：从末尾开始裁剪（DEFERRED 已发现在后面）。
        """
        if tool_schema_tokens is not None:
            budget_tokens = tool_schema_tokens
        else:
            budget_tokens = int(DEFAULT_CONTEXT_WINDOW * self.budget_ratio)

        total_tokens = sum(self._estimate_tokens(s) for s in schemas)

        if total_tokens <= budget_tokens:
            return schemas

        # 防御性拷贝
        schemas = list(schemas)

        while total_tokens > budget_tokens and len(schemas) > self.max_always_count:
            removed = schemas.pop()
            total_tokens -= self._estimate_tokens(removed)

        return schemas

    def _estimate_tokens(self, schema: ToolSchema) -> int:
        """估算单个 ToolSchema 的 token 数。"""
        try:
            text = json.dumps({
                "name": schema.name,
                "description": schema.description,
                "parameters": schema.parameters,
            }, ensure_ascii=False)
            byte_len = len(text.encode("utf-8"))
            return max(1, byte_len // _BYTES_PER_TOKEN)
        except Exception:
            logger.warning(
                "token 估算失败，回落默认值 %d（计费类兜底，见静默降级审计 §2.2）：schema=%s",
                _FALLBACK_TOKEN_ESTIMATE, getattr(schema, "name", "?"), exc_info=True,
            )
            return _FALLBACK_TOKEN_ESTIMATE
