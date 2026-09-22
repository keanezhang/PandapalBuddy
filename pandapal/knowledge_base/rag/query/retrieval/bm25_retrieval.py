# -*- coding: utf-8 -*-
"""
BM25 检索：仅负责从 BM25Store 拿候选，返回原始结果（RawRetrievalResult）。

- search_bm25_raw：调用 bm25_store.query_children，不做格式化和实体过滤。
- 格式化为 ChildDocument、实体过滤与截断由 search 层（Retriever）统一处理。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, TYPE_CHECKING

import logging

from ...schema import RawRetrievalResult, SchemaKeys, EMPTY_RAW_RETRIEVAL_RESULT

if TYPE_CHECKING:
    from ...store.bm25.bm25_store import BM25Store

logger = logging.getLogger(__name__)

# 默认返回结果数量
DEFAULT_BM25_QUERY_RESULTS = 15


def search_bm25_raw(
    bm25_store: Optional["BM25Store"],
    query_analysis: Dict[str, Any],
    n_results: Optional[int] = None,
    verbose: bool = True,
) -> RawRetrievalResult:
    """
    从 BM25 存储获取候选，返回原始检索结果（不做格式化和实体过滤）。

    Args:
        bm25_store: BM25Store 实例
        query_analysis: 查询分析结果，需含 segmented_words（中文分词结果）
        n_results: 返回数量，None 时使用配置默认值
        verbose: 是否打日志

    Returns:
        RawRetrievalResult：documents、metadatas、distances、ids（与 Chroma/BM25 一致）
    """
    # 验证 BM25 存储是否可用
    if bm25_store is None:
        if verbose:
            logger.warning(" BM25Store 未提供，返回空结果")
        return EMPTY_RAW_RETRIEVAL_RESULT.copy()

    # 提取并验证分词结果，去除每个词元首尾空格（避免 ' 和' 等导致检索异常）
    segmented_words_raw = query_analysis.get(SchemaKeys.QA_SEGMENTED_WORDS, [])
    if not isinstance(segmented_words_raw, list):
        segmented_words_raw = []
    segmented_words = [str(w).strip() for w in segmented_words_raw if w is not None and str(w).strip()]

    if not segmented_words:
        if verbose:
            logger.warning(" segmented_words 为空，返回空结果")
        return EMPTY_RAW_RETRIEVAL_RESULT.copy()

    # 设置返回结果数量
    if n_results is None:
        n_results = DEFAULT_BM25_QUERY_RESULTS

    if verbose:
        logger.info(
            "  BM25 检索使用 segmented_words: %s（共 %d 个词元）",
            segmented_words,
            len(segmented_words),
        )

    # 使用规范化后的 segmented_words 传入 store，避免污染上游 query_analysis
    query_analysis_for_bm25 = {**query_analysis, SchemaKeys.QA_SEGMENTED_WORDS: segmented_words}

    # 执行 BM25 检索
    try:
        raw = bm25_store.query_children(
            query_analysis=query_analysis_for_bm25,
            n_results=n_results
        )
        
        if verbose and raw.get(SchemaKeys.RR_DOCUMENTS):
            doc_count = len(raw[SchemaKeys.RR_DOCUMENTS][0]) if raw[SchemaKeys.RR_DOCUMENTS] else 0
            logger.info("  BM25 检索得到 %d 条候选", doc_count)
        
        return raw
    except Exception as e:
        if verbose:
            logger.warning("  BM25 检索失败: %s: %s", type(e).__name__, e)
        return EMPTY_RAW_RETRIEVAL_RESULT.copy()
