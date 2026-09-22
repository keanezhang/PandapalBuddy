#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAG 效果评估模块

提供全面的 RAG 系统评估功能，包括：
1. 检索质量评估（精确率、召回率、F1、MRR、NDCG）
2. 相关性评分（平均相似度、最低相似度、相似度分布）
3. 覆盖度评估（文档块数量、章节覆盖、关键词覆盖）
4. 多样性评估（来源多样性、内容多样性）
5. 三路召回效果评估（向量、BM25、图谱的命中率和贡献度）
6. 重排序效果评估（rerank_score分布和提升效果）
7. RRF融合效果评估（多路召回融合的贡献度）
8. 意图识别评估（意图类型、实体数量）
9. 性能评估（搜索耗时、向量化耗时）
10. 实际应用指标（答案准确性、完整性、相关性）

本模块是纯 SDK 评估模块，只负责对已有的 search_results 做评估，不执行搜索。
报告打印等展示逻辑见 scripts/rag/run_search_cli.py。

使用方式::

    from rag_sdk.evaluation import evaluate_rag, RAGEvaluationResult

    # 1. 先通过 SDK 执行搜索获取 results
    # 2. 然后评估结果（评估类只负责评估，不执行搜索）
    eval_result = evaluate_rag(
        query="李二是谁",
        search_results=results,
        expected_keywords=["李二", "骊珠洞天"],
    )
    print(f"精确率: {eval_result.precision:.3f}")

    # 批量评估
    batch_results = evaluate_rag_batch(test_cases)
    print(f"平均精确率: {batch_results['avg_precision']:.3f}")
"""

import re
import json
from typing import List, Optional, Dict, Any, Tuple, Union
import dataclasses
from dataclasses import dataclass, field

from ..schema import SchemaKeys
import logging

from .metrics.similarity import (
    _distance_to_similarity,
    calculate_similarity_metrics as _calculate_similarity_metrics,
)
from .metrics.retrieval_quality import (
    calculate_retrieval_quality_metrics as _calculate_retrieval_quality_metrics,
    calculate_retrieval_source_metrics as _calculate_retrieval_source_metrics,
)
from .metrics.coverage import (
    calculate_coverage_metrics as _calculate_coverage_metrics,
    calculate_diversity_metrics as _calculate_diversity_metrics,
)
from .metrics.rerank import (
    calculate_rerank_metrics as _calculate_rerank_metrics,
    calculate_rrf_metrics as _calculate_rrf_metrics,
)

logger = logging.getLogger(__name__)

_K = SchemaKeys

# ---------- numpy 延迟导入（仅 evaluate_rag_batch 的平均搜索时间用到） ----------
_np = None  # type: Any


def _ensure_numpy() -> Any:
    global _np
    if _np is None:
        try:
            import numpy as np
            _np = np
        except ImportError:
            raise ImportError(
                "numpy is required for evaluation. "
                "Install with: pip install numpy"
            )
    return _np


# 常量定义
MIN_KEYWORD_LENGTH: int = 2  # 最小关键词长度


# ========== 工具函数 ==========

def _normalize_child_docs_to_eval_format(search_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    将 List[ChildDocument] 转为评估内部使用的文档格式
    （含 best_distance, relevance_score, retrieval_sources, parent_metadata, matched_children, content 等）。
    """
    if not search_results:
        return search_results

    normalized = []
    for doc in search_results:
        d = doc.get(_K.CD_DISTANCE)
        relevance = _distance_to_similarity(d)
        normalized.append({
            "best_distance": d,
            "relevance_score": relevance,
            "retrieval_sources": doc.get(_K.CD_RETRIEVAL_SOURCES) or [],
            "graph_hit": bool(doc.get(_K.CD_GRAPH_HIT, False)),
            "bm25_hit": bool(doc.get(_K.CD_BM25_HIT, False)),
            "rerank_score": doc.get(_K.CD_RERANK_SCORE),
            "rrf_score": doc.get(_K.CD_RRF_SCORE),
            "parent_metadata": doc.get(_K.CD_METADATA) or {},
            "matched_children": [doc],
            "parent_content": "",
            "content": doc.get(_K.CD_CONTENT) or "",
        })
    return normalized


