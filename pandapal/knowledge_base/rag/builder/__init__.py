"""builder — RAG 离线构建流程模块

提供四大构建子流程的核心入口：
- build_rag_data: 数据处理管线（async 函数）
- BuildVectorPipeline: 向量索引构建
- BuildGraphPipeline: 图索引构建
- BM25PipelineBuild: BM25 索引构建
"""

__all__: list[str] = [
    "build_rag_data",
    "BuildVectorPipeline",
    "BuildGraphPipeline",
    "BM25PipelineBuild",
]


def __getattr__(name: str) -> object:
    """延迟导入，避免拉起重型依赖链。"""
    if name == "build_rag_data":
        from .data.build_rag_data import build_rag_data
        globals()["build_rag_data"] = build_rag_data
        return build_rag_data
    if name == "BuildVectorPipeline":
        from .vector.build_vector_pipeline import BuildVectorPipeline
        globals()["BuildVectorPipeline"] = BuildVectorPipeline
        return BuildVectorPipeline
    if name == "BuildGraphPipeline":
        from .graph.build_graph_pipeline import BuildGraphPipeline
        globals()["BuildGraphPipeline"] = BuildGraphPipeline
        return BuildGraphPipeline
    if name == "BM25PipelineBuild":
        from .bm25.build_bm25_pipeline import BM25PipelineBuild
        globals()["BM25PipelineBuild"] = BM25PipelineBuild
        return BM25PipelineBuild
    raise AttributeError(f"module 'rag.builder' has no attribute {name!r}")
