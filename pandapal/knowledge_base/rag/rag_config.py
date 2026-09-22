#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAG 统一配置类。

设计原则：
- constants.py = 非敏感配置的默认值
- 敏感信息（API Key、密码）通过 kwargs 传入，或兜底读 os.getenv()
- 调用方通过 RAGConfig(**kwargs) 传参覆盖一切
- SDK 不绑定任何配置文件格式（yaml / json / toml 都是调用方的选择）

优先级（从高到低）：
  1. 显式传入的 kwargs 值
  2. 环境变量（仅敏感信息）
  3. constants.py 默认值（仅非敏感信息）
  4. None（敏感信息均未设置时）

敏感信息加载约定：
  敏感信息（API Key、密码）查找顺序：kwargs > 环境变量 > None。
  调用方可通过构造函数参数覆盖环境变量::

      config = RAGConfig(
          cloud_embedding_api_key="sk-xxx",  # 覆盖环境变量
          neo4j_password="my_password",
      )
"""

from __future__ import annotations
import os
import warnings
from pathlib import Path
from typing import Any, List, Optional, Set

from .rag_protocol import RAGLLMProvider
from .exceptions import RAGConfigError
from .constants import (
    DEFAULT_USE_CLOUD_EMBEDDING,
    DEFAULT_CLOUD_EMBEDDING_DIMENSION,
    DEFAULT_CLOUD_EMBEDDING_BATCH_SIZE,
    DEFAULT_CLOUD_EMBEDDING_MAX_TOKENS_PER_TEXT,
    DEFAULT_SUPPORTED_DIMENSIONS,
    DEFAULT_API_TYPE,
    DEFAULT_LOCAL_EMBEDDING_BATCH_SIZE,
    DEFAULT_ENABLE_GRAPH,
    DEFAULT_ENABLE_BM25,
    DEFAULT_USE_TRIPLE_RETRIEVAL,
    DEFAULT_TRIPLE_RETRIEVAL_FUSION_STRATEGY,
    DEFAULT_TRIPLE_RETRIEVAL_VECTOR_WEIGHT,
    DEFAULT_TRIPLE_RETRIEVAL_BM25_WEIGHT,
    DEFAULT_TRIPLE_RETRIEVAL_GRAPH_WEIGHT,
    DEFAULT_ENABLE_RERANKER,
    DEFAULT_RERANKER_TOP_K,
    DEFAULT_RERANK_STRATEGY,
    DEFAULT_DEFAULT_QUERY_RESULTS,
    DEFAULT_SIMILARITY_THRESHOLD,
    DEFAULT_MAX_RESULTS_FOR_LLM,
    DEFAULT_MAX_CONTENT_LENGTH,
    DEFAULT_VECTOR_ENABLE_KEYWORD_IN_THREE_PATH,
    DEFAULT_DB_BATCH_SIZE,
    DEFAULT_DEFAULT_DIMENSION,
    DEFAULT_RELATION_EXTRACTION_METHOD,
    DEFAULT_ENABLE_GRAPH_ENHANCEMENT,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CHUNK_OVERLAP_RATIO,
    DEFAULT_CHUNK_OVERLAP_MIN,
    DEFAULT_ENABLE_CHAPTER_SPLIT,
    DEFAULT_PARENT_MAX_TOKENS,
    DEFAULT_MAX_FILE_SIZE_MB,
    DEFAULT_LOG_LEVEL,
    DEFAULT_VERBOSE,
)

# project_root 必须由调用方显式提供


class RAGConfig:
    """
    RAG 统一配置（查询 + 构建合一）。

    SDK 内部模块只访问此类的属性，不直接接触配置文件。
    敏感信息查找顺序：kwargs > 环境变量 > None。

    使用示例::

        # 最简：全部用默认值（敏感信息自动从环境变量读取）
        config = RAGConfig()

        # 覆盖部分参数
        config = RAGConfig(
            db_path="/my/custom/path",
            enable_graph=False,
            neo4j_password="override_password",
        )

        # 调用方自行读 yaml / json 后传入（SDK 不关心来源）
        import yaml
        with open("my_rag.yaml") as f:
            cfg = yaml.safe_load(f)
        config = RAGConfig(**cfg)

    优先级：
      敏感信息：显式传入 > 环境变量 > None
      非敏感信息：显式传入 > constants.py 默认值
    """

    _KNOWN_KEYS: Set[str] = {
        "project_root", "db_path", "collection_name", "documents_dir",
        "qa_llm_provider", "intent_llm_provider",
        "cloud_embedding_api_key", "cloud_embedding_multimodal_model_id",
        "cloud_embedding_multimodal_api_full_url",
        "reranker_cloud_api_key", "reranker_cloud_model_id", "reranker_cloud_url",
        "neo4j_uri", "neo4j_user", "neo4j_database", "neo4j_password",
        "use_cloud_embedding", "cloud_embedding_api_type", "cloud_embedding_dimension",
        "local_embedding_batch_size",
        "cloud_embedding_batch_size", "cloud_embedding_max_tokens_per_text",
        "supported_dimensions",
        "enable_graph", "enable_bm25", "use_triple_retrieval",
        "triple_retrieval_fusion_strategy", "triple_retrieval_vector_weight",
        "triple_retrieval_bm25_weight", "triple_retrieval_graph_weight",
        "enable_reranker", "reranker_top_k",
        "rerank_strategy", "reranker_cloud_instruct",
        "default_query_results", "db_batch_size", "default_dimension",
        "vector_enable_keyword_in_three_path", "similarity_threshold",
        "max_results_for_llm", "max_content_length",
        "entity_dict_path", "relation_extraction_method", "enable_graph_enhancement",
        "chunk_size", "chunk_overlap_ratio", "chunk_overlap_min",
        "enable_chapter_split", "parent_max_tokens", "max_file_size_mb",
        "local_embedding_model_path", "local_reranker_model_path",
        "verbose", "log_level",
        "intent_classifier_instructions", "qa_reasoning_instructions",
        "qa_summary_instructions", "extractor_instructions",
    }

    def __init__(self, **kwargs: Any) -> None:
        # ---- Validate unknown keys ----
        unknown_keys = set(kwargs.keys()) - self._KNOWN_KEYS
        if unknown_keys:
            warnings.warn(
                f"RAGConfig received unknown parameters: {sorted(unknown_keys)}. "
                f"These will be ignored. Check for typos.",
                UserWarning,
                stacklevel=2,
            )

        # ---- 路径与存储 ----
        self.project_root: Optional[Path] = self._as_path(kwargs.get("project_root"))
        self.db_path: Optional[Path] = self._as_path(kwargs.get("db_path"))
        self.collection_name: str = kwargs.get("collection_name") or "default"
        self.documents_dir: Optional[Path] = self._as_path(kwargs.get("documents_dir"))

        # ---- LLM 注入（不注入 = 纯检索模式）----
        self.qa_llm_provider: Optional[RAGLLMProvider] = kwargs.get("qa_llm_provider")
        self.intent_llm_provider: Optional[RAGLLMProvider] = kwargs.get("intent_llm_provider")

        # ---- Embedding 敏感信息（kwargs 优先，兜底读环境变量）----
        self.cloud_embedding_api_key: Optional[str] = kwargs.get("cloud_embedding_api_key") or os.getenv("RAG_CLOUD_EMBEDDING_API_KEY")
        self.cloud_embedding_multimodal_model_id: Optional[str] = kwargs.get("cloud_embedding_multimodal_model_id") or os.getenv("RAG_CLOUD_EMBEDDING_MULTIMODAL_MODEL_ID")
        self.cloud_embedding_multimodal_api_full_url: Optional[str] = kwargs.get("cloud_embedding_multimodal_api_full_url") or os.getenv("RAG_CLOUD_EMBEDDING_MULTIMODAL_API_FULL_URL")

        # ---- Reranker 敏感信息（kwargs 优先，兜底读环境变量）----
        self.reranker_cloud_api_key: Optional[str] = kwargs.get("reranker_cloud_api_key") or os.getenv("RAG_RERANKER_CLOUD_API_KEY")
        self.reranker_cloud_model_id: Optional[str] = kwargs.get("reranker_cloud_model_id") or os.getenv("RAG_RERANKER_CLOUD_MODEL_ID")
        self.reranker_cloud_url: Optional[str] = kwargs.get("reranker_cloud_url") or os.getenv("RAG_RERANKER_CLOUD_URL")
        
        # ---- Neo4j 敏感信息（kwargs 优先，兜底读环境变量）----
        self.neo4j_uri: Optional[str] = kwargs.get("neo4j_uri") or os.getenv("RAG_NEO4J_URI")
        self.neo4j_user: Optional[str] = kwargs.get("neo4j_user") or os.getenv("RAG_NEO4J_USER")
        self.neo4j_database: Optional[str] = kwargs.get("neo4j_database") or os.getenv("RAG_NEO4J_DATABASE")
        self.neo4j_password: Optional[str] = kwargs.get("neo4j_password") or os.getenv("RAG_NEO4J_PASSWORD")

        # ---- Embedding ----
        self.use_cloud_embedding: bool = kwargs.get("use_cloud_embedding", DEFAULT_USE_CLOUD_EMBEDDING)
        self.cloud_embedding_api_type: Optional[str] = kwargs.get("cloud_embedding_api_type", DEFAULT_API_TYPE)
        self.cloud_embedding_dimension: Optional[int] = kwargs.get("cloud_embedding_dimension", DEFAULT_CLOUD_EMBEDDING_DIMENSION)
        self.local_embedding_batch_size: int = kwargs.get("local_embedding_batch_size", DEFAULT_LOCAL_EMBEDDING_BATCH_SIZE)
        self.cloud_embedding_batch_size: int = kwargs.get("cloud_embedding_batch_size", DEFAULT_CLOUD_EMBEDDING_BATCH_SIZE)
        self.cloud_embedding_max_tokens_per_text: int = kwargs.get("cloud_embedding_max_tokens_per_text", DEFAULT_CLOUD_EMBEDDING_MAX_TOKENS_PER_TEXT)
        self.supported_dimensions: List[int] = kwargs.get("supported_dimensions", DEFAULT_SUPPORTED_DIMENSIONS)
        self.local_embedding_model_path: Optional[Path] = self._as_path(kwargs.get("local_embedding_model_path"))

        # ---- 召回策略 ----
        self.enable_graph: bool = kwargs.get("enable_graph", DEFAULT_ENABLE_GRAPH)
        self.enable_bm25: bool = kwargs.get("enable_bm25", DEFAULT_ENABLE_BM25)
        self.use_triple_retrieval: bool = kwargs.get("use_triple_retrieval", DEFAULT_USE_TRIPLE_RETRIEVAL)
        self.triple_retrieval_fusion_strategy: str = kwargs.get("triple_retrieval_fusion_strategy", DEFAULT_TRIPLE_RETRIEVAL_FUSION_STRATEGY)
        self.triple_retrieval_vector_weight: float = kwargs.get("triple_retrieval_vector_weight", DEFAULT_TRIPLE_RETRIEVAL_VECTOR_WEIGHT)
        self.triple_retrieval_bm25_weight: float = kwargs.get("triple_retrieval_bm25_weight", DEFAULT_TRIPLE_RETRIEVAL_BM25_WEIGHT)
        self.triple_retrieval_graph_weight: float = kwargs.get("triple_retrieval_graph_weight", DEFAULT_TRIPLE_RETRIEVAL_GRAPH_WEIGHT)

        # ---- Reranker ----
        self.enable_reranker: bool = kwargs.get("enable_reranker", DEFAULT_ENABLE_RERANKER)
        self.reranker_top_k: int = kwargs.get("reranker_top_k", DEFAULT_RERANKER_TOP_K)
        self.rerank_strategy: str = kwargs.get("rerank_strategy", DEFAULT_RERANK_STRATEGY)
        self.reranker_cloud_instruct: Optional[str] = kwargs.get("reranker_cloud_instruct")
        self.local_reranker_model_path: Optional[Path] = self._as_path(kwargs.get("local_reranker_model_path"))

        # ---- 检索参数 ----
        self.default_query_results: int = kwargs.get("default_query_results", DEFAULT_DEFAULT_QUERY_RESULTS)
        self.db_batch_size: int = kwargs.get("db_batch_size", DEFAULT_DB_BATCH_SIZE)
        self.default_dimension: int = kwargs.get("default_dimension", DEFAULT_DEFAULT_DIMENSION)
        self.vector_enable_keyword_in_three_path: bool = kwargs.get("vector_enable_keyword_in_three_path", DEFAULT_VECTOR_ENABLE_KEYWORD_IN_THREE_PATH)
        self.similarity_threshold: float = kwargs.get("similarity_threshold", DEFAULT_SIMILARITY_THRESHOLD)
        self.max_results_for_llm: int = kwargs.get("max_results_for_llm", DEFAULT_MAX_RESULTS_FOR_LLM)
        self.max_content_length: int = kwargs.get("max_content_length", DEFAULT_MAX_CONTENT_LENGTH)

        # ---- 图谱增强 ----
        self.entity_dict_path: Optional[str] = kwargs.get("entity_dict_path")
        self.relation_extraction_method: str = kwargs.get("relation_extraction_method", DEFAULT_RELATION_EXTRACTION_METHOD)
        self.enable_graph_enhancement: bool = kwargs.get("enable_graph_enhancement", DEFAULT_ENABLE_GRAPH_ENHANCEMENT)

        # ---- 构建流水线 ----
        self.chunk_size: int = kwargs.get("chunk_size", DEFAULT_CHUNK_SIZE)
        self.chunk_overlap_ratio: float = kwargs.get("chunk_overlap_ratio", DEFAULT_CHUNK_OVERLAP_RATIO)
        self.chunk_overlap_min: int = kwargs.get("chunk_overlap_min", DEFAULT_CHUNK_OVERLAP_MIN)
        self.enable_chapter_split: bool = kwargs.get("enable_chapter_split", DEFAULT_ENABLE_CHAPTER_SPLIT)
        self.parent_max_tokens: int = kwargs.get("parent_max_tokens", DEFAULT_PARENT_MAX_TOKENS)
        self.max_file_size_mb: int = kwargs.get("max_file_size_mb", DEFAULT_MAX_FILE_SIZE_MB)

        # ---- 可观测 ----
        self.verbose: bool = kwargs.get("verbose", DEFAULT_VERBOSE)
        self.log_level: int = kwargs.get("log_level", DEFAULT_LOG_LEVEL)

        # ---- 指令覆盖（外部传入优先，内置 .md 作为默认值）----
        self.intent_classifier_instructions: Optional[str] = kwargs.get("intent_classifier_instructions")
        self.qa_reasoning_instructions: Optional[str] = kwargs.get("qa_reasoning_instructions")
        self.qa_summary_instructions: Optional[str] = kwargs.get("qa_summary_instructions")
        self.extractor_instructions: Optional[str] = kwargs.get("extractor_instructions")

        # ---- Parameter validation ----
        self._validate()

    def _validate(self) -> None:
        """Validate configuration parameters."""
        if self.chunk_size <= 0:
            raise RAGConfigError(f"chunk_size must be positive, got {self.chunk_size}")
        if not 0.0 <= self.similarity_threshold <= 1.0:
            raise RAGConfigError(
                f"similarity_threshold must be between 0 and 1, got {self.similarity_threshold}"
            )
        if self.max_results_for_llm <= 0:
            raise RAGConfigError(f"max_results_for_llm must be positive, got {self.max_results_for_llm}")
        if self.reranker_top_k <= 0:
            raise RAGConfigError(f"reranker_top_k must be positive, got {self.reranker_top_k}")
        if self.max_file_size_mb <= 0:
            raise RAGConfigError(f"max_file_size_mb must be positive, got {self.max_file_size_mb}")

    def __repr__(self) -> str:
        """Return a readable repr that masks sensitive fields."""
        def _mask(val: Optional[str]) -> str:
            if val is None:
                return "None"
            if len(val) <= 4:
                return "****"
            return val[:4] + "****"

        return (
            f"RAGConfig("
            f"db_path={self.db_path!r}, "
            f"enable_graph={self.enable_graph}, "
            f"enable_bm25={self.enable_bm25}, "
            f"use_cloud_embedding={self.use_cloud_embedding}, "
            f"cloud_embedding_api_key={_mask(self.cloud_embedding_api_key)}, "
            f"neo4j_uri={self.neo4j_uri!r}, "
            f"neo4j_password={_mask(self.neo4j_password)}"
            f")"
        )

    @staticmethod
    def _as_path(v: Any) -> Optional[Path]:
        if v is None:
            return None
        if isinstance(v, Path):
            return v
        return Path(str(v))

    def resolve_project_root(self) -> Path:
        """解析 project_root：若未设置则抛出异常。"""
        if self.project_root is not None:
            return self.project_root
        raise RAGConfigError("project_root is required and must be explicitly provided")

    def resolve_db_path(self) -> Path:
        """解析绝对 db_path。"""
        p = self.db_path
        if p is None:
            raise RAGConfigError("db_path is required and must be explicitly provided")
        if not p.is_absolute():
            p = self.resolve_project_root() / p
        return p


