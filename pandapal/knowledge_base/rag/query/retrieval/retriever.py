#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检索模块 - 父子文档索引版本

负责多路检索与融合，返回子文档列表（List[ChildDocument]）：
1. 单路：仅向量混合（语义 + 关键词，search_vector_hybrid_raw）+ 本层格式化
2. 三路：向量 + BM25 + 图谱召回，按 parent 合并、RRF/加权排序后，由入口统一做实体过滤与截断
3. 子文档含 parent_id、child_id、content、distance、match_type 等，不返回完整父文档

【标准名/别名交叉处理设计】
用户查询可能是标准名或别名，存储的子文档/实体/关系也可能是标准名或别名。统一策略：

  ┌─────────────────┬──────────────────┬─────────────────────────────────────────────┐
  │ 用户查询        │ 存储侧           │ 处理方式                                    │
  ├─────────────────┼──────────────────┼─────────────────────────────────────────────┤
  │ 标准名          │ 标准名            │ 直接匹配（图谱按 name，文档按原文）          │
  │ 标准名          │ 别名              │ 图谱：query_child_ids_by_entities 传标准+别名 │
  │ 别名            │ 标准名            │ 先解析别名→标准名（get_entity_info）再查     │
  │ 别名            │ 别名              │ 同上 + 实体匹配/关键词用扩展列表（标准+别名）  │
  └─────────────────┴──────────────────┴─────────────────────────────────────────────┘

- 图谱：search_graph 使用上层富化后的 expanded_entities / standard_entity_names。
- 实体匹配/向量/BM25：统一使用上层富化后的 query_analysis（在收到意图 LLM 结果后只做一次富化，再下发给三种检索）。

【三路召回融合逻辑】
1. 三种检索均直接返回子文档列表 List[ChildDocument]（图谱由子文档 ID 在检索层拉取内容后转为子文档）。
2. 按子文档 id（child_id）合并：同一 child_id 在三路中多次出现则合并为一条，并合并三路标记（CD_RETRIEVAL_SOURCES、CD_BM25_HIT、CD_GRAPH_HIT），不丢任何一路的命中记录。
3. RRF 打分：按各路子文档列表中的 child_id 排名，对每个合并后的 doc 用 RRF 公式加权求和得到 CD_RRF_SCORE，再排序。
4. 统计与返回：按 CD_RRF_SCORE 排序后截断前 N 个，并统计前 N 个里各召回源命中数（基于 CD_RETRIEVAL_SOURCES / CD_BM25_HIT / CD_GRAPH_HIT）。

数量约定：单路检索（向量/BM25/图谱）每路最多返回 PER_SOURCE_RETRIEVAL_COUNT（25）条；
融合、实体过滤与截断后最多返回 MAX_RETRIEVAL_RESULT_COUNT（15）条。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from ...store.vector.vector_store import VectorStore
from ...utils.chroma_utils import flatten_chroma_get_result
from ...embedding.embedding import EmbeddingEncoder
from .bm25_retrieval import search_bm25_raw
from .graph_retrieval import search_graph
from .vector_retrieval import search_vector_hybrid_raw
from ...schema import (
    QueryAnalysis,
    ChildDocument,
    RawRetrievalResult,
    SchemaKeys,
    validate_query_analysis_structure,
)
from .search_constants import (
    MATCH_TYPE_BM25,
    MATCH_TYPE_VECTOR,
    MATCH_TYPE_GRAPH,
    MATCH_TYPE_MULTI_SOURCE,
    RETRIEVAL_SOURCE_VECTOR,
    RETRIEVAL_SOURCE_BM25,
    RETRIEVAL_SOURCE_GRAPH,
)
from ..enrich.query_enricher import enrich_query_analysis_once

if TYPE_CHECKING:
    from ...store.bm25.bm25_store import BM25Store
    from ...store.graph.graph_store import GraphStore

logger = logging.getLogger(__name__)

# 检索返回数量：单路召回每路最多返回数；融合后最多返回数（重排序与最终条数由 rag_instance 控制）
PER_SOURCE_RETRIEVAL_COUNT = 25   # 向量/BM25/图谱每路单独检索最多返回 25 条
MAX_RETRIEVAL_RESULT_COUNT = 15   # 融合后返回 ≤15 条（供重排序使用）

# 三路融合默认权重（无意图或未匹配时使用），(vector, bm25, graph)，和为 1.0
DEFAULT_FUSION_WEIGHTS: Tuple[float, float, float] = (0.4, 0.3, 0.3)

# 意图 -> 三路融合权重 (vector, bm25, graph)，和为 1.0；意图取值与 SchemaKeys / query_intent_classifier.INTENT_TYPES 一致
FUSION_WEIGHTS_BY_INTENT: Dict[str, Tuple[float, float, float]] = {
    "关系型查询": (0.25, 0.25, 0.50),   # 谁和谁什么关系：图谱为主
    "情节型查询": (0.35, 0.25, 0.40),   # 发生了什么、因果：图谱+向量
    "事实型查询": (0.45, 0.35, 0.20),   # 是谁/住哪/做什么：向量+BM25
    "总结型查询": (0.40, 0.20, 0.40),   # 概括、前几章：向量+图谱
    "列表型查询": (0.35, 0.25, 0.40),   # X 的徒弟有谁：图谱+向量
    "比较型查询": (0.35, 0.25, 0.40),   # 谁更…：多路均衡偏图谱
    "导航型查询": (0.35, 0.40, 0.25),   # 第几章/哪一节：BM25+向量
}


