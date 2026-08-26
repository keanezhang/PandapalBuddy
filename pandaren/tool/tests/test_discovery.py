"""pandaren/tool/tests/test_discovery.py — DiscoveryManager 测试。

风险映射：
  Risk-1（P0）：discover() 是唯一写入点；发现后 is_discovered/get_step 可查
  Risk-2（P0）：超限后 LRU 淘汰按 step_n 最小者逐出
  Risk-3（P1）：update_step() 刷新已发现工具 step_n（长循环中反复使用的工具不被提前逐出）
  Risk-4（P1）：snapshot/restore 显式序列化；restore 超限时同样触发 LRU
  Risk-5（P2）：undiscover / clear 基本行为
"""

from __future__ import annotations

from pandaren.tool.registry.discovery import DiscoveryManager


class TestDiscover:
    def test_discover_then_query(self):
        # Risk-1
        dm = DiscoveryManager(max_discovered=5)
        dm.discover("tool_a", step_n=3)
        assert dm.is_discovered("tool_a")
        assert dm.get_step("tool_a") == 3

    def test_discover_overwrites_step(self):
        # Risk-1：重复 discover 刷新 step
        dm = DiscoveryManager(max_discovered=5)
        dm.discover("tool_a", step_n=1)
        dm.discover("tool_a", step_n=7)
        assert dm.get_step("tool_a") == 7
        assert len(dm) == 1

    def test_unknown_tool_not_discovered(self):
        dm = DiscoveryManager(max_discovered=5)
        assert not dm.is_discovered("nope")
        assert dm.get_step("nope") is None

    def test_evict_lru_when_over_limit(self):
        # Risk-2：超限 → step_n 最小的先逐出
        dm = DiscoveryManager(max_discovered=3)
        dm.discover("a", step_n=1)
        dm.discover("b", step_n=2)
        dm.discover("c", step_n=3)
        assert len(dm) == 3
        dm.discover("d", step_n=4)  # 超限 1 个
        assert len(dm) == 3
        assert not dm.is_discovered("a")  # step_n 最小 → 逐出
        assert dm.is_discovered("b")
        assert dm.is_discovered("c")
        assert dm.is_discovered("d")

    def test_evict_keeps_most_recently_used(self):
        # Risk-3：update_step 刷新后，旧工具不被逐出
        dm = DiscoveryManager(max_discovered=2)
        dm.discover("old", step_n=1)
        dm.discover("new", step_n=2)
        dm.update_step("old", step_n=100)  # old 最近被使用
        dm.discover("extra", step_n=3)     # 超限
        assert dm.is_discovered("old")     # step 大 → 保留
        assert not dm.is_discovered("new")  # step_n=2 最小 → 逐出


class TestUpdateStep:
    def test_update_step_existing_tool(self):
        dm = DiscoveryManager()
        dm.discover("a", step_n=1)
        dm.update_step("a", step_n=50)
        assert dm.get_step("a") == 50

    def test_update_step_unknown_is_noop(self):
        # Risk-3：未发现工具 update_step 不产生副作用（不得隐式 discover）
        dm = DiscoveryManager()
        dm.update_step("ghost", step_n=5)
        assert not dm.is_discovered("ghost")
        assert len(dm) == 0


class TestSnapshotRestore:
    def test_snapshot_restore_roundtrip(self):
        # Risk-4
        dm = DiscoveryManager(max_discovered=10)
        dm.discover("a", step_n=1)
        dm.discover("b", step_n=2)
        snap = dm.snapshot()
        assert snap == {"a": 1, "b": 2}

        dm2 = DiscoveryManager(max_discovered=10)
        dm2.restore(snap)
        assert dm2.is_discovered("a")
        assert dm2.get_step("b") == 2
        assert len(dm2) == 2

    def test_restore_evicts_when_over_limit(self):
        # Risk-4：restore 超限同样走 LRU
        dm = DiscoveryManager(max_discovered=2)
        dm.discover("a", step_n=1)
        dm.discover("b", step_n=2)
        dm.discover("c", step_n=3)
        assert len(dm) == 2  # restore 内已逐出 a


class TestUndiscover:
    def test_undiscover_existing(self):
        # Risk-5
        dm = DiscoveryManager()
        dm.discover("a", step_n=1)
        assert dm.undiscover("a") is True
        assert not dm.is_discovered("a")
        assert len(dm) == 0

    def test_undiscover_unknown(self):
        # Risk-5
        dm = DiscoveryManager()
        assert dm.undiscover("ghost") is False

    def test_clear(self):
        # Risk-5
        dm = DiscoveryManager()
        dm.discover("a", step_n=1)
        dm.discover("b", step_n=2)
        dm.clear()
        assert len(dm) == 0
        assert not dm.is_discovered("a")
