"""端到端集成：装配 → 写文件 → 跑真 ruff → 反馈进 LLM 文本（设计 §8.5 用例种子 #1）。

前面的单测都在各自的层里打转（gate 用 FakeChecker、stage 用 FakeProvider）。
本文件把整条链接起来跑一遍真的：

    AgentBuilder.behavior(tool_feedback_providers=[gate])
        → HarnessExecutor.execute_tool(write_file)      ← 真的写盘
        → _run_feedback_stage → CodeQualityGate.provide ← 真的跑 ruff 子进程
        → ToolResult.feedback
        → render_tool_result_for_llm                    ← LLM 真正读到的文本

层层都绿、连起来不通，是集成测试存在的唯一理由。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pandaren.behavior.harness.executor import HarnessExecutor
from pandaren.engine.run_core import render_tool_result_for_llm
from pandaren.identity.models import TrustLevel
from pandaren.tool.definition.context import ToolContext
from pandaren.tool.definition.tool_result import FeedbackSeverity, ToolResult

from pandapal.quality import CodeQualityGate, GateConfig

pytestmark = pytest.mark.skipif(
    shutil.which("ruff") is None, reason="ruff 不在 PATH"
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


class _FakeRegistry:
    """冒充 ToolRegistry：不真跑工具，只回一个成功结果。

    门控的触发点是"任何工具执行完"这个通用 stage —— write_file 本体一行不改，
    所以这里用不着真的 registry。
    """

    def __init__(self, data: str = "Successfully wrote file") -> None:
        self._data = data

    async def execute_tool(self, tool_name, args, context) -> ToolResult:
        return ToolResult(success=True, data=self._data, tool_name=tool_name)


class _FakeTool:
    is_idempotent = True          # 跳过 R4 幂等（本测试不关心缓存）
    max_output_bytes = None
    halt_on_failure = False
    audit_required = False
    full_name = "write_file"


def _executor(gate: CodeQualityGate) -> HarnessExecutor:
    ex = HarnessExecutor(_FakeRegistry(), feedback_providers=[gate])
    # 绕开 R1/R3 与 tool 查找：本测试聚焦 stage → provider → render 这一段
    ex._rate_limiter.check = lambda *a, **k: None
    ex._circuit_manager.check = lambda *a, **k: None
    ex._circuit_manager.record_success = lambda *a, **k: None
    ex._circuit_manager.record_failure = lambda *a, **k: None
    ex._registry.get_tool = lambda name: _FakeTool()
    return ex


def _ctx(session_id: str = "sess-e2e") -> ToolContext:
    return ToolContext(
        run_id="run-1", step_n=1, agent_id="a",
        session_id=session_id, trust_level=TrustLevel.ORCHESTRATOR,
    )


def _gate(**cfg) -> CodeQualityGate:
    cfg.setdefault("project_root", str(_REPO_ROOT))
    return CodeQualityGate(GateConfig(**cfg))


async def _run_stage(gate: CodeQualityGate, file_path: str, ctx=None) -> ToolResult:
    """直接驱动 stage（execute_tool 的 R1-R4 已在别处测过）。"""
    ex = _executor(gate)
    return await ex._run_feedback_stage(
        "write_file", {"file_path": file_path},
        ToolResult(success=True, data="Successfully wrote file", tool_name="write_file"),
        ctx or _ctx(),
    )


# ─── #1 主路径：诊断真的到达 LLM 文本 ─────────────────────────────────


async def test_dirty_python_file_feedback_reaches_llm_text(tmp_path: Path) -> None:
    """#1：写含 F401/F821 的 .py → 反馈随 tool 结果渲染进 LLM 文本。"""
    f = tmp_path / "bad.py"
    f.write_text("import os\n\n\ndef f():\n    return reuslt\n", encoding="utf-8")

    result = await _run_stage(_gate(), str(f))
    llm_text = render_tool_result_for_llm(result)

    assert result.feedback is not None
    assert result.feedback.severity is FeedbackSeverity.ERROR
    assert "F401" in llm_text and "F821" in llm_text
    assert "[code_quality_gate]" in llm_text, "反馈应带来源标识"
    # 反馈必须在原始 result_text **之前** —— MicroCompact 截断切的是尾部
    assert llm_text.index("F401") < llm_text.index("Successfully wrote file")


async def test_clean_python_file_is_byte_identical_to_no_gate(tmp_path: Path) -> None:
    """干净文件 → 对 LLM 零打扰：读到的与没门控时**逐字节相同**。

    注意机制变了但不变量没变：干净文件现在**会**产出一条 INFO 反馈（用户屏幕要
    绿灯，见 gate._passed_feedback），但它 llm_visible=False，故 LLM 侧仍逐字节
    相同。断言写成「render 结果相等」而非「feedback is None」——
    前者是真正要守的东西，后者只是它当年的实现方式。
    """
    f = tmp_path / "clean.py"
    f.write_text("x = 1\n", encoding="utf-8")

    result = await _run_stage(_gate(), str(f))

    # 真正的不变量：LLM 读到的一个字都没多
    assert render_tool_result_for_llm(result) == "Successfully wrote file"
    # 且这正是靠 llm_visible=False 达成的，不是靠没有反馈
    assert result.feedback is not None
    assert result.feedback.llm_visible is False
    assert result.feedback.severity is FeedbackSeverity.INFO


