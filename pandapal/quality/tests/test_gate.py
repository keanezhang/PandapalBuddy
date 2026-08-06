"""CodeQualityGate 单元测试（设计 §8.5 用例种子 #2/#3/#4/#6/#7/#8/#16/#18）。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from pandaren.identity.models import TrustLevel
from pandaren.tool.definition.context import ToolContext
from pandaren.tool.definition.tool_result import FeedbackSeverity, ToolResult

from pandapal.quality.gate import GATE_SOURCE, CodeQualityGate
from pandapal.quality.models import CircuitDecision, Diagnostic, GateConfig


# 必须是**当前平台上真正的绝对路径**：expand_path 对相对路径会去 resolve_project_root()，
# 未选定工作区时直接抛。("/repo/a.py" 在 Windows 上不是绝对路径 —— 少了盘符。)
_ROOT = str(Path(tempfile.gettempdir()) / "gate_test_repo")
_FILE = str(Path(_ROOT) / "a.py")


def _under_root(name: str) -> str:
    return str(Path(_ROOT) / name)


# ─── 测试双 ────────────────────────────────────────────────────────────


class FakeChecker:
    """返回可控诊断的检查器。diagnostics=None 表示降级。"""

    name = "fake"

    def __init__(self, diagnostics: list[Diagnostic] | None = None) -> None:
        self._diagnostics = diagnostics
        self.calls: list[str] = []

    async def check(self, file_path, *, timeout, cwd):
        self.calls.append(file_path)
        return self._diagnostics


class ExplodingChecker:
    name = "boom"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def check(self, file_path, *, timeout, cwd):
        self.calls.append(file_path)
        raise RuntimeError("检查器炸了")


def _diag(code: str = "F401", line: int = 1,
          severity: FeedbackSeverity = FeedbackSeverity.ERROR) -> Diagnostic:
    return Diagnostic(
        file=_FILE, line=line, column=8, code=code,
        message=f"{code} 的说明", severity=severity, checker="fake",
    )


def _ctx(session_id: str = "sess-A") -> ToolContext:
    return ToolContext(
        run_id="run-1", step_n=1, agent_id="agent-1",
        session_id=session_id, trust_level=TrustLevel.ORCHESTRATOR,
    )


def _gate(checker=None, **cfg) -> CodeQualityGate:
    cfg.setdefault("project_root", _ROOT)
    return CodeQualityGate(
        GateConfig(**cfg),
        checkers=[checker if checker is not None else FakeChecker([])],
    )


def _ok() -> ToolResult:
    return ToolResult(success=True, data="written")


async def _write(gate: CodeQualityGate, path: str = _FILE,
                 ctx: ToolContext | None = None, tool: str = "write_file"):
    return await gate.provide(tool, {"file_path": path}, _ok(), ctx or _ctx())


# ─── #3 触发判断：非代码文件不启动 checker ────────────────────────────


async def test_non_python_file_skips_checker() -> None:
    """#2：写 .md → 不触发检查，checker 根本不被调用。"""
    checker = FakeChecker([_diag()])
    gate = _gate(checker)

    assert await _write(gate, _under_root("README.md")) is None
    assert checker.calls == [], "非受检后缀不得启动检查器进程"


async def test_non_write_tool_skips() -> None:
    checker = FakeChecker([_diag()])
    gate = _gate(checker)

    assert await _write(gate, _FILE, tool="read_file") is None
    assert checker.calls == []


async def test_edit_file_is_gated() -> None:
    gate = _gate(FakeChecker([_diag()]))
    assert await _write(gate, _FILE, tool="edit_file") is not None


async def test_failed_write_skips() -> None:
    """写失败 → 不门控（文件未变，检查它没有意义）。"""
    checker = FakeChecker([_diag()])
    gate = _gate(checker)

    out = await gate.provide(
        "write_file", {"file_path": _FILE},
        ToolResult(success=False, error="disk full"), _ctx(),
    )
    assert out is None
    assert checker.calls == []


async def test_disabled_gate_skips() -> None:
    checker = FakeChecker([_diag()])
    gate = _gate(checker, enabled=False)

    assert await _write(gate) is None
    assert checker.calls == []


async def test_missing_file_path_arg_skips() -> None:
    gate = _gate(FakeChecker([_diag()]))
    assert await gate.provide("write_file", {}, _ok(), _ctx()) is None


# ─── 场景2：干净 → 对 LLM 静默，对用户绿灯 ─────────────────────────────


