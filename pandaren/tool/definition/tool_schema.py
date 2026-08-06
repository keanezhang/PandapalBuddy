"""pandaren/tool/definition/tool_schema.py — LLM 暴露的工具 schema 模型"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolSchema:
    """暴露给 LLM 的工具 schema（build_tool_schemas 的返回元素）。

    对应 OpenAI function calling 的 tools 数组中的一个元素。
    """
    name: str
    description: str
    parameters: Any  # JsonSchema


@dataclass(frozen=True)
class ToolSearchResult:
    """ToolSearch 搜索结果（search_tools 的返回元素）。"""
    name: str
    description: str
    when_to_use: str