async def test_non_python_file_untouched(tmp_path: Path) -> None:
    f = tmp_path / "notes.md"
    f.write_text("# hi\n", encoding="utf-8")

    result = await _run_stage(_gate(), str(f))

    assert result.feedback is None
    assert render_tool_result_for_llm(result) == "Successfully wrote file"


# ─── ★ 与 CI 同尺，跑通整条链 ★ ───────────────────────────────────────


async def test_end_to_end_respects_project_ignore(tmp_path: Path) -> None:
    """★ 整条链上仍与 CI 同尺：F841 被本仓 ignore → 不该出现在 LLM 文本里。

    这是 5B-c 的最终验收：门控不是在 CI 之外另立标准，而是把同一把尺子从
    "PR 时"提前到"写完那一秒"。
    """
    f = tmp_path / "mixed.py"
    f.write_text("import os\n\n\ndef f():\n    unused_var = 1\n    return 2\n", encoding="utf-8")

    result = await _run_stage(_gate(), str(f))
    llm_text = render_tool_result_for_llm(result)

    assert "F401" in llm_text
    assert "F841" not in llm_text, "报了项目明确 ignore 的规则 —— 与 CI 不同尺"


# ─── 熔断在整条链上 ──────────────────────────────────────────────────


async def test_circuit_fuses_end_to_end(tmp_path: Path) -> None:
    """连续写不干净的同一文件 → 第 3 轮转熔断提示，第 4 轮起彻底静默。"""
    f = tmp_path / "bad.py"
    f.write_text("import os\n", encoding="utf-8")
    gate = _gate(circuit_threshold=3)

    r1 = await _run_stage(gate, str(f))
    r2 = await _run_stage(gate, str(f))
    r3 = await _run_stage(gate, str(f))
    r4 = await _run_stage(gate, str(f))

    assert r1.feedback.severity is FeedbackSeverity.ERROR
    assert r2.feedback.severity is FeedbackSeverity.ERROR
    assert r3.feedback.severity is FeedbackSeverity.WARNING
    assert "如实说明" in r3.feedback.text
    assert r4.feedback is None
    assert render_tool_result_for_llm(r4) == "Successfully wrote file"


async def test_fixing_file_exits_circuit_end_to_end(tmp_path: Path) -> None:
    """熔断后真把文件修好 → 退出熔断（证明"熔断后仍跑检查"这条契约在真链上成立）。"""
    f = tmp_path / "bad.py"
    f.write_text("import os\n", encoding="utf-8")
    gate = _gate(circuit_threshold=2)

    await _run_stage(gate, str(f))
    await _run_stage(gate, str(f))
    assert (await _run_stage(gate, str(f))).feedback is None      # 已熔断

    f.write_text("x = 1\n", encoding="utf-8")                     # 修好
    await _run_stage(gate, str(f))
    assert gate.retry_counts_snapshot() == {}, "pass 应清空计数、退出熔断"

    f.write_text("import os\n", encoding="utf-8")                 # 再次弄脏
    again = await _run_stage(gate, str(f))
    assert again.feedback.severity is FeedbackSeverity.ERROR, "应从 continue 重新起算"


# ─── O3 / 隔离 / 回收 在真链上 ────────────────────────────────────────


async def test_gate_never_breaks_the_run(tmp_path: Path) -> None:
    """门控内部炸了也不许影响工具结果（O3）。"""
    gate = _gate()

    async def boom(*a, **k):
        raise RuntimeError("门控炸了")

    gate._run_checkers = boom
    f = tmp_path / "bad.py"
    f.write_text("import os\n", encoding="utf-8")

    result = await _run_stage(gate, str(f))

    assert result.success is True
    assert result.feedback is None
    assert render_tool_result_for_llm(result) == "Successfully wrote file"


async def test_on_run_end_reclaims_state_end_to_end(tmp_path: Path) -> None:
    """跑完 N 个 session 后全部 run 结束 → 状态归零，无泄漏。"""
    f = tmp_path / "bad.py"
    f.write_text("import os\n", encoding="utf-8")
    gate = _gate()

    for i in range(5):
        await _run_stage(gate, str(f), ctx=_ctx(f"sess-{i}"))
    assert len(gate.retry_counts_snapshot()) == 5

    reclaimer = gate.reclaim_hooks()
    for i in range(5):
        reclaimer.on_run_end("run-x", True, session_id=f"sess-{i}")

    assert gate.retry_counts_snapshot() == {}, "run 结束后不得残留任何 key"


# ─── 装配：builder 真的把 provider 送到了 executor ────────────────────


def test_builder_wires_providers_into_executor() -> None:
    """.behavior(tool_feedback_providers=[...]) → HarnessExecutor._feedback_providers。

    锁死装配这一环：contract 写得再好，builder 没透传就全白搭。
    """
    from pandaren.builder import AgentBuilder

    gate = _gate()
    b = AgentBuilder()
    b.behavior(tool_feedback_providers=[gate])

    assert b._tool_feedback_providers == [gate]

    ex = HarnessExecutor(_FakeRegistry(), feedback_providers=b._tool_feedback_providers)
    assert ex._feedback_providers == [gate]


def test_executor_defaults_to_no_providers() -> None:
    """未注入 → 空列表 → stage 零开销跳过（未用门控的 Agent 完全不受影响）。"""
    assert HarnessExecutor(_FakeRegistry())._feedback_providers == []