async def test_clean_file_reports_pass_to_ui_only() -> None:
    """干净 → 产出 INFO 反馈，但对 LLM 不可见。

    「对 LLM 静默」由 llm_visible=False 承担，而非返回 None ——
    返回 None 会与降级（下一条用例）撞成同一个信号，UI 再也分不出
    「查过了，干净」和「压根没查」。
    """
    gate = _gate(FakeChecker([]))
    out = await _write(gate)

    assert out is not None
    assert out.llm_visible is False           # LLM 一个 token 不花
    assert out.severity is FeedbackSeverity.INFO
    assert out.source == "code_quality_gate"
    assert "fake" in out.text                 # 点名做了什么检查，而非光说"通过"


# ─── #3 降级不误导 ───────────────────────────────────────────────────


async def test_degraded_checker_returns_none() -> None:
    """#3：checker 降级（None）→ 返回 None，绝不产出"代码有问题"。"""
    gate = _gate(FakeChecker(None))
    assert await _write(gate) is None


async def test_pass_and_degraded_are_distinguishable() -> None:
    """★ 假绿灯防线：通过与降级**必须**产出不同信号。

    两者若都返回 None，UI 只能在「什么都不显示」和「据缺失反推通过」之间二选一；
    后者会在 ruff 没装/超时/崩溃时给用户亮绿灯 —— 宣称一次从未发生的检查通过了。
    对一个卖「看得见」的产品，假绿灯比不显示糟得多：不显示只是沉默，假绿灯是撒谎。

    这条用例是那道防线本身。它红了，说明 UI 的绿灯失去了依据。
    """
    passed = await _write(_gate(FakeChecker([])))       # 查过了，干净
    degraded = await _write(_gate(FakeChecker(None)))   # 压根没查成

    assert passed is not None, "通过必须有明确信号，否则 UI 无从亮绿灯"
    assert degraded is None, "降级必须无信号，否则 UI 会为没跑的检查亮绿灯"


async def test_exploding_checker_degrades_not_raises() -> None:
    gate = _gate(ExplodingChecker())
    assert await _write(gate) is None


async def test_degrade_does_not_reset_circuit_count() -> None:
    """降级绝不能重置计数 —— 否则一次 ruff 崩溃就能让已熔断的文件"洗白"。"""
    checker = FakeChecker([_diag()])
    gate = _gate(checker, circuit_threshold=3)

    await _write(gate)                       # n=1
    await _write(gate)                       # n=2
    before = gate.retry_counts_snapshot()

    checker._diagnostics = None              # 转为降级
    assert await _write(gate) is None
    assert gate.retry_counts_snapshot() == before, "降级不得改动熔断计数"


# ─── #4 熔断三段式 ───────────────────────────────────────────────────


async def test_circuit_three_phases() -> None:
    """#4：连续 error → 前两轮回灌诊断，第 3 轮熔断提示，第 4 轮起静默。"""
    gate = _gate(FakeChecker([_diag()]), circuit_threshold=3)

    r1 = await _write(gate)
    r2 = await _write(gate)
    r3 = await _write(gate)
    r4 = await _write(gate)
    r5 = await _write(gate)

    assert r1 is not None and r1.severity is FeedbackSeverity.ERROR
    assert r2 is not None and r2.severity is FeedbackSeverity.ERROR
    assert r3 is not None and r3.severity is FeedbackSeverity.WARNING
    assert "如实说明" in r3.text
    assert r4 is None and r5 is None


async def test_fused_file_still_runs_checker() -> None:
    """熔断的是**回灌行为**，不是**检查行为**。

    若熔断后跳过检查，就永远拿不到 pass → 熔断无出口、key 永不回收。
    """
    checker = FakeChecker([_diag()])
    gate = _gate(checker, circuit_threshold=2)

    for _ in range(4):
        await _write(gate)

    assert len(checker.calls) == 4, "熔断后仍须跑检查（亚秒级，成本可忽略）"


async def test_fused_file_exits_circuit_when_fixed() -> None:
    """#7：熔断后该文件修好 → 退出熔断，计数清零，再错从 continue 起算。"""
    checker = FakeChecker([_diag()])
    gate = _gate(checker, circuit_threshold=2)

    await _write(gate)                       # n=1 continue
    await _write(gate)                       # n=2 fuse
    assert await _write(gate) is None        # n=3 silent（已熔断 → 不回灌）

    checker._diagnostics = []                # 修好了
    out = await _write(gate)                 # pass → 删 key
    # 本用例的要害是「计数清零 = 退出熔断」；pass 顺带产出对 LLM 隐身的绿灯，
    # 与熔断状态无关，故只做轻断言不喧宾夺主。
    assert out is not None and out.llm_visible is False
    assert gate.retry_counts_snapshot() == {}

    checker._diagnostics = [_diag()]         # 再次出错
    again = await _write(gate)
    assert again is not None and again.severity is FeedbackSeverity.ERROR, "应从 continue 重新起算"


