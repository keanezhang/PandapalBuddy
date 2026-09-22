"""query — RAG 查询模块

常用符号可直接从本包导入::

    from agent.rag.query import Retriever, Reranker, QueryIntentClassifier
"""
from typing import List


def __getattr__(name: str):
    """延迟导入：仅在访问时才加载子模块，避免 import query 即拉起全部依赖。"""
    _lazy_imports = {
        "Retriever": ".retrieval.retriever",
        "Reranker": ".rerank.reranker",
        "QueryIntentClassifier": ".understand.query_intent_classifier",
        "enrich_query_analysis_once": ".enrich.query_enricher",
        "build_graph_relation_summary": ".generation.graph_summary_for_prompt",
        "ReasoningPromptBuilder": ".generation.reasoning_prompt_builder",
        "build_reasoning_prompt": ".generation.reasoning_prompt_builder",
        "enrich_query_analysis_for_reasoning": ".generation.reasoning_router",
    }
    if name in _lazy_imports:
        import importlib
        module = importlib.import_module(_lazy_imports[name], __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__: List[str] = [
    "Retriever",
    "Reranker",
    "QueryIntentClassifier",
    "enrich_query_analysis_once",
    "build_graph_relation_summary",
    "ReasoningPromptBuilder",
    "build_reasoning_prompt",
    "enrich_query_analysis_for_reasoning",
]