@dataclass
class RAGEvaluationResult:
    """
    RAG 评估结果数据类

    包含所有评估维度的指标和详细信息
    """
    # ========== 基本信息 ==========
    query: str
    k: int
    total_results: int

    # ========== 检索质量指标 ==========
    precision: float = 0.0
    recall: float = 0.0
    recall_note: Optional[str] = None
    f1_score: float = 0.0
    mrr: float = 0.0
    ndcg: float = 0.0

    # ========== 相关性评分 ==========
    avg_similarity: float = 0.0
    min_similarity: float = 0.0
    max_similarity: float = 0.0
    similarity_std: float = 0.0

    # ========== 覆盖度评估 ==========
    keyword_coverage: float = 0.0
    unique_sources: int = 0
    chapter_coverage: Optional[float] = None

    # ========== 多样性评估 ==========
    source_diversity: float = 0.0
    content_diversity: float = 0.0

    # ========== 性能指标 ==========
    search_time_ms: Optional[float] = None
    embedding_time_ms: float = 0.0

    # ========== 详细信息 ==========
    relevant_docs: List[int] = field(default_factory=list)
    irrelevant_docs: List[int] = field(default_factory=list)
    matched_keywords: List[str] = field(default_factory=list)
    expected_keywords: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    doc_keywords_metadata: List[Optional[Dict[str, Any]]] = field(default_factory=list)

    # ========== 三路召回效果评估 ==========
    vector_hit_count: int = 0
    bm25_hit_count: int = 0
    graph_hit_count: int = 0
    multi_source_hit_count: int = 0
    retrieval_sources_stats: Dict[str, int] = field(default_factory=dict)

    # ========== 重排序效果评估 ==========
    avg_rerank_score: Optional[float] = None
    min_rerank_score: Optional[float] = None
    max_rerank_score: Optional[float] = None
    rerank_score_std: Optional[float] = None
    rerank_improvement: Optional[float] = None

    # ========== RRF融合效果评估 ==========
    avg_rrf_score: Optional[float] = None
    rrf_score_std: Optional[float] = None

    # ========== 意图识别评估 ==========
    intent: Optional[str] = None
    entities_count: int = 0

    # ========== 实际应用指标（需要人工评估或参考答案）==========
    answer_accuracy: Optional[float] = None
    answer_completeness: Optional[float] = None
    answer_relevance: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return dataclasses.asdict(self)


# ========== 关键词相关函数 ==========

def _extract_keywords(query: str, expected_keywords: Optional[Union[str, List[str]]] = None) -> List[str]:
    """
    提取和处理关键词。

    Args:
        query: 搜索查询
        expected_keywords: 预期关键词（None / str / List[str]）

    Returns:
        处理后的关键词列表
    """
    if expected_keywords is None:
        return []

    if isinstance(expected_keywords, str):
        text = expected_keywords.strip()
        if not text:
            return []
        parts = [p.strip() for p in re.split(r"[,，\s]+", text) if p.strip()]
        return [p for p in parts if len(p) >= MIN_KEYWORD_LENGTH]

    if isinstance(expected_keywords, list):
        processed = []
        for kw in expected_keywords:
            if isinstance(kw, str):
                k = kw.strip()
                if k and len(k) >= MIN_KEYWORD_LENGTH:
                    processed.append(k)
        return processed

    raise TypeError(
        f"expected_keywords 必须是 None、str 或 List[str]，但收到了 {type(expected_keywords).__name__}: {expected_keywords}"
    )


