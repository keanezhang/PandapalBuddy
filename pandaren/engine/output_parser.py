"""pandaren/engine/output_parser.py — 解析 LLM 响应 → FINAL 或 TOOL_CALLS"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParsedResult:
    """LLM 响应解析结果。"""
    is_final: bool
    content: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    is_empty: bool = False  # 标记是否为空响应（content 为空且无 tool_calls）


class OutputParser:
    """解析 LLM 响应。

    规则：
      - 有 tool_calls → 提取列表
      - 无 tool_calls 且有非空 content → FINAL
      - 无 tool_calls 且 content 为空 → FINAL + is_empty=True（由 Loop 决定如何处理）
    """

    def parse(self, llm_response: dict[str, Any]) -> ParsedResult:
        """解析 LLM 响应为 ParsedResult。"""
        content = llm_response.get("content")
        tool_calls = llm_response.get("tool_calls")

        if tool_calls:
            return ParsedResult(
                is_final=False,
                content=content,
                tool_calls=tool_calls,
            )

        # 无 tool_calls → FINAL（但需标记是否为空响应）
        is_empty = not content
        return ParsedResult(
            is_final=True,
            content=content,
            is_empty=is_empty,
        )
