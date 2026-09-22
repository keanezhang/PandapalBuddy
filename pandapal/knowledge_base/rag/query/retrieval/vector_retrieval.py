# -*- coding: utf-8 -*-
"""
向量检索模块（语义 + 关键词 RRF 融合）。

职责边界
--------
- 本模块：编码查询 → 向量 top-k → 关键词匹配 → RRF 融合排序 → 输出 RawRetrievalResult。
- 上层（Retriever/search）：格式化为 ChildDocument、实体过滤、截断、重排序等。

数据流
------
query → encode → vector top-k → 关键词匹配(metadata 优先 / 正文兜底)
    → 语义排名 + 关键词排名 → RRF 融合 → 输出 RawRetrievalResult
"""

from typing import Any, Dict, List, Optional, Tuple
import json

import logging

from ...schema import RawRetrievalResult, SchemaKeys, EMPTY_RAW_RETRIEVAL_RESULT
from .search_constants import MATCH_TYPE_KEYWORD, MATCH_TYPE_VECTOR

logger = logging.getLogger(__name__)

_K = SchemaKeys

# ── 常量 ──────────────────────────────────────────────────────────────────────
MAX_SORT_COUNT = 500  # 向量检索最大排序数量，超出此值截断
RRF_K = 60  # RRF 平滑因子，与 retriever 三路 RRF 的 k 保持一致
DEFAULT_N_RESULTS = 20  # 默认向量检索返回数量


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _normalize_keyword(kw: str) -> str:
    """规范化关键词：转小写、去除首尾及内部空格，用于查询词与文档词的一致匹配。"""
    if not kw or not isinstance(kw, str):
        return ""
    return kw.lower().strip().replace(" ", "")


def _parse_keywords_from_metadata(raw_value: Any) -> List[str]:
    """
    从 metadata 的 keywords 字段解析出关键词列表。

    Chroma 只支持标量，写入时用 json.dumps(list) 存储，读回为 str；
    也兼容上层直接传入 list 的场景。
    """
    if isinstance(raw_value, list):
        return [x.strip() for x in raw_value if isinstance(x, str) and x.strip()]
    if isinstance(raw_value, str):
        try:
            parsed = json.loads(raw_value)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if x is not None and str(x).strip()]
        except (json.JSONDecodeError, TypeError):
            pass
    return []


def _unpack_chroma_layer0(
    vector_results: Dict[str, Any],
) -> Tuple[List[str], List[Dict], List[float], List[str]]:
    """
    Chroma query() 返回 list-of-list 结构（第一维对应 query 数），此处仅 1 条 query，
    统一取 [0] 拆包为四个平铺列表。
    """
    def _get0(key: str, fallback: Any = None):
        val = vector_results.get(key) or [[]]
        return val[0] if val and len(val) > 0 else (fallback if fallback is not None else [])

    return _get0(_K.RR_DOCUMENTS), _get0(_K.RR_METADATAS), _get0(_K.RR_DISTANCES), _get0(_K.RR_IDS)


# ── 关键词匹配 ────────────────────────────────────────────────────────────────

