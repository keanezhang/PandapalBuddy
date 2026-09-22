"""
RAG (Retrieval-Augmented Generation) SDK

本包设计为可独立打包给其他项目使用：不依赖 agent.config / agent.agents / agent.utils，
配置通过 RAGConfig(**kwargs) 传参，SDK 不绑定任何配置文件格式。

=== 公开 API（稳定，推荐仅使用以下入口）===

    # 基本使用（门面模式）
    >>> from agent.rag import RAGEngine, RAGConfig
    >>> engine = RAGEngine(config=RAGConfig(enable_graph=True, neo4j_password="xxx"))
    >>> out = engine.query("查询内容", k=5, call_llm=True)

    # 高级用法：显式构造 Config
    >>> from agent.rag import RAGConfig, RAGEngine
    >>> config = RAGConfig(db_path="/my/path", enable_graph=False)
    >>> engine = RAGEngine(config=config)

    # 多知识库
    >>> from agent.rag import RAGEngineManager, RAGEngine
    >>> manager = RAGEngineManager()
    >>> manager.register("kb1", RAGEngine(config=RAGConfig(db_path="/kb1/chroma_db")))
    >>> out = manager.get("kb1").query("问题", k=5)

    # 查询参数调优
    >>> from agent.rag import RAGQueryParams
    >>> qc = RAGQueryParams(top_k=10, enable_rerank=True)
    >>> out = engine.query("问题", query_config=qc)

    # 评估
    >>> from agent.rag import evaluate_rag, RAGEvaluationResult
    >>> result = evaluate_rag("李二是谁", search_results=results, expected_keywords=["李二", "骊珠洞天"])

=== 高级 API（按需从子包导入，不保证向后兼容）===

    存储层:
        from .store.vector.vector_store import VectorStore
        from .store.bm25.bm25_store import BM25Store
        from .store.graph.graph_store import GraphStore
        from .store.graph.graph_store_neo4j import GraphStoreNeo4j
        from .store.graph.graph_builder import GraphBuilder

    检索/重排:
        from .query.retrieval.retriever import Retriever
        from .query.rerank.reranker import Reranker

    构建流程:
        from .builder.data.loader import DocumentLoader
        from .builder.data.splitter import DocumentSplitter

    编码器:
        from .embedding.embedding import EmbeddingEncoder

    Schema:
        from .schema import Entity, Relationship, RelationType, SchemaKeys

    推理/生成:
        from .query.generation import build_reasoning_prompt, ReasoningPromptBuilder

    工具:
        from .utils import estimate_tokens
"""

# ──────────────────────────────────────────────
# 公开 API：延迟导入，避免 import agent.rag 时拉起整条重依赖链
# ──────────────────────────────────────────────

# 配置（轻量，无外部依赖，始终立即导入）
from .rag_config import RAGConfig
from .rag_protocol import RAGLLMProvider
from .exceptions import (
    RAGError,
    RAGConfigError,
    RAGIndexError,
    RAGQueryError,
    RAGBuildError,
    RAGComponentError,
)

__version__ = "0.1.0"

__all__ = [
    # 配置
    "__version__",
    "RAGConfig",
    "RAGLLMProvider",
    # 异常
    "RAGError",
    "RAGConfigError",
    "RAGIndexError",
    "RAGQueryError",
    "RAGBuildError",
    "RAGComponentError",
    # 引擎
    "RAGEngine",
    "RAGEngineManager",
    # 实例 & 查询
    "RAGInstance",
    "RAGQueryParams",
    "RAGQueryResult",
    "RAGQueryDiagnostics",
    # 评估
    "evaluate_rag",
    "evaluate_rag_batch",
    "RAGEvaluationResult",
]


def __getattr__(name: str):  # type: ignore[misc]  # -> Any
    """延迟导入重型模块，仅在实际访问时触发加载。

    首次访问后通过 ``globals().update()`` 缓存，后续访问不再重复导入。
    """

    # 引擎入口
    if name == "RAGEngine":
        from .rag_engine import RAGEngine
        globals()["RAGEngine"] = RAGEngine
        return RAGEngine
    if name == "RAGEngineManager":
        from .engine_manager import RAGEngineManager
        globals()["RAGEngineManager"] = RAGEngineManager
        return RAGEngineManager

    # 核心实例 & 查询数据类
    _instance_names = ("RAGInstance", "RAGQueryParams", "RAGQueryResult", "RAGQueryDiagnostics")
    if name in _instance_names:
        from .rag_instance import RAGInstance, RAGQueryParams, RAGQueryResult, RAGQueryDiagnostics
        _map = {
            "RAGInstance": RAGInstance,
            "RAGQueryParams": RAGQueryParams,
            "RAGQueryResult": RAGQueryResult,
            "RAGQueryDiagnostics": RAGQueryDiagnostics,
        }
        globals().update(_map)
        return _map[name]

    # 评估
    _eval_names = ("evaluate_rag", "evaluate_rag_batch", "RAGEvaluationResult")
    if name in _eval_names:
        from .evaluation.evaluation import (
            evaluate_rag,
            evaluate_rag_batch,
            RAGEvaluationResult,
        )
        _map = {
            "evaluate_rag": evaluate_rag,
            "evaluate_rag_batch": evaluate_rag_batch,
            "RAGEvaluationResult": RAGEvaluationResult,
        }
        globals().update(_map)
        return _map[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