def test_apply_circuit_decision_table() -> None:
    """直测决策表（纯函数）。"""
    gate = _gate(circuit_threshold=3)

    assert gate._apply_circuit("s", "f", has_error=True) is CircuitDecision.CONTINUE   # n=1
    assert gate._apply_circuit("s", "f", has_error=True) is CircuitDecision.CONTINUE   # n=2
    assert gate._apply_circuit("s", "f", has_error=True) is CircuitDecision.FUSE       # n=3
    assert gate._apply_circuit("s", "f", has_error=True) is CircuitDecision.SILENT     # n=4
    assert gate._apply_circuit("s", "f", has_error=False) is CircuitDecision.PASS
    assert gate.retry_counts_snapshot() == {}


# ─── #6 session 隔离（SESSION_ID 契约）────────────────────────────────


async def test_sessions_do_not_pollute_each_other() -> None:
    """#6：A/B 两 session 同文件名 → 计数互不影响。"""
    gate = _gate(FakeChecker([_diag()]), circuit_threshold=2)

    await _write(gate, ctx=_ctx("sess-A"))   # A: n=1
    await _write(gate, ctx=_ctx("sess-A"))   # A: n=2 → fuse
    r_b = await _write(gate, ctx=_ctx("sess-B"))   # B: n=1 → continue

    assert r_b is not None and r_b.severity is FeedbackSeverity.ERROR, "A 的熔断不得影响 B"
    snap = gate.retry_counts_snapshot()
    assert snap[("sess-A", _FILE)] == 2
    assert snap[("sess-B", _FILE)] == 1


# ─── #18 session_id 为空 ─────────────────────────────────────────────


async def test_empty_session_id_returns_none_and_writes_no_key(caplog) -> None:
    """#18：空 session_id → return None + ERROR 留痕 + 不写任何 key。

    绝不能用 "" 当 key：所有空 session 的会话会共用同一个熔断计数，
    A 的失败熔断 B —— 直接踩 SESSION_ID 契约红线 11「物理隔离不坍缩」，且静默。
    """
    gate = _gate(FakeChecker([_diag()]))

    with caplog.at_level("ERROR"):
        out = await _write(gate, ctx=_ctx(""))

    assert out is None
    assert gate.retry_counts_snapshot() == {}, "空 session_id 不得写入任何 key"
    assert any("session_id" in r.message for r in caplog.records), "违反必留痕（红线12）"


# ─── #8 状态回收 ─────────────────────────────────────────────────────


async def test_reclaim_hooks_evicts_only_that_session() -> None:
    """#8：run 结束 → 清掉该 session 全部 key，其他 session 不受影响。

    经**回收适配器**触发（生产里就是这条路：hooks.add(gate.reclaim_hooks())）。
    """
    gate = _gate(FakeChecker([_diag()]))

    await _write(gate, _FILE, ctx=_ctx("sess-A"))
    await _write(gate, _under_root("b.py"), ctx=_ctx("sess-A"))
    await _write(gate, _FILE, ctx=_ctx("sess-B"))

    gate.reclaim_hooks().on_run_end("run-1", True, session_id="sess-A")

    snap = gate.retry_counts_snapshot()
    assert all(k[0] == "sess-B" for k in snap), f"sess-A 的 key 未清干净: {snap}"
    assert len(snap) == 1


def test_reclaim_hooks_with_empty_session_is_noop() -> None:
    gate = _gate()
    gate._retry_counts[("s", "f")] = 1
    gate.reclaim_hooks().on_run_end("run-1", True, session_id="")
    assert gate.retry_counts_snapshot() == {("s", "f"): 1}


def test_gate_itself_is_not_an_agent_hooks() -> None:
    """门控只戴一顶帽子：它是 provider，不是观测者。

    观测面的身份归适配器 —— 门控不该因为想用 1 个 on_run_end 就背上 21 方法的接口，
    更不该能被当成观测者传来传去。
    """
    from pandaren.behavior.harness.tool_feedback import ToolFeedbackProvider
    from pandaren.hook import AgentHooks

    gate = _gate()

    assert isinstance(gate, ToolFeedbackProvider)
    assert not isinstance(gate, AgentHooks), "门控不该是 AgentHooks"
    assert isinstance(gate.reclaim_hooks(), AgentHooks), "观测面身份应由适配器承担"


def test_reclaim_hooks_is_bound_to_its_own_gate() -> None:
    """适配器由 gate 自己产出 → 不可能绑到别的 gate 的状态上。"""
    a, b = _gate(), _gate()
    a._retry_counts[("s", "f")] = 1
    b._retry_counts[("s", "f")] = 1

    a.reclaim_hooks().on_run_end("r", True, session_id="s")

    assert a.retry_counts_snapshot() == {}
    assert b.retry_counts_snapshot() == {("s", "f"): 1}, "只该清自己的状态"


