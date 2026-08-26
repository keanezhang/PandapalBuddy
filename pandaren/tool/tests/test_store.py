"""pandaren/tool/tests/test_store.py — ToolStore CRUD + 索引一致性测试。

风险映射：
  Risk-1（P0）：非 ASCII 工具注册后，LLM-safe 名能反查到真实工具（_safe_name_index）
  Risk-2（P0）：name 含下划线时 safe_name 索引 key 与 schema 名一致（旧 rsplit bug 回归）
  Risk-3（P0）：unregister 的命名空间清理 bug 回归——按 tool.namespace 判定，
                full_name 含下划线时旧 .split(".") 逻辑恒不触发清理
  Risk-4（P1）：重复注册同名工具抛 ToolRegistrationError / skip_if_exists 跳过
  Risk-5（P1）：safe_name 与 full_name 相同的工具不产生冗余索引项
  Risk-6（P2）：__contains__ / __len__ / list_by_tier / items 基本行为
"""

from __future__ import annotations

import hashlib

import pytest

from pandaren.tool.exceptions import ToolRegistrationError
from pandaren.tool.registry.store import ToolStore
from pandaren.tool.types import ToolTier
from pandaren.tool.safe_name import to_safe_name_parts

from .conftest import make_tool


def _md5_prefix(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:8]


class TestRegister:
    def test_register_ascii_tool(self):
        store = ToolStore()
        tool = make_tool("calc", namespace="math")
        store.register(tool)
        assert len(store) == 1
        assert tool.full_name == "math_calc"
        assert store.get("math_calc") is tool

    def test_register_non_ascii_creates_safe_index(self):
        # Risk-1：非 ASCII 工具 → safe_name 索引可反查
        store = ToolStore()
        tool = make_tool("天气预报", namespace="skill")
        store.register(tool)
        safe = to_safe_name_parts("skill", "天气预报")
        assert safe != tool.full_name  # full_name="skill_天气预报" 非 ASCII
        assert store.get(tool.full_name) is tool   # 原始名命中
        assert store.get(safe) is tool             # LLM-safe 名命中
        assert safe in store                       # __contains__ 双向

    def test_register_name_with_underscore_index_matches_schema(self):
        # Risk-2：name 含下划线 → safe_name 必须 = ns + md5(name) 整体
        store = ToolStore()
        tool = make_tool("my_tool_天气", namespace="ns")
        store.register(tool)
        expected = f"ns_{_md5_prefix('my_tool_天气')}"
        assert store.get(expected) is tool
        # 旧 bug 行为对照：若按 full_name rsplit("_") 猜测会得到 "ns_my_tool_xxx" 之类的错误 key
        assert to_safe_name_parts("ns", "my_tool_天气") == expected

    def test_register_duplicate_raises(self):
        # Risk-4
        store = ToolStore()
        store.register(make_tool("calc"))
        with pytest.raises(ToolRegistrationError):
            store.register(make_tool("calc"))

    def test_register_duplicate_skip_if_exists(self):
        # Risk-4
        store = ToolStore()
        t1 = make_tool("calc")
        t2 = make_tool("calc", description="另一个")
        store.register(t1)
        store.register(t2, skip_if_exists=True)
        assert len(store) == 1
        assert store.get("calc") is t1  # 保留首个

    def test_same_name_different_namespace_ok(self):
        store = ToolStore()
        store.register(make_tool("calc", namespace="math"))
        store.register(make_tool("calc", namespace="physics"))
        assert len(store) == 2

    def test_ascii_name_no_redundant_index(self):
        # Risk-5：safe_name == full_name 时不产生冗余索引
        store = ToolStore()
        store.register(make_tool("calc", namespace="math"))
        assert store._safe_name_index == {}


class TestUnregister:
    def test_unregister_by_full_name(self):
        store = ToolStore()
        tool = make_tool("calc", namespace="math")
        store.register(tool)
        assert store.unregister("math_calc") is True
        assert len(store) == 0

    def test_unregister_by_safe_name(self):
        # Risk-1：注销同样支持 safe_name
        store = ToolStore()
        tool = make_tool("天气预报", namespace="skill")
        store.register(tool)
        safe = to_safe_name_parts("skill", "天气预报")
        assert store.unregister(safe) is True
        assert len(store) == 0
        assert store.unregister(safe) is False  # 幂等

    def test_unregister_unknown_returns_false(self):
        store = ToolStore()
        assert store.unregister("nope") is False

    def test_unregister_cleans_safe_index(self):
        store = ToolStore()
        tool = make_tool("天气预报", namespace="skill")
        store.register(tool)
        store.unregister("skill_天气预报")
        assert store.get("skill_天气预报") is None
        assert store.get(to_safe_name_parts("skill", "天气预报")) is None

    def test_namespace_kept_while_other_tools_remain(self):
        # Risk-3：同 ns 下还有工具 → ns 保留
        store = ToolStore()
        store.register(make_tool("a", namespace="skill"))
        store.register(make_tool("b", namespace="skill"))
        store.unregister("skill_a")
        assert "skill" in store._namespace_registry
        assert len(store) == 1

    def test_namespace_removed_when_last_tool_gone(self):
        # Risk-3：核心回归——ns 下无剩余工具时必须移除（旧代码从不触发清理）
        store = ToolStore()
        store.register(make_tool("a", namespace="skill"))
        store.unregister("skill_a")
        assert "skill" not in store._namespace_registry

    def test_namespace_removed_with_underscore_name(self):
        # Risk-3：name 含下划线时（full_name="ns_x_y"）命名空间清理必须仍生效——
        # 这是旧 .split(".") 拆 full_name 猜测 ns 的 bug 场景
        store = ToolStore()
        store.register(make_tool("x_y", namespace="ns"))
        store.unregister("ns_x_y")
        assert "ns" not in store._namespace_registry

    def test_unregister_no_namespace_tool(self):
        store = ToolStore()
        store.register(make_tool("plain"))
        assert store.unregister("plain") is True
        assert len(store) == 0


class TestQueryBasics:
    def test_contains_len(self):
        # Risk-6
        store = ToolStore()
        store.register(make_tool("a"))
        store.register(make_tool("b", namespace="ns"))
        assert len(store) == 2
        assert "a" in store
        assert "ns_b" in store
        assert "missing" not in store

    def test_list_by_tier(self):
        # Risk-6
        store = ToolStore()
        store.register(make_tool("always", tier=ToolTier.ALWAYS))
        store.register(make_tool("deferred"))
        always = store.list_by_tier(ToolTier.ALWAYS)
        deferred = store.list_by_tier(ToolTier.DEFERRED)
        assert [t.name for t in always] == ["always"]
        assert [t.name for t in deferred] == ["deferred"]

    def test_items_returns_pairs(self):
        store = ToolStore()
        store.register(make_tool("a", namespace="ns"))
        items = store.items()
        assert ("ns_a", store.get("ns_a")) in items

    def test_version_increments_on_register_unregister(self):
        store = ToolStore()
        v0 = store.version
        store.register(make_tool("a"))
        assert store.version == v0 + 1
        store.unregister("a")
        assert store.version == v0 + 2
