"""evaluation.metrics — 评估指标计算子模块"""
from typing import List

__all__: List[str] = [
    "calculate_similarity_metrics",
    "calculate_retrieval_quality_metrics",
    "calculate_coverage_metrics",
    "calculate_diversity_metrics",
    "calculate_retrieval_source_metrics",
    "calculate_rerank_metrics",
    "calculate_rrf_metrics",
]


def __getattr__(name: str):
    """延迟导入：仅在访问时才加载子模块。"""
    _lazy = {
        "calculate_similarity_metrics": ".similarity",
        "calculate_retrieval_quality_metrics": ".retrieval_quality",
        "calculate_coverage_metrics": ".coverage",
        "calculate_diversity_metrics": ".coverage",
        "calculate_retrieval_source_metrics": ".retrieval_quality",
        "calculate_rerank_metrics": ".rerank",
        "calculate_rrf_metrics": ".rerank",
    }
    if name in _lazy:
        import importlib
        mod = importlib.import_module(_lazy[name], __name__)
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