def _extract_keywords_from_metadata(metadata: Dict[str, Any]) -> List[str]:
    """从 metadata 中提取关键词列表（小写）。"""
    keywords: List[str] = []

    if 'keywords' in metadata:
        try:
            keywords_str = metadata['keywords']
            if isinstance(keywords_str, str):
                keywords_list = json.loads(keywords_str)
            elif isinstance(keywords_str, list):
                keywords_list = keywords_str
            else:
                keywords_list = []

            if isinstance(keywords_list, list):
                keywords.extend([str(kw).lower().strip() for kw in keywords_list if kw])
        except (json.JSONDecodeError, TypeError, AttributeError) as e:
            logger.debug("解析keywords字段失败: %s", e)

    if not keywords and 'keywords_metadata' in metadata:
        try:
            keywords_metadata = metadata['keywords_metadata']
            if isinstance(keywords_metadata, str):
                keywords_metadata = json.loads(keywords_metadata)

            if isinstance(keywords_metadata, list):
                for kw_item in keywords_metadata:
                    if isinstance(kw_item, dict):
                        kw = kw_item.get('keyword', '')
                    else:
                        kw = str(kw_item)
                    if kw:
                        keywords.append(kw.lower().strip())
        except (json.JSONDecodeError, TypeError, AttributeError) as e:
            logger.debug("解析keywords_metadata字段失败: %s", e)

    return keywords


def _match_keywords_in_document(doc: Dict[str, Any], expected_keywords: List[str]) -> List[str]:
    """
    在文档中匹配关键词（三级策略：父 metadata → 子 metadata → 正文内容）。
    """
    matched: List[str] = []

    expected_kw_lower_map = {kw.lower().strip(): kw for kw in expected_keywords if kw and isinstance(kw, str)}
    expected_kw_lower_set = set(expected_kw_lower_map.keys())

    all_metadata_keywords: set = set()

    # 策略1：父文档 metadata
    parent_metadata = doc.get('parent_metadata', {})
    if isinstance(parent_metadata, dict):
        parent_keywords = _extract_keywords_from_metadata(parent_metadata)
        if parent_keywords:
            all_metadata_keywords.update(parent_keywords)
            matched_lower = expected_kw_lower_set & set(parent_keywords)
            for kw_lower in matched_lower:
                original_kw = expected_kw_lower_map[kw_lower]
                if original_kw not in matched:
                    matched.append(original_kw)
                    logger.debug("关键词 '%s' 在父文档的keywords中找到", original_kw)

    # 策略2：子文档 metadata
    matched_children = doc.get('matched_children', [])
    for child in matched_children:
        child_metadata = child.get('metadata', {})
        if isinstance(child_metadata, dict):
            child_keywords = _extract_keywords_from_metadata(child_metadata)
            if child_keywords:
                all_metadata_keywords.update(child_keywords)
                matched_lower = expected_kw_lower_set & set(child_keywords)
                for kw_lower in matched_lower:
                    original_kw = expected_kw_lower_map[kw_lower]
                    if original_kw not in matched:
                        matched.append(original_kw)
                        logger.debug("关键词 '%s' 在子文档的keywords中找到", original_kw)

    # 策略3：正文内容匹配
    if len(matched) < len(expected_keywords):
        max_content_length = 100000
        all_contents: List[str] = []
        total_length = 0

        parent_content = doc.get('parent_content', '')
        if parent_content and total_length < max_content_length:
            content_part = parent_content[:max_content_length - total_length]
            all_contents.append(content_part)
            total_length += len(content_part)

        for child in matched_children:
            if total_length >= max_content_length:
                break
            child_content = child.get('content', '')
            if child_content:
                content_part = child_content[:max_content_length - total_length]
                all_contents.append(content_part)
                total_length += len(content_part)

        if not all_contents:
            content = doc.get('content', '')
            if content:
                all_contents.append(content[:max_content_length])

        combined_content = "\n".join(all_contents) if all_contents else ''
        content_lower = combined_content.lower() if combined_content else ''

        remaining_keywords = [kw for kw in expected_keywords if kw not in matched]
        for kw in remaining_keywords:
            if not kw or not isinstance(kw, str):
                continue
            kw = kw.strip()
            if not kw:
                continue

            kw_lower = kw.lower()
            if kw in combined_content:
                matched.append(kw)
            elif kw_lower != kw:
                if kw_lower in content_lower:
                    matched.append(kw)

    if not matched and expected_keywords:
        logger.debug("文档未匹配到任何关键词。预期关键词: %s", expected_keywords)

    return matched


