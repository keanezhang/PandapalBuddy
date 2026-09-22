# -*- coding: utf-8 -*-
"""
图谱检索入口：根据查询分析结果执行图谱检索，返回子文档 ID 及统一得分。

执行逻辑概览：
- 默认路径：无显式策略或仅 entity_attribute 时，用实体/关键词查询。
- 策略路径：存在显式策略（direct_relation、path_query 等）时，按策略逐个执行并合并结果，
    再统一打分、排序、截断。多策略为「组合执行、结果合并」。
"""

from typing import Dict, Any, Optional, List, Set, Tuple, DefaultDict, Union
from collections import defaultdict

import logging

from ...store.graph.graph_constants import MAX_RESULTS_LIMIT, STRATEGY_TYPES
from ...store.graph.graph_store import GraphStore
from .graph_strategy import GraphStrategyExecutor
from ...utils.graph_relevance_score import (
    compute_graph_relevance_score,
    get_weights_for_intent,
    relation_distance_to_score,
    entity_coverage_to_score,
    evidence_count_to_score,
)
from ...schema import QueryAnalysis, SchemaKeys

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# 常量
# -----------------------------------------------------------------------------

# 统一打分：证据维度归一化时，实体路径数上限
MAX_ENTITY_PATHS = 3

# 中文实体关键词长度范围
_MIN_ENTITY_KW_LEN = 2
_MAX_ENTITY_KW_LEN = 4

# 必须走 GraphStrategyExecutor 的策略（索引 1~8；0=entity_attribute 走默认路径）
EXPLICIT_STRATEGIES = set(STRATEGY_TYPES[1:])

# 策略先验分（0~1）：越贴意图越高，用于 strategy_prior_score
STRATEGY_PRIOR: Dict[str, float] = {
    STRATEGY_TYPES[8]: 1.0,
    STRATEGY_TYPES[1]: 0.9,
    STRATEGY_TYPES[2]: 0.8,
    STRATEGY_TYPES[4]: 0.7,
    STRATEGY_TYPES[7]: 0.6,
    STRATEGY_TYPES[6]: 0.5,
    STRATEGY_TYPES[5]: 0.5,
    STRATEGY_TYPES[3]: 0.4,
}

# 策略对应的近似跳数（用于 hop_score，越小越近）
STRATEGY_HOP: Dict[str, int] = {
    STRATEGY_TYPES[8]: 1,
    STRATEGY_TYPES[1]: 1,
    STRATEGY_TYPES[2]: 2,
    STRATEGY_TYPES[4]: 2,
    STRATEGY_TYPES[7]: 2,
    STRATEGY_TYPES[6]: 2,
    STRATEGY_TYPES[5]: 2,
    STRATEGY_TYPES[3]: 2,
}


# -----------------------------------------------------------------------------
# 辅助：实体规范化
# -----------------------------------------------------------------------------

def _normalize_entities(entities_raw: List[Union[str, Dict[str, Any]]]) -> List[str]:
    """
    将 entities 规范为字符串列表，兼容元素为 str 或带 SchemaKeys.ENT_TEXT 的 dict。
    """
    if not entities_raw or not isinstance(entities_raw, list):
        return []
    out = []
    for e in entities_raw:
        if isinstance(e, str) and e.strip():
            out.append(e.strip())
        elif isinstance(e, dict) and e.get(SchemaKeys.ENT_TEXT):
            t = str(e.get(SchemaKeys.ENT_TEXT, "")).strip()
            if t:
                out.append(t)
    return out


# -----------------------------------------------------------------------------
# 默认路径：实体 / 关键词查询（无显式策略或兜底时使用）
# -----------------------------------------------------------------------------

