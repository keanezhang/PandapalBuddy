#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图谱子域共用纯常量。

原则：纯常量打散优先，共用才单独文件。本文件保留图谱子域内至少 2 处引用的不可调常量
（关系/结果限制、策略名称、共用 LLM 默认参数）。路径相关常量仅在 graph_store_neo4j 使用，已放入该模块。
实体类型见 entity_type，关系类型枚举见 relation_type。策略的说明文案在 search.query_intent_classifier（指令用）。
运行时可变的（如 max_tokens、temperature、Agent 名等）应进 settings。
"""

from typing import Final, Tuple

# ==================== 图谱策略（9 种，图侧与意图分类器共用） ====================

STRATEGY_TYPES: Final[Tuple[str, ...]] = (
    "entity_attribute",
    "direct_relation",
    "path_query",
    "graph_structure",
    "common_relation_endpoint",
    "list_by_relation_type",
    "list_by_entity",
    "expand_entity",
    "narrative_events",
)

# ==================== 查询相关常量（至少 2 处引用） ====================

# 关系查询
DEFAULT_RELATIONSHIP_LIMIT: Final[int] = 10  # graph_strategy、graph_store
MAX_RESULTS_LIMIT: Final[int] = 50  # graph_retrieval、graph_store_neo4j

# ==================== 共用 LLM 参数 ====================

# entity_relationship_extractor_llm、query_intent_classifier、bakup/graph_chunk_mapper
DEFAULT_TEMPERATURE: Final[float] = 0.3
DEFAULT_MAX_TURNS: Final[int] = 1