def get_fusion_weights_for_intent(intent: Optional[str]) -> Tuple[float, float, float]:
    """
    根据查询意图返回三路融合权重 (vector_weight, bm25_weight, graph_weight)，和为 1.0。
    用于 RRF/weighted 融合时按意图动态调配三路比重；未匹配意图时返回 DEFAULT_FUSION_WEIGHTS。
    """
    if not intent or not isinstance(intent, str):
        return DEFAULT_FUSION_WEIGHTS
    key = intent.strip()
    if key in FUSION_WEIGHTS_BY_INTENT:
        return FUSION_WEIGHTS_BY_INTENT[key]
    return DEFAULT_FUSION_WEIGHTS


def filter_and_sort_by_entity_match(
    documents: List[ChildDocument],
    entities: List[str],
    max_results: int = MAX_RETRIEVAL_RESULT_COUNT,
    drop_zero_match: bool = True,
) -> List[ChildDocument]:
    """
    根据实体匹配数量过滤和排序文档
    
    【排序规则】
    1. 与query给的实体数量一致的排在最前面（完全匹配）
    2. 按照匹配实体数量依次类推（部分匹配）
    3. 0实体匹配的：drop_zero_match=True 时丢弃，False 时保留并排在最后
    4. 同实体匹配数下按 distance 升序（distance 越小 BM25/相似度越好），保证 BM25 高分不会排在低分后面

    【单路向量】当所有候选都完全匹配实体时，最终顺序 = 纯按向量 distance 升序（即相似度高的在前）。
    语义相似度高的片段不一定最“切题”（例如“陈平安是谁”时，介绍性段落可能相似度低于叙事段落），
    若需“谁是谁”类问答优先介绍型片段，可在此层之后加 Reranker 或按意图对定义型片段加权。
    
    【示例】drop_zero_match=True
    假设查询有2个实体：['陈平安', '刘羡阳']
    - 同时包含2个实体的文档 → 排在最前面
    - 只包含1个实体的文档 → 排在后面
    - 不包含任何实体的文档 → 丢弃
    
    【图谱召回】drop_zero_match=False
    图谱已按实体关系选出 chunk，向量库取回的内容可能用词不同（如「镇子」），
    因此仅按匹配数排序，不丢弃 0 匹配的文档。
    
    Args:
        documents: 文档列表
        entities: 查询中的实体列表（实体名称字符串列表）
        max_results: 最大返回结果数（可以小于等于此值）
        drop_zero_match: 是否丢弃 0 实体匹配的文档；图谱召回时建议 False
        
    Returns:
        过滤和排序后的文档列表（最多max_results个，但可能少于max_results）
    """
    if not entities:
        # 如果没有实体，直接返回原列表（限制数量）
        return documents[:max_results]

    # 过滤空串/纯空格实体，避免 "" in content 使所有文档被误判为匹配
    entities = [e for e in entities if e and (e.strip() if isinstance(e, str) else True)]
    if not entities:
        return documents[:max_results]

    # 计算每个文档匹配的实体数量
    scored_docs = []
    K = SchemaKeys
    for doc in documents:
        content = doc.get(K.CD_CONTENT, '')
        matched_count = sum(1 for entity in entities if entity in content)
        
        if matched_count > 0:
            scored_docs.append((matched_count, doc))
        elif not drop_zero_match:
            # 图谱召回等场景：保留 0 匹配，仅参与排序（排在最后）
            scored_docs.append((0, doc))
    
    # 按匹配数量降序、再按 distance 升序（同实体数下 BM25/相似度高的在前）
    # distance 可能为 None（异常数据），统一按 +inf 处理，避免排序时 None 参与比较抛 TypeError
    def _safe_distance(doc: ChildDocument) -> float:
        d = doc.get(K.CD_DISTANCE)
        if d is None:
            return float("inf")
        try:
            return float(d)
        except (TypeError, ValueError):
            return float("inf")

    scored_docs.sort(key=lambda x: (-x[0], _safe_distance(x[1])))
    
    # 提取文档（去掉分数）
    filtered_docs = [doc for _, doc in scored_docs]
    
    # 返回最多max_results个（但可能少于max_results）
    return filtered_docs[:max_results]


def validate_query_analysis(query_analysis: Optional[QueryAnalysis], raise_on_missing: bool = True) -> QueryAnalysis:
    """
    验证并标准化query_analysis格式
    
    Args:
        query_analysis: 查询分析结果（QueryAnalysis类型）
        raise_on_missing: 如果query_analysis为None是否抛出异常
        
    Returns:
        标准化后的QueryAnalysis
        
    Raises:
        ValueError: 如果query_analysis为None且raise_on_missing=True，或格式不合法
    """
    if query_analysis is None:
        if raise_on_missing:
            raise ValueError("query_analysis 不能为空，请先进行意图识别和查询分析")
        from ...schema import create_empty_query_analysis
        return create_empty_query_analysis()
    
    return validate_query_analysis_structure(query_analysis)


