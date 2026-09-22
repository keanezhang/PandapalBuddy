"""检索质量指标（精确率/召回率/F1/MRR/NDCG）与三路召回统计"""

import math
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def calculate_retrieval_quality_metrics(
    relevant_docs: List[int],
    irrelevant_docs: List[int],
    expected_relevant_docs: Optional[List[int]] = None,
    total_results: int = 0,
) -> Dict[str, Any]:
    """
    计算检索质量指标（精确率、召回率、F1、MRR、NDCG）。

    Args:
        relevant_docs: 相关文档索引列表（基于关键词匹配判断）
        irrelevant_docs: 不相关文档索引列表
        expected_relevant_docs: 预期相关文档索引列表（标准答案模式）
        total_results: 总结果数

    Returns:
        包含检索质量指标的字典
    """
    result: Dict[str, Any] = {
        'precision': 0.0,
        'recall': 0.0,
        'f1_score': 0.0,
        'mrr': 0.0,
        'ndcg': 0.0,
        'recall_note': None,
    }

    if total_results == 0:
        result['recall_note'] = "无检索结果，无法计算指标"
        logger.warning("无检索结果，返回默认指标值")
        return result

    # 计算精确率和召回率
    if expected_relevant_docs is None:
        tp = len(relevant_docs)
        fp = len(irrelevant_docs)

        if tp + fp > 0:
            result['precision'] = tp / (tp + fp)

        result['recall'] = 0.0
        result['recall_note'] = (
            "召回率无法准确计算：基于关键词匹配的快速评估模式，"
            "无法确定知识库中有多少相关文档未被检索到（fn 未知）。"
            "如需准确计算召回率，请提供 expected_relevant_docs 参数（标准答案模式）。"
        )
    else:
        invalid_indices = [idx for idx in expected_relevant_docs if not (0 <= idx < total_results)]
        if invalid_indices:
            logger.warning(
                "expected_relevant_docs 中包含无效索引: %s (有效范围: [0, %d))，已自动过滤",
                invalid_indices, total_results,
            )
            expected_relevant_docs = [idx for idx in expected_relevant_docs if 0 <= idx < total_results]

        if not expected_relevant_docs:
            result['recall_note'] = "expected_relevant_docs 为空或所有索引无效，无法计算召回率"
            logger.warning("expected_relevant_docs 无效，返回默认指标值")
            return result

        relevant_set = set(expected_relevant_docs)
        retrieved_set = set(range(total_results))

        tp = len(relevant_set & retrieved_set)
        fp = len(retrieved_set - relevant_set)
        fn = len(relevant_set - retrieved_set)

        if tp + fp > 0:
            result['precision'] = tp / (tp + fp)

        if tp + fn > 0:
            result['recall'] = tp / (tp + fn)
        else:
            result['recall'] = 0.0

    # F1分数
    if result['precision'] + result['recall'] > 0:
        result['f1_score'] = 2 * (result['precision'] * result['recall']) / (result['precision'] + result['recall'])

    # MRR
    if relevant_docs:
        first_relevant_rank = relevant_docs[0] + 1
        result['mrr'] = 1.0 / first_relevant_rank

    # NDCG — 使用 math.log2 替代 np.log2，消除对 numpy 的硬依赖
    if relevant_docs:
        relevance = [1 if i in relevant_docs else 0 for i in range(total_results)]

        dcg = 0.0
        for i, rel in enumerate(relevance):
            if rel > 0:
                rank = i + 1
                dcg += rel / math.log2(rank + 1)

        ideal_relevance = sorted(relevance, reverse=True)
        idcg = 0.0
        for i, rel in enumerate(ideal_relevance):
            if rel > 0:
                rank = i + 1
                idcg += rel / math.log2(rank + 1)

        if idcg > 0:
            result['ndcg'] = dcg / idcg

    return result


def calculate_retrieval_source_metrics(search_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    计算三路召回效果指标。

    Returns:
        包含召回源统计的字典：vector/bm25/graph_hit_count、multi_source_hit_count、retrieval_sources_stats
    """
    vector_hit_count = 0
    bm25_hit_count = 0
    graph_hit_count = 0
    multi_source_hit_count = 0
    retrieval_sources_stats: Dict[str, int] = {}

    for doc in search_results:
        retrieval_sources = doc.get('retrieval_sources') or []
        graph_hit = bool(doc.get('graph_hit', False))
        bm25_hit = bool(doc.get('bm25_hit', False))

        has_vector = 'vector' in retrieval_sources
        has_bm25 = bm25_hit or 'bm25' in retrieval_sources
        has_graph = graph_hit or 'graph' in retrieval_sources

        if has_vector:
            vector_hit_count += 1
        if has_bm25:
            bm25_hit_count += 1
        if has_graph:
            graph_hit_count += 1

        source_count = sum([has_vector, has_bm25, has_graph])
        if source_count > 1:
            multi_source_hit_count += 1

        sources_key = ','.join(sorted(
            s for s, h in [('vector', has_vector), ('bm25', has_bm25), ('graph', has_graph)] if h
        ))
        if sources_key:
            retrieval_sources_stats[sources_key] = retrieval_sources_stats.get(sources_key, 0) + 1

    return {
        'vector_hit_count': vector_hit_count,
        'bm25_hit_count': bm25_hit_count,
        'graph_hit_count': graph_hit_count,
        'multi_source_hit_count': multi_source_hit_count,
        'retrieval_sources_stats': retrieval_sources_stats,
    }
