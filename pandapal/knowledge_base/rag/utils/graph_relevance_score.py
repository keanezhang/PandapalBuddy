# -*- coding: utf-8 -*-
"""
图谱检索统一相关性加权分（彻底改版）

目标：统一支持「默认实体召回」与「策略召回」两类图谱检索形态，输出可解释且可融合的总分。

统一总分公式（推荐）：
  total = w_hop * hop_score
        + w_cov * entity_coverage_score
        + w_prior * strategy_prior_score
        + w_evd * evidence_score

其中四项均建议归一化到 0~1：
- hop_score：最小跳数的接近程度（越近越高）
- entity_coverage_score：覆盖查询实体的比例（越多越高）
- strategy_prior_score：命中策略的先验重要程度（越贴意图越高；无策略则为 0）
- evidence_score：证据数量/强度的归一化（例如命中多条路径/多种策略）

按意图动态权重：不同意图下四维比重不同（见 get_weights_for_intent）。
"""

from typing import Dict, Optional, Tuple

# 默认权重(与下方默认参数一致):hop/cov 偏重,prior/evidence 次之
DEFAULT_WEIGHTS: Tuple[float, float, float, float] = (0.35, 0.35, 0.2, 0.1)

# 意图 -> (w_hop, w_entity_coverage, w_strategy_prior, w_evidence),和为 1.0
# 意图类型取值需与 SchemaKeys / query_intent_classifier.INTENT_TYPES 一致
INTENT_WEIGHTS: Dict[str, Tuple[float, float, float, float]] = {
    "情节型查询": (0.20, 0.25, 0.35, 0.20),   # 叙事/因果:策略先验与证据更重要
    "关系型查询": (0.40, 0.35, 0.15, 0.10),   # 谁和谁什么关系:跳数近、多实体覆盖
    "事实型查询": (0.40, 0.35, 0.15, 0.10),   # 是谁/住哪/做什么:直接事实,hop+coverage
    "总结型查询": (0.20, 0.25, 0.35, 0.20),   # 概括/前几章:同情节,prior+evidence
    "列表型查询": (0.25, 0.35, 0.25, 0.15),   # X 的徒弟有谁:覆盖多实体 + 策略贴列表
    "比较型查询": (0.25, 0.35, 0.20, 0.20),   # 谁更…:多实体、多证据
    "导航型查询": (0.45, 0.25, 0.20, 0.10),   # 第几章/哪一节:位置近优先
}


def get_weights_for_intent(intent: Optional[str]) -> Tuple[float, float, float, float]:
    """
    根据查询意图返回四维权重 (w_hop, w_entity_coverage, w_strategy_prior, w_evidence)。
    未匹配意图或 intent 为空时返回默认权重;返回值保证和为 1.0。

    Args:
        intent: 意图类型字符串(如「情节型查询」),来自 query_analysis.intent

    Returns:
        四元组 (weight_hop, weight_entity_coverage, weight_strategy_prior, weight_evidence)
    """
    if not intent or not isinstance(intent, str):
        return DEFAULT_WEIGHTS
    
    key = intent.strip()
    if key in INTENT_WEIGHTS:
        return INTENT_WEIGHTS[key]
    
    return DEFAULT_WEIGHTS


