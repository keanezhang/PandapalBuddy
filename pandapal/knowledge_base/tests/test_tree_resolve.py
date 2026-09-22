"""pandapal/knowledge_base/tests/test_tree_resolve.py — 路径安全解析（防穿越）。

覆盖设计文档用例 12–14：
  - 用例 12：合法相对路径等价类 → 归一化绝对路径（""/"." → 根；a\\b → a/b）
  - 用例 13：越界/绝对路径等价类 → KnowledgeBaseError('invalid_path')
  - 用例 14：[property] 随机路径串永远落在 documents_dir 之内（inv-4）

仅验证 ``_resolve_under_documents`` 解析逻辑，不触碰真实 I/O。
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from pandapal.knowledge_base.manager import KnowledgeBaseError
from pandapal.knowledge_base.models import KBConfig


def _cfg(tmp_path) -> KBConfig:
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    return KBConfig(name="K", documents_dir=str(docs))


# ── 用例 12：合法相对路径等价类 ────────────────────────────────────────────


@pytest.mark.parametrize("rel, expected_rel", [
    ("", ""),            # 根
    (".", ""),           # 根（显式 .）
    ("a", "a"),          # 单段
    ("a/b", "a/b"),      # 多段
    ("./a/b", "a/b"),    # 带前导 ./
    ("a\\b", "a/b"),     # 反斜杠归一化
    ("a/../b", "b"),     # 中间 .. 折回
])
def test_tree12_valid_rel_resolves_within_root(manager, tmp_path, rel, expected_rel):
    cfg = _cfg(tmp_path)
    root = Path(cfg.documents_dir).resolve()
    expected = root if expected_rel == "" else (root / expected_rel).resolve()

    assert manager._resolve_under_documents(cfg, rel) == expected


# ── 用例 13：越界/绝对路径 → invalid_path ─────────────────────────────────


@pytest.mark.parametrize("rel", [
    "/etc/passwd",        # 绝对路径
    "C:\\Windows",        # 盘符绝对路径
    "..",                 # 单 .. 逃逸
    "../x",
    "a/../../b",
    "..\\x",              # 反斜杠形式逃逸
    "a/../../../root",
])
def test_tree13_out_of_range_rel_rejected(manager, tmp_path, rel):
    cfg = _cfg(tmp_path)

    with pytest.raises(KnowledgeBaseError) as ei:
        manager._resolve_under_documents(cfg, rel)

    assert ei.value.code == "invalid_path"


# ── 用例 14：[property] 随机路径串永远落在 documents_dir 之内（inv-4）─────


def test_tree14_random_rel_never_escapes_root(manager, tmp_path):
    cfg = _cfg(tmp_path)
    root = Path(cfg.documents_dir).resolve()
    rng = random.Random(20240610)
    tokens = ["a", "b", "..", ".", "/", "\\", "C:", "sub"]

    checked = 0
    for _ in range(300):
        rel = "".join(rng.choice(tokens) for _ in range(rng.randint(1, 5)))
        try:
            resolved = manager._resolve_under_documents(cfg, rel)
        except KnowledgeBaseError as exc:
            assert exc.code == "invalid_path", (rel, exc.code)
            continue
        assert resolved == root or resolved.is_relative_to(root), (rel, resolved)
        checked += 1

    assert checked > 0  # 样本中确有被接受者，属性断言非空跑