def _judge_document_relevance(
    search_results: List[Dict[str, Any]],
    expected_keywords: List[str],
    require_all_keywords: bool = True,
) -> Tuple[List[int], List[int], List[str]]:
    """
    判断文档相关性并收集匹配的关键词。

    Returns:
        (relevant_docs, irrelevant_docs, matched_keywords)
    """
    relevant_docs: List[int] = []
    irrelevant_docs: List[int] = []
    matched_keywords_set: set = set()

    logger.debug("开始评估 %d 个文档的关键词匹配情况...", len(search_results))

    for i, doc in enumerate(search_results):
        matched = _match_keywords_in_document(doc, expected_keywords)

        if matched:
            matched_keywords_set.update(matched)

        if require_all_keywords:
            is_relevant = len(matched) == len(expected_keywords)
        else:
            is_relevant = len(matched) > 0

        if is_relevant:
            relevant_docs.append(i)
        else:
            irrelevant_docs.append(i)

    logger.debug(
        "匹配结果汇总: 相关文档 %d 个, 不相关文档 %d 个, 匹配的关键词: %s",
        len(relevant_docs), len(irrelevant_docs), matched_keywords_set,
    )

    return relevant_docs, irrelevant_docs, list(matched_keywords_set)


# ========== 公共 API ==========

