"""pandapal/knowledge_base/tests/test_builder.py — 建库四段流水线编排。

覆盖设计文档用例 21（inv-16 / R3.4 / R6.1）：
  - rebuild=True → 全量；rebuild=False 且 diff.mode=="full" → 回退全量（incremental_arg=None）
  - 真增量 → incremental_arg={delete_sources, only_sources}
  - BM25 全量重建条件：rebuild 或本次增量含删除；纯增量复用 → rebuild=False
  - 进度回调：stage 含 prepare/vector/bm25/done，percent 单调不减、末值 100
  - 故障注入：向量阶段失败 → RuntimeError 且不再走 BM25
  - 取消：is_cancelled 为真 → CancelledError

重量依赖（NexusRAG / chromadb / numpy）通过 sys.modules 注入假模块，绝不触发真实导入。
"""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass, field

import pytest

from pandapal.knowledge_base.builder import run_build
from pandapal.knowledge_base.models import KBConfig

_PREPARED = {"prepared": True}


@dataclass
class _FakeDiff:
    mode: str
    changed_sources: list[str] = field(default_factory=list)
    deleted_sources: list[str] = field(default_factory=list)


class _FakeRagBoundary:
    """假 NexusRAG 边界：记录四段流水线调用参数，行为可配置。"""

    def __init__(self) -> None:
        self.diff_mode = "incremental"
        self.changed_sources: list[str] = []
        self.deleted_sources: list[str] = []
        self.vector_success = True
        self.vector_error = ""
        self.bm25_success = True
        self.bm25_error = ""
        self.build_rag_data_calls: list[dict] = []
        self.build_rag_data_incremental_calls: list[dict] = []
        self.vector_calls: list[dict] = []
        self.bm25_rebuild_args: list[bool] = []

    async def build_rag_data(self, **kwargs):
        self.build_rag_data_calls.append(kwargs)
        return _PREPARED

    async def build_rag_data_incremental(self, **kwargs):
        self.build_rag_data_incremental_calls.append(kwargs)
        diff = _FakeDiff(self.diff_mode, list(self.changed_sources), list(self.deleted_sources))
        return _PREPARED, diff


def _install_fake_rag_modules(boundary: _FakeRagBoundary) -> dict[str, types.ModuleType]:
    class _FakeConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _FakeVectorPipeline:
        def __init__(self, config):
            self.config = config

        def run_build_vector_pipeline(self, prepared, rebuild, *rest):
            boundary.vector_calls.append(
                {"prepared": prepared, "rebuild": rebuild, "incremental_arg": rest[-1] if rest else None}
            )
            return {"builds": {"success": boundary.vector_success, "error": boundary.vector_error}}

    class _FakeBm25Pipeline:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run_build(self, rebuild):
            boundary.bm25_rebuild_args.append(rebuild)
            return {"index_build": {"success": boundary.bm25_success, "error": boundary.bm25_error}}

    def _mod(**attrs) -> types.ModuleType:
        module = types.ModuleType("fake")
        for key, value in attrs.items():
            setattr(module, key, value)
        return module

    return {
        "pandapal.knowledge_base.rag": _mod(RAGConfig=_FakeConfig),
        "pandapal.knowledge_base.rag.builder": _mod(),
        "pandapal.knowledge_base.rag.builder.data": _mod(),
        "pandapal.knowledge_base.rag.builder.data.build_rag_data": _mod(
            build_rag_data=boundary.build_rag_data,
            build_rag_data_incremental=boundary.build_rag_data_incremental,
        ),
        "pandapal.knowledge_base.rag.builder.vector": _mod(),
        "pandapal.knowledge_base.rag.builder.vector.build_vector_pipeline": _mod(
            BuildVectorPipeline=_FakeVectorPipeline,
            BuildVectorPipelineConfig=_FakeConfig,
        ),
        "pandapal.knowledge_base.rag.embedding": _mod(),
        "pandapal.knowledge_base.rag.embedding.embedding": _mod(CloudEmbeddingConfig=_FakeConfig),
        "pandapal.knowledge_base.rag.builder.bm25": _mod(),
        "pandapal.knowledge_base.rag.builder.bm25.build_bm25_pipeline": _mod(
            BM25PipelineBuild=_FakeBm25Pipeline
        ),
    }


