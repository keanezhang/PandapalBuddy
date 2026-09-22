#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAG 工具函数模块

提供 RAG 系统所需的工具函数，包括：
1. 文本处理：NER、Token估算
2. Parent ID 工具：构建、解析、验证 parent_id

注意：本模块只包含纯工具函数，不转发其他模块的类。
文档处理、检索等功能请直接从对应模块导入。
"""

# ==================== 文本处理工具 ====================
from .text_utils import (
    load_ner_model,
    unload_ner_model,
    extract_entities_by_hanlp,
    estimate_tokens,
    generate_chunk_title,
    normalize_list_field,
    HANLP_AVAILABLE,
)

# ==================== Parent ID 工具 ====================
from .parent_id import (
    build_parent_id,
    parse_parent_id,
    validate_parent_id_format,
)

# ==================== Chroma 工具 ====================
from .chroma_utils import ChromaGetResult, flatten_chroma_get_result

# ==================== 图谱评分工具 ====================
from .graph_relevance_score import (
    compute_graph_relevance_score,
    evidence_count_to_score,
    entity_coverage_to_score,
    relation_distance_to_score,
    get_weights_for_intent,
)

__all__ = [
    # 文本处理
    "is_ner_available",
    "load_ner_model",
    "unload_ner_model",
    "extract_entities_by_hanlp",
    "estimate_tokens",
    "generate_chunk_title",
    "normalize_list_field",
    # Parent ID 工具
    "build_parent_id",
    "parse_parent_id",
    "validate_parent_id_format",
    "flatten_chroma_get_result",
    "ChromaGetResult",
    # 图谱评分
    "compute_graph_relevance_score",
    "evidence_count_to_score",
    "entity_coverage_to_score",
    "relation_distance_to_score",
    "get_weights_for_intent",
]


def is_ner_available() -> bool:
    """Check if HanLP NER is available."""
    return HANLP_AVAILABLE