def validate_child_chunk_id(chunk_id: Any) -> bool:
    """
    验证子文档ID格式（只支持新格式 _child_）
    
    Args:
        chunk_id: 子文档ID
        
    Returns:
        是否为有效的子文档ID格式
    """
    if not isinstance(chunk_id, str):
        return False
    if not chunk_id or chunk_id == "N/A":
        return False
    # 只支持新格式 _child_，不再兼容旧格式 _chunk_
    return "_child_" in chunk_id


class Retriever:
    """
    检索器 - 父子文档索引版本
    
    职责：编排向量/BM25/图谱检索，按 parent 合并与融合，返回子文档列表。
    - 有 bm25_store 且 graph_store：三路召回 + 融合 + 入口实体过滤与截断
    - 否则：单路向量混合 raw + 本层格式化 + 入口实体过滤与截断
    返回 List[ChildDocument]（含 parent_id、child_id、content、distance、match_type 等），不返回完整父文档。
    """

    def __init__(
        self,
        vector_store: VectorStore,
        embedding_encoder: EmbeddingEncoder,
        graph_store: Optional["GraphStore"] = None,
        bm25_store: Optional["BM25Store"] = None,
        verbose: bool = True,
        *,
        vector_enable_keyword_in_three_path: bool = True,
    ):
        """初始化检索器。纯 SDK：vector_enable_keyword_in_three_path 由参数传入。"""
        self.vector_store = vector_store
        self.embedding_encoder = embedding_encoder
        self.graph_store = graph_store
        self.bm25_store = bm25_store
        self.verbose = verbose
        self._vector_enable_keyword_in_three_path = vector_enable_keyword_in_three_path

    def _enrich_query_analysis_once(self, query_analysis: QueryAnalysis) -> QueryAnalysis:
        """委托给模块级 enrich_query_analysis_once，便于 Retriever 与仅图谱测试共用。"""
        return enrich_query_analysis_once(
            query_analysis,
            graph_store=self.graph_store,
            verbose=self.verbose,
        )
    
    def _ensure_enriched(self, query_analysis: QueryAnalysis) -> QueryAnalysis:
        """确保 query_analysis 已富化，未富化则执行富化"""
        K = SchemaKeys
        if query_analysis.get(K.QA_EXPANDED_ENTITIES) is None:
            return self._enrich_query_analysis_once(query_analysis)
        return query_analysis

    def _entities_for_match(self, query_analysis: QueryAnalysis) -> List[str]:
        """供实体匹配使用：优先用上层富化后的 expanded_entities，否则用原始 entities 文本。"""
        K = SchemaKeys
        expanded = query_analysis.get(K.QA_EXPANDED_ENTITIES)
        if expanded:
            return expanded
        raw = [e.get(K.ENT_TEXT, "").strip() for e in query_analysis.get(K.QA_ENTITIES, []) if isinstance(e, dict) and e.get(K.ENT_TEXT)]
        return list(dict.fromkeys(raw))

    def search(
        self,
        query: str,
        query_analysis: Optional[QueryAnalysis] = None,
        fusion_strategy: str = "rrf",
        *,
        vector_weight: Optional[float] = None,
        bm25_weight: Optional[float] = None,
        graph_weight: Optional[float] = None,
    ) -> List[ChildDocument]:
        """
        搜索相关文档（返回子文档列表）
        
        职责：只负责检索与融合，不负责意图分析和分词；意图与富化由外部完成，query_analysis 必需传入。
        
        流程：
        1. 若有 bm25_store 且 graph_store：三路召回（向量 + BM25 + 图谱）并融合排序
        2. 否则：单路向量混合（search_vector_hybrid_raw）+ 本层格式化为 ChildDocument
        3. 入口统一：实体匹配过滤与截断至 MAX_RETRIEVAL_RESULT_COUNT，再返回
        
        Args:
            query: 搜索查询
            query_analysis: 查询分析结果（必需），由外部意图分类与富化后传入，格式含：
                intent, strategies, entities, relation_type, segmented_words, keywords 等
            fusion_strategy: 融合策略（"rrf" 或 "weighted"），仅三路时生效
            vector_weight, bm25_weight, graph_weight: 三路权重（0-1），三路融合时使用；
                有意图识别结果时会按意图动态覆盖（如关系型抬图谱、事实型抬向量+BM25）
            
        Returns:
            子文档列表（最多 MAX_RETRIEVAL_RESULT_COUNT 个），每项含 content, metadata, distance,
            child_id, parent_id, match_type（"vector"、"bm25"、"graph" 等）
        """

        # 记录检索信息（召回策略将在后续根据组件可用性确定）
        if self.verbose:
            logger.info("\n%s", "=" * 60)
            logger.info(" 开始检索: '%s'", query)
            logger.info("   目标返回: 最多 %d 个子文档（融合后，供重排序）", MAX_RETRIEVAL_RESULT_COUNT)
            logger.info("   融合策略: %s", fusion_strategy)
            logger.info("%s", "=" * 60)
        
        # ==================== 第一步：统一数据格式 ====================
        # 验证并标准化query_analysis格式
        query_analysis = validate_query_analysis(query_analysis, raise_on_missing=True)
        # 上层统一富化（标准名+别名扩展）：若调用方已做富化（如 CLI 前置步骤）则跳过，避免重复
        query_analysis = self._ensure_enriched(query_analysis)

        # ==================== 第二步：执行召回 ====================
        # 有三路（向量 + BM25 + 图谱）则执行三路融合，否则单路：向量混合 raw + 本层格式化
        if self.bm25_store is not None and self.graph_store is not None:
            if self.verbose:
                logger.info("   召回策略: 三路召回（向量 + BM25 + 图谱）")
            # 三路融合权重：优先使用调用方显式传入；否则按意图动态调配（关系型抬图谱、事实型抬向量+BM25 等）
            vw = bw = gw = None
            if vector_weight is not None or bm25_weight is not None or graph_weight is not None:
                # 要求三者都给出，否则回退到按意图权重，避免“半配置”造成不可解释结果
                if vector_weight is None or bm25_weight is None or graph_weight is None:
                    if self.verbose:
                        logger.warning("  仅部分提供三路权重，将忽略显式权重并回退到按意图权重")
                else:
                    vw, bw, gw = float(vector_weight), float(bm25_weight), float(graph_weight)
                    if self.verbose:
                        logger.info("   融合权重（显式）: vector=%.2f bm25=%.2f graph=%.2f", vw, bw, gw)
            if vw is None:
                K = SchemaKeys
                intent = (query_analysis.get(K.QA_INTENT) or "").strip()
                vw, bw, gw = get_fusion_weights_for_intent(intent)
                if self.verbose and intent:
                    logger.info("   融合权重（按意图）: intent=%r -> vector=%.2f bm25=%.2f graph=%.2f", intent, vw, bw, gw)
            results = self._fuse_triple_retrieval(
                query=query,
                query_analysis=query_analysis,
                fusion_strategy=fusion_strategy,
                vector_weight=vw,
                bm25_weight=bw,
                graph_weight=gw
            )
        else:
            search_k = PER_SOURCE_RETRIEVAL_COUNT
            raw = search_vector_hybrid_raw(
                vector_store=self.vector_store,
                embedding_encoder=self.embedding_encoder,
                query=query,
                query_analysis=query_analysis,
                n_results=search_k,
                verbose=self.verbose,
            )
            results = self._format_child_results(raw, match_type=MATCH_TYPE_VECTOR, max_count=search_k)
        
        # 入口统一：实体匹配过滤与截断（三路/单路内部不再重复处理）
        # 三路召回时保留 0 实体匹配文档（图谱召回的 chunk 可能正文用词与查询实体不同），单路时丢弃
        entities = self._entities_for_match(query_analysis)
        drop_zero = not (self.bm25_store is not None and self.graph_store is not None)
        if entities:
            results = filter_and_sort_by_entity_match(
                results, entities, MAX_RETRIEVAL_RESULT_COUNT, drop_zero_match=drop_zero
            )
        else:
            results = results[:MAX_RETRIEVAL_RESULT_COUNT]
        return results

    def search_bm25(
        self,
        query_analysis: Optional[QueryAnalysis] = None,
        n_results: Optional[int] = None
    ) -> List[ChildDocument]:
        """
        仅 BM25 检索（用于单路 BM25 验证，如 test_bm25_only）。
        不执行向量与图谱召回；做 query_analysis 校验与富化、BM25 raw 检索、
        格式化为 ChildDocument、实体过滤与截断。

        Args:
            query_analysis: 查询分析结果（必需），含 segmented_words 等
            n_results: 返回数量上限，None 时使用 PER_SOURCE_RETRIEVAL_COUNT 做召回再截断

        Returns:
            子文档列表（最多 MAX_RETRIEVAL_RESULT_COUNT 个）
        """
        if self.bm25_store is None:
            raise ValueError("BM25Store 未提供，无法执行 BM25 检索")
        query_analysis = validate_query_analysis(query_analysis, raise_on_missing=True)
        query_analysis = self._ensure_enriched(query_analysis)
        search_k = n_results if n_results is not None else PER_SOURCE_RETRIEVAL_COUNT
        raw = search_bm25_raw(
            bm25_store=self.bm25_store,
            query_analysis=query_analysis,
            n_results=search_k,
            verbose=self.verbose,
        )
        results = self._format_child_results(raw, match_type=MATCH_TYPE_BM25, max_count=search_k)
        entities = self._entities_for_match(query_analysis)
        if entities:
            results = filter_and_sort_by_entity_match(results, entities, MAX_RETRIEVAL_RESULT_COUNT)
        else:
            results = results[:MAX_RETRIEVAL_RESULT_COUNT]
        return results

    def _format_child_results(
        self,
        child_results: RawRetrievalResult,
        match_type: str = "vector",
        max_count: int = MAX_RETRIEVAL_RESULT_COUNT
    ) -> List[ChildDocument]:
        """
        将检索结果转换为子文档格式
        
        【使用场景】
        此方法用于处理从ChromaDB/BM25等存储系统返回的原始检索结果格式。
        输入是包含 documents、metadatas、distances、ids 等字段的字典结构。
        
        Args:
            child_results: 检索结果字典，包含 documents, metadatas, distances, ids
                - documents: List[List[str]] - 文档内容列表（嵌套列表格式）
                - metadatas: List[List[Dict]] - 元数据列表（嵌套列表格式）
                - distances: List[List[float]] - 距离列表（嵌套列表格式）
                - ids: List[List[str]] - 文档ID列表（嵌套列表格式）
            match_type: 匹配类型（"vector"、"bm25"、"keyword"、"hybrid" 等）
            max_count: 最大返回数量
            
        Returns:
            标准化的子文档列表，每个元素包含：
            - content: str - 文档内容
            - metadata: Dict - 文档元数据
            - distance: float - 距离分数
            - child_id: str - 子文档ID
            - parent_id: str - 父文档ID
            - match_type: str - 匹配类型
        """
        K = SchemaKeys
        child_docs = []
        if child_results.get(K.RR_DOCUMENTS) and len(child_results[K.RR_DOCUMENTS]) > 0:
            documents = child_results[K.RR_DOCUMENTS][0]
            metadatas_raw = child_results.get(K.RR_METADATAS, [[]])
            distances_raw = child_results.get(K.RR_DISTANCES, [[]])
            ids_raw = child_results.get(K.RR_IDS, [[]])
            metadatas = metadatas_raw[0] if metadatas_raw and len(metadatas_raw) > 0 else []
            distances = distances_raw[0] if distances_raw and len(distances_raw) > 0 else []
            ids = ids_raw[0] if ids_raw and len(ids_raw) > 0 else []

            for i, doc in enumerate(documents[:max_count]):
                metadata = metadatas[i] if i < len(metadatas) else {}
                distance = distances[i] if i < len(distances) else None
                # 有有效 id 时用 id，否则用带 match_type 的 fallback，避免与其它召回源的 fallback 合并成同一条
                child_id = (
                    ids[i]
                    if (i < len(ids) and ids[i] and str(ids[i]).strip())
                    else f"child_{match_type}_{i}"
                )

                child_doc: ChildDocument = {
                    K.CD_CONTENT: doc,
                    K.CD_METADATA: metadata,
                    K.CD_DISTANCE: distance,
                    K.CD_CHILD_ID: child_id,
                    K.CD_PARENT_ID: metadata.get(K.CDM_PARENT_ID),
                    K.CD_MATCH_TYPE: match_type,
                    K.CD_RETRIEVAL_SOURCES: [match_type] if match_type else [],
                    K.CD_GRAPH_HIT: match_type == MATCH_TYPE_GRAPH if match_type else False,
                    K.CD_BM25_HIT: match_type == MATCH_TYPE_BM25 if match_type else False,
                    K.CD_RERANK_SCORE: None,
                    K.CD_RRF_SCORE: None,
                    K.CD_FINAL_SCORE: None,
                }
                child_docs.append(child_doc)

        return child_docs

    def _execute_retrieval_sources(
        self,
        query: str,
        query_analysis: QueryAnalysis
    ) -> Tuple[List[ChildDocument], List[ChildDocument], List[ChildDocument]]:
        """
        执行所有召回源的检索；三种检索均直接返回子文档列表 List[ChildDocument]。

        1. 向量检索：必须调用，返回子文档列表
        2. BM25检索：若提供 bm25_store 则调用，返回子文档列表
        3. 图谱检索：若提供 graph_store 则调用，先拿子文档 ID 再拉取内容转为子文档列表

        Returns:
            (vector_results, bm25_results, graph_results) 三元组，均为 List[ChildDocument]
        """
        vector_results: List[ChildDocument] = []
        bm25_results: List[ChildDocument] = []
        graph_results: List[ChildDocument] = []

        search_k = PER_SOURCE_RETRIEVAL_COUNT
        # ==================== 向量检索 ====================
        # 三路时可通过 vector_enable_keyword_in_three_path 关闭向量路关键词排名，避免与 BM25 双重加权
        enable_vector_keyword = self._vector_enable_keyword_in_three_path
        try:
            vector_raw = search_vector_hybrid_raw(
                vector_store=self.vector_store,
                embedding_encoder=self.embedding_encoder,
                query=query,
                query_analysis=query_analysis,
                n_results=search_k,
                verbose=self.verbose,
                enable_keyword_rank=enable_vector_keyword,
            )
            vector_results = self._format_child_results(vector_raw, match_type=MATCH_TYPE_VECTOR, max_count=search_k)
            if self.verbose:
                logger.info("  向量召回: %d 个子文档", len(vector_results))
        except Exception as e:
            logger.error("  向量召回失败: %s，继续使用其他召回源", e)

        # ==================== BM25检索 ====================
        if self.bm25_store is not None:
            try:
                bm25_raw = search_bm25_raw(
                    bm25_store=self.bm25_store,
                    query_analysis=query_analysis,
                    n_results=search_k,
                    verbose=self.verbose,
                )
                bm25_results = self._format_child_results(bm25_raw, match_type=MATCH_TYPE_BM25, max_count=search_k)
                if self.verbose:
                    logger.info("  BM25召回: %d 个子文档", len(bm25_results))
            except Exception as e:
                logger.warning("  BM25召回失败: %s，继续使用其他召回源", e)
        else:
            if self.verbose:
                logger.debug("  BM25Store未提供，跳过BM25检索")

        # ==================== 图谱检索：返回子文档列表（由 ID 拉取内容） ====================
        if self.graph_store is not None:
            try:
                if not query_analysis:
                    if self.verbose:
                        logger.warning("  query_analysis为空，跳过图谱检索")
                else:
                    graph_result = search_graph(
                        query=query,
                        query_analysis=query_analysis,
                        graph_store=self.graph_store,
                        max_results=search_k,
                        verbose=self.verbose,
                    )
                    graph_child_ids = graph_result[0] if isinstance(graph_result, tuple) else graph_result
                    graph_score_list = graph_result[1] if isinstance(graph_result, tuple) and len(graph_result) > 1 else None
                    raw_graph_ids = graph_child_ids or []
                    valid_ids = [cid for cid in raw_graph_ids if validate_child_chunk_id(cid)]
                    if len(valid_ids) != len(raw_graph_ids):
                        logger.warning(
                            "  图谱召回 %d 个 ID 中有 %d 个非标准格式（非 _child_）被丢弃",
                            len(raw_graph_ids), len(raw_graph_ids) - len(valid_ids),
                        )
                    graph_id_to_score: Optional[Dict[str, float]] = None
                    if graph_score_list:
                        graph_id_to_score = {}
                        for item in graph_score_list:
                            if isinstance(item, (list, tuple)) and len(item) >= 2:
                                graph_id_to_score[str(item[0])] = float(item[1])
                    if valid_ids and self.vector_store:
                        graph_results = self._fetch_graph_child_documents(valid_ids, graph_id_to_score=graph_id_to_score)
                    if self.verbose:
                        logger.info("  图谱召回: %d 个子文档", len(graph_results))
            except Exception as e:
                logger.warning("  图谱召回失败: %s，继续使用向量和BM25结果", e)
        else:
            if self.verbose:
                logger.info("  GraphStore未提供，跳过图谱检索")

        return vector_results, bm25_results, graph_results

    def _fetch_graph_child_documents(
        self,
        child_ids: List[str],
        graph_id_to_score: Optional[Dict[str, float]] = None,
    ) -> List[ChildDocument]:
        """
        根据子文档 ID 列表从向量库拉取内容，返回带图谱召回标记与图谱分数的子文档列表（顺序与 child_ids 一致）。
        三种检索均直接返回子文档，图谱路径在此处补齐为 List[ChildDocument]。
        """
        if not child_ids or not self.vector_store:
            return []
        K = SchemaKeys
        try:
            children_collection = self.vector_store.get_collection("children")
            if not children_collection:
                return []
            results = children_collection.get(
                ids=child_ids,
                include=["documents", "metadatas"]
            )
            ids_flat, docs_flat, metas_flat = flatten_chroma_get_result(results)
            id_to_idx = {cid: i for i, cid in enumerate(ids_flat)}
            out: List[ChildDocument] = []
            for cid in child_ids:
                if cid not in id_to_idx:
                    continue
                idx = id_to_idx[cid]
                content = docs_flat[idx] if idx < len(docs_flat) else ""
                metadata = metas_flat[idx] if idx < len(metas_flat) else {}
                parent_id = metadata.get(K.CDM_PARENT_ID)
                graph_score = graph_id_to_score.get(cid) if graph_id_to_score else None
                out.append({
                    K.CD_CHILD_ID: cid,
                    K.CD_CONTENT: content,
                    K.CD_METADATA: metadata,
                    K.CD_PARENT_ID: parent_id,
                    K.CD_DISTANCE: 1.0,
                    K.CD_RETRIEVAL_SOURCES: [RETRIEVAL_SOURCE_GRAPH],
                    K.CD_GRAPH_HIT: True,
                    K.CD_GRAPH_SCORE: graph_score,
                    K.CD_BM25_HIT: False,
                    K.CD_MATCH_TYPE: MATCH_TYPE_GRAPH,
                    K.CD_RERANK_SCORE: None,
                    K.CD_RRF_SCORE: None,
                    K.CD_FINAL_SCORE: None,
                })
            return out
        except Exception as e:
            if self.verbose:
                logger.warning(" 拉取图谱子文档失败: %s", e)
            return []

    def _merge_retrieval_results_by_child_id(
        self,
        vector_results: List[ChildDocument],
        bm25_results: List[ChildDocument],
        graph_results: List[ChildDocument],
    ) -> List[ChildDocument]:
        """
        按子文档 id（child_id）合并三路结果：同一 child_id 只保留一条，并合并三路召回标记（不丢任何一路）。
        内容/元数据/距离以首次出现的源为准（向量 > BM25 > 图谱）。
        """
        K = SchemaKeys
        by_child: Dict[str, ChildDocument] = {}

        def add_or_merge(doc: ChildDocument, source: str, is_bm25: bool, is_graph: bool) -> None:
            cid = doc.get(K.CD_CHILD_ID)
            if not cid:
                return
            if cid not in by_child:
                doc_copy = doc.copy()
                doc_copy[K.CD_RETRIEVAL_SOURCES] = [source]
                doc_copy[K.CD_GRAPH_HIT] = is_graph
                doc_copy[K.CD_BM25_HIT] = is_bm25
                by_child[cid] = doc_copy
            else:
                existing = by_child[cid]
                if K.CD_RETRIEVAL_SOURCES not in existing or not existing[K.CD_RETRIEVAL_SOURCES]:
                    existing[K.CD_RETRIEVAL_SOURCES] = []
                if source not in existing[K.CD_RETRIEVAL_SOURCES]:
                    existing[K.CD_RETRIEVAL_SOURCES].append(source)
                if is_bm25:
                    existing[K.CD_BM25_HIT] = True
                if is_graph:
                    existing[K.CD_GRAPH_HIT] = True
                    if doc.get(K.CD_GRAPH_SCORE) is not None:
                        existing[K.CD_GRAPH_SCORE] = doc.get(K.CD_GRAPH_SCORE)

        for doc in vector_results:
            add_or_merge(doc, RETRIEVAL_SOURCE_VECTOR, is_bm25=False, is_graph=False)
        for doc in bm25_results:
            add_or_merge(doc, RETRIEVAL_SOURCE_BM25, is_bm25=True, is_graph=False)
        for doc in graph_results:
            add_or_merge(doc, RETRIEVAL_SOURCE_GRAPH, is_bm25=False, is_graph=True)

        # 多路命中时统一标记为主要匹配类型为 multi_source，避免仍显示为单路 hybrid (vector)
        for doc in by_child.values():
            sources = doc.get(K.CD_RETRIEVAL_SOURCES) or []
            if len(sources) > 1:
                doc[K.CD_MATCH_TYPE] = MATCH_TYPE_MULTI_SOURCE

        return list(by_child.values())

    def _calculate_rrf_scores(
        self,
        all_children: List[ChildDocument],
        vector_results: List[ChildDocument],
        bm25_results: List[ChildDocument],
        graph_results: List[ChildDocument],
        vector_weight: float,
        bm25_weight: float,
        graph_weight: float
    ) -> None:
        """
        使用RRF（Reciprocal Rank Fusion）策略计算融合分数；三路均按 child_id 排名。
        """
        rrf_k = 60
        total_weight = vector_weight + bm25_weight + graph_weight
        
        # 权重归一化
        if abs(total_weight - 1.0) > 0.01:
            if self.verbose:
                logger.warning("  RRF权重总和不为1.0: %.2f，自动归一化", total_weight)
            vector_weight /= total_weight
            bm25_weight /= total_weight
            graph_weight /= total_weight

        K = SchemaKeys
        vector_child_ranks = {
            doc.get(K.CD_CHILD_ID): rank + 1
            for rank, doc in enumerate(vector_results)
            if doc.get(K.CD_CHILD_ID)
        }
        bm25_child_ranks = {
            doc.get(K.CD_CHILD_ID): rank + 1
            for rank, doc in enumerate(bm25_results)
            if doc.get(K.CD_CHILD_ID)
        }
        graph_child_ranks = {
            doc.get(K.CD_CHILD_ID): rank + 1
            for rank, doc in enumerate(graph_results)
            if doc.get(K.CD_CHILD_ID)
        }

        for doc in all_children:
            child_id = doc.get(K.CD_CHILD_ID)
            rrf_score = 0.0
            if child_id and child_id in vector_child_ranks:
                rrf_score += vector_weight / (rrf_k + vector_child_ranks[child_id])
            if child_id and child_id in bm25_child_ranks:
                rrf_score += bm25_weight / (rrf_k + bm25_child_ranks[child_id])
            if child_id and child_id in graph_child_ranks:
                rrf_score += graph_weight / (rrf_k + graph_child_ranks[child_id])
            doc[K.CD_RRF_SCORE] = rrf_score
            doc[K.CD_FINAL_SCORE] = rrf_score

    def _calculate_weighted_scores(
        self,
        all_children: List[ChildDocument],
        vector_weight: float,
        bm25_weight: float,
        graph_weight: float
    ) -> None:
        """
        使用加权策略计算融合分数
        
        【加权算法说明】
        加权策略是一种简单的多路召回融合方法，通过将不同召回源的分数加权求和。
        
        【计算公式】
        对于每个文档，最终分数 = Σ(weight_i * base_score_i)，权重会先归一化到和为 1。
        其中：
        - weight_i: 第i个召回源的权重（vector_weight、bm25_weight、graph_weight）
        - base_score_i: 向量/BM25 用 clamp(1.0 - distance)（0~1 相似度），图谱用 graph_score（0~1）
        
        【示例】
        假设某个文档被向量和BM25召回命中（向量距离 0.2、BM25 距离 0.3），且被图谱命中（graph_score=0.9）：
        - 向量基础分数 = clamp(1.0 - 0.2) = 0.8
        - BM25基础分数 = clamp(1.0 - 0.3) = 0.7
        - 向量贡献 = 0.4 * 0.8 = 0.32
        - BM25贡献 = 0.3 * 0.7 = 0.21
        - 图谱贡献 = 0.3 * 0.9 = 0.27
        - 最终分数 = 0.32 + 0.21 + 0.27 = 0.80
        
        【与RRF的区别】
        - 加权策略：基于距离分数，适合有明确相似度分数的场景
        - RRF策略：基于排名，适合只有排名信息的场景
        
        Args:
            all_children: 所有子文档列表（会被修改，添加 final_score 字段）
            vector_weight: 向量召回权重（0-1之间）
            bm25_weight: BM25召回权重（0-1之间）
            graph_weight: 图谱召回权重（0-1之间）
        """
        # 权重归一化：与 RRF 保持一致，避免权重和不等于 1 时结果系统性偏大/偏小
        total_weight = vector_weight + bm25_weight + graph_weight
        if total_weight <= 0:
            vector_weight = bm25_weight = graph_weight = 1.0 / 3.0
        elif abs(total_weight - 1.0) > 0.01:
            vector_weight /= total_weight
            bm25_weight /= total_weight
            graph_weight /= total_weight

        K = SchemaKeys
        for doc in all_children:
            final_score = 0.0
            sources = doc.get(K.CD_RETRIEVAL_SOURCES, [])

            # 向量/BM25：distance 越小越好，转为 0~1 相似度；distance 缺失/非法时视为 0 相似度
            distance = doc.get(K.CD_DISTANCE)
            base_score = 0.0
            if distance is not None:
                try:
                    base_score = max(0.0, min(1.0, 1.0 - float(distance)))
                except (TypeError, ValueError):
                    base_score = 0.0

            if RETRIEVAL_SOURCE_VECTOR in sources:
                final_score += vector_weight * base_score
            if RETRIEVAL_SOURCE_BM25 in sources:
                final_score += bm25_weight * base_score

            # 图谱：graph_score 本身即 0~1 相关性分；命中但无分数时按满分兜底（图谱召回是强信号）
            if doc.get(K.CD_GRAPH_HIT, False):
                graph_score = doc.get(K.CD_GRAPH_SCORE)
                if graph_score is None:
                    gs = 1.0
                else:
                    try:
                        gs = max(0.0, min(1.0, float(graph_score)))
                    except (TypeError, ValueError):
                        gs = 0.0
                final_score += graph_weight * gs

            doc[K.CD_FINAL_SCORE] = final_score

    def _log_fusion_statistics(self, final_results: List[ChildDocument]) -> None:
        """
        记录三路融合统计信息（按前 MAX 个子文档统计各召回源命中数）
        
        Args:
            final_results: 融合排序后的子文档列表（未截断）
        """
        if not self.verbose:
            return

        K = SchemaKeys
        vector_hit_count = sum(
            1 for doc in final_results[:MAX_RETRIEVAL_RESULT_COUNT]
            if RETRIEVAL_SOURCE_VECTOR in doc.get(K.CD_RETRIEVAL_SOURCES, [])
        )
        bm25_hit_count = sum(
            1 for doc in final_results[:MAX_RETRIEVAL_RESULT_COUNT]
            if doc.get(K.CD_BM25_HIT, False)
        )
        graph_hit_count = sum(
            1 for doc in final_results[:MAX_RETRIEVAL_RESULT_COUNT]
            if doc.get(K.CD_GRAPH_HIT, False)
        )
        logger.info("  三路召回融合完成: 前 %d 个结果中", MAX_RETRIEVAL_RESULT_COUNT)
        logger.info("     向量命中: %d, BM25命中: %d, 图谱命中: %d", vector_hit_count, bm25_hit_count, graph_hit_count)

    def _fuse_triple_retrieval(
        self,
        query: str,
        query_analysis: QueryAnalysis,
        fusion_strategy: str = "rrf",
        vector_weight: float = 0.4,
        bm25_weight: float = 0.3,
        graph_weight: float = 0.3
    ) -> List[ChildDocument]:
        """
        三路召回融合：三种检索均返回子文档，按 child_id 合并、RRF/加权排序后返回。
        """
        if self.verbose:
            logger.info("\n%s", "=" * 60)
            logger.info(" 开始检索融合")
            logger.info("   融合策略: %s", fusion_strategy)
            logger.info("%s", "=" * 60)

        vector_results, bm25_results, graph_results = self._execute_retrieval_sources(
            query, query_analysis
        )

        all_children = self._merge_retrieval_results_by_child_id(
            vector_results, bm25_results, graph_results
        )

        if fusion_strategy == "rrf":
            self._calculate_rrf_scores(
                all_children, vector_results, bm25_results, graph_results,
                vector_weight, bm25_weight, graph_weight
            )
            K = SchemaKeys
            final_results = sorted(
                all_children,
                key=lambda x: x.get(K.CD_RRF_SCORE, 0.0),
                reverse=True
            )
        else:
            self._calculate_weighted_scores(
                all_children, vector_weight, bm25_weight, graph_weight
            )
            K = SchemaKeys
            final_results = sorted(
                all_children,
                key=lambda x: x.get(K.CD_FINAL_SCORE, 0.0),
                reverse=True
            )

        self._log_fusion_statistics(final_results)
        return final_results
