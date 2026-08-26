"""pandaren/tool/tests/test_safe_name.py — to_safe_name_parts 单元测试。

风险映射：
  Risk-1（P0）：name 含非 ASCII → 确定性 md5 后缀，且 namespace 前缀保留
  Risk-2（P0）：输出恒为 ASCII（OpenAI 兼容 API 400 根因防护）
  Risk-3（P1）：name 本身含下划线时不得误拆（旧 rsplit 猜测 bug 的回归护栏）
  Risk-4（P1）：namespace 含非 ASCII → 同样 hash 化
  Risk-5（P2）：None namespace 行为（无前缀）
"""

from __future__ import annotations

import hashlib

from pandaren.tool.safe_name import to_safe_name_parts


def _md5_prefix(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:8]


class TestAsciiName:
    def test_ascii_name_no_namespace(self):
        # Risk-1/5：纯 ASCII + 无 namespace → 原样返回
        assert to_safe_name_parts(None, "search_tools") == "search_tools"

    def test_ascii_name_with_namespace(self):
        assert to_safe_name_parts("skill", "list_tools") == "skill_list_tools"

    def test_ascii_name_with_underscore(self):
        # Risk-3：name 含下划线但纯 ASCII → 原样拼接，不得拆解
        assert to_safe_name_parts("ns", "my_tool_v2") == "ns_my_tool_v2"


class TestNonAsciiName:
    def test_non_ascii_name_suffix_is_deterministic(self):
        # Risk-1/2：同一输入两次调用结果一致（确定性），且输出 ASCII
        a = to_safe_name_parts("skill", "天气预报")
        b = to_safe_name_parts("skill", "天气预报")
        assert a == b == f"skill_{_md5_prefix('天气预报')}"
        assert a.isascii()

    def test_no_namespace_non_ascii(self):
        assert to_safe_name_parts(None, "天气预报") == _md5_prefix("天气预报")

    def test_non_ascii_name_with_underscore_not_split(self):
        # Risk-3：name 含下划线 + 非 ASCII → 整体 hash，不能把 "my_tool" 当 namespace
        result = to_safe_name_parts("ns", "my_tool_天气")
        assert result == f"ns_{_md5_prefix('my_tool_天气')}"
        assert result.isascii()

    def test_namespace_non_ascii_is_hashed(self):
        # Risk-4：namespace 非 ASCII → ns 也 hash 化，输出仍恒 ASCII
        result = to_safe_name_parts("技能", "search")
        assert result == f"{_md5_prefix('技能')}_search"
        assert result.isascii()

    def test_both_non_ascii(self):
        result = to_safe_name_parts("技能", "天气预报")
        assert result == f"{_md5_prefix('技能')}_{_md5_prefix('天气预报')}"
        assert result.isascii()

    def test_different_names_same_hash_length(self):
        # Risk-2：不同中文名产出不同后缀（长度一致 8 位）
        a = to_safe_name_parts(None, "天气预报")
        b = to_safe_name_parts(None, "股市行情")
        assert a != b
        assert len(a) == len(b) == 8
