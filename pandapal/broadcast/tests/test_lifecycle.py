"""★ 根本解回归测试：transport 生命周期驱动（2026-06-10）。

背景：
  之前 PandaPalApp.start() 只注册 transport 不调 start()，
  导致 WSSGateway 永远处于 DISCONNECTED 状态。
  根本解：把 transport 生命周期下沉到 MessageBroadcast.start()/stop()。

本文件验证：
  1. Transport Protocol 强制要求 is_started
  2. MessageBroadcast.start() 驱动所有已注册 transport
  3. MessageBroadcast.stop() 对称关闭
  4. 幂等：重复 start/stop 不报错
  5. 失败隔离：单个 transport.start() 失败不影响其他
  6. 启动自检：get_lifecycle_snapshot() 报告真实状态
  7. ★ 核心回归：PandaPalApp.start() 流程包含 broadcast.start() 的调用
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from pandapal.broadcast.broadcaster import MessageBroadcast
from pandapal.broadcast.channel_registry import (
    ChannelCapability,
    ChannelInfo,
    ChannelRegistry,
    ChannelType,
)
from pandapal.broadcast.transport import Transport
from pandapal.events.normalized import EventType, NormalizedEvent


# ══════════════════════════════════════════════════════════════════════════════
# Mock Transport：完整实现 Transport 协议
# ══════════════════════════════════════════════════════════════════════════════


class MockTransport:
    """可被 broadcast 驱动的 mock transport。"""

    def __init__(self, fail_on_start: bool = False) -> None:
        self._started = False
        self._fail_on_start = fail_on_start
        self.start_called = 0
        self.stop_called = 0
        self.sent_events: list[NormalizedEvent] = []

    @property
    def is_started(self) -> bool:
        return self._started

    async def start(self) -> None:
        self.start_called += 1
        if self._fail_on_start:
            raise RuntimeError("simulated start failure")
        self._started = True

    async def stop(self) -> None:
        self.stop_called += 1
        self._started = False

    async def send(self, event: NormalizedEvent) -> None:
        self.sent_events.append(event)


# ══════════════════════════════════════════════════════════════════════════════
# Transport Protocol 契约测试
# ══════════════════════════════════════════════════════════════════════════════


def test_transport_protocol_requires_is_started():
    """Transport Protocol 必须声明 is_started（★ 根本解契约）。"""
    # 通过 inspect 检查 Protocol 是否有 is_started 成员
    assert hasattr(Transport, "is_started"), (
        "Transport Protocol missing is_started — "
        "根本解要求生命周期状态可查询"
    )


def test_mock_transport_satisfies_transport_protocol():
    """MockTransport 是 Transport Protocol 的合法实现（runtime_checkable）。"""
    assert isinstance(MockTransport(), Transport)


# ══════════════════════════════════════════════════════════════════════════════
# MessageBroadcast 生命周期驱动测试
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_broadcast_start_drives_all_transports():
    """★ 核心回归：broadcast.start() 必须驱动所有已注册 transport。"""
    registry = ChannelRegistry()
    t1, t2 = MockTransport(), MockTransport()
    registry.register(ChannelInfo(
        id="local_1", type=ChannelType.LOCAL,
        capabilities=frozenset({ChannelCapability.TEXT}),
        transport=t1,
    ))
    registry.register(ChannelInfo(
        id="remote_1", type=ChannelType.REMOTE,
        capabilities=frozenset({ChannelCapability.TEXT}),
        transport=t2,
    ))

    broadcast = MessageBroadcast(registry=registry)
    await broadcast.start()

    # 两个 transport 都被驱动
    assert t1.is_started is True, "transport 1 未被 broadcast.start() 启动"
    assert t2.is_started is True, "transport 2 未被 broadcast.start() 启动"
    assert t1.start_called == 1
    assert t2.start_called == 1


@pytest.mark.asyncio
async def test_broadcast_start_is_idempotent():
    """broadcast.start() 重复调用不会重复启动 transport。"""
    registry = ChannelRegistry()
    t = MockTransport()
    registry.register(ChannelInfo(
        id="c1", type=ChannelType.LOCAL,
        capabilities=frozenset({ChannelCapability.TEXT}),
        transport=t,
    ))

    broadcast = MessageBroadcast(registry=registry)
    await broadcast.start()
    await broadcast.start()  # 第二次
    await broadcast.start()  # 第三次

    assert t.start_called == 1, "重复 start() 不应重复调用 transport.start()"


@pytest.mark.asyncio
async def test_broadcast_stop_is_idempotent_and_symmetric():
    """broadcast.stop() 关闭已启动 transport；幂等；与 start 对称。"""
    registry = ChannelRegistry()
    t = MockTransport()
    registry.register(ChannelInfo(
        id="c1", type=ChannelType.LOCAL,
        capabilities=frozenset({ChannelCapability.TEXT}),
        transport=t,
    ))

    broadcast = MessageBroadcast(registry=registry)
    await broadcast.start()
    assert t.is_started is True

    await broadcast.stop()
    assert t.is_started is False
    assert t.stop_called == 1

    # 幂等：再次 stop 不应报错，也不应再调 transport.stop()
    await broadcast.stop()
    await broadcast.stop()
    assert t.stop_called == 1


@pytest.mark.asyncio
async def test_broadcast_start_isolates_failure():
    """单个 transport.start() 失败不影响其他 transport（HC3 Fail-Safe）。"""
    registry = ChannelRegistry()
    good = MockTransport()
    bad = MockTransport(fail_on_start=True)
    registry.register(ChannelInfo(
        id="good", type=ChannelType.LOCAL,
        capabilities=frozenset({ChannelCapability.TEXT}),
        transport=good,
    ))
    registry.register(ChannelInfo(
        id="bad", type=ChannelType.LOCAL,
        capabilities=frozenset({ChannelCapability.TEXT}),
        transport=bad,
    ))

    broadcast = MessageBroadcast(registry=registry)
    # 不应抛异常——单个 transport 失败被内部消化
    await broadcast.start()

    # 好的那个被启动
    assert good.is_started is True
    # 坏的那个 is_started 仍是 False（其 start() 抛异常，未设置 _started）
    assert bad.is_started is False


@pytest.mark.asyncio
async def test_broadcast_start_skips_channels_without_transport():
    """内部 channel（transport=None）应被 start() 跳过。"""
    registry = ChannelRegistry()
    registry.register(ChannelInfo(
        id="__hitl_bridge__", type=ChannelType.LOCAL,
        capabilities=frozenset(), transport=None,
    ))

    broadcast = MessageBroadcast(registry=registry)
    # 不应抛异常
    await broadcast.start()


# ══════════════════════════════════════════════════════════════════════════════
# 启动自检测试
# ══════════════════════════════════════════════════════════════════════════════


def test_lifecycle_snapshot_reports_real_state():
    """get_lifecycle_snapshot() 报告每个 transport 的真实状态（不是"构造完成"）。"""
    registry = ChannelRegistry()
    t = MockTransport()  # 还没 start
    registry.register(ChannelInfo(
        id="c1", type=ChannelType.LOCAL,
        capabilities=frozenset({ChannelCapability.TEXT}),
        transport=t,
    ))

    broadcast = MessageBroadcast(registry=registry)
    snapshot = broadcast.get_lifecycle_snapshot()

    assert len(snapshot) == 1
    assert snapshot[0] == {
        "channel_id": "c1",
        "transport": "MockTransport",
        "is_started": False,  # 关键：构造完成 ≠ 启动
    }


@pytest.mark.asyncio
async def test_lifecycle_snapshot_reflects_started_state():
    """start() 之后 snapshot 显示 is_started=True。"""
    registry = ChannelRegistry()
    t = MockTransport()
    registry.register(ChannelInfo(
        id="c1", type=ChannelType.LOCAL,
        capabilities=frozenset({ChannelCapability.TEXT}),
        transport=t,
    ))

    broadcast = MessageBroadcast(registry=registry)
    await broadcast.start()

    snapshot = broadcast.get_lifecycle_snapshot()
    assert snapshot[0]["is_started"] is True


# ══════════════════════════════════════════════════════════════════════════════
# ChannelRegistry 访问器测试
# ══════════════════════════════════════════════════════════════════════════════


def test_channel_registry_all_transports():
    """ChannelRegistry.all_transports() 返回所有 transport（含 None）。"""
    registry = ChannelRegistry()
    t1, t2 = MockTransport(), MockTransport()
    registry.register(ChannelInfo(
        id="c1", type=ChannelType.LOCAL,
        capabilities=frozenset({ChannelCapability.TEXT}),
        transport=t1,
    ))
    registry.register(ChannelInfo(
        id="c2", type=ChannelType.LOCAL,
        capabilities=frozenset(),
        transport=None,
    ))
    registry.register(ChannelInfo(
        id="c3", type=ChannelType.REMOTE,
        capabilities=frozenset({ChannelCapability.TEXT}),
        transport=t2,
    ))

    transports = registry.all_transports()
    assert len(transports) == 3
    assert transports[0] is t1
    assert transports[1] is None
    assert transports[2] is t2


# ══════════════════════════════════════════════════════════════════════════════
# ★ 核心回归测试：PandaPalApp.start() 必须委托给 broadcast.start()
# ══════════════════════════════════════════════════════════════════════════════


_PANDAPAL_APP_PATH = (
    Path(__file__).resolve().parent.parent.parent / "app.py"
)


def _read_pandapal_app_source() -> str:
    """读取 PandaPalApp 源文件（不导入，避免 aiosqlite 等重依赖）。"""
    return _PANDAPAL_APP_PATH.read_text(encoding="utf-8")


def _get_pandapal_app_class_source(method_name: str) -> str:
    """用 AST 从源文件提取 PandaPalApp.<method_name>() 源码（不导入模块）。

    ★ 注意：async def 在 AST 里是 AsyncFunctionDef，不是 FunctionDef。
      必须同时检查两种类型，否则会漏掉异步方法。
    """
    source = _read_pandapal_app_source()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "PandaPalApp":
            for item in node.body:
                # ★ 同步 + 异步都检查
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and item.name == method_name:
                    return ast.get_source_segment(source, item) or ""
    return ""


def test_pandapal_app_start_delegates_to_container_start_all():
    """★ 层次 3 回归：PandaPalApp.start() 必须委托给 SubsystemContainer.start_all()。

    之前（层次 2）：手写 `await self._broadcast.start()`。
    之后（层次 3）：`await self._container.start_all()`（所有子系统通过容器启动）。
    这条测试防止以后有人退回到"ad-hoc 调每个子系统 start()"模式。
    """
    src = _get_pandapal_app_class_source("start")
    assert src, "无法在 pandapal/app.py 中找到 PandaPalApp.start()"
    assert "container.start_all" in src, (
        "PandaPalApp.start() 未委托给 container.start_all() — "
        "层次 3 要求所有子系统通过 SubsystemContainer 启动，"
        "否则 11 步手写序列又会回归。"
    )


def test_pandapal_app_stop_delegates_to_container_stop_all():
    """★ 层次 3 回归：PandaPalApp.stop() 必须委托给 SubsystemContainer.stop_all()。"""
    src = _get_pandapal_app_class_source("stop")
    assert src, "无法在 pandapal/app.py 中找到 PandaPalApp.stop()"
    assert "container.stop_all" in src, (
        "PandaPalApp.stop() 未委托给 container.stop_all() — "
        "stop 必须与 start 对称，否则子系统会泄露。"
    )


def test_pandapal_app_does_not_call_broadcast_start_directly():
    """★ 关键回归防护：PandaPalApp.start() 不应再直接调 `self._broadcast.start()`。

    之前（层次 2）：手写 `await self._broadcast.start()`。
    之后（层次 3）：通过 `await self._container.start_all()` 委托给容器。
    """
    src = _get_pandapal_app_class_source("start")
    assert "self._broadcast.start" not in src, (
        "PandaPalApp.start() 又出现 `self._broadcast.start()` — "
        "层次 3 要求通过 container.start_all() 统一驱动所有子系统。"
    )


def test_pandapal_app_registers_task_scheduler_route_handlers_via_container():
    """★ 层次 3 回归：PandaPalApp.start() 通过 container.get() 拿到 task_scheduler 并注册 route handlers。"""
    src = _get_pandapal_app_class_source("start")
    assert "task_scheduler.register_route_handlers" in src, (
        "PandaPalApp.start() 应通过 container.get('task_scheduler') 拿到 task_scheduler "
        "并调用 register_route_handlers()。层次 3 不再直接持有 self._task_scheduler。"
    )


def test_pandapal_app_initializes_task_scheduler():
    """PandaPalApp.start() 源码中必须包含 await task_scheduler.initialize_scheduler() 调用。"""
    src = _get_pandapal_app_class_source("start")
    assert "initialize_scheduler" in src, (
        "PandaPalApp.start() 未 await task_scheduler.initialize_scheduler() — "
        "调度引擎不会启动，cron 任务不会触发。"
    )


def test_pandapal_app_shuts_down_via_container():
    """★ 层次 3 回归：PandaPalApp.stop() 通过 container.stop_all() 统一关闭。

    之前（层次 2）：手写 `await self._task_scheduler.shutdown_scheduler()`。
    之后（层次 3）：委托给容器。
    """
    src = _get_pandapal_app_class_source("stop")
    assert "container.stop_all" in src, (
        "PandaPalApp.stop() 应通过 container.stop_all() 关闭所有子系统，"
        "不再需要手写每个子系统的 shutdown。"
    )


def test_pandapal_app_accepts_config_manager():
    """PandaPalApp.__init__ 必须接受 config_manager 参数（用于 TaskScheduler）。"""
    src = _read_pandapal_app_source()
    assert "config_manager" in src, (
        "PandaPalApp.__init__ 缺少 config_manager 形参 — "
        "TaskScheduler 无法读取 task_timeout_minutes 配置。"
    )


# ══════════════════════════════════════════════════════════════════════════════
# ★ 层次 3 根本解：inject_xxx() 全局副作用函数已被消除
#   - 工具函数改用 Provider 类（SchedulerTools / AgentTaskTools）构造函数注入依赖
#   - 没有 module-level 单例，没有 inject_xxx() 全局副作用
#   - 这些测试改为"反模式不存在"的新断言
# ══════════════════════════════════════════════════════════════════════════════


_TOOLS_DIR = (
    Path(__file__).resolve().parent.parent.parent / "tools"
)


def _discover_inject_functions() -> list[str]:
    """扫描 pandapal/tools/*.py 找出所有 def inject_xxx() 函数名（不导入模块）。"""
    inject_funcs: list[str] = []
    for tools_file in _TOOLS_DIR.glob("*_tools.py"):
        src = tools_file.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name.startswith("inject_"):
                inject_funcs.append(node.name)
    return sorted(set(inject_funcs))


# ★ 已删除的旧测试（断言反模式"存在"）—— 层次 3 后反模式已消除，这些断言不再有意义：
#   - test_pandapal_app_injects_task_scheduler     → 由 test_no_inject_xxx_functions_in_tools 替代
#   - test_pandapal_app_injects_all_tool_dependencies → 由 test_no_inject_xxx_functions_in_tools 替代
#   - test_pandapal_app_injects_agent_task_repo    → 由 test_no_inject_xxx_functions_in_tools 替代
#   - test_pandapal_app_injects_agent_task_broadcaster → 由 test_no_inject_xxx_functions_in_tools 替代


# ══════════════════════════════════════════════════════════════════════════════
# ★ 根本解 2026-06-10 第五波：run_local.py 私有属性 hack 防回归
# ══════════════════════════════════════════════════════════════════════════════


_RUN_LOCAL_PATH = (
    Path(__file__).resolve().parent.parent.parent / "local" / "run_local.py"
)


def _read_run_local_source() -> str:
    return _RUN_LOCAL_PATH.read_text(encoding="utf-8")


def test_run_local_build_blueprint_takes_storage_manager():
    """run_local.py 的 _build_blueprint() 必须接受 storage_manager 参数。

    修复前用 `session_manager._storage_manager`（私有属性）反向 hack + `try/except: pass` 静默吞错。
    这条测试防止以后有人改回"通过私有属性 hack"模式。
    """
    src = _read_run_local_source()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_build_blueprint":
            args = [a.arg for a in node.args.args]
            assert "storage_manager" in args, (
                f"{node.name} 形参应包含 storage_manager（实际为 {args}） — "
                "不应再通过 session_manager._storage_manager 反向 hack。"
            )
            return
    raise AssertionError(
        "未在 run_local.py 中找到 _build_blueprint 函数定义"
    )


def test_run_local_does_not_hack_storage_manager():
    """run_local.py 不应再访问 session_manager._storage_manager 私有属性。

    ★ 用 AST 扫描 ast.Attribute 节点，过滤注释和字符串里的同名引用。
    """
    src = _read_run_local_source()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr != "_storage_manager":
            continue
        # 找到调用链根节点（处理 a.b.c 链）
        root = node
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name) and root.id == "session_manager":
            raise AssertionError(
                "run_local.py 又出现 `session_manager._storage_manager` 私有属性 hack（AST 节点） — "
                "这是破坏封装 + 静默吞错的反模式。"
                "修复：直接传 storage_manager 给 _build_blueprint。"
            )


# ══════════════════════════════════════════════════════════════════════════════
# ★ 层次 3 根本解 2026-06-11：反模式"不存在"的新断言
#   之前的测试断言反模式存在（inject_xxx 必须被调）；
#   层次 3 之后反模式被消除了，测试应反转：断言反模式**不存在**。
# ══════════════════════════════════════════════════════════════════════════════


def test_no_inject_xxx_functions_in_tools():
    """★ 层次 3 反转断言：pandapal/tools/ 下不应再有 inject_xxx() 函数。

    之前 scheduler_tools / agent_task_tools 各自有 inject_xxx() 全局副作用函数。
    层次 3 改造后：
      - 依赖通过 Provider 类构造函数注入（SchedulerTools / AgentTaskTools）
      - 不再需要全局 inject_xxx() 函数
    这条测试防止以后有人又退回到"module-level 单例 + inject()"模式。
    """
    inject_funcs = _discover_inject_functions()
    assert not inject_funcs, (
        f"pandapal/tools/ 不应再有 inject_xxx() 函数 —— 层次 3 改造应使用 Provider 模式。\n"
        f"发现的反模式函数: {inject_funcs}\n"
        f"修复：删除这些函数，依赖改用 Provider 类构造函数注入。"
    )


def test_scheduler_tools_class_exists():
    """SchedulerTools Provider 类必须存在（替代 module-level 单例）。"""
    # ★ 用 AST 而非 import（避免拉起 task_scheduler → OutboundMessage 预存问题）
    scheduler_tools_path = (
        Path(__file__).resolve().parent.parent.parent / "tools" / "scheduler_tools.py"
    )
    src = scheduler_tools_path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    found_class = False
    init_params: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "SchedulerTools":
            found_class = True
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    init_params = [a.arg for a in item.args.args]
    assert found_class, "pandapal/tools/scheduler_tools.py 缺少 SchedulerTools Provider 类"
    assert "task_scheduler" in init_params, (
        f"SchedulerTools.__init__ 必须接受 task_scheduler 参数（实际为 {init_params}）"
    )


def test_pandapal_app_uses_container_start_all():
    """★ 层次 3 关键断言：PandaPalApp.start() 必须用 container.start_all()。

    之前手写 11 步序列（步骤 1/2/2.5/2.6/2.7/3/5/6/6.5/6.6/7/8），每个子系统在 app.py 中 import + 构造。
    层次 3：所有子系统通过 SubsystemContainer 启动，app.py 调一次 container.start_all() 即可。
    """
    src = _get_pandapal_app_class_source("start")
    assert "container.start_all" in src or "self._container.start_all" in src, (
        "PandaPalApp.start() 未委托给 container.start_all() — "
        "层次 3 改造要求所有子系统通过 SubsystemContainer 启动，"
        "否则 11 步手写序列又会回归。"
    )


def test_pandapal_app_stop_uses_container_stop_all():
    """PandaPalApp.stop() 必须用 container.stop_all()（与 start 对称）。"""
    src = _get_pandapal_app_class_source("stop")
    assert "container.stop_all" in src or "self._container.stop_all" in src, (
        "PandaPalApp.stop() 未委托给 container.stop_all() — "
        "stop 必须与 start 对称，否则子系统会泄露。"
    )


def test_pandapal_app_does_not_import_subsystem_classes():
    """★ 层次 3 关键断言：app.py 不应直接 import 多个子系统类。

    之前 app.py 顶部有 8+ 个 import（MessageRouter / HITLBridge / AgentScheduler / TaskScheduler / IpcStdoutTransport...）
    层次 3：app.py 只保留通用类型 + SubsystemContainer + subsystem_registry。
    子系统类由 subsystem_registry.py 集中 import（不在 app.py 出现）。
    """
    src = _read_pandapal_app_source()
    tree = ast.parse(src)
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if node.module and node.module.startswith("pandapal."):
                    imported_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.asname or alias.name.split(".")[0])

    # app.py 不应 import 这些子系统类
    forbidden = {
        "MessageRouter",  # 改由 container 持有
        "HITLBridge",
        "AgentScheduler",
        "TaskScheduler",
        "IpcStdoutTransport",  # 改由 container.get("ipc_transport") 拿
        "ChannelInfo",  # 改由 subsystem_registry 持有
        "ChannelCapability",
        "ChannelType",
    }
    found_forbidden = imported_names & forbidden
    assert not found_forbidden, (
        f"PandaPalApp 不应 import 子系统类 {found_forbidden} —— "
        f"层次 3 要求 app.py 只 import 通用类型 + 容器。子系统类应在 subsystem_registry.py 集中 import。"
    )


def test_subsystem_registry_registers_all_core_subsystems():
    """subsystem_registry 必须注册所有核心子系统（防止遗漏）。"""
    # 不直接 import subsystem_registry（避免拉起 aiosqlite 等重依赖），
    # 用 AST 扫描 SubsystemSpec(name=...) 调用
    registry_path = (
        Path(__file__).resolve().parent.parent.parent / "subsystem_registry.py"
    )
    src = registry_path.read_text(encoding="utf-8")
    tree = ast.parse(src)

    registered: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # 找 SubsystemSpec(name="...") 调用
        if isinstance(node.func, ast.Name) and node.func.id == "SubsystemSpec":
            # 优先取 name= kwarg
            for kw in node.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    registered.add(kw.value.value)
                    break
            # 否则取第一个 positional
            if not registered and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant):
                    registered.add(first.value)

    required = {"registry", "broadcast", "router", "hitl", "agent_scheduler", "task_scheduler", "scheduler_tools"}
    missing = required - registered
    assert not missing, (
        f"subsystem_registry.register_pandapal_subsystems() 必须注册所有核心子系统。\n"
        f"  缺失: {missing}\n"
        f"  已注册: {registered}"
    )


def test_pandapal_app_has_container_property():
    """PandaPalApp 应暴露 container 属性（让外部访问子系统）。"""
    src = _read_pandapal_app_source()
    # AST 找 @property def container
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "PandaPalApp":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "container":
                    if any(isinstance(d, ast.Name) and d.id == "property" for d in item.decorator_list):
                        found = True
    assert found, "PandaPalApp 缺少 @property def container — 层次 3 要求暴露容器引用"