def _default_graph_query(
    graph_store: GraphStore,
    query: Optional[str] = None,
    keywords: Optional[List[str]] = None,
    entities: Optional[List[str]] = None,
    max_results: int = 100,
    verbose: bool = True,
) -> Tuple[List[str], Optional[List]]:
    """
    默认路径：优先按实体查；无实体时用关键词尝试识别实体再查。
    返回 (child_ids, scores)，无得分时 scores 为 None。
    """
    if entities:
        if verbose:
            logger.debug(" 使用实体查询模式")
        try:
            if hasattr(graph_store, "query_child_ids_by_entities"):
                result = graph_store.query_child_ids_by_entities(
                    entity_names=entities,
                    relation_type=None,
                    max_results=max_results,
                    verbose=verbose,
                )
                return (result[0], result[1]) if isinstance(result, tuple) else (result, None)
        except Exception as e:
            if verbose:
                logger.warning(" 实体查询失败: %s", e)
        return ([], None)

    if query and keywords:
        if verbose:
            logger.debug(" 使用关键词查询模式")
        return _query_by_keywords(graph_store, query, keywords, max_results, verbose)

    if verbose:
        logger.warning(" 既无实体也无关键词，无法执行图谱查询")
    return ([], None)


def _query_by_keywords(
    graph_store: GraphStore,
    query: str,
    keywords: List[str],
    max_results: int,
    verbose: bool,
) -> Tuple[List[str], Optional[List]]:
    """从关键词中识别实体后执行实体查询。返回 (child_ids, scores) 或 ([], None)。"""
    if not keywords:
        return ([], None)
    # 短中文片段视为潜在实体
    potential = [
        k for k in keywords
        if _MIN_ENTITY_KW_LEN <= len(k) <= _MAX_ENTITY_KW_LEN and any("\u4e00" <= c <= "\u9fff" for c in k)
    ]
    if not potential:
        if verbose:
            logger.debug(" 未找到可能的实体关键词")
        return ([], None)
    valid = []
    for name in potential:
        try:
            if graph_store.get_entity_info(name):
                valid.append(name)
        except Exception:
            continue
    if not valid:
        if verbose:
            logger.debug(" 关键词中无图谱实体")
        return ([], None)
    if verbose:
        logger.info(" 从关键词识别实体: %s", valid)
    try:
        if hasattr(graph_store, "query_child_ids_by_entities"):
            result = graph_store.query_child_ids_by_entities(
                entity_names=valid,
                relation_type=None,
                max_results=max_results,
                verbose=verbose,
            )
            return (result[0], result[1]) if isinstance(result, tuple) else (result, None)
    except Exception as e:
        if verbose:
            logger.warning(" 关键词查询失败: %s", e)
    return ([], None)


# -----------------------------------------------------------------------------
# 策略路径：合并召回 + 统一打分
# -----------------------------------------------------------------------------

def _build_score_list(
    pool_ids: List[str],
    cid_to_strategies: DefaultDict[str, Set[str]],
    entity_signals: Dict[str, Dict[str, Any]],
    score_entities: List[str],
    relation_type: Optional[str],
    intent: str,
    explicit_list: List[str],
    verbose: bool,
) -> Tuple[List[tuple], Dict[str, float]]:
    """
    对候选池做四维统一打分（hop / coverage / strategy_prior / evidence），
    按意图权重加权得到总分。
    返回 (score_list, cid_to_score)。score_list 项为 (cid, total, hop, cov, prior, evd)。
    """
    w_hop, w_cov, w_prior, w_evd = get_weights_for_intent(intent)
    if verbose and intent:
        logger.info(
            " 意图权重: intent=%r -> hop=%.2f cov=%.2f prior=%.2f evd=%.2f",
            intent, w_hop, w_cov, w_prior, w_evd,
        )

    max_strategy_evidence = max(1, len(explicit_list))
    max_evidence = MAX_ENTITY_PATHS + max_strategy_evidence
    query_entity_count = len(score_entities) if score_entities else 0

    score_list: List[tuple] = []
    for cid in pool_ids:
        cid_s = str(cid)
        sig = entity_signals.get(cid_s) or {}

        # 跳数：实体 hop 与策略 hop 取最小（越小越近）
        entity_hop = int(sig.get("hop_count", 3))
        strategy_hops = [STRATEGY_HOP.get(s, 3) for s in cid_to_strategies.get(cid_s, set())]
        strategy_hop = min(strategy_hops) if strategy_hops else 3
        hop_count = min(entity_hop, strategy_hop)
        hop_score = relation_distance_to_score(hop_count)

        covered = int(sig.get("covered_entity_count", 0))
        coverage_score = entity_coverage_to_score(covered, query_entity_count)

        strategies_hit = cid_to_strategies.get(cid_s, set())
        strategy_prior_score = max((STRATEGY_PRIOR.get(s, 0.0) for s in strategies_hit), default=0.0)

        entity_path_count = int(sig.get("entity_path_count", 0))
        strategy_hit_count = len(strategies_hit)
        evidence_score = evidence_count_to_score(entity_path_count + strategy_hit_count, max_evidence)

        total = compute_graph_relevance_score(
            hop_score=hop_score,
            entity_coverage_score=coverage_score,
            strategy_prior_score=strategy_prior_score,
            evidence_score=evidence_score,
            weight_hop=w_hop,
            weight_entity_coverage=w_cov,
            weight_strategy_prior=w_prior,
            weight_evidence=w_evd,
        )
        score_list.append((cid_s, total, hop_score, coverage_score, strategy_prior_score, evidence_score))

    score_list.sort(key=lambda x: -x[1])
    cid_to_score = {x[0]: x[1] for x in score_list}
    return score_list, cid_to_score


