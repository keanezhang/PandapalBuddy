# -*- coding: utf-8 -*-
"""
检索子域共用封闭取值常量。

原则：纯常量打散优先，共用才单独文件。本文件为检索子域内共用的封闭取值
（匹配类型、召回源）。意图类型列表仅 query_intent_classifier 使用，已放在该模块内。
"""

# -----------------------------------------------------------------------------
# 匹配类型（检索结果 match_type）
# 与召回源对齐：向量路=vector、BM25路=bm25、图谱路=graph；细分类型保留 hybrid/keyword。
# -----------------------------------------------------------------------------

MATCH_TYPE_VECTOR = "vector"       # 与 RETRIEVAL_SOURCE_VECTOR 一致
MATCH_TYPE_BM25 = "bm25"           # 与 RETRIEVAL_SOURCE_BM25 一致
MATCH_TYPE_GRAPH = "graph"         # 与 RETRIEVAL_SOURCE_GRAPH 一致
MATCH_TYPE_KEYWORD = "keyword"     # 向量路细分：关键词匹配
MATCH_TYPE_HYBRID = "hybrid"       # 向量路细分：语义+关键词混合
MATCH_TYPE_MULTI_SOURCE = "multi_source"  # 多路召回（≥2 路命中）

# -----------------------------------------------------------------------------
# 召回源（检索结果 retrieval_sources 取值）
# -----------------------------------------------------------------------------

RETRIEVAL_SOURCE_VECTOR = "vector"
RETRIEVAL_SOURCE_BM25 = "bm25"
RETRIEVAL_SOURCE_GRAPH = "graph"

# 云端重排序 API（Dashscope 等兼容）默认 URL，Reranker 显式未传 cloud_url 时使用
# 注意：这是默认值，生产环境应通过配置覆盖
DASHSCOPE_RERANK_URL: str = ""  # Must be set via reranker cloud_url parameter