def evaluate_rag(
    query: str,
    search_results: Optional[List[Dict[str, Any]]] = None,
    expected_keywords: Optional[Union[str, List[str]]] = None,
    expected_relevant_docs: Optional[List[int]] = None,
    k: Optional[int] = None,
    require_all_keywords: bool = True,
    query_analysis: Optional[Dict[str, Any]] = None,
) -> RAGEvaluationResult:
    """
    评估 RAG 系统的检索效果（纯评估，不执行搜索）。

    调用方需自行执行搜索并传入 search_results；如需一体化「搜索+评估」请在调用层编排。

    Args:
        query: 搜索查询
        search_results: 搜索结果列表（必须由调用方提供）
        expected_keywords: 预期关键词
        expected_relevant_docs: 预期相关的文档索引列表
        k: 请求的文档数量
        require_all_keywords: 是否要求文档包含所有关键词才认为相关
        query_analysis: 查询分析结果（可选）

    Returns:
        RAGEvaluationResult
    """
    if search_results is None:
        raise ValueError("必须提供 search_results（SDK 评估模块不负责执行搜索）")

    if not query:
        raise ValueError("query 参数不能为空")

    if not isinstance(search_results, list):
        raise TypeError(f"search_results 必须是列表类型，但收到了 {type(search_results).__name__}")

    if not search_results:
        logger.warning("搜索结果为空: query='%s'", query)
        return RAGEvaluationResult(
            query=query,
            k=k or 0,
            total_results=0,
            recall_note="搜索结果为空，无法进行评估",
        )

    try:
        search_results = _normalize_child_docs_to_eval_format(search_results)
    except Exception as e:
        logger.error("格式化搜索结果失败: %s", e)
        raise ValueError(f"搜索结果格式不正确: {e}")

    eval_result = RAGEvaluationResult(
        query=query,
        k=k or len(search_results),
        total_results=len(search_results),
    )

    # 1. 相关性评分
    try:
        similarity_metrics = _calculate_similarity_metrics(search_results)
        eval_result.avg_similarity = similarity_metrics['avg_similarity']
        eval_result.min_similarity = similarity_metrics['min_similarity']
        eval_result.max_similarity = similarity_metrics['max_similarity']
        eval_result.similarity_std = similarity_metrics['similarity_std']
    except Exception as e:
        logger.error("计算相似度指标失败: %s", e)

    # 1.1 意图识别
    if query_analysis:
        eval_result.intent = query_analysis.get(_K.QA_INTENT)
        entities = query_analysis.get(_K.QA_ENTITIES, [])
        eval_result.entities_count = len(entities) if isinstance(entities, list) else 0

    # 2. 关键词
    if query_analysis and expected_keywords is None:
        expected_keywords = query_analysis.get(_K.QA_KEYWORDS, [])

    try:
        expected_keywords = _extract_keywords(query, expected_keywords)
        logger.debug("提取的关键词: %s, 数量: %d", expected_keywords, len(expected_keywords))
    except Exception as e:
        logger.error("提取关键词失败: %s", e)
        expected_keywords = []

    if not expected_keywords:
        logger.warning(
            "预期关键词为空：当前评估模块不再自动从 query 里抽取关键词。"
            "请显式传入 expected_keywords（推荐使用 query_analysis[SchemaKeys.QA_KEYWORDS]）。"
        )

    # 3. 文档相关性
    if expected_keywords:
        try:
            relevant_docs, irrelevant_docs, matched_keywords = _judge_document_relevance(
                search_results, expected_keywords, require_all_keywords,
            )

            eval_result.relevant_docs = relevant_docs
            eval_result.irrelevant_docs = irrelevant_docs
            eval_result.matched_keywords = matched_keywords
            eval_result.expected_keywords = expected_keywords

            if len(expected_keywords) > 0:
                eval_result.keyword_coverage = len(matched_keywords) / len(expected_keywords)
            else:
                eval_result.keyword_coverage = 0.0

            # 4. 检索质量指标
            quality_metrics = _calculate_retrieval_quality_metrics(
                relevant_docs, irrelevant_docs, expected_relevant_docs, len(search_results),
            )
            eval_result.precision = quality_metrics['precision']
            eval_result.recall = quality_metrics['recall']
            eval_result.f1_score = quality_metrics['f1_score']
            eval_result.mrr = quality_metrics['mrr']
            eval_result.ndcg = quality_metrics['ndcg']
            eval_result.recall_note = quality_metrics['recall_note']
        except Exception as e:
            logger.error("判断文档相关性或计算检索质量指标失败: %s", e)
    else:
        eval_result.relevant_docs = []
        eval_result.irrelevant_docs = []
        eval_result.matched_keywords = []
        eval_result.expected_keywords = []
        eval_result.keyword_coverage = 0.0

    # 5. 覆盖度
    try:
        coverage_metrics = _calculate_coverage_metrics(search_results)
        eval_result.sources = coverage_metrics['sources']
        eval_result.unique_sources = coverage_metrics['unique_sources']
        eval_result.source_diversity = coverage_metrics['source_diversity']
        eval_result.chapter_coverage = coverage_metrics['chapter_coverage']
    except Exception as e:
        logger.error("计算覆盖度指标失败: %s", e)

    # 6. 多样性
    try:
        eval_result.content_diversity = _calculate_diversity_metrics(search_results)
    except Exception as e:
        logger.error("计算多样性指标失败: %s", e)

    # 7. 三路召回
    try:
        retrieval_source_metrics = _calculate_retrieval_source_metrics(search_results)
        eval_result.vector_hit_count = retrieval_source_metrics['vector_hit_count']
        eval_result.bm25_hit_count = retrieval_source_metrics['bm25_hit_count']
        eval_result.graph_hit_count = retrieval_source_metrics['graph_hit_count']
        eval_result.multi_source_hit_count = retrieval_source_metrics['multi_source_hit_count']
        eval_result.retrieval_sources_stats = retrieval_source_metrics['retrieval_sources_stats']
    except Exception as e:
        logger.error("计算三路召回效果指标失败: %s", e)

    # 8. 重排序
    try:
        rerank_metrics = _calculate_rerank_metrics(search_results)
        eval_result.avg_rerank_score = rerank_metrics['avg_rerank_score']
        eval_result.min_rerank_score = rerank_metrics['min_rerank_score']
        eval_result.max_rerank_score = rerank_metrics['max_rerank_score']
        eval_result.rerank_score_std = rerank_metrics['rerank_score_std']
        eval_result.rerank_improvement = rerank_metrics['rerank_improvement']
    except Exception as e:
        logger.error("计算重排序效果指标失败: %s", e)

    # 9. RRF
    try:
        rrf_metrics = _calculate_rrf_metrics(search_results)
        eval_result.avg_rrf_score = rrf_metrics['avg_rrf_score']
        eval_result.rrf_score_std = rrf_metrics['rrf_score_std']
    except Exception as e:
        logger.error("计算RRF融合效果指标失败: %s", e)

    # 10. 性能指标
    eval_result.search_time_ms = None

    return eval_result


