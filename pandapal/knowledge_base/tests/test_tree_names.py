"""pandapal/knowledge_base/tests/test_tree_names.py — 用户命名末段校验。

覆盖设计文档用例 15–16：
  - 用例 15：非法名等价类 → KnowledgeBaseError('invalid_name')
    空 / 空白 / . / .. / 分隔符 / Windows 保留符号 / 控制字符
  - 用例 16：合法名等价类 → 原样去除首尾空白后返回（不改变大小写 / 扩展名）

纯函数校验，零 I/O、零替身。
"""

from __future__ import annotations

import pytest

from pandapal.knowledge_base.manager import KnowledgeBaseError, KnowledgeBaseManager


# ── 用例 15：非法名 → invalid_name ─────────────────────────────────────────


@pytest.mark.parametrize("raw", [
    "",          # 空串
    "   ",       # 纯空白
    ".",         # 当前目录
    "..",        # 上级目录
    "a/b",       # 正斜杠
    "a\\b",      # 反斜杠
    "a:b",       # 冒号
    "a*b",       # 星号
    'a"b',       # 双引号
    "a<b",       # 小于号
    "a>b",       # 大于号
    "a|b",       # 竖线
    "a?b",       # 问号
    "a\x00b",    # 空字节
    "a\nb",      # 换行（控制字符）
])
def test_tree15_invalid_entry_name_rejected(raw):
    with pytest.raises(KnowledgeBaseError) as ei:
        KnowledgeBaseManager._validate_entry_name(raw)

    assert ei.value.code == "invalid_name"


# ── 用例 16：合法名 → 去首尾空白后原样返回 ────────────────────────────────


@pytest.mark.parametrize("raw, expected", [
    ("文档", "文档"),          # 中文
    ("a.md", "a.md"),          # 扩展名保留
    ("a b", "a b"),            # 内部空格保留
    ("a_b-c", "a_b-c"),        # 下划线 / 连字符
    ("  a  ", "a"),            # 首尾空白剔除
    ("a.b.c", "a.b.c"),        # 多段点保留
])
def test_tree16_valid_entry_name_passthrough(raw, expected):
    assert KnowledgeBaseManager._validate_entry_name(raw) == expected


def test_tree16_hidden_name_passes_validation():
    # NG-2：合法命名允许点开头（如 .hidden），但扫描会跳过 —— 已知限制，此处只断言校验层放行
    assert KnowledgeBaseManager._validate_entry_name(".hidden") == ".hidden"
