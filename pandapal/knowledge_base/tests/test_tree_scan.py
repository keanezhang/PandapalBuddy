"""pandapal/knowledge_base/tests/test_tree_scan.py — 文档树扫描（纯读）。

覆盖设计文档用例 1、4–11：
  - 用例 1：DocNode.to_dict 线上格式（wire format）
  - 用例 4：documents_dir 缺失 → 空树
  - 用例 5：嵌套 structure + POSIX 相对路径
  - 用例 6：节点元数据（size / suffix 小写 / mtime / is_dir / children）
  - 用例 7：目录优先 + 名称升序排序
  - 用例 8：扫描卫生（跳过点项 / 符号链接）
  - 用例 9：emit_tree 只发 1 个 KB_TREE_RESULT
  - 用例 10：[property] 随机 FS 全树与磁盘快照一致（inv-4）
  - 用例 11：[known-gap] 大小写并列 A.md/a.md 只做集合断言（NG-1）

仅用例 5/6 需真实文件系统，其余纯逻辑；全部走 tmp_path，无 mock。
"""

from __future__ import annotations

import os
import random
import shutil
from pathlib import Path

import pytest

from pandapal.events.normalized import EventType
from pandapal.knowledge_base.models import DocNode


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


def _tree_of(broadcast) -> list:
    trees = _events(broadcast, EventType.KB_TREE_RESULT)
    assert len(trees) == 1
    return trees[0].payload["tree"]


def _flatten(nodes: list, acc: list | None = None) -> list:
    acc = [] if acc is None else acc
    for n in nodes:
        acc.append(n)
        if n["children"]:
            _flatten(n["children"], acc)
    return acc


def _names(nodes: list) -> list:
    return [n["name"] for n in nodes]


def _fs_case_insensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.write_text("x", encoding="utf-8")
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.unlink()


# ── 用例 1：DocNode.to_dict 线上格式 ────────────────────────────────────────


def test_tree1_docnode_to_dict_wire_format():
    file_node = DocNode(
        path="d/a.pdf", name="a.pdf", is_dir=False,
        size=12, suffix=".PDF", mtime=1700000000.0, children=None,
    )
    dir_node = DocNode(
        path="d", name="d", is_dir=True,
        size=0, suffix="", mtime=1700000000.0, children=[file_node],
    )

    f = file_node.to_dict()
    assert set(f.keys()) == {"path", "name", "is_dir", "size", "suffix", "mtime", "children"}
    assert f["children"] is None
    assert f["path"] == "d/a.pdf"
    assert f["is_dir"] is False

    d = dir_node.to_dict()
    assert d["suffix"] == ""
    assert d["size"] == 0
    assert d["is_dir"] is True
    assert d["children"] == [f]  # 递归子节点


# ── 用例 4：documents_dir 缺失 → 空树 ──────────────────────────────────────


async def test_tree4_missing_documents_dir_returns_empty_tree(manager, broadcast, tmp_path):
    await manager.create_kb(_cfg_dict("K"))
    shutil.rmtree(tmp_path / "K" / "documents")
    broadcast.events.clear()

    await manager.emit_tree("K")

    assert _tree_of(broadcast) == []
    assert len(broadcast.events) == 1


# ── 用例 5：嵌套 structure + POSIX 相对路径 ────────────────────────────────


async def test_tree5_nested_structure_and_posix_paths(manager, broadcast, tmp_path):
    await manager.create_kb(_cfg_dict("K"))
    docs = tmp_path / "K" / "documents"
    (docs / "a.md").write_text("a", encoding="utf-8")
    (docs / "sub").mkdir()
    (docs / "sub" / "b.pdf").write_bytes(b"b")
    (docs / "sub" / "deep").mkdir()
    (docs / "sub" / "deep" / "c.txt").write_text("c", encoding="utf-8")
    broadcast.events.clear()

    await manager.emit_tree("K")
    tree = _tree_of(broadcast)

    assert _names(tree) == ["sub", "a.md"]           # 目录优先
    assert tree[0]["path"] == "sub"                  # POSIX 相对路径
    assert tree[1]["path"] == "a.md"
    assert _names(tree[0]["children"]) == ["deep", "b.pdf"]
    deep = tree[0]["children"][0]
    assert deep["path"] == "sub/deep"
    assert [c["path"] for c in deep["children"]] == ["sub/deep/c.txt"]


# ── 用例 6：节点元数据 ─────────────────────────────────────────────────────


async def test_tree6_node_metadata(manager, broadcast, tmp_path):
    await manager.create_kb(_cfg_dict("K"))
    docs = tmp_path / "K" / "documents"
    uploaded = docs / "A.PDF"
    uploaded.write_bytes(b"1234567")
    os.utime(uploaded, (1700000000, 1700000000))
    (docs / "Empty").mkdir()
    broadcast.events.clear()

    await manager.emit_tree("K")
    by_name = {n["name"]: n for n in _tree_of(broadcast)}

    assert by_name["A.PDF"]["size"] == 7
    assert by_name["A.PDF"]["suffix"] == ".pdf"      # 小写（含点）
    assert by_name["A.PDF"]["mtime"] == pytest.approx(1700000000.0)
    assert by_name["A.PDF"]["is_dir"] is False
    assert by_name["A.PDF"]["children"] is None

    assert by_name["Empty"]["size"] == 0
    assert by_name["Empty"]["suffix"] == ""
    assert by_name["Empty"]["is_dir"] is True


# ── 用例 7：目录优先 + 名称升序排序 ────────────────────────────────────────


