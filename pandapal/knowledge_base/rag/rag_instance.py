#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAG SDK：开箱即用的单知识库 RAG 门面对象（替代 CLI 的核心流程）。

目标：提供稳定、可复用、可测试的“意图 → 富化 → 检索融合 → 重排 → Prompt → LLM →（可选）评估”全链路能力。

关键设计：
- **纯 SDK**：通过 from_config(config: RAGConfig) 创建实例，不读 agent.config。
- **结构化返回**：query() 返回 RAGQueryResult，包含 results/query_analysis/prompt/answer/diagnostics。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from .exceptions import RAGConfigError, RAGIndexError
from .rag_config import RAGConfig
from .rag_protocol import RAGLLMProvider
from .schema import SchemaKeys, create_empty_query_analysis

if TYPE_CHECKING:
    # 仅供字符串注解解析（本模块 from __future__ import annotations，运行时不求值）
    from .evaluation.evaluation import RAGEvaluationResult
    from .embedding.embedding import EmbeddingEncoder
    from .query.rerank.reranker import Reranker
    from .query.retrieval.retriever import Retriever
    from .query.understand.query_intent_classifier import QueryIntentClassifier
    from .query.understand.query_segmenter import QuerySegmenter
    from .schema import ChildDocument, ParentDocument
    from .store.bm25.bm25_store import BM25Store
    from .store.graph.graph_store import GraphStore
    from .store.vector.vector_store import VectorStore

# ---------- SDK 内默认常量（硬编码，非配置文件；可改代码或通过参数覆盖） ----------
# 本地模型/重排模型路径必须由调用方通过 RAGConfig 显式传入
SDK_LOCAL_EMBEDDING_PATH_SUFFIX = None  # Must be set via RAGConfig.local_embedding_model_path
SDK_LOCAL_RERANKER_PATH_SUFFIX = None   # Must be set via RAGConfig.local_reranker_model_path
# LLM 调用默认参数（_run_llm 内使用；非来自 settings）
SDK_LLM_DEFAULT_TEMPERATURE = 0.3
SDK_LLM_DEFAULT_MAX_TOKENS = 4096
SDK_LLM_AGENT_NAME = "rag_qa_agent"

logger = logging.getLogger(__name__)


@dataclass
class RAGQueryParams:
    """
    单次查询的默认配置，可由 query() 参数覆盖。

    说明：此处均为 **SDK 内默认值**，非从外部配置文件读取；如需与 settings 一致，请在构造
    RAGInstance 或 from_config(config, query_config=...) 时显式传入。
    """
    top_n_after_rerank: int = 15
    max_chunks: int = 15
    max_chars_per_chunk: int = 800
    fusion_strategy: str = "rrf"  # "rrf" / "weighted"
    # weighted 融合权重（仅 fusion_strategy="weighted" 时生效；三路缺任意一路时忽略）
    vector_weight: float = 0.4
    bm25_weight: float = 0.3
    graph_weight: float = 0.3
    # 是否执行评估（query(..., evaluate=True) 时也可单次覆盖）
    default_evaluate: bool = False


@dataclass
class RAGQueryDiagnostics:
    """查询诊断信息（用于 CLI/服务端观测、调试与评估）。"""
    timings_ms: Dict[str, float] = field(default_factory=dict)
    retrieval_source_counts: Dict[str, int] = field(default_factory=dict)
    fusion_strategy: str = ""
    fusion_weights: Optional[Tuple[float, float, float]] = None  # (vector, bm25, graph)
    errors: List[str] = field(default_factory=list)


@dataclass
class RAGQueryResult:
    """query() 结构化返回。"""
    query: str
    query_analysis: Dict[str, Any]
    need_reasoning: bool
    results: List[ParentDocument]
    prompt: str
    answer: str = ""
    evaluation: Optional[RAGEvaluationResult] = None
    diagnostics: RAGQueryDiagnostics = field(default_factory=RAGQueryDiagnostics)