# -----------------------------------------------------------------------------
# 入口：search_graph
# -----------------------------------------------------------------------------

def search_graph(
    query: str,
    query_analysis: QueryAnalysis,
    graph_store: GraphStore,
    max_results: int = 100,
    verbose: bool = True,
) -> Tuple[List[str], Optional[List]]:
    """
    根据 query_analysis 执行图谱检索，返回 (child_ids, score_list)。

    两条路径：
    1. 默认路径：无显式策略或仅 entity_attribute → 实体/关键词查询，直接返回。
    2. 策略路径：有显式策略 → 默认路径（可选）+ 各策略依次执行，结果合并后统一打分、排序、截断。

    Returns:
        (child_ids, score_list)。score_list 与 child_ids 顺序一致，
        每项为 (cid, total, hop_score, coverage_score, strategy_prior_score, evidence_score)。
    """
    # ----- 1. 输入校验与参数规整 -----
    if not query_analysis:
        if verbose:
            logger.warning(" query_analysis 为空，不做图谱检索")
        return ([], None)

    if max_results > MAX_RESULTS_LIMIT:
        if verbose:
            logger.warning(" max_results=%d 超过上限，已限制为 %d", max_results, MAX_RESULTS_LIMIT)
        max_results = MAX_RESULTS_LIMIT
    elif max_results <= 0:
        if verbose:
            logger.warning(" max_results=%d 无效，使用默认值 100", max_results)
        max_results = 100

    # ----- 2. 从 query_analysis 解析字段 -----
    K = SchemaKeys
    entities_raw = query_analysis.get(K.QA_ENTITIES, [])
    entities = _normalize_entities(entities_raw)
    standard_entity_names = query_analysis.get(K.QA_STANDARD_ENTITY_NAMES) or entities
    expanded_entities = query_analysis.get(K.QA_EXPANDED_ENTITIES) or entities
    keywords = list(query_analysis.get(K.QA_KEYWORDS) or [])
    strategies_raw = query_analysis.get(K.QA_STRATEGIES) or []
    strategies = (
        [s.strip() for s in strategies_raw if s and isinstance(s, str) and s.strip()]
        if isinstance(strategies_raw, list) and len(strategies_raw) > 0
        else []
    )
    relation_type = query_analysis.get(K.QA_RELATION_TYPE)
    intent = (query_analysis.get(K.QA_INTENT) or "").strip()

    # ----- 3. 确定显式策略列表与是否走默认路径 -----
    # 显式策略：需走 GraphStrategyExecutor（除 entity_attribute 外的 1~8）
    explicit_list = [s for s in strategies if s in EXPLICIT_STRATEGIES]
    # narrative_events 排在最前，保证其召回优先参与后续 narrative 筛选
    if STRATEGY_TYPES[8] in explicit_list:
        explicit_list = [STRATEGY_TYPES[8]] + [s for s in explicit_list if s != STRATEGY_TYPES[8]]
    has_explicit = bool(explicit_list)
    use_default_strategy = STRATEGY_TYPES[0] in strategies or (not strategies)

    if verbose:
        logger.info(
            " 图谱检索: intent=%s, strategies=%s, entities=%d, relation_type=%s",
            intent, strategies or ["（默认）"], len(entities), relation_type,
        )

    # ----- 4. 仅默认路径：无显式策略时直接实体/关键词查询并返回 -----
    if use_default_strategy and not has_explicit:
        if verbose:
            logger.info(
" 未指定策略，按实体/关键词查询"
                if not strategies
else " 使用默认策略 entity_attribute（实体/关键词查询）"
            )
        return _default_graph_query(
            graph_store,
            query=query,
            keywords=keywords,
            entities=expanded_entities or standard_entity_names,
            max_results=max_results,
            verbose=verbose,
        )

    # ----- 5. 策略路径：构建 understanding，合并召回（默认 + 各显式策略） -----
    understanding: Dict[str, Any] = {
        K.QA_INTENT: intent,
        K.QA_ENTITIES: standard_entity_names,
        K.QA_RELATION_TYPE: relation_type,
        K.QA_KEYWORDS: keywords,
    }
    if expanded_entities:
        understanding[K.QA_EXPANDED_ENTITIES] = expanded_entities

    all_ids: List[str] = []
    seen: Set[str] = set()
    cid_to_strategies: DefaultDict[str, Set[str]] = defaultdict(set)

    if use_default_strategy:
        default_ids, _ = _default_graph_query(
            graph_store,
            query=query,
            keywords=keywords,
            entities=expanded_entities or standard_entity_names,
            max_results=max_results,
            verbose=verbose,
        )
        for cid in (default_ids or []):
            if cid and cid not in seen:
                seen.add(cid)
                all_ids.append(cid)
        if verbose and default_ids:
            logger.info(" 默认路径（实体+关键词）返回 %d 个子文档ID", len(default_ids))

    executor = GraphStrategyExecutor(graph_store, verbose=verbose)
    for s in explicit_list:
        understanding[K.QA_STRATEGY] = s
        child_ids = executor.execute(understanding, max_results=max_results)
        for cid in (child_ids or []):
            if cid and cid not in seen:
                seen.add(cid)
                all_ids.append(cid)
            if cid:
                cid_to_strategies[str(cid)].add(s)
        if verbose and child_ids:
            logger.info(" 策略 %s 返回 %d 个子文档ID", s, len(child_ids))

    # ----- 6. 确定候选池（参与打分的 ID 列表） -----
    pool_ids = all_ids[:max_results]

    if not pool_ids:
        return ([], None)

    score_entities = expanded_entities or standard_entity_names
    entity_signals: Dict[str, Dict[str, Any]] = {}
    if score_entities and hasattr(graph_store, "get_entity_signals_for_child_ids"):
        try:
            entity_signals = graph_store.get_entity_signals_for_child_ids(
                entity_names=score_entities,
                child_ids=pool_ids,
                relation_type=relation_type,
            )
        except Exception:
            entity_signals = {}

    # ----- 7. 统一打分并得到 cid_to_score -----
    score_list, cid_to_score = _build_score_list(
        pool_ids=pool_ids,
        cid_to_strategies=cid_to_strategies,
        entity_signals=entity_signals,
        score_entities=score_entities,
        relation_type=relation_type,
        intent=intent,
        explicit_list=explicit_list,
        verbose=verbose,
    )

    # ----- 8. 按分数排序并截断 -----
    ids_sorted = [x[0] for x in score_list][:max_results]

    # ----- 9. 只保留最终 id 对应的 score，且顺序与 ids_sorted 一致 -----
    id_order = {cid: i for i, cid in enumerate(ids_sorted)}
    score_list_final = [x for x in score_list if x[0] in id_order]
    score_list_final.sort(key=lambda x: id_order[x[0]])

    return (ids_sorted, score_list_final)
