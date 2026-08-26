"""pandaren/tool/tests/test_schema_builder.py — SchemaBuilder 三段式暴露测试。

风险映射：
  Risk-1（P0）：非 ASCII 工具的 ToolSchema.name == store 索引 key
                （LLM 回传 schema 名必须能经 store.get() 反查到真实工具）
  Risk-2（P0）：三段式分类——ALWAYS 进 schemas / DEFERRED 已发现进 schemas +
                摘要 / DEFERRED 未发现仅进摘要 + search_enum
  Risk-3（P1）：search_tools 带动态 enum，只含「延迟未发现」工具
  Risk-4（P1）：门链过滤生效（filtered_count / 不进入任何输出）
  Risk-5（P2）：GateChain.default() 为 4 道门
"""

from __future__ import annotations

from pandaren.tool.exposure.budget import ToolBudget
from pandaren.tool.exposure.gate_chain import ExposureContext, GateChain
from pandaren.tool.exposure.schema_builder import SchemaBuilder
from pandaren.tool.registry.discovery import DiscoveryManager
from pandaren.tool.registry.store import ToolStore
from pandaren.tool.types import ToolTier

from .conftest import make_tool


def _make_builder(store: ToolStore, discovery: DiscoveryManager) -> SchemaBuilder:
    return SchemaBuilder(
        store=store,
        discovery=discovery,
        gate_chain=GateChain.default(),
        budget=ToolBudget(),
    )


class TestThreeSegment:
    def test_always_tool_in_schemas(self):
        # Risk-2
        store = ToolStore()
        store.register(make_tool("always_a", tier=ToolTier.ALWAYS))
        builder = _make_builder(store, DiscoveryManager())
        result = builder.build(ExposureContext())
        names = [s.name for s in result.schemas]
        assert "always_a" in names
        assert result.stats.always_count == 1
        assert result.stats.deferred_unfound_count == 0

    def test_deferred_found_full_schema(self):
        # Risk-2：先 promote 再 build
        store = ToolStore()
        tool = make_tool("calc")
        store.register(tool)
        discovery = DiscoveryManager()
        discovery.discover(tool.full_name, step_n=5)
        builder = _make_builder(store, discovery)
        result = builder.build(ExposureContext())
        names = [s.name for s in result.schemas]
        assert "calc" in names
        assert result.stats.deferred_found_count == 1
        # 已发现也要保留摘要（缓存命中不破坏）
        catalog_names = [d["name"] for d in result.deferred_catalog]
        assert "calc" in catalog_names

    def test_deferred_unfound_summary_only(self):
        # Risk-2
        store = ToolStore()
        store.register(make_tool("heavy"))
        builder = _make_builder(store, DiscoveryManager())
        result = builder.build(ExposureContext())
        assert [s.name for s in result.schemas] == []  # 不进 schemas
        assert [d["name"] for d in result.deferred_catalog] == ["heavy"]
        assert result.search_enum == ["heavy"]
        assert result.stats.deferred_unfound_count == 1

    def test_mixed_three_segments(self):
        # Risk-2：三段并存
        store = ToolStore()
        store.register(make_tool("always1", tier=ToolTier.ALWAYS))
        found = make_tool("found1")
        store.register(found)
        store.register(make_tool("unfound1"))
        discovery = DiscoveryManager()
        discovery.discover(found.full_name, step_n=1)
        builder = _make_builder(store, discovery)
        result = builder.build(ExposureContext())
        names = [s.name for s in result.schemas]
        assert "always1" in names
        assert "found1" in names
        assert "unfound1" not in names
        assert "unfound1" in result.search_enum
        assert result.stats.always_count == 1
        assert result.stats.deferred_found_count == 1
        assert result.stats.deferred_unfound_count == 1


