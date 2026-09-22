"""pandaren/tool/exposure/budget.py — 工具 schema Token 预算（熔断线）控制。

从原 tool_budget.py 迁移。

语义：``tool_schema_max_tokens`` 是**熔断线**（绝对 token 上限），
与 ``ContextWindowBudget.tool_cap_tokens`` 同一把尺子（见 COMPACT_BUDGET_LAYERING_SPEC §2.3）。
"""

from __future__ import annotations

import json
import logging

from ..definition.tool_schema import ToolSchema
from ...memory.protocols import CharBasedTokenEstimator, TokenEstimator

# token 估算属「计费/预算类」：兜底绝不静默（§九金额类留痕硬要求）——
# 估算失败回落 _FALLBACK_TOKEN_ESTIMATE 会影响工具暴露预算裁剪，必须 warning 留痕。
logger = logging.getLogger(__name__)

# 模块级默认值
DEFAULT_MAX_ALWAYS_COUNT: int = 15
DEFAULT_MAX_DISCOVERED: int = 20

_FALLBACK_TOKEN_ESTIMATE: int = 100


def _sdk_fallback_tool_cap() -> int:
    """缺省熔断线 = SDK 兜底预算对象的 tool_cap（延迟 import，避免循环依赖）。"""
    from ...behavior.context_window_budget import SDK_FALLBACK_BUDGET

    return SDK_FALLBACK_BUDGET.tool_cap_tokens


class ToolBudget:
    """工具 schema 的 token 熔断线管理。"""

    def __init__(
        self,
        tool_schema_max_tokens: int | None = None,
        max_always_count: int = DEFAULT_MAX_ALWAYS_COUNT,
        max_discovered_per_session: int = DEFAULT_MAX_DISCOVERED,
        token_estimator: TokenEstimator | None = None,
    ) -> None:
        if tool_schema_max_tokens is None:
            tool_schema_max_tokens = _sdk_fallback_tool_cap()
        if not isinstance(tool_schema_max_tokens, int) or tool_schema_max_tokens <= 0:
            raise ValueError(
                f"tool_schema_max_tokens 必须是正整数，收到 {tool_schema_max_tokens!r}"
            )
        self.tool_schema_max_tokens = tool_schema_max_tokens
        self.max_always_count = max_always_count
        self.max_discovered_per_session = max_discovered_per_session
        # 注入后与压缩链路同一把尺子；None = 回落 CharBasedTokenEstimator（chars/4）
        self._token_estimator = token_estimator
        self._fallback_estimator = CharBasedTokenEstimator()

    def enforce(
        self,
        schemas: list[ToolSchema],
        *,
        tool_schema_tokens: int | None = None,
    ) -> list[ToolSchema]:
        """强制 token 预算。超出时裁剪 DEFERRED 已发现工具。

        裁剪策略：从末尾开始裁剪（DEFERRED 已发现在后面）。
        """
        budget_tokens = (
            tool_schema_tokens
            if tool_schema_tokens is not None
            else self.tool_schema_max_tokens
        )

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
        """估算单个 ToolSchema 的 token 数（优先用注入的 estimator）。"""
        try:
            text = json.dumps({
                "name": schema.name,
                "description": schema.description,
                "parameters": schema.parameters,
            }, ensure_ascii=False)
            estimator = self._token_estimator or self._fallback_estimator
            return max(1, estimator.estimate([{"role": "user", "content": text}]))
        except Exception:
            logger.warning(
                "token 估算失败，回落默认值 %d（计费类兜底，见静默降级审计 §2.2）：schema=%s",
                _FALLBACK_TOKEN_ESTIMATE, getattr(schema, "name", "?"), exc_info=True,
            )
            return _FALLBACK_TOKEN_ESTIMATE
