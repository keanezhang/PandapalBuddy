"""HarnessExecutor 全链路 + CircuitBreakerManager + IdempotencyGuard + OutputGuard —— pytest 落地（设计 §2.3/4.4/4.5/4.6）。

覆盖：
  EX-01..10 HarnessExecutor（component(fake)：五道关卡编排 + 并发闸门）
  CB-01..08 CircuitBreakerManager（真实状态机 + RecordingHooks）
  ID-01..08 IdempotencyGuard（turn 级去重 + 并发）
  OG-01..05 OutputGuard（截断 + hooks）

已修复差异（3 个 P0 关闭，断言转正）：
  EX-02/03/04  R1/R3 拒绝与 R4 去重命中路径已补写审计（inv-EX-3）
  EX-05        R2 截断已 dc_replace 产出新对象 + R4 store 移至 R2 后（inv-EX-5）
  ID-08        并发同 key 已 in-flight 去重，仅 1 次执行、等待者不挂死（inv-ID-7）

known-gap（主断言按实际行为 + xfail(strict) 按设计预期，修复后意外通过即报警）：
  CB-07        退避达 max_recovery_timeout 后仍转 HALF_OPEN 探活（设计 inv-CB-5 要求永久 OPEN）
  ID-02        check 命中返回缓存对象原样（deduplicated 由 executor R4 命中路径标记）
  OG-01        截断后追加提示，序列化总长 > max_bytes（设计 inv-OG-1 要求 ≤ 上限）

Fixture 粒度：每个用例内新建 Fake 实例（防用例间状态串扰，§7 落地约定 2）。
确定性：时间依赖仅 CB-05/06/07 的 asyncio.sleep(0.06)（≥2 倍量级差，§7 约定 4）；
EX-09 并发峰值用事件计数证明（设计 §2.3）。
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from pandaren.behavior.harness.circuit_breaker import CircuitBreakerManager
from pandaren.behavior.harness.executor import HarnessExecutor
from pandaren.behavior.harness.idempotency import IdempotencyGuard
from pandaren.behavior.harness.output_guard import OutputGuard
from pandaren.behavior.harness.tests.test_feedback_stage import RecordingProvider, _ctx
from pandaren.tool.definition.tool_result import ToolResult
from pandaren.tool.types import CircuitBreakerConfig, SensitivityLevel


# ─── 测试双 ────────────────────────────────────────────────────────────


class FakeTool:
    """行为层测试用假工具定义（只暴露 HarnessExecutor 用到的字段）。"""

    def __init__(self, name: str = "write_file", *, max_calls_per_turn=None,
                 is_idempotent: bool = True, max_output_bytes=None,
                 halt_on_failure: bool = False, audit_required: bool = False,
                 sensitivity=SensitivityLevel.MEDIUM) -> None:
        self.full_name = name
        self.max_calls_per_turn = max_calls_per_turn
        self.is_idempotent = is_idempotent
        self.max_output_bytes = max_output_bytes
        self.halt_on_failure = halt_on_failure
        self.audit_required = audit_required
        self.sensitivity = sensitivity


class FakeRegistry:
    """内存注册表：get_tool 返回假工具，execute_tool 记录调用并返回预置结果。"""

    def __init__(self, tool: FakeTool) -> None:
        self._tool = tool
        self.calls: list[tuple[str, dict]] = []
        self._result = ToolResult(success=True, data="ok")
        self._fail_for: set[str] = set()

    def get_tool(self, tool_name: str) -> FakeTool:
        return self._tool

    def set_result(self, result: ToolResult) -> None:
        self._result = result

    def fail_on(self, tool_name: str) -> None:
        self._fail_for.add(tool_name)

    async def execute_tool(self, tool_name: str, args: dict, context) -> ToolResult:
        self.calls.append((tool_name, args))
        if tool_name in self._fail_for:
            raise RuntimeError(f"boom: {tool_name}")
        return self._result


class SlowRegistry(FakeRegistry):
    """并发闸门测试用：每次 execute_tool 记录 in-flight 峰值（事件计数证明，不猜时序）。"""

    def __init__(self, tool: FakeTool) -> None:
        super().__init__(tool)
        self.in_flight = 0
        self.peak = 0

    async def execute_tool(self, tool_name: str, args: dict, context) -> ToolResult:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        await asyncio.sleep(0.05)  # 模拟真实耗时（让并发调度真实发生）
        self.in_flight -= 1
        return ToolResult(success=True, data="ok", tool_name=tool_name)


class FakeAudit:
    """内存审计日志：append 条目，供断言留痕内容与次数。"""

    def __init__(self) -> None:
        self.entries: list[dict] = []

    def write_sync(self, event_type, *, agent_id, run_id, detail, session_id, step_n, tool_name) -> None:
        self.entries.append({
            "event_type": event_type,
            "agent_id": agent_id,
            "run_id": run_id,
            "detail": detail,
            "session_id": session_id,
            "step_n": step_n,
            "tool_name": tool_name,
        })


class RecordingHooks:
    """Recording AgentHooks：只记录被测路径会触碰的 7 个扩展点，参数存 dict 列表。"""

    def __init__(self) -> None:
        self.on_tool_output_truncated_calls: list[dict] = []
        self.on_tool_circuit_open_calls: list[dict] = []
        self.on_tool_circuit_close_calls: list[dict] = []
        self.on_before_tool_call_calls: list[dict] = []
        self.on_after_tool_call_calls: list[dict] = []
        self.on_halt_calls: list[dict] = []
        self.on_concurrent_execution_failure_calls: list[dict] = []

    def on_tool_output_truncated(self, tool_name: str, original_size: int, max_size: int) -> None:
        self.on_tool_output_truncated_calls.append(
            {"tool_name": tool_name, "original_size": original_size, "max_size": max_size}
        )

    def on_tool_circuit_open(self, tool_name: str, failure_count: int, recovery_timeout: float) -> None:
        self.on_tool_circuit_open_calls.append(
            {"tool_name": tool_name, "failure_count": failure_count, "recovery_timeout": recovery_timeout}
        )

    def on_tool_circuit_close(self, tool_name: str) -> None:
        self.on_tool_circuit_close_calls.append({"tool_name": tool_name})

    def on_before_tool_call(self, tool_name: str, args: dict, run_id: str, *, step_n: int = 0, session_id: str = "") -> None:
        self.on_before_tool_call_calls.append(
            {"tool_name": tool_name, "args": args, "run_id": run_id, "step_n": step_n, "session_id": session_id}
        )

    def on_after_tool_call(self, tool_name: str, result, run_id: str, *, step_n: int = 0,
                           duration_ms: float = 0.0, session_id: str = "") -> None:
        self.on_after_tool_call_calls.append(
            {"tool_name": tool_name, "result": result, "run_id": run_id,
             "step_n": step_n, "duration_ms": duration_ms, "session_id": session_id}
        )

    def on_halt(self, reason: str, run_id: str) -> None:
        self.on_halt_calls.append({"reason": reason, "run_id": run_id})

    def on_concurrent_execution_failure(self, tool_names: list[str], run_id: str, step_n: int, *,
                                        session_id: str = "") -> None:
        self.on_concurrent_execution_failure_calls.append(
            {"tool_names": tool_names, "run_id": run_id, "step_n": step_n, "session_id": session_id}
        )


# ─── §2.3 HarnessExecutor 全链路（component(fake)）──────────────────────


def _executor(registry=None, *, tool=None, hooks=None, audit=None,
              providers=None, max_concurrency=None) -> HarnessExecutor:
    if registry is None:
        registry = FakeRegistry(tool or FakeTool())
    return HarnessExecutor(
        registry,
        audit_log=audit,
        hooks=hooks,
        feedback_providers=providers,
        max_concurrency=max_concurrency,
    )


async def test_ex_01_happy_path_all_checks_pass():
    """EX-01: 全链路 happy path —— 执行 1 次、审计 1 条、反馈挂载（inv-EX-1/3）"""
    tool = FakeTool(name="write_file", is_idempotent=False, audit_required=True)
    registry = FakeRegistry(tool)
    registry.set_result(ToolResult(success=True, data="ok"))
    audit = FakeAudit()
    hooks = RecordingHooks()
    provider = RecordingProvider(text="审计完成")
    ex = _executor(registry, hooks=hooks, audit=audit, providers=[provider])

    result = await ex.execute_tool("write_file", {"path": "a.txt"}, _ctx())

    assert result.success is True
    assert result.data == "ok"
    assert registry.calls == [("write_file", {"path": "a.txt"})]  # 执行恰好 1 次
    assert len(audit.entries) == 1
    assert audit.entries[0]["tool_name"] == "write_file"           # HC4 留痕
    assert result.feedback is not None and "审计完成" in result.feedback.text
    assert hooks.on_tool_output_truncated_calls == []              # 未超限不触发


async def test_ex_02_rate_limit_rejects_before_execution():
    """EX-02: R1 超限拒绝 —— registry 零调用 + 拒绝结果 + 拒绝路径不触发 hooks + 拒绝同样留痕（inv-EX-2/3）"""
    tool = FakeTool(name="write_file", max_calls_per_turn=2, audit_required=True)
    registry = FakeRegistry(tool)
    audit = FakeAudit()
    hooks = RecordingHooks()
    ex = _executor(registry, hooks=hooks, audit=audit)

    await ex.execute_tool("write_file", {"path": "a.txt"}, _ctx())  # 预热 1
    await ex.execute_tool("write_file", {"path": "a.txt"}, _ctx())  # 预热 2
    before = len(hooks.on_before_tool_call_calls)

    result = await ex.execute_tool("write_file", {"path": "a.txt"}, _ctx())  # 第 3 次 → R1 拒绝

    assert result.success is False
    assert "已达本轮调用上限" in result.error
    assert len(registry.calls) == 2                                  # 拒绝调用零执行（inv-EX-2）
    assert len(hooks.on_before_tool_call_calls) == before            # 拒绝路径不触发 hooks
    assert len(audit.entries) == 3                                   # 拒绝同样留痕（inv-EX-3）
    assert "success=False" in audit.entries[2]["detail"]             # 拒绝条目标记为失败


async def test_ex_03_circuit_open_rejects_before_execution():
    """EX-03: R3 熔断 OPEN 拒绝 —— registry 零调用 + 拒绝结果（Risk-EX-1/2）"""
    tool = FakeTool(name="write_file", max_calls_per_turn=5, audit_required=True)
    registry = FakeRegistry(tool)
    registry.set_result(ToolResult(success=False, error="boom"))
    audit = FakeAudit()
    hooks = RecordingHooks()
    ex = _executor(registry, hooks=hooks, audit=audit)
    ex.register_circuit_breaker(
        "write_file",
        CircuitBreakerConfig(failure_threshold=1, recovery_timeout=30.0, max_recovery_timeout=300.0),
    )

    await ex.execute_tool("write_file", {"path": "a.txt"}, _ctx())  # 失败 → 熔断 OPEN
    assert ex.is_circuit_tripped("write_file") is True
    before = len(hooks.on_before_tool_call_calls)

    result = await ex.execute_tool("write_file", {"path": "a.txt"}, _ctx())  # R3 拒绝

    assert result.success is False
    assert "已熔断" in result.error
    assert len(registry.calls) == 1                                  # 拒绝调用零执行（inv-EX-2）
    assert len(hooks.on_before_tool_call_calls) == before            # 拒绝路径不触发 hooks
    assert len(audit.entries) == 2                                   # 拒绝同样留痕（inv-EX-3）
    assert "success=False" in audit.entries[1]["detail"]             # 拒绝条目标记为失败


async def test_ex_04_idempotency_dedup_second_call():
    """EX-04: R4 去重命中 —— registry 仅 1 次、第二次 deduplicated=True（inv-EX-2/4 + Risk-ID-1）"""
    tool = FakeTool(name="write_file", is_idempotent=False, audit_required=True)
    registry = FakeRegistry(tool)
    registry.set_result(ToolResult(success=True, data="ok"))
    audit = FakeAudit()
    ex = _executor(registry, audit=audit)

    r1 = await ex.execute_tool("write_file", {"path": "a.txt"}, _ctx())
    r2 = await ex.execute_tool("write_file", {"path": "a.txt"}, _ctx())

    assert r1.success is True and r1.deduplicated is False
    assert r2.deduplicated is True
    assert len(registry.calls) == 1                                  # 第二次不重复执行（inv-EX-4）
    assert len(audit.entries) == 2                                   # 命中同样留痕（inv-EX-3）
    assert audit.entries[1]["detail"].startswith("tool=")            # 命中条目标记正常


async def test_ex_05_r2_truncates_oversized_output():
    """EX-05: R2 截断 —— truncated=True + 截断提示 + hook 参数精确 + 原对象不可变（inv-EX-5 + Risk-EX-5）"""
    tool = FakeTool(name="write_file", max_output_bytes=1024)
    registry = FakeRegistry(tool)
    original = ToolResult(success=True, data="x" * 100_000, tool_name="write_file")
    registry.set_result(original)
    hooks = RecordingHooks()
    ex = _executor(registry, hooks=hooks)

    result = await ex.execute_tool("write_file", {"path": "a.txt"}, _ctx())

    assert result.truncated is True
    assert len(result.data) < 100_000                 # 确实截断（蜕变关系：不硬编码截断内容）
    assert "输出已截断" in result.data                # 截断说明存在
    assert result is not original                     # 截断产出新对象
    assert original.truncated is False                # 原对象未被就地改写（inv-EX-5，防 R4 缓存污染）
    assert len(hooks.on_tool_output_truncated_calls) == 1
    call = hooks.on_tool_output_truncated_calls[0]
    assert call["tool_name"] == "write_file"
    assert call["original_size"] == 100_002           # json.dumps 含两端引号
    assert call["max_size"] == 1024


async def test_ex_06_halt_flag_passthrough():
    """EX-06: S6 halt 硬停止 —— halt 标记透传、审计留痕、反馈不因 halt 短路（Risk-EX-4）"""
    tool = FakeTool(name="write_file", audit_required=True)
    registry = FakeRegistry(tool)
    registry.set_result(ToolResult(success=True, data="ok", halt=True))
    audit = FakeAudit()
    provider = RecordingProvider(text="halt 后仍反馈")
    ex = _executor(registry, audit=audit, providers=[provider])

    result = await ex.execute_tool("write_file", {"path": "a.txt"}, _ctx())

    assert result.halt is True                        # 硬停止信号到达调用方
    assert len(audit.entries) == 1                    # halt 路径同样留痕
    assert result.feedback is not None and "halt 后仍反馈" in result.feedback.text


async def test_ex_07_audit_before_feedback():
    """EX-07: 审计先于反馈 —— 反馈内容不进审计、留痕字段完整（inv-EX-6 + Risk-EX-6）"""
    tool = FakeTool(name="write_file", audit_required=True)
    registry = FakeRegistry(tool)
    registry.set_result(ToolResult(success=True, data="ok"))
    audit = FakeAudit()
    provider = RecordingProvider(text="诊断内容")
    ex = _executor(registry, audit=audit, providers=[provider])

    result = await ex.execute_tool("write_file", {"path": "a.txt"}, _ctx())

    entry = audit.entries[0]
    assert "诊断内容" not in entry["detail"]          # 审计在反馈前落盘（inv-EX-6）
    assert entry["tool_name"] == "write_file"
    assert "args_hash=" in entry["detail"]            # 参数指纹留痕
    assert result.feedback is not None                # 反馈在审计后挂载到返回值


async def test_ex_08_set_hooks_single_injection():
    """EX-08: set_hooks 单次注入 —— 二次注入抛 RuntimeError、首次注入生效（inv-EX-7 + Risk-EX-7）"""
    tool = FakeTool(name="write_file", max_output_bytes=1024)
    registry = FakeRegistry(tool)
    registry.set_result(ToolResult(success=True, data="x" * 100_000))
    hooks_a = RecordingHooks()
    hooks_b = RecordingHooks()
    ex = _executor(registry)
    ex.set_hooks(hooks_a)

    with pytest.raises(RuntimeError):
        ex.set_hooks(hooks_b)                         # 二次注入被拒（_hooks_locked，HC4 原则）

    await ex.execute_tool("write_file", {"path": "a.txt"}, _ctx())

    assert len(hooks_a.on_tool_output_truncated_calls) == 1  # 首次注入生效
    assert hooks_b.on_tool_output_truncated_calls == []      # 二次注入不生效（inv-EX-7）


async def test_ex_09_concurrent_gate_max_in_flight():
    """EX-09: execute_tools_concurrent 并发闸门 —— 同时 in-flight ≤ max_concurrency（inv-EX-8 + Risk-EX-8）"""
    registry = SlowRegistry(FakeTool(name="tool", max_calls_per_turn=10))
    ex = _executor(registry, max_concurrency=2)

    results = await ex.execute_tools_concurrent(
        [{"name": "t1", "args": {}}, {"name": "t2", "args": {}}, {"name": "t3", "args": {}}],
        _ctx(),
    )

    assert registry.peak == 2                         # 并发峰值由事件计数证明，不猜时序
    assert len(results) == 3                          # 闸门排队不丢工具
    assert all(r.success for r in results)


async def test_ex_10_concurrent_failure_propagates():
    """EX-10: 并发任一失败 → 异常向上传播（不允许假成功形态，Risk-EX-3；设计 §6-2 允许抛异常）"""
    registry = FakeRegistry(FakeTool(name="tool", max_calls_per_turn=10))
    registry.fail_on("t2")
    ex = _executor(registry, max_concurrency=2)

    with pytest.raises(RuntimeError, match="boom"):
        await ex.execute_tools_concurrent(
            [{"name": "t1", "args": {}}, {"name": "t2", "args": {}}, {"name": "t3", "args": {}}],
            _ctx(),
        )


async def test_ex_11_concurrent_same_key_executes_once():
    """EX-11: 并发同 key 经 executor 全链路 —— 仅 1 次执行、其余 deduplicated、每次调用留痕（inv-EX-3/4 + inv-ID-7）"""
    tool = FakeTool(name="write_file", is_idempotent=False, audit_required=True)
    registry = FakeRegistry(tool)
    registry.set_result(ToolResult(success=True, data="ok"))
    audit = FakeAudit()
    ex = _executor(registry, audit=audit)

    results = await ex.execute_tools_concurrent(
        [
            {"name": "write_file", "args": {"path": "a.txt"}},
            {"name": "write_file", "args": {"path": "a.txt"}},
            {"name": "write_file", "args": {"path": "a.txt"}},
        ],
        _ctx(),
    )

    assert len(registry.calls) == 1                # 并发同 key 仅执行 1 次（inv-ID-7）
    assert sum(r.deduplicated for r in results) == 2
    assert all(r.success for r in results)
    assert len(audit.entries) == 3                 # 执行 + 2 次命中，每条调用都留痕（inv-EX-3）


# ─── §4.4 CircuitBreakerManager 状态机（component(fake)）────────────────


def _cb_manager(hooks: RecordingHooks | None = None) -> CircuitBreakerManager:
    mgr = CircuitBreakerManager()
    if hooks is not None:
        mgr.set_hooks(hooks)
    return mgr


def test_cb_01_unregistered_tool_passes():
    """CB-01: 未注册熔断器的工具 → check 放行、hooks 未触发（inv-CB-1）"""
    hooks = RecordingHooks()
    mgr = _cb_manager(hooks)

    result = mgr.check("unregistered_tool")

    assert result is None
    assert hooks.on_tool_circuit_open_calls == []
    assert hooks.on_tool_circuit_close_calls == []


def test_cb_02_below_threshold_stays_closed():
    """CB-02: 失败 1 次 < threshold=2 → 保持 CLOSED 放行、open hook 未触发（inv-CB-2 + Risk-CB-1）"""
    hooks = RecordingHooks()
    mgr = _cb_manager(hooks)
    mgr.register("tool_a", CircuitBreakerConfig(
        failure_threshold=2, recovery_timeout=0.02, max_recovery_timeout=0.5,
    ))

    mgr.record_failure("tool_a")

    assert mgr.check("tool_a") is None                # 未达阈值不误杀
    assert hooks.on_tool_circuit_open_calls == []


def test_cb_03_threshold_hit_opens_with_exact_hook():
    """CB-03: 恰好达 threshold → OPEN + open hook 恰好 1 次（参数精确，inv-CB-2 + Risk-CB-1/5）"""
    hooks = RecordingHooks()
    mgr = _cb_manager(hooks)
    mgr.register("tool_a", CircuitBreakerConfig(
        failure_threshold=2, recovery_timeout=0.02, max_recovery_timeout=0.5,
    ))

    mgr.record_failure("tool_a")  # 1 次，未 OPEN
    mgr.record_failure("tool_a")  # 第 2 次 → OPEN

    assert mgr.is_tripped("tool_a") is True
    assert len(hooks.on_tool_circuit_open_calls) == 1
    assert hooks.on_tool_circuit_open_calls[0] == {
        "tool_name": "tool_a", "failure_count": 2, "recovery_timeout": 0.02,
    }


def test_cb_04_open_rejects_check():
    """CB-04: OPEN 期间 check → 拒绝 ToolResult、无 hook 触发（inv-CB-3 + Risk-CB-2）"""
    hooks = RecordingHooks()
    mgr = _cb_manager(hooks)
    mgr.register("tool_a", CircuitBreakerConfig(
        failure_threshold=1, recovery_timeout=0.02, max_recovery_timeout=0.5,
    ))
    mgr.record_failure("tool_a")  # 1 次即 OPEN

    result = mgr.check("tool_a")  # 立即 check（elapsed << 0.02s，无竞态）

    assert result is not None
    assert result.success is False
    assert result.tool_name == "tool_a"
    assert "熔断" in result.error
    assert len(hooks.on_tool_circuit_open_calls) == 1  # OPEN 拒绝不是状态迁移，不新增 hook


async def test_cb_05_recovery_success_closes():
    """CB-05: 超时 → HALF_OPEN 探活成功 → CLOSED + close hook（inv-CB-4 + Risk-CB-3）"""
    hooks = RecordingHooks()
    mgr = _cb_manager(hooks)
    mgr.register("tool_a", CircuitBreakerConfig(
        failure_threshold=1, recovery_timeout=0.02, max_recovery_timeout=0.5,
    ))
    mgr.record_failure("tool_a")  # OPEN
    assert mgr.is_tripped("tool_a") is True
    await asyncio.sleep(0.06)     # ≥ 2 倍 recovery_timeout，杜绝竞态

    assert mgr.check("tool_a") is None   # 超时 → HALF_OPEN → 放行一次探活
    mgr.record_success("tool_a")         # 探活成功 → CLOSED

    assert mgr.is_tripped("tool_a") is False
    assert len(hooks.on_tool_circuit_close_calls) == 1
    assert hooks.on_tool_circuit_close_calls[0] == {"tool_name": "tool_a"}


async def test_cb_06_half_open_failure_backs_off():
    """CB-06: HALF_OPEN 探活失败 → 回 OPEN + 退避翻倍（inv-CB-4 + Risk-CB-4）"""
    hooks = RecordingHooks()
    mgr = _cb_manager(hooks)
    mgr.register("tool_a", CircuitBreakerConfig(
        failure_threshold=1, recovery_timeout=0.02, max_recovery_timeout=0.5,
    ))
    mgr.record_failure("tool_a")  # OPEN #1（退避 0.02）
    await asyncio.sleep(0.06)
    mgr.check("tool_a")           # → HALF_OPEN

    mgr.record_failure("tool_a")  # 探活失败 → OPEN #2（退避翻倍）

    assert mgr.is_tripped("tool_a") is True
    assert len(hooks.on_tool_circuit_open_calls) == 2
    assert hooks.on_tool_circuit_open_calls[1]["failure_count"] == 2
    assert hooks.on_tool_circuit_open_calls[1]["recovery_timeout"] == 0.04  # 退避翻倍


async def test_cb_07_backoff_capped_at_max():
    """CB-07: 退避被 max_recovery_timeout 钳制（0.02→0.04→0.05），不再翻倍增长"""
    hooks = RecordingHooks()
    mgr = _cb_manager(hooks)
    mgr.register("tool_a", CircuitBreakerConfig(
        failure_threshold=1, recovery_timeout=0.02, max_recovery_timeout=0.05,
    ))

    mgr.record_failure("tool_a")                # OPEN #1：退避 0.02
    await asyncio.sleep(0.06)
    mgr.check("tool_a")                         # → HALF_OPEN
    mgr.record_failure("tool_a")                # OPEN #2：退避 0.04
    await asyncio.sleep(0.06)
    mgr.check("tool_a")                         # → HALF_OPEN
    mgr.record_failure("tool_a")                # OPEN #3：退避 min(0.08, 0.05) = 0.05

    assert mgr.is_tripped("tool_a") is True
    assert hooks.on_tool_circuit_open_calls[2]["recovery_timeout"] == 0.05  # 钳制生效

    await asyncio.sleep(0.06)                   # 超过 0.05 上限
    assert mgr.check("tool_a") is None          # 现状：仍转 HALF_OPEN 放行（达上限不阻止探活）

    mgr.record_failure("tool_a")                # OPEN #4：仍 0.05，不再翻倍
    assert hooks.on_tool_circuit_open_calls[3]["recovery_timeout"] == 0.05


@pytest.mark.xfail(
    strict=True,
    reason=(
        "known-gap: 设计 inv-CB-5 要求退避达 max_recovery_timeout 后永久 OPEN（不再探活）；"
        "实现 _CircuitBreakerState.should_allow 无上限判断，超时仍转 HALF_OPEN 放行探活。"
        "修复后本用例应转 passed。"
    ),
)
async def test_cb_07_stays_open_after_max_backoff():
    """CB-07(设计预期): 达 max_recovery_timeout 后永久 OPEN，check 仍拒绝（inv-CB-5）"""
    hooks = RecordingHooks()
    mgr = _cb_manager(hooks)
    mgr.register("tool_a", CircuitBreakerConfig(
        failure_threshold=1, recovery_timeout=0.02, max_recovery_timeout=0.05,
    ))

    mgr.record_failure("tool_a")
    await asyncio.sleep(0.06)
    mgr.check("tool_a")
    mgr.record_failure("tool_a")
    await asyncio.sleep(0.06)
    mgr.check("tool_a")
    mgr.record_failure("tool_a")                # 达上限 0.05

    await asyncio.sleep(0.06)
    assert mgr.check("tool_a") is not None      # 设计预期：仍拒绝（永久 OPEN）


def test_cb_08_closed_success_resets_count_no_hook():
    """CB-08: CLOSED 成功 record_success → 不触发 hook、失败计数复位（inv-CB-6 + Risk-CB-5）"""
    hooks = RecordingHooks()
    mgr = _cb_manager(hooks)
    mgr.register("tool_a", CircuitBreakerConfig(
        failure_threshold=2, recovery_timeout=0.02, max_recovery_timeout=0.5,
    ))
    mgr.record_failure("tool_a")  # 计数 1
    mgr.record_success("tool_a")  # CLOSED 内成功：复位计数，不触发 hook

    assert hooks.on_tool_circuit_open_calls == []
    assert hooks.on_tool_circuit_close_calls == []

    mgr.record_failure("tool_a")                # 复位后从 0 起算：1 次仍未 OPEN
    assert mgr.is_tripped("tool_a") is False
    mgr.record_failure("tool_a")                # 第 2 次 → OPEN（证明计数确已复位而非累加）
    assert mgr.is_tripped("tool_a") is True


# ─── §4.5 IdempotencyGuard（component(fake)）───────────────────────────


async def test_id_01_first_check_miss_returns_none():
    """ID-01: 首次 check 未命中 → None（放行可执行，inv-ID-2）"""
    guard = IdempotencyGuard()

    result = await guard.check("write_file", {"path": "a.txt"})

    assert result is None


async def test_id_02_hit_returns_cached_result():
    """ID-02: 已 store 的同 key check → 命中返回缓存结果（不重新执行，Risk-ID-1；deduplicated 由 executor 层标记）"""
    guard = IdempotencyGuard()
    cached = ToolResult(success=True, data="ok")
    await guard.store("write_file", {"path": "a.txt"}, cached)

    result = await guard.check("write_file", {"path": "a.txt"})

    assert result is cached


@pytest.mark.xfail(
    strict=True,
    reason=(
        "known-gap: 设计 inv-ID-2 要求 check 命中返回 deduplicated=True 结果；"
        "实现 check 命中返回缓存对象原样（deduplicated 标记由 HarnessExecutor R4 命中路径 "
        "dc_replace(cached, deduplicated=True) 添加，见 EX-04）。修复后本用例应转 passed。"
    ),
)
async def test_id_02_hit_marks_deduplicated():
    """ID-02(设计预期): 命中结果带 deduplicated=True（inv-ID-2）"""
    guard = IdempotencyGuard()
    cached = ToolResult(success=True, data="ok")
    await guard.store("write_file", {"path": "a.txt"}, cached)

    result = await guard.check("write_file", {"path": "a.txt"})

    assert result.deduplicated is True


async def test_id_03_different_args_no_false_positive():
    """ID-03: 不同 args → 不同 key，不误判为重复（inv-ID-1 + Risk-ID-4）"""
    guard = IdempotencyGuard()
    await guard.store("write_file", {"path": "a.txt"}, ToolResult(success=True, data="ok"))

    result = await guard.check("write_file", {"path": "b.txt"})

    assert result is None                         # 不同参数不命中缓存，照常执行


def test_id_04_make_key_deterministic():
    """ID-04: _make_key 确定性 + tool_name 参与 key（inv-ID-1）"""
    guard = IdempotencyGuard()

    k1 = guard._make_key("t", {"a": 1})
    k2 = guard._make_key("t", {"a": 1})
    k3 = guard._make_key("t2", {"a": 1})

    assert k1 == k2                                # 确定性
    assert k1 != k3                                # tool_name 参与 key


async def test_id_05_reset_turn_clears_cache():
    """ID-05: reset_turn 清空 → 同 key 再次可执行（inv-ID-4）"""
    guard = IdempotencyGuard()
    await guard.store("write_file", {"path": "a.txt"}, ToolResult(success=True, data="ok"))
    assert await guard.check("write_file", {"path": "a.txt"}) is not None

    guard.reset_turn()

    assert await guard.check("write_file", {"path": "a.txt"}) is None  # 新 turn 可重新执行


async def test_id_06_locks_no_leak():
    """ID-06: _locks 无泄漏 —— check 后非空、reset_turn 后清空（inv-ID-5 + Risk-ID-2）"""
    guard = IdempotencyGuard()
    await guard.check("write_file", {"path": "a.txt"})

    assert len(guard._locks) == 1                 # check 触发 lock 创建

    guard.reset_turn()

    assert len(guard._locks) == 0                 # 无泄漏（否则后续调用死锁）


async def test_id_07_async_check_sync_store_shared():
    """ID-07: async check 与 sync store 共享同一 _cache（inv-ID-6 + Risk-ID-3）"""
    guard = IdempotencyGuard()
    cached = ToolResult(success=True, data="ok")
    guard.store_sync("write_file", {"path": "a.txt"}, cached)

    result = await guard.check("write_file", {"path": "a.txt"})

    assert result is cached                       # async/sync 互见，不各自为政


async def test_id_08_concurrent_same_key_runs_once():
    """ID-08: 并发同 key —— in-flight 去重：仅 1 次执行、其余命中缓存（inv-ID-7 + Risk-ID-1）"""
    guard = IdempotencyGuard()
    executed: list[int] = []

    async def check_and_execute() -> ToolResult:
        cached = await guard.check("write_file", {"path": "a.txt"})
        if cached is not None:
            from dataclasses import replace as dc_replace
            return dc_replace(cached, deduplicated=True)
        executed.append(1)
        await asyncio.sleep(0)                    # 模拟真实异步工具执行（让出事件循环，暴露并发窗口）
        await guard.store("write_file", {"path": "a.txt"}, ToolResult(success=True, data="ok"))
        return ToolResult(success=True, data="ok")

    results = await asyncio.gather(*[check_and_execute() for _ in range(5)])

    assert len(executed) == 1                     # 设计预期：仅 1 次真正执行（inv-ID-7）
    assert sum(r.deduplicated for r in results) == 4
    assert all(r.success for r in results)
    assert len(guard._locks) == 1                 # 锁不泄漏
    assert len(guard._inflight) == 0              # in-flight 已消费，无泄漏


async def test_id_09_executor_abort_wakes_waiters():
    """ID-09: 执行方抛异常 → complete 置 RuntimeError，等待者不永久挂起（inv-ID-7 失效模式）"""
    guard = IdempotencyGuard()
    executed: list[int] = []

    async def exec_and_abort() -> ToolResult:
        cached = await guard.check("write_file", {"path": "a.txt"})
        if cached is not None:
            from dataclasses import replace as dc_replace
            return dc_replace(cached, deduplicated=True)
        executed.append(1)
        try:
            raise RuntimeError("boom")            # 模拟真实执行路径抛异常
        finally:
            guard.complete("write_file", {"path": "a.txt"}, None)  # executor finally 的收口动作

    with pytest.raises(RuntimeError):
        await asyncio.gather(*[exec_and_abort() for _ in range(3)])

    assert len(executed) == 1                     # 仍只有执行者真正动手
    assert len(guard._inflight) == 0              # 异常路径登记已清理，无残留挂起


# ─── §4.6 OutputGuard（component(fake)）────────────────────────────────


def test_og_01_oversized_truncates_with_hook():
    """OG-01: 超限截断 —— truncated + 截断提示 + hook 参数精确（inv-OG-1 的序列化长度 ≤ 上限见 xfail）"""
    guard = OutputGuard()
    hooks = RecordingHooks()
    guard.set_hooks(hooks)
    result = ToolResult(success=True, data="x" * 100_000, tool_name="write_file")

    out = guard.check(result, max_bytes=1024)

    assert out.truncated is True
    assert len(out.data) < 100_000                 # 确实截断（蜕变关系：不硬编码截断内容）
    assert "输出已截断" in out.data                # 截断说明存在
    assert len(hooks.on_tool_output_truncated_calls) == 1
    call = hooks.on_tool_output_truncated_calls[0]
    assert call["tool_name"] == "write_file"
    assert call["original_size"] == 100_002        # json.dumps 含两端引号
    assert call["max_size"] == 1024


@pytest.mark.xfail(
    strict=True,
    reason=(
        "known-gap: 设计 inv-OG-1 要求截断后序列化长度 ≤ max_bytes（二分收敛到上限内）；"
        "实现只保证截断前缀 ≤ max_bytes，追加截断提示后 json 序列化总长超限。"
        "修复后本用例应转 passed。"
    ),
)
def test_og_01_truncated_length_within_max():
    """OG-01(设计预期): 截断后 json 序列化长度 ≤ max_bytes（inv-OG-1）"""
    guard = OutputGuard()
    result = ToolResult(success=True, data="x" * 100_000)

    out = guard.check(result, max_bytes=1024)

    assert len(json.dumps(out.data)) <= 1024


def test_og_02_within_limit_returns_original():
    """OG-02: 未超限 → 原对象原样返回（is 同一对象）+ 不触发 hook（inv-OG-2）"""
    guard = OutputGuard()
    hooks = RecordingHooks()
    guard.set_hooks(hooks)
    original = ToolResult(success=True, data="ok")

    out = guard.check(original, max_bytes=1024)

    assert out is original
    assert out.truncated is False
    assert hooks.on_tool_output_truncated_calls == []


def test_og_03_three_truncations_three_hooks():
    """OG-03: 连续 3 次截断 → hook 触发 3 次，每次参数含 max_size=1024（inv-OG-3）"""
    guard = OutputGuard()
    hooks = RecordingHooks()
    guard.set_hooks(hooks)
    result = ToolResult(success=True, data="x" * 100_000, tool_name="write_file")

    for _ in range(3):
        result = guard.check(result, max_bytes=1024)
        assert result.truncated is True

    assert len(hooks.on_tool_output_truncated_calls) == 3
    for call in hooks.on_tool_output_truncated_calls:
        assert call["tool_name"] == "write_file"
        assert call["max_size"] == 1024


def test_og_04_no_hooks_degrades_with_warning(caplog):
    """OG-04: 无 hooks → 降级（仍截断）+ warning（inv-OG-4 + Risk-OG-2）"""
    guard = OutputGuard()
    result = ToolResult(success=True, data="x" * 100_000)

    with caplog.at_level(logging.WARNING, logger="pandaren.behavior.harness.output_guard"):
        out = guard.check(result, max_bytes=1024)

    assert out.truncated is True                  # 截断不因无 hooks 而失效（降级正确）
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_og_05_none_data_passthrough():
    """OG-05: data=None → 原样返回、不截断、不触发 hook（inv-OG-5）"""
    guard = OutputGuard()
    hooks = RecordingHooks()
    guard.set_hooks(hooks)
    result = ToolResult(success=False, data=None, error="tool error")

    out = guard.check(result, max_bytes=1024)

    assert out is result
    assert out.truncated is False
    assert hooks.on_tool_output_truncated_calls == []
