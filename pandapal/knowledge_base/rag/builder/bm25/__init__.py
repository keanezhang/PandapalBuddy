"""builder/bm25 — BM25 索引构建流水线"""

__all__ = [
    "BM25PipelineBuild",
]


def __getattr__(name: str):
    if name == "BM25PipelineBuild":
        from .build_bm25_pipeline import BM25PipelineBuild
        globals()["BM25PipelineBuild"] = BM25PipelineBuild
        return BM25PipelineBuild
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
