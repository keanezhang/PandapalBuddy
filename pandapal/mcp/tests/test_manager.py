"""pandapal/mcp/tests/test_manager.py — McpManager 编排不变量。

用三个**有真实语义的 Fake**（可控 connect / 记录注册调用 / 收集事件）验证编排逻辑：
  - Risk-1 失败隔离：一个 server connect 抛错不中断 start()、不影响其他 server
  - Risk-2 热重连：save_server 先 unregister 旧工具（按 full_name）再 register 新工具
  - Risk-3 断开完备：disconnect_server 注销该 server 全部工具 + status=disconnected
  - Risk-4 测试隔离：test_server 不注册工具、不落盘、不污染主 client_manager
  - Risk-5 未知 server / 非法配置：发全局 ERROR + report_degradation，绝不静默
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

import pandapal.mcp.manager as manager_module
from pandapal.events.normalized import EventType
from pandapal.mcp.config_store import McpConfigStore
from pandapal.mcp.manager import McpManager
from pandaren.mcp.config import McpServerConfig, McpServerStatus, McpTransport
from pandaren.tool.definition.tool import Tool
from pandaren.tool.definition.tool_policy import ToolPolicy
from pandaren.tool.types import SensitivityLevel


# ══════════════════════════════════════════════════════════════════════════
# 替身（有状态、可审计）
# ══════════════════════════════════════════════════════════════════════════


class FakeClientManager:
    """内存版 McpClientManager：预置连接结果 / 可控异常，记录调用。"""

    def __init__(self) -> None:
        self.connect_results: dict[str, list[Tool]] = {}
        self.connect_errors: dict[str, Exception] = {}
        self._tools: dict[str, list[Tool]] = {}
        self._status: dict[str, McpServerStatus] = {}
        self.connect_calls: list[str] = []
        self.disconnect_calls: list[str] = []

    def seed_connected(self, name: str, tools: list[Tool]) -> None:
        """预置「已连接」状态（模拟 save_server 前的旧会话）。"""
        self._tools[name] = list(tools)
        self._status[name] = McpServerStatus.CONNECTED

    async def connect(self, cfg: McpServerConfig) -> list[Tool]:
        self.connect_calls.append(cfg.name)
        err = self.connect_errors.get(cfg.name)
        if err is not None:
            self._status[cfg.name] = McpServerStatus.ERROR
            raise err
        tools = list(self.connect_results.get(cfg.name, []))
        self._tools[cfg.name] = tools
        self._status[cfg.name] = McpServerStatus.CONNECTED
        return tools

    async def disconnect(self, name: str) -> list[str]:
        removed = [t.full_name for t in self._tools.pop(name, [])]
        self.disconnect_calls.append(name)
        self._status[name] = McpServerStatus.DISCONNECTED
        return removed

    async def aclose_all(self) -> None:
        for name in list(self._tools):
            await self.disconnect(name)

    def status(self, name: str) -> McpServerStatus:
        return self._status.get(name, McpServerStatus.DISCONNECTED)

    def tools_for(self, name: str) -> list[Tool]:
        return list(self._tools.get(name, []))


class RecordingToolRegistry:
    """记录 register / unregister 调用**顺序与工具名**（热重连顺序断言用）。"""

    def __init__(self) -> None:
        self.ops: list[tuple[str, str]] = []
        self._tools: dict[str, Tool] = {}
        self._version = 0

    @property
    def version(self) -> int:
        return self._version

    def register_tool(self, tool: Tool, *, skip_if_exists: bool = False) -> None:
        if skip_if_exists and tool.full_name in self._tools:
            return
        self._tools[tool.full_name] = tool
        self._version += 1
        self.ops.append(("register", tool.full_name))

    def unregister_tool(self, tool_name: str) -> bool:
        self._tools.pop(tool_name, None)
        self._version += 1
        self.ops.append(("unregister", tool_name))
        return True


class FakeBroadcast:
    """收集出站事件（MessageBroadcast.send 的替身）。"""

    def __init__(self) -> None:
        self.events: list = []

    async def send(self, event, origin_channel_id=None) -> None:
        self.events.append(event)


class SpyConfigStore(McpConfigStore):
    """真实 McpConfigStore + 记录 save 调用（断言「未落盘」用）。"""

    def __init__(self, toml_path) -> None:
        super().__init__(toml_path)
        self.saved: list[McpServerConfig] = []

    def save(self, cfg: McpServerConfig) -> None:
        self.saved.append(cfg)
        super().save(cfg)


@dataclass
class Harness:
    manager: McpManager
    client: FakeClientManager
    registry: RecordingToolRegistry
    broadcast: FakeBroadcast
    store: SpyConfigStore


# ══════════════════════════════════════════════════════════════════════════
# Fixtures / helpers
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def harness(tmp_path) -> Harness:
    client = FakeClientManager()
    registry = RecordingToolRegistry()
    broadcast = FakeBroadcast()
    store = SpyConfigStore(tmp_path / "mcp" / "servers.toml")
    manager = McpManager(
        config_store=store,
        client_manager=client,
        tool_registry=registry,
        broadcast=broadcast,
    )
    return Harness(manager, client, registry, broadcast, store)


def _stdio(name: str = "fs", **kw) -> McpServerConfig:
    kw.setdefault("command", "npx")
    return McpServerConfig(name=name, transport=McpTransport.STDIO, **kw)


def _make_tool(name: str) -> Tool:
    return Tool(
        name=name,
        description=f"tool {name}",
        executor=lambda ctx, **kw: None,
        policy=ToolPolicy(sensitivity=SensitivityLevel.LOW),
        input_schema={"type": "object", "properties": {}},
        when_to_use=f"use {name}",
        namespace="mcp",
    )


def _events(broadcast: FakeBroadcast, event_type: EventType) -> list:
    return [e for e in broadcast.events if e.event_type == event_type]


# ══════════════════════════════════════════════════════════════════════════
# Risk-1 失败隔离
# ══════════════════════════════════════════════════════════════════════════


async def test_start_isolates_single_server_failure(harness):
    harness.store.save(_stdio("good"))
    harness.store.save(_stdio("bad"))
    harness.client.connect_errors["bad"] = RuntimeError("boom")
    harness.client.connect_results["good"] = [_make_tool("good__ping")]

    await harness.manager.start()  # 不抛

    assert harness.client.connect_calls == ["good", "bad"]
    # 失败者不注册工具、状态置 ERROR；成功者照常注册
    assert harness.registry.ops == [("register", "mcp_good__ping")]
    assert harness.client.status("bad") is McpServerStatus.ERROR
    assert harness.client.status("good") is McpServerStatus.CONNECTED
    # 启动期 emit=False：不产生状态推送
    assert harness.broadcast.events == []


# ══════════════════════════════════════════════════════════════════════════
# Risk-2 热重连
# ══════════════════════════════════════════════════════════════════════════


async def test_save_server_unregisters_old_then_registers_new(harness):
    old = _make_tool("fs__read")
    harness.client.seed_connected("fs", [old])
    harness.client.connect_results["fs"] = [_make_tool("fs__list")]

    cfg = _stdio("fs")
    await harness.manager.save_server(cfg)

    assert harness.registry.ops == [
        ("unregister", "mcp_fs__read"),
        ("register", "mcp_fs__list"),
    ]
    assert harness.store.saved == [cfg]
    saved = _events(harness.broadcast, EventType.MCP_SAVED)
    assert [e.payload["name"] for e in saved] == ["fs"]


# ══════════════════════════════════════════════════════════════════════════
# Risk-3 断开完备
# ══════════════════════════════════════════════════════════════════════════


async def test_disconnect_server_unregisters_all_tools(harness):
    harness.client.seed_connected("fs", [_make_tool("fs__a"), _make_tool("fs__b")])

    await harness.manager.disconnect_server("fs")

    assert harness.registry.ops == [
        ("unregister", "mcp_fs__a"),
        ("unregister", "mcp_fs__b"),
    ]
    assert harness.client.disconnect_calls == ["fs"]
    status = _events(harness.broadcast, EventType.MCP_STATUS_CHANGED)
    assert [e.payload["name"] for e in status] == ["fs"]
    assert [e.payload["status"] for e in status] == ["disconnected"]


# ══════════════════════════════════════════════════════════════════════════
# Risk-4 test_server 完全隔离
# ══════════════════════════════════════════════════════════════════════════


class _ProbeClient:
    """test_server 内部临时 McpClientManager 的替身。"""

    tools: list[Tool] = []
    instances: list["_ProbeClient"] = []

    def __init__(self) -> None:
        self.connect_calls: list[str] = []
        self.closed = False
        self.__class__.instances.append(self)

    async def connect(self, cfg: McpServerConfig) -> list[Tool]:
        self.connect_calls.append(cfg.name)
        return list(self.__class__.tools)

    async def aclose_all(self) -> None:
        self.closed = True


async def test_test_server_does_not_touch_registry_config_or_main_client(
    harness, monkeypatch
):
    monkeypatch.setattr(manager_module, "McpClientManager", _ProbeClient)
    _ProbeClient.tools = [_make_tool("fs__ping")]
    _ProbeClient.instances.clear()

    await harness.manager.test_server(_stdio("fs"))

    probe = _ProbeClient.instances[-1]
    assert probe.connect_calls == ["fs"]
    assert probe.closed is True  # 临时会话已清理
    # 不注册工具 / 不落盘 / 不污染主 client_manager
    assert harness.registry.ops == []
    assert harness.store.saved == []
    assert harness.client.connect_calls == []

    result = _events(harness.broadcast, EventType.MCP_TEST_RESULT)
    assert [e.payload["ok"] for e in result] == [True]
    assert result[0].payload["tools"] == [
        {
            "name": "mcp_fs__ping",
            "description": "tool fs__ping",
            "when_to_use": "use fs__ping",
            "sensitivity": "low",
        }
    ]


# ══════════════════════════════════════════════════════════════════════════
# Risk-5 未知 server / 非法配置 → 全局 ERROR，绝不静默
# ══════════════════════════════════════════════════════════════════════════


async def test_connect_server_unknown_emits_global_error(harness, monkeypatch):
    degraded: list[str] = []
    monkeypatch.setattr(
        manager_module,
        "report_degradation",
        lambda code, **kw: degraded.append(code),
    )

    await harness.manager.connect_server("ghost")

    assert harness.client.connect_calls == []
    errors = _events(harness.broadcast, EventType.ERROR)
    assert [e.payload["error_code"] for e in errors] == ["mcp_server_unknown"]
    assert degraded == ["mcp.server_unknown"]


async def test_save_server_invalid_config_emits_error_without_persist(harness):
    invalid = _stdio("fs")
    object.__setattr__(invalid, "command", None)  # 缺 command 的 stdio

    await harness.manager.save_server(invalid)

    errors = _events(harness.broadcast, EventType.ERROR)
    assert [e.payload["error_code"] for e in errors] == ["mcp_config_invalid"]
    assert harness.store.saved == []  # config_store.save 未被调用
    assert harness.client.connect_calls == []


# ══════════════════════════════════════════════════════════════════════════
# Risk-6 启用/禁用开关：保留配置，仅切换工具加载（绝不 via delete）
# ══════════════════════════════════════════════════════════════════════════


async def test_set_enabled_false_unregisters_tools_keeps_config(harness):
    """禁用：注销工具 + 断开，配置保留（save 而非 delete），不 connect。"""
    harness.store.save(_stdio("fs"))
    old = _make_tool("fs__read")
    harness.client.seed_connected("fs", [old])

    await harness.manager.set_enabled("fs", False)

    # 配置保留：最后一次落盘的是 enabled=False 的配置（绝非 delete）
    assert harness.store.saved[-1].enabled is False
    # 工具被注销
    assert ("unregister", "mcp_fs__read") in harness.registry.ops
    # 禁用后不重新连接
    assert harness.client.connect_calls == []
    # 状态推送 disconnected + 保存确认
    status = _events(harness.broadcast, EventType.MCP_STATUS_CHANGED)
    assert [e.payload["status"] for e in status] == ["disconnected"]
    assert len(_events(harness.broadcast, EventType.MCP_SAVED)) == 1


async def test_set_enabled_true_connects_and_registers_tools(harness):
    """启用：连接 + 注册工具。"""
    harness.store.save(_stdio("fs", enabled=False))
    harness.client.connect_results["fs"] = [_make_tool("fs__list")]

    await harness.manager.set_enabled("fs", True)

    assert harness.store.saved[-1].enabled is True
    assert harness.client.connect_calls == ["fs"]
    assert ("register", "mcp_fs__list") in harness.registry.ops
    status = _events(harness.broadcast, EventType.MCP_STATUS_CHANGED)
    assert [e.payload["status"] for e in status] == ["connected"]


async def test_set_enabled_unknown_server_emits_global_error(harness, monkeypatch):
    degraded: list[str] = []
    monkeypatch.setattr(
        manager_module,
        "report_degradation",
        lambda code, **kw: degraded.append(code),
    )

    await harness.manager.set_enabled("ghost", True)

    assert harness.client.connect_calls == []
    errors = _events(harness.broadcast, EventType.ERROR)
    assert [e.payload["error_code"] for e in errors] == ["mcp_server_unknown"]
    assert degraded == ["mcp.server_unknown"]