async def test_tree7_sorting_dirs_first_then_name_ascending(manager, broadcast, tmp_path):
    await manager.create_kb(_cfg_dict("K"))
    docs = tmp_path / "K" / "documents"
    (docs / "dir1").mkdir()
    (docs / "dirA").mkdir()
    (docs / "a.md").write_text("a", encoding="utf-8")
    (docs / "B.md").write_text("b", encoding="utf-8")
    broadcast.events.clear()

    await manager.emit_tree("K")

    # dir1 < dirA（'1' < 'a'）；目录全部排在文件前
    assert _names(_tree_of(broadcast)) == ["dir1", "dirA", "a.md", "B.md"]


# ── 用例 8：扫描卫生（跳过点项 / 符号链接）────────────────────────────────


async def test_tree8a_scan_skips_dot_entries(manager, broadcast, tmp_path):
    await manager.create_kb(_cfg_dict("K"))
    docs = tmp_path / "K" / "documents"
    (docs / "visible.md").write_text("v", encoding="utf-8")
    (docs / ".DS_Store").write_text("junk", encoding="utf-8")
    (docs / ".git").mkdir()
    (docs / ".git" / "config").write_text("x", encoding="utf-8")
    broadcast.events.clear()

    await manager.emit_tree("K")
    nodes = _flatten(_tree_of(broadcast))

    assert _names(nodes) == ["visible.md"]
    assert all(".DS_Store" not in n["path"] and ".git" not in n["path"] for n in nodes)


async def test_tree8b_scan_skips_symlinks(manager, broadcast, tmp_path):
    await manager.create_kb(_cfg_dict("K"))
    docs = tmp_path / "K" / "documents"
    (docs / "visible.md").write_text("v", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("target", encoding="utf-8")
    try:
        os.symlink(outside, docs / "extern_link")
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建符号链接")
    broadcast.events.clear()

    await manager.emit_tree("K")

    assert _names(_flatten(_tree_of(broadcast))) == ["visible.md"]


# ── 用例 9：emit_tree 只发 1 个 KB_TREE_RESULT ─────────────────────────────


async def test_tree9_emit_tree_exactly_one_event(manager, broadcast, tmp_path):
    await manager.create_kb(_cfg_dict("K"))
    (tmp_path / "K" / "documents" / "x.md").write_text("x", encoding="utf-8")
    broadcast.events.clear()

    await manager.emit_tree("K")

    trees = _events(broadcast, EventType.KB_TREE_RESULT)
    assert len(trees) == 1
    assert trees[0].payload["name"] == "K"
    assert len(trees[0].payload["tree"]) == 1
    assert len(broadcast.events) == 1                        # 无其它事件类型
    assert trees[0].payload["tree"][0]["children"] is None   # 空目录 children == []


# ── 用例 10：[property] 随机 FS 全树与磁盘快照一致（inv-4）─────────────────


def _build_random_tree(rng: random.Random, directory: Path, depth: int) -> None:
    for i in range(rng.randint(0, 4)):
        if rng.random() < 0.5 and depth < 4:
            sub = directory / f"d_{depth}_{i}"
            sub.mkdir()
            _build_random_tree(rng, sub, depth + 1)
        else:
            (directory / f"f_{depth}_{i}.md").write_text("x", encoding="utf-8")
    if rng.random() < 0.3:  # 点项：扫描应跳过
        (directory / f".hidden_{depth}").write_text("x", encoding="utf-8")


def _clear_dir(directory: Path) -> None:
    for child in directory.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _assert_level_order(nodes: list) -> None:
    dirs = [n for n in nodes if n["is_dir"]]
    files = [n for n in nodes if not n["is_dir"]]
    assert nodes == dirs + files                             # 目录优先
    lows = [n["name"].lower() for n in nodes]
    assert lows == sorted(lows)                              # 名称升序（不区分大小写）
    for n in nodes:
        if n["children"]:
            _assert_level_order(n["children"])


async def test_tree10_tree_matches_fs_snapshot(manager, broadcast, tmp_path):
    await manager.create_kb(_cfg_dict("K"))
    docs = tmp_path / "K" / "documents"
    rng = random.Random(20240610)

    for _ in range(25):
        _clear_dir(docs)
        _build_random_tree(rng, docs, depth=1)
        broadcast.events.clear()

        await manager.emit_tree("K")
        nodes = _flatten(_tree_of(broadcast))

        actual = {n["path"] for n in nodes}
        expected = {
            p.relative_to(docs).as_posix()
            for p in docs.rglob("*")
            if not p.name.startswith(".") and not p.is_symlink()
        }
        assert actual == expected                            # inv-4：树与磁盘一一对应

        for n in nodes:
            assert not n["path"].startswith("/")
            assert ".." not in n["path"].split("/")
            assert "\\" not in n["path"]
            if n["is_dir"]:
                assert n["children"] is not None
            else:
                assert n["children"] is None
        _assert_level_order(_tree_of(broadcast))             # inv-5：排序不变式（按层递归）


# ── 用例 11：[known-gap] 大小写并列 A.md/a.md（NG-1）──────────────────────


async def test_tree11_case_parallel_files_asserted_as_set(manager, broadcast, tmp_path):
    await manager.create_kb(_cfg_dict("K"))
    docs = tmp_path / "K" / "documents"
    if _fs_case_insensitive(docs):
        pytest.skip("大小写不敏感文件系统无法构造 A.md/a.md 并列（NG-1）")

    (docs / "A.md").write_text("a", encoding="utf-8")
    (docs / "a.md").write_text("b", encoding="utf-8")
    broadcast.events.clear()

    await manager.emit_tree("K")
    names = _names(_tree_of(broadcast))

    # NG-1：当前按 name.lower() 排序，二者等价 → 只断集合，不硬断言不确定的相对顺序
    assert set(names) == {"A.md", "a.md"}
