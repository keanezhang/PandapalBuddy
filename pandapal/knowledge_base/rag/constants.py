#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAG SDK 全局默认常量。

所有非敏感配置的默认值集中在此，替代 rag.yaml。
调用方通过 RAGConfig(**kwargs) 传参覆盖任意字段。

敏感信息（API Key、密码）不在此定义，由 RAGConfig 通过 kwargs 或环境变量加载。
"""

from typing import Final, List


# ======================== 路径与存储 ========================

# ======================== Embedding ========================
# 云端
DEFAULT_USE_CLOUD_EMBEDDING: Final[bool] = True

# 以下敏感/业务相关配置由调用方通过 RAGConfig 显式传入或从环境变量加载：
#   cloud_embedding_api_key, cloud_embedding_multimodal_model_id,
#   cloud_embedding_multimodal_api_full_url, cloud_embedding_api_type
DEFAULT_CLOUD_EMBEDDING_DIMENSION: Final[int] = 2048
DEFAULT_CLOUD_EMBEDDING_BATCH_SIZE: Final[int] = 4
DEFAULT_CLOUD_EMBEDDING_MAX_TOKENS_PER_TEXT: Final[int] = 4096
DEFAULT_SUPPORTED_DIMENSIONS: Final[List[int]] = [1024, 2048]
DEFAULT_API_TYPE: Final[str]= "multimodal"

# 本地
DEFAULT_LOCAL_EMBEDDING_BATCH_SIZE: Final[int] = 32

# ======================== 召回策略 ========================
DEFAULT_ENABLE_GRAPH: Final[bool] = True
DEFAULT_ENABLE_BM25: Final[bool] = True
DEFAULT_USE_TRIPLE_RETRIEVAL: Final[bool] = True

# 三路融合权重
DEFAULT_TRIPLE_RETRIEVAL_FUSION_STRATEGY: Final[str] = "rrf"
DEFAULT_TRIPLE_RETRIEVAL_VECTOR_WEIGHT: Final[float] = 0.4
DEFAULT_TRIPLE_RETRIEVAL_BM25_WEIGHT: Final[float] = 0.3
DEFAULT_TRIPLE_RETRIEVAL_GRAPH_WEIGHT: Final[float] = 0.3

# ======================== Reranker ========================
DEFAULT_ENABLE_RERANKER: Final[bool] = True
# 云端 reranker 凭证（api_key, model_id, url）和本地模型路径由 RAGConfig 通过 kwargs 或环境变量加载
DEFAULT_RERANKER_TOP_K: Final[int] = 20
DEFAULT_RERANK_STRATEGY: Final[str] = "child"

# ======================== 检索参数 ========================
DEFAULT_DEFAULT_QUERY_RESULTS: Final[int] = 15
DEFAULT_SIMILARITY_THRESHOLD: Final[float] = 0.2
DEFAULT_MAX_RESULTS_FOR_LLM: Final[int] = 5
DEFAULT_MAX_CONTENT_LENGTH: Final[int] = 2000
DEFAULT_VECTOR_ENABLE_KEYWORD_IN_THREE_PATH: Final[bool] = True

# ======================== 向量数据库 ========================
DEFAULT_DB_BATCH_SIZE: Final[int] = 5000
DEFAULT_DEFAULT_DIMENSION: Final[int] = 2048

# ======================== 图谱增强 ========================
DEFAULT_RELATION_EXTRACTION_METHOD: Final[str] = "rule"
DEFAULT_ENABLE_GRAPH_ENHANCEMENT: Final[bool] = True

# ======================== 构建流水线 ========================
DEFAULT_CHUNK_SIZE: Final[int] = 800
DEFAULT_CHUNK_OVERLAP_RATIO: Final[float] = 0.08
DEFAULT_CHUNK_OVERLAP_MIN: Final[int] = 20
DEFAULT_ENABLE_CHAPTER_SPLIT: Final[bool] = True
DEFAULT_PARENT_MAX_TOKENS: Final[int] = 5000
DEFAULT_MAX_FILE_SIZE_MB: Final[int] = 50

# ======================== 可观测 ========================
DEFAULT_LOG_LEVEL: Final[int] = 20        # 10=DEBUG, 20=INFO, 30=WARNING
DEFAULT_VERBOSE: Final[bool] = True