# 大模型作答用 instruction：need_reasoning 时用推理版，否则用总结版
REASONING_LLM_INSTRUCTIONS = """你是一个基于证据的分析助手。用户会提供【用户问题】、【图谱关系】和【相关文本】以及【作答要求】。
请仅根据这些证据进行分析和回答，并在回答中注明依据（如引用具体关系或文本）。不要编造证据中没有的内容。"""
SUMMARY_LLM_INSTRUCTIONS = """你是一个问答助手。用户会提供从图谱与检索得到的相关文本和问题。请根据文本内容简要总结并回答用户问题；若文本中无相关信息，请说明。不要编造文本中没有的内容。"""


def _create_instance_from_config(
    config: RAGConfig,
    *,
    knowledge_base_name: str = "default",
    try_load_bm25_cache: bool = True,
    try_connect_graph: bool = True,
    query_config: Optional[RAGQueryParams] = None,
) -> RAGInstance:
    """
    从 RAGConfig 构建 RAGInstance（工厂）。不读 agent.config；意图/LLM 用 config 显式字段或为 None。
    """
    # 延迟导入重型依赖，避免 import rag_instance 即拉起全部子模块
    from .store.vector.vector_store import VectorStore
    from .embedding.embedding import EmbeddingEncoder, LocalEmbeddingConfig, CloudEmbeddingConfig
    from .store.bm25.bm25_store import BM25Store
    from .query.retrieval.retriever import Retriever
    from .query.rerank.reranker import Reranker
    from .store.graph.graph_store_neo4j import GraphStoreNeo4j
    from .query.understand.query_intent_classifier import QueryIntentClassifier
    from .query.understand.query_segmenter import QuerySegmenter

    pr = config.resolve_project_root()
    verbose = config.verbose

    # 1) VectorStore（阶段 3：参数来自 config，不读 settings）
    db_path = config.resolve_db_path()
    vector_store = VectorStore(
        db_path=db_path,
        collection_name=config.collection_name,
        project_root=pr,
        verbose=verbose,
        db_batch_size=config.db_batch_size,
        default_query_results=config.default_query_results,
    )

    # 2) Embedding
    if config.use_cloud_embedding and config.cloud_embedding_api_key:
        api_type = config.cloud_embedding_api_type.lower()
        cloud_config = CloudEmbeddingConfig(
            api_key=config.cloud_embedding_api_key,
            model=config.cloud_embedding_multimodal_model_id,
            api_type=api_type,
            api_full_url=config.cloud_embedding_multimodal_api_full_url,
            dimension=config.cloud_embedding_dimension,
        )
        embedding_provider = EmbeddingEncoder(
            cloud_config=cloud_config,
            verbose=verbose,
            local_batch_size=config.local_embedding_batch_size,
            supported_dimensions=config.supported_dimensions,
            cloud_max_tokens_per_text=config.cloud_embedding_max_tokens_per_text,
            cloud_batch_size=config.cloud_embedding_batch_size,
        )
    else:
        embedding_model_name = config.local_embedding_model
        local_path = config.local_embedding_model_path
        if local_path is None:
            raise RAGConfigError(
                "local_embedding_model_path is required when use_cloud_embedding=False. "
                "Please set it in RAGConfig(local_embedding_model_path=...)."
            )
        local_config = LocalEmbeddingConfig(model_name=embedding_model_name, model_path=local_path)
        embedding_provider = EmbeddingEncoder(
            local_config=local_config,
            verbose=verbose,
            local_batch_size=config.local_embedding_batch_size,
            supported_dimensions=config.supported_dimensions,
            cloud_max_tokens_per_text=config.cloud_embedding_max_tokens_per_text,
            cloud_batch_size=config.cloud_embedding_batch_size,
        )

    # 3) intent classifier + LLM（仅用注入的 provider）
    intent_llm_provider = config.intent_llm_provider
    intent_classifier = QueryIntentClassifier(
        llm_provider=intent_llm_provider,
        verbose=verbose,
        instructions=config.intent_classifier_instructions,
    )

    # 分词器：内部固定 LLM（复用 intent_llm_provider）。
    # 语义理解由外部 LLM 通过 search(query_analysis=...) 提供；分词由内部 LLM 补全。
    segmenter = QuerySegmenter(
        llm_provider=intent_llm_provider,
        verbose=verbose,
    )

    # QA LLM：仅用注入的 provider
    qa_llm_provider = config.qa_llm_provider

    # 4) graph/bm25
    enable_graph = config.enable_graph or config.use_triple_retrieval
    enable_bm25 = config.enable_bm25 or config.use_triple_retrieval

    graph_store: Optional[GraphStore] = None
    if enable_graph and try_connect_graph:
        # 显式配置了 Neo4j 连接参数（uri + password）时，连接失败应暴露真错误；
        # 未配置（默认开启但未接图）时降级为纯向量检索并给出提示。
        if config.neo4j_uri and config.neo4j_password:
            graph_store = GraphStoreNeo4j(
                uri=config.neo4j_uri,
                user=config.neo4j_user,
                password=config.neo4j_password,
                database=config.neo4j_database,
                verbose=verbose,
            )
        else:
            logger.warning(
                "enable_graph=True 但未配置 Neo4j 连接参数（neo4j_uri/neo4j_password），已禁用图谱检索"
            )

    bm25_store: Optional[BM25Store] = None
    if enable_bm25:
        try:
            bm25_store = BM25Store(
                vector_store=vector_store,
                verbose=verbose,
                default_query_results=config.default_query_results,
            )
        except Exception as e:
            logger.warning("BM25 store creation failed, disabling BM25: %s", e)
            bm25_store = None

    # 5) Retriever
    retriever = Retriever(
        vector_store=vector_store,
        embedding_encoder=embedding_provider,
        bm25_store=bm25_store,
        graph_store=graph_store,
        verbose=verbose,
        vector_enable_keyword_in_three_path=config.vector_enable_keyword_in_three_path,
    )

    # 6) Reranker（有云端凭证走云端，否则走本地 model_path）
    reranker: Optional[Reranker] = None
    if config.enable_reranker:
        has_cloud = bool(
            config.reranker_cloud_api_key
            and config.reranker_cloud_model_id
            and config.reranker_cloud_url
            and config.reranker_cloud_instruct
        )
        rpath = config.local_reranker_model_path
        # 配置错误（未提供任何重排凭据）必须在 try 之外抛出，不能被静默降级吞掉
        if not has_cloud and rpath is None:
            raise RAGConfigError(
                "enable_reranker=True but no cloud credentials (reranker_cloud_api_key + reranker_cloud_model_id + reranker_cloud_url + reranker_cloud_instruct) "
                "and no local_reranker_model_path provided. "
                "Please set cloud credentials or RAGConfig(local_reranker_model_path=...)."
            )
        try:
            if has_cloud:
                reranker = Reranker(
                    cloud_api_key=config.reranker_cloud_api_key,
                    cloud_model_id=config.reranker_cloud_model_id,
                    cloud_url=config.reranker_cloud_url,
                    cloud_instruct=config.reranker_cloud_instruct,
                    verbose=verbose,
                )
            else:
                reranker = Reranker(model_path=rpath, verbose=verbose)
        except Exception as e:
            logger.warning("Reranker initialization failed, disabling rerank: %s", e)
            reranker = None

    inst = RAGInstance(
        knowledge_base_name=knowledge_base_name,
        vector_store=vector_store,
        embedding_provider=embedding_provider,
        retriever=retriever,
        bm25_store=bm25_store,
        graph_store=graph_store,
        reranker=reranker,
        intent_classifier=intent_classifier,
        segmenter=segmenter,
        qa_llm_provider=qa_llm_provider,
        verbose=verbose,
        query_config=query_config,
        reasoning_instructions=config.qa_reasoning_instructions,
        summary_instructions=config.qa_summary_instructions,
        try_load_bm25_cache=try_load_bm25_cache,
    )
    inst.load_indexes()
    return inst