async def test_capacity_cap_evicts_oldest() -> None:
    """容量兜底：超限淘汰最旧，防 on_run_end 未触发的慢泄漏。"""
    gate = _gate(FakeChecker([_diag()]), max_state_entries=3)

    for i in range(5):
        await _write(gate, _under_root(f"f{i}.py"))

    snap = gate.retry_counts_snapshot()
    assert len(snap) == 3
    assert ("sess-A", _under_root("f0.py")) not in snap, "最旧的应被淘汰"
    assert ("sess-A", _under_root("f4.py")) in snap


# ─── #16 severity / feedback_warnings ────────────────────────────────


async def test_ruff_diagnostics_are_all_error_and_not_swallowed() -> None:
    """#16：feedback_warnings=false（默认）不得吞掉任何 ruff 诊断。

    若有人把 F401 映射成 warning，配上默认的 feedback_warnings=false，
    门控对 PRD 点名的目标问题就静默归零。
    """
    gate = _gate(FakeChecker([_diag("F401"), _diag("F821", line=5)]))

    out = await _write(gate)

    assert out is not None
    assert out.severity is FeedbackSeverity.ERROR
    assert "F401" in out.text and "F821" in out.text


async def test_warnings_suppressed_when_flag_off() -> None:
    """真有 warning 级诊断时（phase-2 的 mypy note 等），默认开关关掉它们。

    warning 被抑制 → 无 error → 判 PASS → 走通过态绿灯（对 LLM 仍隐身）。
    要害仍是「W1 没被回灌给 LLM」，这点不变；变的只是 PASS 不再返回 None。
    """
    gate = _gate(FakeChecker([_diag("W1", severity=FeedbackSeverity.WARNING)]),
                 feedback_warnings=False)
    out = await _write(gate)

    assert out is not None and out.llm_visible is False
    assert "W1" not in out.text              # 抑制的诊断绝不出现在任何通路上


async def test_warnings_reported_when_flag_on() -> None:
    gate = _gate(FakeChecker([_diag("W1", severity=FeedbackSeverity.WARNING)]),
                 feedback_warnings=True)

    out = await _write(gate)
    assert out is not None and out.severity is FeedbackSeverity.WARNING


# ─── 反馈文本 ────────────────────────────────────────────────────────


async def test_feedback_text_shape() -> None:
    gate = _gate(FakeChecker([_diag("F401", line=1), _diag("F821", line=5)]))

    out = await _write(gate)

    assert out.source == GATE_SOURCE
    assert out.llm_visible is True, "有问题必须让 LLM 读到，否则它无从修"
    assert "发现 2 个 error" in out.text
    assert "fake" in out.text, "要点名做了什么检查（与通过态对称）"
    assert "a.py:1:8 F401" in out.text
    assert "请修复后重新写入。" in out.text
    assert "[code_quality_gate]" not in out.text, "source 前缀由渲染层加，这里不该重复"


async def test_feedback_uses_relative_path() -> None:
    gate = _gate(FakeChecker([_diag()]), project_root=_ROOT)
    out = await _write(gate)
    assert "a.py:1:8" in out.text
    assert _FILE not in out.text, "应转为项目相对路径"


async def test_feedback_caps_diagnostics_without_silent_truncation() -> None:
    """截断必须明说还剩多少条（No silent caps）。"""
    gate = _gate(FakeChecker([_diag(f"F{i}", line=i) for i in range(30)]),
                 max_diagnostics_shown=5)

    out = await _write(gate)

    assert "发现 30 个 error" in out.text
    assert "另有 25 条未列出" in out.text


# ─── 多检查器：部分降级不拖垮其余 ─────────────────────────────────────


async def test_partial_checker_degrade_keeps_others() -> None:
    good = FakeChecker([_diag("F401")])
    gate = CodeQualityGate(
        GateConfig(project_root=_ROOT),
        checkers=[FakeChecker(None), good, ExplodingChecker()],
    )

    out = await _write(gate)

    assert out is not None and "F401" in out.text


async def test_all_checkers_degraded_returns_none() -> None:
    gate = CodeQualityGate(
        GateConfig(project_root=_ROOT),
        checkers=[FakeChecker(None), ExplodingChecker()],
    )
    assert await _write(gate) is None


# ─── 配置校验 ────────────────────────────────────────────────────────


@pytest.mark.parametrize("kwargs", [
    {"circuit_threshold": 0},
    {"check_timeout_seconds": 0},
    {"max_state_entries": 0},
])
def test_invalid_config_rejected(kwargs) -> None:
    with pytest.raises(ValueError):
        GateConfig(**kwargs)