def evaluate_rag_batch(
    test_cases: List[Dict[str, Any]],
    return_detailed_results: bool = False,
) -> Dict[str, Any]:
    """
    批量评估 RAG 系统（纯评估，不执行搜索）。

    每个用例须含 query 和 search_results。如需一体化「搜索+评估」请在调用层编排。

    Args:
        test_cases: 测试用例列表，每个用例须含 query 和 search_results。
        return_detailed_results: 是否返回详细的单个评估结果（默认 False）

    Returns:
        包含平均指标的字典
    """
    if not test_cases:
        logger.warning("批量评估：测试用例列表为空")
        return {}

    results = []
    total_cases = len(test_cases)

    sum_precision = 0.0
    sum_recall = 0.0
    sum_f1_score = 0.0
    sum_mrr = 0.0
    sum_ndcg = 0.0
    sum_similarity = 0.0
    sum_keyword_coverage = 0.0
    sum_source_diversity = 0.0
    search_times: List[float] = []
    num_success = 0

    for i, test_case in enumerate(test_cases):
        query = test_case['query']
        search_results = test_case.get('search_results', [])
        expected_keywords = test_case.get('expected_keywords')
        expected_relevant_docs = test_case.get('expected_relevant_docs')
        k = test_case.get('k')
        require_all_keywords = test_case.get('require_all_keywords', True)
        query_analysis = test_case.get('query_analysis')

        try:
            result = evaluate_rag(
                query=query,
                search_results=search_results,
                expected_keywords=expected_keywords,
                expected_relevant_docs=expected_relevant_docs,
                k=k,
                require_all_keywords=require_all_keywords,
                query_analysis=query_analysis,
            )
            sum_precision += result.precision
            sum_recall += result.recall
            sum_f1_score += result.f1_score
            sum_mrr += result.mrr
            sum_ndcg += result.ndcg
            sum_similarity += result.avg_similarity
            sum_keyword_coverage += result.keyword_coverage
            sum_source_diversity += result.source_diversity
            if result.search_time_ms is not None:
                search_times.append(result.search_time_ms)
            num_success += 1
            if return_detailed_results:
                results.append(result)
        except Exception as e:
            logger.error("评估测试用例 %d/%d 失败: %s, 错误: %s", i + 1, total_cases, query, e)
            continue

    num_results = num_success
    if num_results == 0:
        logger.warning("批量评估：没有成功评估的用例")
        return {}

    np = _ensure_numpy()
    avg_search_time = float(np.mean(search_times)) if search_times else None

    result_dict = {
        "total_cases": total_cases,
        "successful_cases": num_results,
        "avg_precision": sum_precision / num_results,
        "avg_recall": sum_recall / num_results,
        "avg_f1_score": sum_f1_score / num_results,
        "avg_mrr": sum_mrr / num_results,
        "avg_ndcg": sum_ndcg / num_results,
        "avg_similarity": sum_similarity / num_results,
        "avg_keyword_coverage": sum_keyword_coverage / num_results,
        "avg_source_diversity": sum_source_diversity / num_results,
        "avg_search_time_ms": avg_search_time,
    }

    if return_detailed_results:
        result_dict["results"] = [r.to_dict() for r in results]

    return result_dict
