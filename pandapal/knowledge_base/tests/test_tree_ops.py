"""pandapal/knowledge_base/tests/test_tree_ops.py — 结构操作（文件夹/文档增删改移）。

覆盖设计文档用例 17–33：
  - 17 create_folder 成功；18 父目录非法；19 同名；20 非法名
  - 21 rename_folder 成功；22 rename_folder 拒绝表
  - 23 delete_folder 递归；24 delete_folder 根/幂等/非目录
  - 25 删空库不触发 build
  - 26 rename_document 成功；27 rename_document 拒绝表
  - 28 move_entry 成功；29 防环；30 move_entry 拒绝表；31 target==src 幂等
  - 32 建库进行中 → 5 个写操作全部 kb_busy 且无 FS 副作用
  - 33 故障注入 io_error（mkdir / shutil.move 抛 OSError）

真实文件系统走 tmp_path；建库流水线用 ``_stub_build_kb`` 隔离（重量依赖）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pandapal.events.normalized import EventType
from pandapal.knowledge_base.manager import KnowledgeBaseError


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


def _tree(broadcast) -> list:
    trees = _events(broadcast, EventType.KB_TREE_RESULT)
    assert len(trees) == 1
    return trees[0].payload["tree"]


def _names(nodes: list) -> list:
    return [n["name"] for n in nodes]


def _stub_build_kb(monkeypatch, manager, sink: list) -> None:
    async def spy(name, rebuild=False):
        sink.append((name, rebuild))

    monkeypatch.setattr(manager, "build_kb", spy)


# ── 用例 17：create_folder 成功 ────────────────────────────────────────────


async def test_tree17_create_folder_success(manager, store, broadcast, tmp_path):
    await manager.create_kb(_cfg_dict("K"))
    docs = tmp_path / "K" / "documents"
    store.calls.clear()
    broadcast.events.clear()

    await manager.create_folder("K", "", "手册")

    created = docs / "手册"
    assert created.is_dir()
    assert list(created.iterdir()) == []                      # 空文件夹
    assert store.calls_of("update_status") == []              # 空文件夹不入索引
    assert store.calls_of("save") == []
    assert _events(broadcast, EventType.KB_DOCUMENTS_CHANGED) == []
    assert "手册" in _names(_tree(broadcast))


# ── 用例 18：父目录非法 → path_not_found，无副作用 ─────────────────────────


@pytest.mark.parametrize("parent_path", ["ghost", "a.md"])
async def test_tree18_create_folder_bad_parent(manager, broadcast, tmp_path, parent_path):
    await manager.create_kb(_cfg_dict("K"))
    docs = tmp_path / "K" / "documents"
    (docs / "a.md").write_text("x", encoding="utf-8")
    broadcast.events.clear()
    before = set(tmp_path.rglob("*"))

    with pytest.raises(KnowledgeBaseError) as ei:
        await manager.create_folder("K", parent_path, "x")

    assert ei.value.code == "path_not_found"
    assert parent_path in ei.value.detail
    assert set(tmp_path.rglob("*")) == before
    assert _events(broadcast, EventType.KB_TREE_RESULT) == []


# ── 用例 19：同名冲突 → name_conflict，原内容不动 ──────────────────────────


async def test_tree19_create_folder_name_conflict(manager, broadcast, tmp_path):
    await manager.create_kb(_cfg_dict("K"))
    docs = tmp_path / "K" / "documents"
    (docs / "existing").mkdir()
    (docs / "existing" / "keep.md").write_text("原始", encoding="utf-8")
    broadcast.events.clear()

    with pytest.raises(KnowledgeBaseError) as ei:
        await manager.create_folder("K", "", "existing")

    assert ei.value.code == "name_conflict"
    assert (docs / "existing" / "keep.md").read_text(encoding="utf-8") == "原始"
    assert _events(broadcast, EventType.KB_TREE_RESULT) == []


# ── 用例 20：非法名 → invalid_name，无副作用 ───────────────────────────────


async def test_tree20_create_folder_invalid_name(manager, broadcast, tmp_path):
    await manager.create_kb(_cfg_dict("K"))
    broadcast.events.clear()
    before = set(tmp_path.rglob("*"))

    with pytest.raises(KnowledgeBaseError) as ei:
        await manager.create_folder("K", "", "a/b")

    assert ei.value.code == "invalid_name"
    assert set(tmp_path.rglob("*")) == before
    assert _events(broadcast, EventType.KB_TREE_RESULT) == []


# ── 用例 21：rename_folder 成功 ────────────────────────────────────────────


async def test_tree21_rename_folder_success(manager, store, broadcast, tmp_path):
    await manager.create_kb(_cfg_dict("K"))
    docs = tmp_path / "K" / "documents"
    (docs / "old").mkdir()
    (docs / "old" / "f.md").write_text("f", encoding="utf-8")
    store.calls.clear()
    broadcast.events.clear()

    await manager.rename_folder("K", "old", "new")

    assert not (docs / "old").exists()
    assert (docs / "new" / "f.md").is_file()
    assert store.calls_of("update_status") == []
    tree = _tree(broadcast)
    assert "new" in _names(tree)
    assert "old" not in _names(tree)


# ── 用例 22：rename_folder 拒绝表 ──────────────────────────────────────────


@pytest.mark.parametrize("path, new_name, expected_code", [
    ("", "x", "invalid_path"),          # 根目录不可重命名
    ("ghost", "x", "path_not_found"),   # 源不存在
    ("a.md", "x", "path_not_found"),    # 源不是文件夹
    ("old", "taken", "name_conflict"),  # 目标同名
])
async def test_tree22_rename_folder_reject_table(
    manager, store, broadcast, tmp_path, path, new_name, expected_code
):
    await manager.create_kb(_cfg_dict("K"))
    docs = tmp_path / "K" / "documents"
    (docs / "old").mkdir()
    (docs / "taken").mkdir()
    (docs / "a.md").write_text("x", encoding="utf-8")
    store.calls.clear()
    broadcast.events.clear()

    with pytest.raises(KnowledgeBaseError) as ei:
        await manager.rename_folder("K", path, new_name)

    assert ei.value.code == expected_code
    assert (docs / "old").is_dir()                    # 源未被改动
    assert store.calls_of("update_status") == []
    assert _events(broadcast, EventType.KB_TREE_RESULT) == []


# ── 用例 23：delete_folder 递归 + PENDING + 增量 + 树 ──────────────────────


async def test_tree23_delete_folder_recursive(manager, store, broadcast, tmp_path, monkeypatch):
    await manager.create_kb(_cfg_dict("K", auto_rebuild=True))
    docs = tmp_path / "K" / "documents"
    (docs / "sub").mkdir()
    (docs / "sub" / "x.md").write_text("x", encoding="utf-8")
    (docs / "keep.md").write_text("k", encoding="utf-8")
    built: list = []
    _stub_build_kb(monkeypatch, manager, built)
    broadcast.events.clear()

    await manager.delete_folder("K", "sub")

    assert not (docs / "sub").exists()
    assert (docs / "keep.md").is_file()
    assert store.get("K").status == "pending"
    assert built == [("K", False)]
    assert _names(_tree(broadcast)) == ["keep.md"]


# ── 用例 24：delete_folder 根/幂等/非目录 ──────────────────────────────────


async def test_tree24_delete_folder_root_rejected(manager, store, broadcast, tmp_path, monkeypatch):
    await manager.create_kb(_cfg_dict("K", auto_rebuild=True))
    docs = tmp_path / "K" / "documents"
    (docs / "a.md").write_text("x", encoding="utf-8")
    built: list = []
    _stub_build_kb(monkeypatch, manager, built)
    store.calls.clear()
    broadcast.events.clear()
    before = set(tmp_path.rglob("*"))

    with pytest.raises(KnowledgeBaseError) as ei:
        await manager.delete_folder("K", "")

    assert ei.value.code == "invalid_path"
    assert set(tmp_path.rglob("*")) == before
    assert store.calls_of("update_status") == []
    assert _events(broadcast, EventType.KB_TREE_RESULT) == []


async def test_tree24_delete_folder_missing_is_idempotent(
    manager, store, broadcast, tmp_path, monkeypatch
):
    await manager.create_kb(_cfg_dict("K", auto_rebuild=True))
    docs = tmp_path / "K" / "documents"
    (docs / "a.md").write_text("x", encoding="utf-8")
    built: list = []
    _stub_build_kb(monkeypatch, manager, built)
    store.calls.clear()
    broadcast.events.clear()

    await manager.delete_folder("K", "ghost")          # 不存在 → no-op

    assert store.calls_of("update_status") == []
    assert built == []
    assert len(_events(broadcast, EventType.KB_TREE_RESULT)) == 1


async def test_tree24_delete_folder_non_dir_rejected(manager, broadcast, tmp_path):
    await manager.create_kb(_cfg_dict("K"))
    docs = tmp_path / "K" / "documents"
    (docs / "a.md").write_text("x", encoding="utf-8")
    broadcast.events.clear()

    with pytest.raises(KnowledgeBaseError) as ei:
        await manager.delete_folder("K", "a.md")

    assert ei.value.code == "path_not_found"
    assert (docs / "a.md").is_file()


# ── 用例 25：删除库内最后一篇文档 → 不触发 build ───────────────────────────


async def test_tree25_delete_last_document_skips_build(
    manager, store, broadcast, tmp_path, monkeypatch
):
    await manager.create_kb(_cfg_dict("K", auto_rebuild=True))
    docs = tmp_path / "K" / "documents"
    (docs / "sub").mkdir()
    (docs / "sub" / "only.md").write_text("x", encoding="utf-8")
    built: list = []
    _stub_build_kb(monkeypatch, manager, built)
    broadcast.events.clear()

    await manager.delete_folder("K", "sub")

    assert store.get("K").status == "pending"
    assert built == []
    assert _events(broadcast, EventType.KB_BUILD_FAILED) == []


# ── 用例 26：rename_document 成功 ──────────────────────────────────────────


async def test_tree26_rename_document_success(manager, store, broadcast, tmp_path, monkeypatch):
    await manager.create_kb(_cfg_dict("K", auto_rebuild=True))
    docs = tmp_path / "K" / "documents"
    (docs / "old.md").write_text("v1", encoding="utf-8")
    built: list = []
    _stub_build_kb(monkeypatch, manager, built)
    broadcast.events.clear()

    await manager.rename_document("K", "old.md", "new.md")

    assert not (docs / "old.md").exists()
    assert (docs / "new.md").read_text(encoding="utf-8") == "v1"
    assert store.get("K").status == "pending"
    assert built == [("K", False)]
    assert "new.md" in _names(_tree(broadcast))


# ── 用例 27：rename_document 拒绝表 ────────────────────────────────────────


@pytest.mark.parametrize("path, new_name, expected_code", [
    ("ghost.md", "x.md", "path_not_found"),
    ("sub", "x.md", "path_not_found"),      # 目录不是文件
    ("a.md", "b.md", "name_conflict"),
    ("a.md", "a/b.md", "invalid_name"),
])
async def test_tree27_rename_document_reject_table(
    manager, store, broadcast, tmp_path, monkeypatch, path, new_name, expected_code
):
    await manager.create_kb(_cfg_dict("K", auto_rebuild=True))
    docs = tmp_path / "K" / "documents"
    (docs / "a.md").write_text("a", encoding="utf-8")
    (docs / "b.md").write_text("b", encoding="utf-8")
    (docs / "sub").mkdir()
    built: list = []
    _stub_build_kb(monkeypatch, manager, built)
    store.calls.clear()
    broadcast.events.clear()

    with pytest.raises(KnowledgeBaseError) as ei:
        await manager.rename_document("K", path, new_name)

    assert ei.value.code == expected_code
    assert (docs / "a.md").is_file() and (docs / "b.md").is_file()
    assert (docs / "sub").is_dir()
    assert store.calls_of("update_status") == []
    assert built == []
    assert _events(broadcast, EventType.KB_TREE_RESULT) == []


# ── 用例 28：move_entry 成功 ───────────────────────────────────────────────


async def test_tree28_move_entry_success(manager, store, broadcast, tmp_path, monkeypatch):
    await manager.create_kb(_cfg_dict("K", auto_rebuild=True))
    docs = tmp_path / "K" / "documents"
    (docs / "a.md").write_text("x", encoding="utf-8")
    (docs / "dest").mkdir()
    built: list = []
    _stub_build_kb(monkeypatch, manager, built)
    broadcast.events.clear()

    await manager.move_entry("K", "a.md", "dest")

    assert not (docs / "a.md").exists()
    assert (docs / "dest" / "a.md").is_file()
    assert store.get("K").status == "pending"
    assert built == [("K", False)]
    dest_node = next(n for n in _tree(broadcast) if n["name"] == "dest")
    assert "a.md" in _names(dest_node["children"])


# ── 用例 29：move_entry 防环（移入自身或子孙）──────────────────────────────


@pytest.mark.parametrize("target_dir", ["outer", "outer/inner"])
async def test_tree29_move_entry_into_own_subtree_rejected(
    manager, store, broadcast, tmp_path, target_dir
):
    await manager.create_kb(_cfg_dict("K"))
    docs = tmp_path / "K" / "documents"
    (docs / "outer" / "inner").mkdir(parents=True)
    (docs / "outer" / "inner" / "f.md").write_text("f", encoding="utf-8")
    store.calls.clear()
    broadcast.events.clear()
    before = set(tmp_path.rglob("*"))

    with pytest.raises(KnowledgeBaseError) as ei:
        await manager.move_entry("K", "outer", target_dir)

    assert ei.value.code == "invalid_target"
    assert set(tmp_path.rglob("*")) == before
    assert store.calls_of("update_status") == []
    assert _events(broadcast, EventType.KB_TREE_RESULT) == []


# ── 用例 30：move_entry 拒绝表 ─────────────────────────────────────────────


@pytest.mark.parametrize("source, target_dir, expected_code", [
    ("ghost.md", "dest", "path_not_found"),   # 源不存在
    ("a.md", "ghostdir", "invalid_target"),   # 目标不存在
    ("a.md", "afile.md", "invalid_target"),   # 目标不是目录
    ("a.md", "dest", "name_conflict"),        # 目标已同名
    ("", "dest", "invalid_path"),             # 源为根
])
async def test_tree30_move_entry_reject_table(
    manager, store, broadcast, tmp_path, source, target_dir, expected_code
):
    await manager.create_kb(_cfg_dict("K"))
    docs = tmp_path / "K" / "documents"
    (docs / "a.md").write_text("x", encoding="utf-8")
    (docs / "afile.md").write_text("y", encoding="utf-8")
    (docs / "dest").mkdir()
    (docs / "dest" / "a.md").write_text("z", encoding="utf-8")
    store.calls.clear()
    broadcast.events.clear()

    with pytest.raises(KnowledgeBaseError) as ei:
        await manager.move_entry("K", source, target_dir)

    assert ei.value.code == expected_code
    assert (docs / "a.md").is_file()
    assert (docs / "dest" / "a.md").is_file()
    assert store.calls_of("update_status") == []
    assert _events(broadcast, EventType.KB_TREE_RESULT) == []


# ── 用例 31：move_entry 目标即源目录 → 仅重发树 ────────────────────────────


async def test_tree31_move_entry_target_equals_source(manager, store, broadcast, tmp_path, monkeypatch):
    await manager.create_kb(_cfg_dict("K", auto_rebuild=True))
    docs = tmp_path / "K" / "documents"
    (docs / "a.md").write_text("x", encoding="utf-8")
    built: list = []
    _stub_build_kb(monkeypatch, manager, built)
    store.calls.clear()
    broadcast.events.clear()

    await manager.move_entry("K", "a.md", "")          # 已在根 → 无变化

    assert (docs / "a.md").is_file()
    assert store.calls_of("update_status") == []
    assert built == []
    assert len(_events(broadcast, EventType.KB_TREE_RESULT)) == 1


# ── 用例 33：故障注入 io_error ─────────────────────────────────────────────


async def test_tree33a_mkdir_oserror_maps_io_error(manager, broadcast, tmp_path, monkeypatch):
    await manager.create_kb(_cfg_dict("K"))
    docs = tmp_path / "K" / "documents"

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "mkdir", boom)
    broadcast.events.clear()

    with pytest.raises(KnowledgeBaseError) as ei:
        await manager.create_folder("K", "", "x")

    assert ei.value.code == "io_error"
    assert "disk full" in ei.value.detail
    assert not (docs / "x").exists()
    assert _events(broadcast, EventType.KB_TREE_RESULT) == []


async def test_tree33b_shutil_move_oserror_maps_io_error(
    manager, store, broadcast, tmp_path, monkeypatch
):
    await manager.create_kb(_cfg_dict("K"))
    docs = tmp_path / "K" / "documents"
    (docs / "a.md").write_text("x", encoding="utf-8")
    (docs / "dest").mkdir()

    def boom(*_args, **_kwargs):
        raise OSError("target busy")

    monkeypatch.setattr("shutil.move", boom)
    store.calls.clear()
    broadcast.events.clear()

    with pytest.raises(KnowledgeBaseError) as ei:
        await manager.move_entry("K", "a.md", "dest")

    assert ei.value.code == "io_error"
    assert (docs / "a.md").is_file()
    assert not (docs / "dest" / "a.md").exists()
    assert store.calls_of("update_status") == []


# ── 用例 32：建库进行中 → 写操作全部 kb_busy 且无 FS 副作用 ────────────────


async def test_tree32_structural_ops_rejected_while_building(
    manager, store, broadcast, tmp_path, monkeypatch
):
    await manager.create_kb(_cfg_dict("K"))
    docs = tmp_path / "K" / "documents"
    (docs / "a.md").write_text("a", encoding="utf-8")
    (docs / "d").mkdir()
    (docs / "d" / "x.md").write_text("x", encoding="utf-8")

    # 阻塞式建库替身：建库任务停在 release.wait()，_build_tasks["K"] 处于未完成态。
    release = asyncio.Event()

    async def _blocked(cfg, rebuild):
        await release.wait()

    monkeypatch.setattr(manager, "_run_build_task", _blocked)
    await manager.build_kb("K")                                # 产生进行中任务
    assert not manager._build_tasks["K"].done()

    store.calls.clear()
    broadcast.events.clear()
    before = set(tmp_path.rglob("*"))

    write_ops = {
        "create_folder": lambda: manager.create_folder("K", "", "new"),
        "rename_folder": lambda: manager.rename_folder("K", "d", "d2"),
        "delete_folder": lambda: manager.delete_folder("K", "d"),
        "rename_document": lambda: manager.rename_document("K", "a.md", "b.md"),
        "move_entry": lambda: manager.move_entry("K", "a.md", "d"),
    }
    for op_name, op in write_ops.items():
        with pytest.raises(KnowledgeBaseError) as ei:
            await op()
        assert ei.value.code == "kb_busy", op_name

    # 只读的 emit_tree 不受闸门限制（不查 _assert_not_busy）→ 正常推 1 条树
    await manager.emit_tree("K")
    assert len(_events(broadcast, EventType.KB_TREE_RESULT)) == 1

    assert set(tmp_path.rglob("*")) == before                  # 无 FS 副作用
    assert store.calls_of("update_status") == []

    release.set()                                             # 收尾：放行任务
    await manager._build_tasks["K"]