def compute_graph_relevance_score(
    hop_score: Optional[float] = None,
    entity_coverage_score: Optional[float] = None,
    strategy_prior_score: Optional[float] = None,
    evidence_score: Optional[float] = None,
    weight_hop: Optional[float] = None,
    weight_entity_coverage: Optional[float] = None,
    weight_strategy_prior: Optional[float] = None,
    weight_evidence: Optional[float] = None,
    _default_weights: Tuple[float, float, float, float] = DEFAULT_WEIGHTS,
) -> float:
    """
    计算图谱统一相关性加权总分(改版后)。

    四项维度(均建议归一化到 0~1,不存在时传 None 或视为 0):
    - hop_score: 最小跳数接近程度,1=最近(0跳/实体提及),越小越远
    - entity_coverage_score: 覆盖查询实体比例,1=全覆盖
    - strategy_prior_score: 策略先验重要程度,1=最贴意图;无策略为 0
    - evidence_score: 证据数量/强度归一化,1=证据最充足

    权重:若四个 weight_* 均为 None,使用 _default_weights(默认 DEFAULT_WEIGHTS);
    若调用方传入 intent,建议用 get_weights_for_intent(intent) 得到四维权重后传入。

    Args:
        hop_score: 跳数接近程度得分,None 视为 0
        entity_coverage_score: 多实体覆盖维度得分,None 视为 0
        strategy_prior_score: 策略先验维度得分,None 视为 0
        evidence_score: 证据维度得分,None 视为 0
        weight_hop: 跳数权重,None 时用 _default_weights[0]
        weight_entity_coverage: 多实体覆盖权重,None 时用 _default_weights[1]
        weight_strategy_prior: 策略先验权重,None 时用 _default_weights[2]
        weight_evidence: 证据权重,None 时用 _default_weights[3]
        _default_weights: 当四个 weight_* 均为 None 时使用的 (hop, cov, prior, evd)

    Returns:
        加权总分;四项都不存在时为 0.0
    """
    w_hop = weight_hop if weight_hop is not None else _default_weights[0]
    w_cov = weight_entity_coverage if weight_entity_coverage is not None else _default_weights[1]
    w_prior = weight_strategy_prior if weight_strategy_prior is not None else _default_weights[2]
    w_evd = weight_evidence if weight_evidence is not None else _default_weights[3]
    
    def _s(x: Optional[float]) -> float:
        """将输入值限制在 0~1 范围内"""
        if x is None:
            return 0.0
        return max(0.0, min(1.0, float(x)))

    s_hop = _s(hop_score)
    s_cov = _s(entity_coverage_score)
    s_prior = _s(strategy_prior_score)
    s_evd = _s(evidence_score)

    total = w_hop * s_hop + w_cov * s_cov + w_prior * s_prior + w_evd * s_evd
    return round(total, 6)


def evidence_count_to_score(evidence_count: int, max_evidence: int) -> float:
    """
    将「证据次数」归一化到 0~1，供 compute_graph_relevance_score 的 evidence_score 使用。

    Args:
        evidence_count: 证据次数（例如命中路径数、命中策略数等）
        max_evidence: 最大可能证据次数（用于归一化）

    Returns:
        0~1 的分数，0 表示无证据
    """
    if max_evidence <= 0 or evidence_count <= 0:
        return 0.0
    return min(1.0, evidence_count / max_evidence)


def entity_coverage_to_score(covered_entity_count: int, query_entity_count: int) -> float:
    """
    将「覆盖的查询实体数」归一化到 0~1，供 compute_graph_relevance_score 的 entity_coverage_score 使用。

    Args:
        covered_entity_count: 该 chunk 覆盖的查询实体数
        query_entity_count: 查询中的实体总数

    Returns:
        0~1 的分数，0 表示未覆盖任何查询实体
    """
    if query_entity_count <= 0:
        return 0.0
    return min(1.0, covered_entity_count / query_entity_count)


def relation_distance_to_score(hop_count: int) -> float:
    """
    将「关系跳数」映射为 0~1 的距离分(1=最近),供 compute_graph_relevance_score 的 relation_distance_score 使用。

    约定:0 跳(实体提及) = 1.0,1 跳 = 0.8,2 跳 = 0.5,3+ 跳 = 0.3。

    Args:
        hop_count: 从查询实体到该 chunk 的最小跳数,0 表示实体直接提及

    Returns:
        0~1 的分数
    """
    _HOP_SCORE_MAP = {0: 1.0, 1: 0.8, 2: 0.5}
    return _HOP_SCORE_MAP.get(hop_count, 0.3) if hop_count >= 0 else 0.3