@pytest.fixture
def rag(monkeypatch) -> _FakeRagBoundary:
    boundary = _FakeRagBoundary()
    for name, module in _install_fake_rag_modules(boundary).items():
        monkeypatch.setitem(sys.modules, name, module)
    return boundary


def _cfg(documents_dir) -> KBConfig:
    return KBConfig(
        name="K",
        documents_dir=str(documents_dir),
        embedding_api_key="sk-x",
        embedding_model="text-embedding-v3",
        embedding_api_url="https://api.example.com/v1",
        embedding_api_type="text",
        embedding_dimension=1024,
        llm_provider="dashscope",
        llm_api_key="sk-y",
        llm_model="qwen-plus",
        enable_bm25=True,
    )


async def _noop_cb(*_args) -> None:
    pass


# ── 分派：全量 / 回退全量 / 真增量（含与不含删除）──────────────────────────


@pytest.mark.parametrize(
    "rebuild, diff_mode, deleted, expected_incremental_arg, expected_bm25_rebuild",
    [
        (True, "full", [], None, True),                                                   # 全量重建
        (False, "full", [], None, False),                                                 # 增量回退全量
        (False, "incremental", ["del.md"],                                                   # 真增量含删除
         {"delete_sources": ["del.md"], "only_sources": ["a.md"]}, True),
        (False, "incremental", [],                                                           # 真增量无删除
         {"delete_sources": [], "only_sources": ["a.md"]}, False),
    ],
)
async def test_run_build_dispatch(
    rag, tmp_path, rebuild, diff_mode, deleted, expected_incremental_arg, expected_bm25_rebuild
):
    rag.diff_mode = diff_mode
    rag.changed_sources = ["a.md"]
    rag.deleted_sources = deleted

    await run_build(
        config=_cfg(tmp_path / "K" / "documents"),
        llm_provider=object(),
        embedding_api_key="sk-x",
        rebuild=rebuild,
        progress_cb=_noop_cb,
        is_cancelled=lambda: False,
    )

    if rebuild:
        assert rag.build_rag_data_calls and not rag.build_rag_data_incremental_calls
    else:
        assert rag.build_rag_data_incremental_calls
    assert rag.vector_calls[0]["rebuild"] is rebuild
    assert rag.vector_calls[0]["incremental_arg"] == expected_incremental_arg
    assert rag.bm25_rebuild_args == [expected_bm25_rebuild]


# ── 进度回调：阶段齐全、percent 单调不减、末值 100 ─────────────────────────


async def test_run_build_reports_monotonic_progress(rag, tmp_path):
    rag.diff_mode = "incremental"
    rag.changed_sources = ["a.md"]
    progress: list[tuple] = []

    async def cb(stage, percent, message):
        progress.append((stage, percent, message))

    await run_build(
        config=_cfg(tmp_path / "K" / "documents"),
        llm_provider=object(),
        embedding_api_key="sk-x",
        rebuild=False,
        progress_cb=cb,
        is_cancelled=lambda: False,
    )

    stages = [p[0] for p in progress]
    assert {"prepare", "vector", "bm25", "done"} <= set(stages)
    percents = [p[1] for p in progress]
    assert percents == sorted(percents)
    assert percents[-1] == 100
    assert stages[-1] == "done"


# ── 故障注入：向量阶段失败 → RuntimeError，不再走 BM25 ──────────────────────


async def test_run_build_vector_failure_raises_before_bm25(rag, tmp_path):
    rag.diff_mode = "incremental"
    rag.changed_sources = ["a.md"]
    rag.vector_success = False
    rag.vector_error = "维度不匹配"

    with pytest.raises(RuntimeError, match="向量索引构建失败"):
        await run_build(
            config=_cfg(tmp_path / "K" / "documents"),
            llm_provider=object(),
            embedding_api_key="sk-x",
            rebuild=False,
            progress_cb=_noop_cb,
            is_cancelled=lambda: False,
        )

    assert rag.bm25_rebuild_args == []


# ── 取消：is_cancelled 为真 → CancelledError ────────────────────────────────


async def test_run_build_cancelled_raises(rag, tmp_path):
    with pytest.raises(asyncio.CancelledError):
        await run_build(
            config=_cfg(tmp_path / "K" / "documents"),
            llm_provider=object(),
            embedding_api_key="sk-x",
            rebuild=False,
            progress_cb=_noop_cb,
            is_cancelled=lambda: True,
        )

    assert rag.build_rag_data_incremental_calls == []
