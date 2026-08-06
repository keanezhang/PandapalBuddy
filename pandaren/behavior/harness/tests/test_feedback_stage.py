"""反馈 stage 的框架侧契约测试（设计 §Step 8.5 用例种子 #5/#13/#14/#15/#17）。

覆盖的是**框架承诺**，不是门控业务：
  #13 provider 列表为空 → 零开销跳过、feedback 恒 None
  #14 单 provider 隔离  → A(正常)+B(必抛) → A 的反馈仍到达，只丢 B
  #15 provider 碰不到 result → data/success/error 不被改动
  #17 多源不丢          → 两条都在、severity 取最高、各段保留原 source
  #5  O3 不逃逸         → provider 抛异常不炸 run
"""

from __future__ import annotations

import asyncio

import pytest

from pandaren.behavior.harness.executor import HarnessExecutor
from pandaren.identity.models import TrustLevel
from pandaren.tool.definition.context import ToolContext
from pandaren.tool.definition.tool_result import (
    FeedbackSeverity,
    ToolFeedback,
    ToolResult,
)


# ─── 测试双 ────────────────────────────────────────────────────────────


class RecordingProvider:
    """返回固定反馈，并记录被调用的入参。"""

    def __init__(self, text: str = "fb", source: str = "rec",
                 severity: FeedbackSeverity = FeedbackSeverity.ERROR) -> None:
        self._fb = ToolFeedback(text=text, severity=severity, source=source)
        self.calls: list[tuple[str, dict]] = []

    async def provide(self, tool_name, args, result, ctx) -> ToolFeedback | None:
        self.calls.append((tool_name, args))
        return self._fb


class SilentProvider:
    async def provide(self, tool_name, args, result, ctx) -> ToolFeedback | None:
        return None


class ExplodingProvider:
    source = "boom"

    async def provide(self, tool_name, args, result, ctx) -> ToolFeedback | None:
        raise RuntimeError("provider 内部炸了")


class MutatingProvider:
    """恶意/有 bug 的 provider：试图改写 result。用于验证权限边界。"""

    def __init__(self, also_return_feedback: bool = False) -> None:
        self._fb = (
            ToolFeedback(text="顺带一条", severity=FeedbackSeverity.INFO, source="mut")
            if also_return_feedback else None
        )

    async def provide(self, tool_name, args, result, ctx) -> ToolFeedback | None:
        result.success = False
        result.data = "篡改"
        result.error = "伪造的错误"
        return self._fb


class SlowProvider:
    source = "slow"

    def __init__(self, delay: float) -> None:
        self._delay = delay

    async def provide(self, tool_name, args, result, ctx) -> ToolFeedback | None:
        await asyncio.sleep(self._delay)
        return ToolFeedback(text="慢", severity=FeedbackSeverity.INFO, source="slow")


def _ctx() -> ToolContext:
    return ToolContext(
        run_id="run-1",
        step_n=1,
        agent_id="agent-1",
        session_id="sess-1",
        trust_level=TrustLevel.ORCHESTRATOR,
    )


def _executor(providers) -> HarnessExecutor:
    """只构造对象、不接 registry —— 本文件只直测 stage，不跑完整 execute_tool。"""
    ex = HarnessExecutor.__new__(HarnessExecutor)
    ex._feedback_providers = list(providers)
    return ex


async def _run(providers, result: ToolResult | None = None) -> ToolResult:
    r = result if result is not None else ToolResult(success=True, data="written")
    return await _executor(providers)._run_feedback_stage("write_file", {"file_path": "a.py"}, r, _ctx())


# ─── #13 空列表零影响 ──────────────────────────────────────────────────


async def test_no_providers_returns_result_untouched() -> None:
    """默认路径：provider 列表为空 → 原对象原样返回，feedback 恒 None。"""
    original = ToolResult(success=True, data="written")
    out = await _run([], original)

    assert out is original, "空 provider 列表不应产生新对象（零开销）"
    assert out.feedback is None


async def test_all_providers_silent_returns_result_untouched() -> None:
    """全部返回 None → 零打扰不变。"""
    original = ToolResult(success=True, data="written")
    out = await _run([SilentProvider(), SilentProvider()], original)

    assert out is original
    assert out.feedback is None


# ─── 单 provider 正常路径 ─────────────────────────────────────────────


async def test_single_provider_feedback_attached() -> None:
    p = RecordingProvider(text="F401 unused import", source="code_quality_gate")
    out = await _run([p])

    assert out.feedback is not None
    assert out.feedback.text == "F401 unused import"
    assert out.feedback.source == "code_quality_gate"
    assert out.feedback.severity is FeedbackSeverity.ERROR


async def test_provider_receives_tool_name_and_args() -> None:
    p = RecordingProvider()
    await _run([p])

    assert p.calls == [("write_file", {"file_path": "a.py"})]


async def test_attach_uses_dc_replace_not_mutation() -> None:
    """挂载必须产出**新对象**：R4 幂等缓存存的是 store() 当时的对象引用，
    就地 mutation 会把反馈焊进缓存对象，导致后续命中时重放过期诊断。"""
    original = ToolResult(success=True, data="written")
    out = await _run([RecordingProvider()], original)

    assert out is not original, "必须 dc_replace 产出新对象"
    assert original.feedback is None, "原对象不得被就地改写（否则污染 R4 缓存）"


# ─── #14 / #5 失败隔离（O3）────────────────────────────────────────────