def _match_keywords_for_candidates(
    docs: List[str],
    metadatas: List[Dict],
    distances: List[float],
    keywords: List[str],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    对每条语义候选做关键词匹配，返回候选行列表和元数据使用统计。

    匹配策略：
    - 优先用 metadata["keywords"] 做集合交集（精确匹配）。
    - 无 metadata 关键词时，退化为正文子串匹配。

    Returns:
        rows: 候选列表，每条含 index / content / metadata / distance / matched_keywords / matched_count
        stats: {"with_metadata": int, "without_metadata": int, "total": int}
    """
    stats = {"with_metadata": 0, "without_metadata": 0, "total": 0}
    rows: List[Dict[str, Any]] = []

    for i, doc in enumerate(docs):
        metadata = metadatas[i] if i < len(metadatas) else {}
        distance = distances[i] if i < len(distances) else None

        # 解析文档侧关键词
        stored_keywords = _parse_keywords_from_metadata(metadata.get(_K.CDM_KEYWORDS))
        stats["total"] += 1

        # 匹配
        if stored_keywords:
            stored_set = {_normalize_keyword(kw) for kw in stored_keywords if kw}
            matched = [kw for kw in keywords if kw in stored_set]
            stats["with_metadata"] += 1
        else:
            doc_lower = doc.lower() if doc else ""
            matched = [kw for kw in keywords if kw and kw in doc_lower]
            stats["without_metadata"] += 1

        rows.append({
            "index": i,
            _K.CD_CONTENT: doc,
            _K.CD_METADATA: metadata,
            "distance": distance,
            "matched_keywords": matched,
            "matched_count": len(matched),
        })

    return rows, stats


# ── RRF 排名计算 ──────────────────────────────────────────────────────────────

def _compute_rrf_scores(
    rows: List[Dict[str, Any]],
    enable_keyword_rank: bool,
) -> None:
    """
    就地为每条候选计算 RRF 融合分数（写入 rows[j]["rrf"]）。

    双路排名：
    - 语义排名（rank_sem）：按 distance 升序，1-based。
    - 关键词排名（rank_kw）：仅 matched_count > 0 的候选参与排名；
      matched_count = 0 的候选统一给同一最差排名，避免伪排名干扰。
    - enable_keyword_rank=False 时，所有候选 rank_kw 相同，排序完全由语义决定。

    RRF: score = 1/(RRF_K + rank_sem) + 1/(RRF_K + rank_kw)
    """
    n = len(rows)
    if n == 0:
        return

    # ── 语义排名 ──
    order_sem = sorted(range(n), key=lambda j: rows[j]["distance"] if rows[j]["distance"] is not None else float("inf"))
    rank_sem = [0] * n
    for r, j in enumerate(order_sem):
        rank_sem[j] = r + 1

    # ── 关键词排名 ──
    tail_rank = n + 1  # 未命中关键词的统一排名
    rank_kw = [tail_rank] * n

    if enable_keyword_rank:
        hit_indices = [j for j in range(n) if rows[j]["matched_count"] > 0]
        hit_indices.sort(key=lambda j: (-rows[j]["matched_count"], rows[j]["index"]))
        for r, j in enumerate(hit_indices):
            rank_kw[j] = r + 1

    # ── 融合 ──
    for j in range(n):
        rows[j]["rrf"] = 1.0 / (RRF_K + rank_sem[j]) + 1.0 / (RRF_K + rank_kw[j])


# ── 结果组装 ──────────────────────────────────────────────────────────────────

def _assemble_raw_result(
    rows_sorted: List[Dict[str, Any]],
    ids0: List[str],
    has_keywords: bool,
) -> RawRetrievalResult:
    """
    将排序后的候选行列表组装为 RawRetrievalResult（与 Chroma 结构一致的两层 list）。

    distance 字段保留向量库原始值（保证下游阈值过滤/展示兼容）。
    """
    documents: List[List[str]] = [[r[_K.CD_CONTENT] for r in rows_sorted]]

    metadatas_out: List[Dict[str, Any]] = []
    for r in rows_sorted:
        meta = dict(r.get(_K.CD_METADATA) or {})
        if r.get("matched_keywords"):
            meta[_K.CDM_MATCHED_KEYWORDS] = r["matched_keywords"]
        meta[_K.CDM_MATCH_TYPE] = (
            MATCH_TYPE_KEYWORD if (has_keywords and r["matched_count"] > 0) else MATCH_TYPE_VECTOR
        )
        metadatas_out.append(meta)

    # 保留原始向量距离，不用 -rrf 替代（避免下游出现负数或 >100% 的相似度）
    distances_out: List[List[float]] = [[r["distance"] for r in rows_sorted]]
    ids_out = [ids0[r["index"]] if r["index"] < len(ids0) else f"child_{r['index']}" for r in rows_sorted]

    return {
        _K.RR_DOCUMENTS: documents,
        _K.RR_METADATAS: [metadatas_out],
        _K.RR_DISTANCES: distances_out,
        _K.RR_IDS: [ids_out],
    }


# ── 主入口 ────────────────────────────────────────────────────────────────────

def search_vector_hybrid_raw(
    vector_store: Any,
    embedding_encoder: Any,
    query: str,
    query_analysis: Dict[str, Any],
    n_results: Optional[int] = None,
    verbose: bool = True,
    enable_keyword_rank: bool = True,
) -> RawRetrievalResult:
    """
    混合检索（语义 + 关键词 RRF 融合），返回 RawRetrievalResult。

    Args:
        vector_store:         VectorStore 实例。
        embedding_encoder:    EmbeddingEncoder 实例，用于 encode_query()。
        query:                用户查询文本。
        query_analysis:       查询分析结果，含 keywords 列表。
        n_results:            向量 top-k，None 时由调用方/Retriever 传入的 default_query_results（如 15）。
        verbose:              是否输出日志。
        enable_keyword_rank:  是否启用关键词排名融合。
                              False 时排序完全由语义决定（三路模式下避免与 BM25 双重关键词加权）。

    Returns:
        RawRetrievalResult（documents / metadatas / distances / ids，均为 list-of-list）。
    """
    # ── 1. 参数校验 ──
    if vector_store is None or embedding_encoder is None:
        if verbose:
            logger.warning(" vector_store 或 embedding_encoder 未提供，返回空结果")
        return EMPTY_RAW_RETRIEVAL_RESULT.copy()

    if n_results is None:
        n_results = 15

    # ── 2. 语义检索 ──
    query_embedding = embedding_encoder.encode_query(query)
    vector_results = vector_store.query_children(query_embedding, n_results=n_results)

    if verbose:
        doc_count = len(vector_results.get(_K.RR_DOCUMENTS, [[]])[0]) if vector_results.get(_K.RR_DOCUMENTS) else 0
        logger.info("  语义检索得到 %d 条候选", doc_count)

    # ── 3. 拆包 Chroma 返回值 ──
    docs0, metadatas0, distances0, ids0 = _unpack_chroma_layer0(vector_results)
    if not docs0:
        return EMPTY_RAW_RETRIEVAL_RESULT.copy()

    # ── 4. 提取并规范化查询关键词 ──
    keywords_raw = query_analysis.get(_K.QA_KEYWORDS, [])
    if not isinstance(keywords_raw, list):
        keywords_raw = []
    keywords = [_normalize_keyword(kw) for kw in keywords_raw
                if kw and isinstance(kw, str) and _normalize_keyword(kw)]

    if verbose:
        if keywords:
            logger.info("  查询关键词: %d 个 - %s", len(keywords), keywords)
        else:
            logger.info("  未提取到关键词，仅使用向量检索")

    # ── 5. 关键词匹配 ──
    rows, match_stats = _match_keywords_for_candidates(docs0, metadatas0, distances0, keywords)

    if verbose and match_stats["total"] > 0:
        ratio = match_stats["with_metadata"] / match_stats["total"] * 100
        logger.info("  元数据关键词覆盖: %d/%d (%.1f%%)", match_stats["with_metadata"], match_stats["total"], ratio)

    # ── 6. RRF 融合排序 ──
    _compute_rrf_scores(rows, enable_keyword_rank)
    rows_sorted = sorted(rows, key=lambda r: r["rrf"], reverse=True)[:MAX_SORT_COUNT]

    if verbose:
        keyword_hits = sum(1 for r in rows_sorted if r["matched_count"] > 0) if keywords else 0
        suffix = "" if enable_keyword_rank else "（关键词排名已关闭）"
        logger.info("  混合检索(RRF)共 %d 条候选，关键词命中 %d 条%s", len(rows_sorted), keyword_hits, suffix)

    # ── 7. 组装输出 ──
    return _assemble_raw_result(rows_sorted, ids0, has_keywords=bool(keywords))
