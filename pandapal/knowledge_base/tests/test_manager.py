"""pandapal/knowledge_base/tests/test_manager.py — 知识库生命周期编排。

覆盖设计文档用例 1–3、5、7–19（manager 编排层）：
  - create：契约校验顺序、重名/上限拦截、派生 documents_dir、事件发射
  - inv-7：_derive_status 真值表（0-switch 全组合）
  - inv-8：上传落点/路径穿越/大小格式兜底，批量分派不丢不重
  - R3.5：_maybe_auto_build 触发条件真值表（仅四分支）
  - inv-10：同名上传覆盖、delete_document 幂等、空库不建、抢占保护
  - 故障注入：建库异常 → failed + _build_errors；取消 → 回 pending
  - inv-13：delete_kb 级联清理

真实文件系统（documents_dir/db_path）走 tmp_path；重量依赖（NexusRAG / 建库流水线）用替身隔离。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

import pytest

from pandapal.events.normalized import EventType
from pandapal.knowledge_base.models import KBConfig, MAX_KB_COUNT


# ── 辅助 ───────────────────────────────────────────────────────────────────


def _cfg_dict(name: str, **overrides) -> dict:
    base = dict(
        name=name,
        embedding_api_key="sk-x",
        embedding_model="text-embedding-v3",
        embedding_api_url="https://api.example.com/v1",
        llm_provider="dashscope",
        llm_api_key="sk-y",
        llm_model="qwen-plus",
    )
    base.update(overrides)
    return base


def _events(broadcast, event_type) -> list:
    return [e for e in broadcast.events if e.event_type == event_type]


def _stub_build_kb(monkeypatch, manager, sink: list) -> None:
    """把 build_kb 换成记录器，隔离重量建库流水线。"""

    async def spy(name, rebuild=False):
        sink.append((name, rebuild))

    monkeypatch.setattr(manager, "build_kb", spy)


def _patch_size(monkeypatch, filename: str, size_bytes: int) -> None:
    """仅对指定文件名伪造 st_size（避免真写 50MB 文件）。"""
    real_stat = Path.stat

    def fake_stat(self, *, follow_symlinks=True):
        st = real_stat(self, follow_symlinks=follow_symlinks)
        if self.name == filename:
            parts = list(st)
            parts[6] = size_bytes
            return os.stat_result(parts)
        return st

    monkeypatch.setattr(Path, "stat", fake_stat)


class _RunningTask:
    def done(self) -> bool:
        return False


# ── 用例 1：create 合法 → 落盘 + 派生目录 + kb_saved ─────────────────────────


async def test_create_kb_persists_and_derives_documents_dir(manager, store, broadcast, tmp_path):
    await manager.create_kb(_cfg_dict("公司制度"))

    saved = _events(broadcast, EventType.KB_SAVED)
    assert [e.payload["name"] for e in saved] == ["公司制度"]
    assert (tmp_path / "公司制度" / "documents").is_dir()

    cfg = store.get("公司制度")
    assert cfg is not None
    assert cfg.status == "empty"
    assert cfg.documents_dir == str(tmp_path / "公司制度" / "documents")
    assert _events(broadcast, EventType.ERROR) == []


# ── 用例 2：重名 → kb_duplicate，旧配置不被覆盖 ──────────────────────────────


async def test_create_duplicate_name_keeps_old_config(manager, store, broadcast):
    await manager.create_kb(_cfg_dict("制度", description="旧"))
    broadcast.events.clear()

    await manager.create_kb(_cfg_dict("制度", description="新"))

    errors = _events(broadcast, EventType.ERROR)
    assert [e.payload["error_code"] for e in errors] == ["kb_duplicate"]
    assert _events(broadcast, EventType.KB_SAVED) == []
    assert store.get("制度").description == "旧"
    assert store.calls_of("save") == [("save", "制度")]


# ── 用例 3：达上限 → kb_limit，未落盘、未建目录 ──────────────────────────────


async def test_create_at_limit_rejected(manager, store, broadcast, tmp_path):
    assert MAX_KB_COUNT == 10
    for i in range(MAX_KB_COUNT):
        store.seed(KBConfig.from_dict(_cfg_dict(f"kb{i}")))

    await manager.create_kb(_cfg_dict("第11个"))

    errors = _events(broadcast, EventType.ERROR)
    assert [e.payload["error_code"] for e in errors] == ["kb_limit"]
    assert len(store.load_all()) == MAX_KB_COUNT
    assert store.calls_of("save") == []
    assert not (tmp_path / "第11个").exists()


# ── 用例 5：inv-7 _derive_status 真值表（0-switch 全组合）────────────────────


@pytest.mark.parametrize(
    "has_docs, running, status, index_exists, expected",
    [
        (False, False, "ready", False, "empty"),
        (True, False, "pending", False, "pending"),
        (True, False, "ready", True, "ready"),
        (True, False, "ready", False, "pending"),
        (True, False, "failed", False, "failed"),
        (True, False, "failed", True, "failed"),
        (True, True, "pending", False, "building"),
        (True, True, "ready", True, "building"),
        (False, True, "ready", False, "building"),
    ],
)
def test_derive_status_truth_table(manager, monkeypatch, has_docs, running, status, index_exists, expected):
    cfg = KBConfig(name="K", documents_dir="/x/documents", status=status)
    monkeypatch.setattr(manager, "_list_documents", lambda c: [{"name": "a.md"}] if has_docs else [])
    monkeypatch.setattr(manager, "_index_exists", lambda c: index_exists)
    if running:
        manager._build_tasks["K"] = _RunningTask()

    assert manager._derive_status(cfg) == expected


# ── 用例 7：inv-8 文件名清洗 + 路径穿越防护 ─────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("../../../evil.txt", "evil.md"),
        ("..\\..\\evil", "evil.md"),
        ("a/b/c.txt", "c.md"),
        ("", "对话记录.md"),
        ("对话:记录", "对话_记录.md"),
        ("正常.md", "正常.md"),
    ],
)
def test_safe_filename_cleans(raw, expected):
    from pandapal.knowledge_base.manager import KnowledgeBaseManager

    assert KnowledgeBaseManager._safe_filename(raw) == expected


def test_unique_target_appends_suffix(tmp_path):
    from pandapal.knowledge_base.manager import KnowledgeBaseManager

    (tmp_path / "正常.md").write_text("x", encoding="utf-8")
    (tmp_path / "正常_1.md").write_text("y", encoding="utf-8")
    assert KnowledgeBaseManager._unique_target(tmp_path, "正常.md").name == "正常_2.md"
    assert KnowledgeBaseManager._unique_target(tmp_path, "新.md").name == "新.md"


@pytest.mark.parametrize(
    "raw, expected_name",
    [
        ("../../../evil.txt", "evil.md"),
        ("..\\..\\evil", "evil.md"),
        ("a/b/c.txt", "c.md"),
    ],
)
async def test_save_text_as_document_stays_inside_documents_dir(manager, broadcast, tmp_path, raw, expected_name):
    await manager.create_kb(_cfg_dict("K"))
    docs = tmp_path / "K" / "documents"

    await manager.save_text_as_document("K", raw, "内容")

    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert files == [docs / expected_name]
    assert not (tmp_path / "evil.txt").exists()

    evs = _events(broadcast, EventType.KB_DOCUMENTS_CHANGED)
    assert len(evs) == 1
    assert evs[0].payload["uploaded"] == [{"name": expected_name, "suffix": ".md"}]


async def test_save_text_as_document_empty_name_defaults(manager, tmp_path):
    await manager.create_kb(_cfg_dict("K"))

    await manager.save_text_as_document("K", "", "内容")

    assert (tmp_path / "K" / "documents" / "对话记录.md").read_text(encoding="utf-8") == "内容"


async def test_save_text_as_document_conflict_does_not_overwrite(manager, tmp_path):
    await manager.create_kb(_cfg_dict("K"))
    docs = tmp_path / "K" / "documents"

    await manager.save_text_as_document("K", "正常.md", "原始")
    await manager.save_text_as_document("K", "正常.md", "新内容")

    assert (docs / "正常.md").read_text(encoding="utf-8") == "原始"
    assert (docs / "正常_1.md").read_text(encoding="utf-8") == "新内容"


# ── 用例 8：inv-8 批量上传分派守恒（混合合法/非法）──────────────────────────


async def test_upload_documents_splits_and_conserves(manager, store, broadcast, tmp_path, monkeypatch):
    await manager.create_kb(_cfg_dict("K"))
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.pdf").write_bytes(b"a")
    (src / "b.md").write_text("b", encoding="utf-8")
    (src / "c.exe").write_bytes(b"c")
    (src / "d.pdf").write_bytes(b"big")
    ghost = src / "ghost.pdf"  # 不创建：触发「文件不存在」

    _patch_size(monkeypatch, "d.pdf", 51 * 1024 * 1024)
    build_calls: list = []
    _stub_build_kb(monkeypatch, manager, build_calls)

    await manager.upload_documents(
        "K",
        [str(src / "a.pdf"), str(src / "b.md"), str(src / "c.exe"), str(ghost), str(src / "d.pdf")],
    )

    evs = _events(broadcast, EventType.KB_DOCUMENTS_CHANGED)
    assert len(evs) == 1
    payload = evs[0].payload
    assert payload["name"] == "K"
    assert len(payload["uploaded"]) + len(payload["rejected"]) == 5  # 守恒
    assert {u["name"] for u in payload["uploaded"]} == {"a.pdf", "b.md"}
    assert all("suffix" in u for u in payload["uploaded"])
    reasons = {r["name"]: r["reason"] for r in payload["rejected"]}
    assert set(reasons) == {"c.exe", "ghost.pdf", "d.pdf"}
    assert "不支持" in reasons["c.exe"]
    assert "过大" in reasons["d.pdf"]
    assert reasons["ghost.pdf"]

    docs = tmp_path / "K" / "documents"
    assert {f.name for f in docs.iterdir()} == {"a.pdf", "b.md"}
    assert store.get("K").status == "pending"


# ── 用例 9：全部被拒 → 不置 pending、不触发建库 ─────────────────────────────


async def test_upload_all_rejected_does_not_build_or_set_pending(manager, store, broadcast, tmp_path, monkeypatch):
    await manager.create_kb(_cfg_dict("K", auto_rebuild=True))
    src = tmp_path / "bad.exe"
    src.write_bytes(b"x")
    build_calls: list = []
    _stub_build_kb(monkeypatch, manager, build_calls)

    await manager.upload_documents("K", [str(src)])

    evs = _events(broadcast, EventType.KB_DOCUMENTS_CHANGED)
    assert len(evs) == 1
    assert evs[0].payload["uploaded"] == []
    assert len(evs[0].payload["rejected"]) == 1
    assert build_calls == []
    assert store.calls_of("update_status") == []
    assert store.get("K").status == "empty"


# ── 用例 10：R3.5 _maybe_auto_build 触发条件真值表 ──────────────────────────


@pytest.mark.parametrize(
    "auto_rebuild, has_docs, expected_calls",
    [
        (True, True, 1),
        (False, True, 0),
        (True, False, 0),
        (False, False, 0),
    ],
)
async def test_maybe_auto_build_gating(manager, monkeypatch, tmp_path, auto_rebuild, has_docs, expected_calls):
    cfg = KBConfig(name="K", documents_dir=str(tmp_path / "K" / "documents"), auto_rebuild=auto_rebuild)
    monkeypatch.setattr(manager, "_list_documents", lambda c: [{"name": "a.md"}] if has_docs else [])
    recorded: list = []
    _stub_build_kb(monkeypatch, manager, recorded)

    await manager._maybe_auto_build(cfg)

    assert len(recorded) == expected_calls
    if expected_calls:
        assert recorded[0] == ("K", False)  # 增量重建（rebuild=False）


# ── 用例 11：同名上传 → 拒绝、不覆盖（R1：明确语义）────────────────────────


async def test_upload_same_name_rejected(manager, store, broadcast, tmp_path, monkeypatch):
    # R1（knowledge-base-design.md:93）：目标目录已存在同名文件 → 记入 rejected，不拷贝、不覆盖
    await manager.create_kb(_cfg_dict("K"))
    docs = tmp_path / "K" / "documents"
    (docs / "a.pdf").write_bytes(b"OLD")
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.pdf").write_bytes(b"NEW")
    build_calls: list = []
    _stub_build_kb(monkeypatch, manager, build_calls)

    await manager.upload_documents("K", [str(src / "a.pdf")])

    # 既有文件未被覆盖，目录中仍只有原文件
    assert (docs / "a.pdf").read_bytes() == b"OLD"
    assert [f.name for f in docs.iterdir()] == ["a.pdf"]
    # 事件：uploaded 空、rejected 含该文件
    evs = _events(broadcast, EventType.KB_DOCUMENTS_CHANGED)
    assert len(evs) == 1
    assert evs[0].payload["uploaded"] == []
    assert [r["name"] for r in evs[0].payload["rejected"]] == ["a.pdf"]
    # 未入库 → 不置 PENDING、不触发建库
    assert store.calls_of("update_status") == []
    assert store.get("K").status == "empty"
    assert build_calls == []


# ── 用例 12：delete_document 幂等 + 空库不建库 ──────────────────────────────


async def test_delete_document_idempotent_and_no_build_when_empty(manager, store, broadcast, tmp_path, monkeypatch):
    await manager.create_kb(_cfg_dict("K", auto_rebuild=True))
    docs = tmp_path / "K" / "documents"
    (docs / "only.pdf").write_bytes(b"x")
    built: list = []
    _stub_build_kb(monkeypatch, manager, built)

    await manager.delete_document("K", "only.pdf")

    assert not (docs / "only.pdf").exists()
    assert store.get("K").status == "pending"
    assert len(_events(broadcast, EventType.KB_DOCUMENTS_CHANGED)) == 1
    assert built == []  # 库已空 → 不建库

    # 幂等：再删不存在的文件不抛、不改状态、不再发 changed（no-op；固化现状契约）
    await manager.delete_document("K", "ghost.pdf")
    assert len(_events(broadcast, EventType.KB_DOCUMENTS_CHANGED)) == 1
    assert built == []


# ── 用例 13：空库建库 → kb_build_failed ─────────────────────────────────────


async def test_build_kb_empty_dir_rejected(manager, broadcast, tmp_path):
    await manager.create_kb(_cfg_dict("K"))

    await manager.build_kb("K", rebuild=False)

    fails = _events(broadcast, EventType.KB_BUILD_FAILED)
    assert len(fails) == 1
    assert "文档目录为空" in fails[0].payload["error"]
    assert "K" not in manager._build_tasks


# ── 用例 14：进行中建库 → 重复触发被忽略 ────────────────────────────────────


async def test_build_kb_in_progress_ignored(manager, tmp_path, monkeypatch):
    await manager.create_kb(_cfg_dict("K"))
    (tmp_path / "K" / "documents" / "a.md").write_text("x", encoding="utf-8")

    release = asyncio.Event()

    async def blocking(cfg, rebuild):
        await release.wait()

    monkeypatch.setattr(manager, "_run_build_task", blocking)

    await manager.build_kb("K")
    first = manager._build_tasks["K"]
    await manager.build_kb("K")  # 进行中 → 忽略

    assert manager._build_tasks["K"] is first
    assert len(manager._build_tasks) == 1

    release.set()
    await first


# ── 用例 15：inv-6 建库成功 → incremental 标志 + ready + 缓存失效 ───────────


@pytest.mark.parametrize("rebuild, expected_incremental", [(False, True), (True, False)])
async def test_run_build_success_sets_flag_ready_and_invalidates_cache(
    manager, store, broadcast, tmp_path, monkeypatch, rebuild, expected_incremental
):
    await manager.create_kb(_cfg_dict("K"))
    (tmp_path / "K" / "documents" / "a.md").write_text("x", encoding="utf-8")

    import pandapal.knowledge_base.builder as builder_mod
    import pandapal.knowledge_base.rag_llm_provider as provider_mod

    async def fake_run_build(*, config, llm_provider, embedding_api_key, rebuild, progress_cb, is_cancelled):
        await progress_cb("prepare", 0, "解析中")
        await progress_cb("done", 100, "建库完成")

    monkeypatch.setattr(builder_mod, "run_build", fake_run_build)
    monkeypatch.setattr(provider_mod, "build_provider", lambda **kw: object())

    cfg = store.get("K")
    manager._engine_loaded.add("K")  # 预置缓存，验证被失效

    await manager._run_build_task(cfg, rebuild)

    progress = _events(broadcast, EventType.KB_BUILD_PROGRESS)
    assert progress
    assert all(e.payload["incremental"] is expected_incremental for e in progress)
    assert len(_events(broadcast, EventType.KB_BUILD_DONE)) == 1
    assert store.get("K").status == "ready"
    assert "K" not in manager._engine_loaded


# ── 用例 16：故障注入 → failed + _build_errors（错误信息透传）───────────────


async def test_run_build_failure_sets_failed_and_records_error(
    manager, store, broadcast, tmp_path, monkeypatch
):
    await manager.create_kb(_cfg_dict("K"))
    (tmp_path / "K" / "documents" / "a.md").write_text("x", encoding="utf-8")

    import pandapal.knowledge_base.builder as builder_mod
    import pandapal.knowledge_base.rag_llm_provider as provider_mod

    async def boom(*, config, llm_provider, embedding_api_key, rebuild, progress_cb, is_cancelled):
        raise RuntimeError("LLM 抽取超时")

    monkeypatch.setattr(builder_mod, "run_build", boom)
    monkeypatch.setattr(provider_mod, "build_provider", lambda **kw: object())

    await manager._run_build_task(store.get("K"), rebuild=False)  # 异常被吸收，不抛出

    fails = _events(broadcast, EventType.KB_BUILD_FAILED)
    assert len(fails) == 1
    assert "LLM 抽取超时" in fails[0].payload["error"]
    assert store.get("K").status == "failed"
    assert manager._build_errors["K"] == "LLM 抽取超时"


async def test_run_build_missing_embedding_key_fails_before_pipeline(
    manager, store, broadcast, tmp_path
):
    await manager.create_kb(_cfg_dict("K"))
    cfg = store.get("K")
    cfg.embedding_api_key = ""  # 前置校验失败

    await manager._run_build_task(cfg, rebuild=False)

    fails = _events(broadcast, EventType.KB_BUILD_FAILED)
    assert len(fails) == 1
    assert "Embedding API Key" in fails[0].payload["error"]
    assert store.get("K").status == "failed"


# ── 用例 17：inv-11 取消建库（资源回收）────────────────────────────────────


async def test_cancel_build_emits_failed_and_resets_pending(manager, store, broadcast, tmp_path, monkeypatch):
    await manager.create_kb(_cfg_dict("K"))
    (tmp_path / "K" / "documents" / "a.md").write_text("x", encoding="utf-8")

    release = asyncio.Event()

    async def blocking(cfg, rebuild):
        await release.wait()

    monkeypatch.setattr(manager, "_run_build_task", blocking)

    await manager.build_kb("K")
    task = manager._build_tasks["K"]

    await manager.cancel_build("K")

    fails = _events(broadcast, EventType.KB_BUILD_FAILED)
    assert [e.payload["error"] for e in fails] == ["已取消"]
    assert store.get("K").status == "pending"

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancel_build_without_running_task_is_noop(manager, broadcast):
    await manager.cancel_build("ghost")

    assert broadcast.events == []


# ── 用例 18：inv-13 delete_kb 级联清理 ─────────────────────────────────────


async def test_delete_kb_cascades(manager, store, broadcast, tmp_path, monkeypatch):
    await manager.create_kb(_cfg_dict("K"))
    docs = tmp_path / "K" / "documents"
    (docs / "a.md").write_text("x", encoding="utf-8")
    db = Path(store.get("K").db_path)
    db.mkdir(parents=True)
    (db / "index.bin").write_bytes(b"i")

    class FakeEngine:
        def __init__(self):
            self.closed = 0

        def close(self):
            self.closed += 1

    release = asyncio.Event()

    async def blocking(cfg, rebuild):
        await release.wait()

    monkeypatch.setattr(manager, "_run_build_task", blocking)
    await manager.build_kb("K")
    task = manager._build_tasks["K"]

    engine = FakeEngine()
    manager._engines["K"] = engine
    manager._engine_loaded.add("K")
    manager._build_errors["K"] = "old error"

    await manager.delete_kb("K")

    assert [e.payload["name"] for e in _events(broadcast, EventType.KB_DELETED)] == ["K"]
    assert not docs.exists()
    assert not db.exists()
    assert store.get("K") is None
    assert "K" not in manager._build_tasks
    assert "K" not in manager._engines
    assert "K" not in manager._engine_loaded
    assert "K" not in manager._build_errors
    assert engine.closed == 1

    with contextlib.suppress(asyncio.CancelledError):
        await task


# ── 用例 19：删除不存在的库 → kb_not_found，无副作用 ────────────────────────


async def test_delete_kb_missing_emits_not_found(manager, broadcast, tmp_path):
    before = set(tmp_path.rglob("*"))

    await manager.delete_kb("ghost")

    errors = _events(broadcast, EventType.ERROR)
    assert [e.payload["error_code"] for e in errors] == ["kb_not_found"]
    assert _events(broadcast, EventType.KB_DELETED) == []
    assert set(tmp_path.rglob("*")) == before