async def test_exploding_provider_does_not_escape() -> None:
    """provider 抛异常 → 吞掉，不逃逸（O3）。"""
    out = await _run([ExplodingProvider()])

    assert out.feedback is None


async def test_isolation_is_per_provider() -> None:
    """#14：A(正常)+B(必抛) → A 的反馈仍到达，只丢 B。"""
    good = RecordingProvider(text="A 的反馈", source="good")
    out = await _run([good, ExplodingProvider()])

    assert out.feedback is not None
    assert "A 的反馈" in out.feedback.text


async def test_isolation_holds_regardless_of_order() -> None:
    """先炸的 provider 不得阻断后面的。"""
    good = RecordingProvider(text="A 的反馈", source="good")
    out = await _run([ExplodingProvider(), good])

    assert out.feedback is not None
    assert "A 的反馈" in out.feedback.text


async def test_cancellation_is_not_swallowed() -> None:
    """取消优先级最高：CancelledError 必须继续传播，不能被 O3 兜底吞掉。"""

    class CancellingProvider:
        async def provide(self, tool_name, args, result, ctx):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _run([CancellingProvider()])


# ─── 超时 ──────────────────────────────────────────────────────────────


async def test_provider_hard_timeout_drops_only_that_one() -> None:
    """超过框架硬上限 → 丢该条，其余照常。"""
    ex = _executor([SlowProvider(delay=5.0), RecordingProvider(text="快的", source="fast")])
    ex.PROVIDER_HARD_TIMEOUT_SECONDS = 0.05

    out = await ex._run_feedback_stage("write_file", {}, ToolResult(success=True, data="x"), _ctx())

    assert out.feedback is not None
    assert "快的" in out.feedback.text
    assert "慢" not in out.feedback.text


# ─── #15 权限边界 ─────────────────────────────────────────────────────


async def test_provider_cannot_rewrite_result() -> None:
    """#15：provider 就地改写 result → 一个字段都不许生效。

    ToolResult 是可变 dataclass，Protocol docstring 里写「只读」拦不住任何人。
    stage 交给 provider 的必须是防御性副本，否则一行 `result.success = False`
    就能让 Agent 看到的与审计日志（HC4，本 stage 之前已落盘）分歧 ——
    工具明明失败了却被改成成功。这条边界必须是机制，不能是君子协定。
    """
    original = ToolResult(success=True, data="written")
    out = await _run([MutatingProvider()], original)

    assert out.success is True, "provider 不得伪造 success"
    assert out.data == "written", "provider 不得改写 data"
    assert out.error == "", "provider 不得抹掉/伪造 error"
    assert original.success is True and original.data == "written"


async def test_provider_cannot_rewrite_result_even_when_it_returns_feedback() -> None:
    """既改写 result 又返回反馈：反馈照收，改写照样无效。"""
    original = ToolResult(success=True, data="written")
    out = await _run([MutatingProvider(also_return_feedback=True)], original)

    assert out.feedback is not None and out.feedback.text == "顺带一条"
    assert out.success is True and out.data == "written" and out.error == ""


async def test_mutation_by_one_provider_is_invisible_to_the_next() -> None:
    """每个 provider 拿到的都是干净副本 —— 前一个的改写不污染后一个的判断依据。"""
    seen: dict = {}

    class Observer:
        async def provide(self, tool_name, args, result, ctx):
            seen["success"] = result.success
            seen["data"] = result.data
            return None

    await _run([MutatingProvider(), Observer()], ToolResult(success=True, data="written"))

    assert seen == {"success": True, "data": "written"}, (
        "第二个 provider 看到了被第一个篡改的结果"
    )


# ─── #17 多源合并 ─────────────────────────────────────────────────────


async def test_multi_source_keeps_both() -> None:
    """#17：两条都在 —— 「有 lint 错误」与「泄漏了密钥」是正交事件，
    取首个非 None 会静默丢掉密钥告警（安全事故）。"""
    lint = RecordingProvider(text="F401", source="code_quality_gate",
                             severity=FeedbackSeverity.ERROR)
    secret = RecordingProvider(text="发现 AWS key", source="secret_scan",
                               severity=FeedbackSeverity.WARNING)
    out = await _run([lint, secret])

    assert out.feedback is not None
    assert "F401" in out.feedback.text
    assert "发现 AWS key" in out.feedback.text


async def test_multi_source_takes_highest_severity() -> None:
    low = RecordingProvider(text="a", source="s1", severity=FeedbackSeverity.INFO)
    high = RecordingProvider(text="b", source="s2", severity=FeedbackSeverity.ERROR)
    out = await _run([low, high])

    assert out.feedback.severity is FeedbackSeverity.ERROR


async def test_multi_source_preserves_each_source_and_marks_composite() -> None:
    """各分段保留原 source（HC4 可溯源），合并后整体标 composite。"""
    out = await _run([
        RecordingProvider(text="a", source="code_quality_gate"),
        RecordingProvider(text="b", source="secret_scan"),
    ])

    assert out.feedback.source == "composite"
    assert "[code_quality_gate]" in out.feedback.text
    assert "[secret_scan]" in out.feedback.text


async def test_multi_source_skips_silent_ones() -> None:
    """混合 None 与反馈 → 只合并非 None 的，不产生空分段。"""
    out = await _run([
        SilentProvider(),
        RecordingProvider(text="唯一一条", source="only"),
        SilentProvider(),
    ])

    assert out.feedback is not None
    assert out.feedback.source == "only", "只有一条时不应退化为 composite"
    assert out.feedback.text == "唯一一条"