# ---------- RAG 实例 ----------

class RAGInstance:
    """
    单知识库 RAG SDK：开箱即用 + 结构化返回。

    - 推荐用法：instance = RAGInstance.from_config(config); out = instance.query("问题")
    - 高级用法：自行注入组件创建 instance（用于自定义/测试）。
    """

    def __init__(
        self,
        knowledge_base_name: str,
        vector_store: VectorStore,
        embedding_provider: EmbeddingEncoder,
        retriever: Retriever,
        *,
        bm25_store: Optional[BM25Store] = None,
        graph_store: Optional[GraphStore] = None,
        reranker: Optional[Reranker] = None,
        intent_classifier: Optional[QueryIntentClassifier] = None,
        segmenter: Optional[QuerySegmenter] = None,
        qa_llm_provider: Optional[RAGLLMProvider] = None,
        verbose: bool = True,
        query_config: Optional[RAGQueryParams] = None,
        reasoning_instructions: Optional[str] = None,
        summary_instructions: Optional[str] = None,
        try_load_bm25_cache: bool = True,
    ):
        """
        初始化 RAG 实例。所有能力依赖均通过参数传入。

        Args:
            knowledge_base_name: 知识库名称（用于日志与标识）
            vector_store: 向量存储（必需）
            embedding_provider: 向量编码器（必需）
            retriever: 检索器（必需）
            reranker: 重排序器（可选；由 config.enable_reranker 控制）
            bm25_store: BM25 存储（可选，三路召回时需要）
            graph_store: 图谱存储（可选，三路召回与推理 prompt 时需要）
            intent_classifier: 意图分类器（可选；query() 主流程必须配置）
            segmenter: 中文分词器（可选；search() 用内部固定 LLM 补 segmented_words）
            qa_llm_provider: 符合 RAGLLMProvider 的问答 LLM 注入
            verbose: 是否输出详细日志
            query_config: 默认查询配置；为 None 时使用 RAGQueryParams 默认值
            reasoning_instructions: 外部传入的推理型 QA 指令（可选）。
                传入则覆盖内置 REASONING_LLM_INSTRUCTIONS。
            summary_instructions: 外部传入的总结型 QA 指令（可选）。
                传入则覆盖内置 SUMMARY_LLM_INSTRUCTIONS。
            try_load_bm25_cache: 是否在 load_indexes() 时尝试加载 BM25 分词缓存
                （缓存缺失/失效时回退到从 Chroma 重建）。为 False 时跳过 BM25 索引加载。
        """
        self.knowledge_base_name = knowledge_base_name
        self.vector_store = vector_store
        self.embedding_provider = embedding_provider
        self.retriever = retriever
        self.reranker = reranker
        self.bm25_store = bm25_store
        self.graph_store = graph_store
        self.intent_classifier = intent_classifier
        self.segmenter = segmenter
        self.qa_llm_provider = qa_llm_provider
        self.verbose = verbose
        self._query_config = query_config or RAGQueryParams()
        self._reasoning_instructions = reasoning_instructions or REASONING_LLM_INSTRUCTIONS
        self._summary_instructions = summary_instructions or SUMMARY_LLM_INSTRUCTIONS
        self._try_load_bm25_cache = try_load_bm25_cache
        self._is_loaded = False
        self._lock = Lock()

    @classmethod
    def from_config(
        cls,
        config: RAGConfig,
        *,
        knowledge_base_name: str = "default",
        try_load_bm25_cache: bool = True,
        try_connect_graph: bool = True,
        query_config: Optional[RAGQueryParams] = None,
    ) -> "RAGInstance":
        """
        从 RAGConfig 创建实例（纯 SDK 路径）。所有配置来自 config，不读 agent.config。
        """
        return _create_instance_from_config(
            config,
            knowledge_base_name=knowledge_base_name,
            try_load_bm25_cache=try_load_bm25_cache,
            try_connect_graph=try_connect_graph,
            query_config=query_config,
        )

    def load_indexes(self) -> bool:
        """
        连接并校验已构建的索引（向量 / BM25 / 图谱），不创建、不构建数据。
        """
        with self._lock:
            if self._is_loaded:
                if self.verbose:
                    logger.info("[%s] 索引已加载，跳过重复初始化", self.knowledge_base_name)
                return True

            errors: List[str] = []

            try:
                if self.verbose:
                    logger.info("[%s] 初始化向量索引...", self.knowledge_base_name)
                self.vector_store.initialize()
                child_count = self.vector_store.count("children")
                if child_count <= 0:
                    errors.append(f"[{self.knowledge_base_name}] 向量索引不存在或为空，请先在外部构建索引")
                elif self.verbose:
                    logger.info("[%s] 向量索引就绪，子文档数: %s", self.knowledge_base_name, child_count)
            except Exception as e:
                errors.append(f"[{self.knowledge_base_name}] 向量索引初始化失败: {e}")

            if self.bm25_store is not None and self._try_load_bm25_cache:
                try:
                    if self.verbose:
                        logger.info("[%s] 初始化 BM25 索引...", self.knowledge_base_name)
                    if not self.bm25_store.is_index_built():
                        # 优先加载分词缓存；缓存缺失/失效时回退到从 Chroma 重建
                        self.bm25_store.rebuild_or_load_index_from_cache()
                    if not self.bm25_store.is_index_built():
                        errors.append(f"[{self.knowledge_base_name}] BM25 索引未构建，请先在外部构建")
                    elif self.verbose:
                        logger.info("[%s] BM25 索引就绪", self.knowledge_base_name)
                except Exception as e:
                    errors.append(f"[{self.knowledge_base_name}] BM25 初始化失败: {e}")

            if self.graph_store is not None:
                try:
                    if self.verbose:
                        logger.info("[%s] 初始化图谱索引...", self.knowledge_base_name)
                    self.graph_store.initialize()
                    entity_count = self.graph_store.get_entity_count()
                    if entity_count <= 0:
                        errors.append(f"[{self.knowledge_base_name}] 图谱索引不存在或为空，请先在外部构建")
                    elif self.verbose:
                        logger.info("[%s] 图谱就绪，实体数: %s", self.knowledge_base_name, entity_count)
                except Exception as e:
                    errors.append(f"[{self.knowledge_base_name}] 图谱初始化失败: {e}")

            if errors:
                self._is_loaded = False
                raise RAGIndexError("索引初始化失败:\n" + "\n".join(f"  - {e}" for e in errors))

            self._is_loaded = True
            if self.verbose:
                logger.info("[%s] 所有索引初始化完成", self.knowledge_base_name)
            return True

    def is_loaded(self) -> bool:
        """检查实例是否已加载（不触发初始化）。"""
        return self._is_loaded

    async def search(
        self,
        query: str,
        query_analysis: Optional[Dict[str, Any]] = None,
        k: Optional[int] = None,
        *,
        fusion_strategy: Optional[str] = None,
        vector_weight: Optional[float] = None,
        bm25_weight: Optional[float] = None,
        graph_weight: Optional[float] = None,
        **extra_retrieval_kwargs: Any,
    ) -> List["ChildDocument"]:
        """
        纯检索入口：仅做「分词（内部固定 LLM）→ 富化 → 多路召回 →（可选）重排」，返回原文片段，不调用 LLM 生成答案。

        语义理解（意图/实体/关系/策略/关键词）由调用方（外部 LLM）通过 ``query_analysis`` 提供；
        分词 ``segmented_words`` 由内部 ``segmenter``（固定 LLM）补全。

        Args:
            query: 用户原始问句。
            query_analysis: 外部 LLM 提供的查询分析（含 intent/entities/relation_type/strategies/keywords 等），可选。
            k: 最终保留条数，None 时使用 query_config.top_n_after_rerank。
            fusion_strategy / vector_weight / bm25_weight / graph_weight: 融合策略与权重（可选）。
            **extra_retrieval_kwargs: 其它传给 Retriever.search 的参数。

        Returns:
            子文档列表（每项含 content 原文片段、metadata 溯源、distance、retrieval_sources 等）。
        """
        from .schema import validate_query_analysis_structure
        from .query.enrich.query_enricher import enrich_query_analysis_once

        cfg = self._query_config
        top_k = k if k is not None else cfg.top_n_after_rerank
        fusion = fusion_strategy if fusion_strategy is not None else cfg.fusion_strategy
        vw = cfg.vector_weight if vector_weight is None else float(vector_weight)
        bw = cfg.bm25_weight if bm25_weight is None else float(bm25_weight)
        gw = cfg.graph_weight if graph_weight is None else float(graph_weight)

        # 1) 标准化外部传入的 query_analysis（语义理解由外部 LLM 提供）
        if query_analysis is None:
            qa = create_empty_query_analysis()
        else:
            qa = validate_query_analysis_structure(query_analysis)

        # 2) 分词：内部固定 LLM 补 segmented_words（供 BM25 使用）
        if self.segmenter is not None:
            try:
                segmented = await self.segmenter.segment(query)
                if segmented:
                    qa[SchemaKeys.QA_SEGMENTED_WORDS] = segmented
            except Exception as e:
                logger.warning("[%s] 分词失败，BM25 将退化: %s", self.knowledge_base_name, e)

        # 3) 富化（标准名/别名扩展，查图谱，确定性、无需 LLM）
        try:
            qa = enrich_query_analysis_once(
                qa, graph_store=self.graph_store, verbose=self.verbose,
            )
        except Exception as e:
            logger.warning("[%s] 富化失败: %s", self.knowledge_base_name, e)

        # 4) 多路召回
        retrieval_kw = {
            "fusion_strategy": fusion,
            "vector_weight": vw,
            "bm25_weight": bw,
            "graph_weight": gw,
            **extra_retrieval_kwargs,
        }
        results = self.retriever.search(query, query_analysis=qa, **retrieval_kw)

        # 5) 可选重排 + 截断
        if self.reranker is not None and results:
            try:
                results = self.reranker.rerank(query, results, top_k=top_k)
            except Exception as e:
                logger.warning("[%s] 重排失败: %s", self.knowledge_base_name, e)
                results = results[:top_k]
        else:
            results = results[:top_k]

        return results

    def get_parent_documents(self, parent_ids: List[str]) -> List[Dict[str, Any]]:
        """
        按 parent_id 获取完整父文档（原文 + 元数据）。

        Args:
            parent_ids: 父文档 ID 列表（可含重复，去重后按传入顺序返回存在者）。

        Returns:
            父文档列表，每项含 parent_id / parent_content / parent_metadata。
        """
        if not parent_ids:
            return []
        from .utils.chroma_utils import flatten_chroma_get_result

        unique = list(dict.fromkeys(parent_ids))
        raw = self.vector_store.get_parents_by_ids(unique)
        result = flatten_chroma_get_result(raw)
        out: List[Dict[str, Any]] = []
        for i, pid in enumerate(result.ids):
            out.append({
                SchemaKeys.PD_PARENT_ID: pid,
                SchemaKeys.PD_PARENT_CONTENT: result.documents[i] if i < len(result.documents) else "",
                SchemaKeys.PD_PARENT_METADATA: result.metadatas[i] if i < len(result.metadatas) else {},
            })
        return out

    def __enter__(self) -> "RAGInstance":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def close(self) -> None:
        """释放索引连接与资源。"""
        with self._lock:
            for name, obj in [
                ("graph_store", self.graph_store),
                ("vector_store", self.vector_store),
                ("bm25_store", self.bm25_store),
            ]:
                if obj is not None and hasattr(obj, "close"):
                    try:
                        obj.close()
                        if self.verbose:
                            logger.info("[%s] %s 已关闭", self.knowledge_base_name, name)
                    except Exception as e:
                        logger.error("[%s] 关闭 %s 时出错: %s", self.knowledge_base_name, name, e)
            self._is_loaded = False
            if self.verbose:
                logger.info("[%s] 资源已释放", self.knowledge_base_name)
