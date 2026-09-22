"""pandapal/knowledge_base/builder.py — 建库长任务编排。

编排 NexusRAG 的建库四段流水线（一期跳过图谱）：
    1. build_rag_data（切分 + LLM 抽取实体/关键词）
    2. BuildVectorPipeline（向量索引）
    3. BM25PipelineBuild（BM25 索引）

全程通过 ``progress_cb`` 上报阶段进度，通过 ``is_cancelled`` 支持取消。
NexusRAG（``pandapal.knowledge_base.rag``）与 chromadb / numpy 为重量依赖，均延迟导入。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from pandapal.knowledge_base.models import (
    KBConfig,
    SUPPORTED_EMBEDDING_DIMENSIONS,
    collection_name_for,
)

logger = logging.getLogger(__name__)

# 阶段 → 进度区间（百分比）
_STAGES: tuple[tuple[str, int, int], ...] = (
    ("prepare", 0, 35),
    ("vector", 35, 75),
    ("bm25", 75, 95),
)

ProgressCallback = Callable[[str, int, str], Awaitable[None]]


async def run_build(
    *,
    config: KBConfig,
    llm_provider: Any,
    embedding_api_key: str,
    rebuild: bool = False,
    progress_cb: ProgressCallback,
    is_cancelled: Callable[[], bool],
) -> dict[str, Any]:
    """执行完整建库流程（纯向量 + BM25，跳过图谱）。

    Args:
        config: 知识库配置
        llm_provider: 符合 RAGLLMProvider 协议的 LLM（建库抽取用）
        embedding_api_key: 云端 embedding 的 api_key（复用 BYOK 凭据）
        rebuild: 是否全量重建
        progress_cb: 异步进度回调 (stage, percent, message)
        is_cancelled: 取消检查
    """
    # 延迟导入 pandapal.knowledge_base.rag（重量依赖）
    from pandapal.knowledge_base.rag import RAGConfig  # noqa: PLC0415

    from pandapal.knowledge_base.rag.builder.data.build_rag_data import (  # noqa: PLC0415
        build_rag_data,
        build_rag_data_incremental,
    )
    from pandapal.knowledge_base.rag.builder.vector.build_vector_pipeline import (  # noqa: PLC0415
        BuildVectorPipeline,
        BuildVectorPipelineConfig,
    )
    from pandapal.knowledge_base.rag.embedding.embedding import (  # noqa: PLC0415
        CloudEmbeddingConfig,
    )

    documents_dir = Path(config.documents_dir)
    project_root = documents_dir.parent
    db_path = Path(config.db_path)
    collection_name = collection_name_for(config.name)

    rag_config = RAGConfig(
        project_root=project_root,
        db_path=db_path,
        collection_name=collection_name,
        documents_dir=documents_dir,
        use_cloud_embedding=True,
        cloud_embedding_api_key=embedding_api_key,
        cloud_embedding_multimodal_model_id=config.embedding_model,
        cloud_embedding_multimodal_api_full_url=config.embedding_api_url_full,
        cloud_embedding_api_type=config.embedding_api_type,
        cloud_embedding_dimension=config.embedding_dimension,
        supported_dimensions=SUPPORTED_EMBEDDING_DIMENSIONS,
        enable_graph=False,
        enable_bm25=config.enable_bm25,
        use_triple_retrieval=False,
        enable_reranker=False,
        max_file_size_mb=50,
        verbose=False,
    )

    async def _report(stage: str, message: str) -> None:
        for name, start, _end in _STAGES:
            if name == stage:
                await progress_cb(stage, start, message)
                return

    def _ensure_not_cancelled() -> None:
        if is_cancelled():
            raise asyncio.CancelledError("建库已取消")

    # ── 1) 数据准备（rebuild=False 走"真增量"：只重建变更文件）──
    await _report("prepare", "正在解析文档并抽取内容…")
    _ensure_not_cancelled()

    incremental_arg = None
    if rebuild:
        prepared = await build_rag_data(
            documents_dir=documents_dir,
            project_root=project_root,
            collection_name=collection_name,
            db_path=db_path,
            rebuild=True,
            verbose=False,
            build_config=rag_config,
            llm_provider=llm_provider,
        )
    else:
        prepared, diff = await build_rag_data_incremental(
            documents_dir=documents_dir,
            project_root=project_root,
            collection_name=collection_name,
            db_path=db_path,
            verbose=False,
            build_config=rag_config,
            llm_provider=llm_provider,
        )
        if diff.mode == "full":
            # 无法增量（缓存缺失/旧 schema/切分参数变更）→ 已全量重建，走全量 upsert
            incremental_arg = None
            await _report("prepare", "全量准备完成（缓存已失效，自动回退全量）")
        else:
            incremental_arg = {
                "delete_sources": list(diff.deleted_sources),
                "only_sources": list(diff.changed_sources),
            }
            await _report(
                "prepare",
                f"增量准备完成：重建 {len(diff.changed_sources)} 个文件，清理 {len(diff.deleted_sources)} 个",
            )

    # ── 2) 向量索引 ──
    await progress_cb("vector", 40, "正在构建向量索引…")
    _ensure_not_cancelled()

    cloud_config = CloudEmbeddingConfig(
        api_key=embedding_api_key,
        model=config.embedding_model,
        api_type=config.embedding_api_type,
        api_full_url=config.embedding_api_url_full,
        dimension=config.embedding_dimension,
    )
    vector_pipeline = BuildVectorPipeline(
        BuildVectorPipelineConfig(
            documents_dir=documents_dir,
            db_path=db_path,
            collection_name=collection_name,
            project_root=project_root,
            use_cloud_embedding=True,
            cloud_config=cloud_config,
            verbose=False,
        )
    )
    vector_result = await asyncio.to_thread(
        vector_pipeline.run_build_vector_pipeline, prepared, rebuild, False, incremental_arg
    )
    if not vector_result.get("builds", {}).get("success"):
        raise RuntimeError(f"向量索引构建失败: {vector_result.get('builds', {}).get('error')}")

    await progress_cb("vector", 70, "向量索引构建完成")

    # ── 3) BM25 索引（可选）──
    if config.enable_bm25:
        # 增量删除清理（需求 §6 风险 1）：BM25 从 VectorStore 子文档抽词条，
        # 且 rebuild=False 时走缓存复用，不会感知向量的按源删除——若本次增量含
        # delete_sources，必须强制全量重建 BM25，否则残留已删文档词条 → 脏召回。
        bm25_rebuild = rebuild or bool(incremental_arg and incremental_arg.get("delete_sources"))
        await progress_cb(
            "bm25",
            80,
            "正在重建 BM25 索引（含删除清理）…" if bm25_rebuild and not rebuild
            else "正在构建 BM25 索引…",
        )
        _ensure_not_cancelled()

        from pandapal.knowledge_base.rag.builder.bm25.build_bm25_pipeline import (  # noqa: PLC0415
            BM25PipelineBuild,
        )

        bm25_pipeline = BM25PipelineBuild(
            documents_dir=documents_dir,
            db_path=db_path,
            collection_name=collection_name,
            project_root=project_root,
            verbose=False,
        )
        bm25_result = await asyncio.to_thread(bm25_pipeline.run_build, bm25_rebuild)
        if not bm25_result.get("index_build", {}).get("success"):
            raise RuntimeError(f"BM25 索引构建失败: {bm25_result.get('index_build', {}).get('error')}")

    await progress_cb("done", 100, "建库完成")
    return {
        "success": True,
        "documents_dir": str(documents_dir),
        "db_path": str(db_path),
        "rebuild": rebuild,
    }