class TestSearchToolsEnum:
    def test_search_enum_only_unfound_deferred(self):
        # Risk-3：enum 只含延迟未发现工具
        store = ToolStore()
        store.register(make_tool("always1", tier=ToolTier.ALWAYS))
        found = make_tool("found1")
        store.register(found)
        store.register(make_tool("unfound1"))
        discovery = DiscoveryManager()
        discovery.discover(found.full_name, step_n=1)
        builder = _make_builder(store, discovery)
        result = builder.build(ExposureContext())
        assert result.search_enum == ["unfound1"]  # 无 always、无已发现

    def test_search_tools_schema_has_enum(self):
        # Risk-3：search_tools 的 tool_name 属性带 enum
        store = ToolStore()
        store.register(make_tool("search_tools", tier=ToolTier.ALWAYS))
        store.register(make_tool("unfound1"))
        builder = _make_builder(store, DiscoveryManager())
        result = builder.build(ExposureContext())
        search_schema = next(s for s in result.schemas if s.name == "search_tools")
        enum = search_schema.parameters["properties"]["tool_name"].get("enum")
        assert enum == ["unfound1"]

    def test_search_tools_absent_when_no_tool(self):
        # search_tools 未注册 → 不生成
        store = ToolStore()
        store.register(make_tool("plain"))
        builder = _make_builder(store, DiscoveryManager())
        result = builder.build(ExposureContext())
        assert all(s.name != "search_tools" for s in result.schemas)


class TestNameConsistency:
    def test_non_ascii_schema_name_matches_store_index(self):
        # Risk-1：核心契约——schema 名必须能反查回工具
        store = ToolStore()
        store.register(make_tool("天气预报", namespace="skill"))
        builder = _make_builder(store, DiscoveryManager())
        result = builder.build(ExposureContext())
        assert result.stats.deferred_unfound_count == 1
        catalog_name = result.deferred_catalog[0]["name"]
        # schema 名（未发现时在 catalog）→ store.get 必须命中
        assert store.get(catalog_name) is not None
        assert catalog_name.isascii()

    def test_deferred_found_schema_name_reverse_lookup(self):
        # Risk-1：已发现工具，schema.name 反查 store 成功
        store = ToolStore()
        tool = make_tool("天气预报", namespace="skill")
        store.register(tool)
        discovery = DiscoveryManager()
        discovery.discover(tool.full_name, step_n=1)
        builder = _make_builder(store, discovery)
        result = builder.build(ExposureContext())
        schema = result.schemas[0]
        assert schema.name.isascii()
        assert store.get(schema.name) is tool

    def test_underscore_name_reverse_lookup(self):
        # Risk-1：name 含下划线（"ns_my_tool_天气"）时索引一致
        store = ToolStore()
        tool = make_tool("my_tool_天气", namespace="ns")
        store.register(tool)
        discovery = DiscoveryManager()
        discovery.discover(tool.full_name, step_n=1)
        builder = _make_builder(store, discovery)
        result = builder.build(ExposureContext())
        assert result.schemas[0].name.isascii()
        assert store.get(result.schemas[0].name) is tool


class TestGateChainDefault:
    def test_default_has_four_gates(self):
        # Risk-5
        chain = GateChain.default()
        names = [g.name for g in chain._gates]
        assert names == ["allow_list", "enabled", "agent_whitelist", "skill_whitelist"]

    def test_allow_list_filters(self):
        # Risk-4：agent_allowed_tools 白名单过滤
        store = ToolStore()
        store.register(make_tool("a"))
        store.register(make_tool("b"))
        builder = _make_builder(store, DiscoveryManager())
        result = builder.build(ExposureContext(agent_allowed_tools={"a"}))
        assert result.stats.filtered_count == 1
        assert result.search_enum == ["a"]

    def test_enabled_cache_filters(self):
        # Risk-4：enabled_cache 动态禁用
        store = ToolStore()
        store.register(make_tool("a"))
        store.register(make_tool("b"))
        builder = _make_builder(store, DiscoveryManager())
        result = builder.build(ExposureContext(enabled_cache={"b": False}))
        assert result.search_enum == ["a"]
        assert result.stats.filtered_count == 1
