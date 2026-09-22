"""跨层端到端：模型配置 → 预算 → Memory → AgentLoop → CostBudgetGuard → footer payload。

不 mock 本仓库任何代码：真 resolver / 真 AgentBuilder / 真 Memory / 真压缩管线 /
真 AgentLoop / 真 CostBudgetGuard / 真 BPE 估算器。只把 LLM 换成脚本桩（零网络、零时间依赖）。

断言的终点是**前端 REPLY_END.usage 收到的那个 dict**——即桌面端 footer 的渲染契约。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pandapal.config.budget.guard import CostBudgetGuard
from pandapal.config.llm.context_budget import (
    SYSTEM_PROMPT_CAP,
    TOOL_SCHEMA_CAP,
    compute_cw,
)
from pandaren.behavior.context_window_budget import ContextWindowBudget
from pandaren.builder import AgentBuilder
from pandaren.identity.models import TrustLevel
from pandaren.llm.types import LLMResponse
from pandaren.memory.constants import BALANCED
from pandaren.memory.estimators import TiktokenEstimator

VOCAB = Path(__file__).resolve().parents[2] / "resources" / "tokenizer" / "cl100k_base.tiktoken"

MODEL_ID = "x-1m-y"          # 命中 pattern "*1m*" → huge 档
STEP1_IN = 100
STEP2_IN = 123_456
STEP2_CACHED = STEP2_IN - 40_000

# 前端 types/api.ts 的 ReplyUsage 键集（多一个少一个都算破坏契约）
FOOTER_KEYS = {
    "model", "net_cost_usd", "full_cost_usd", "saved_usd",
    "input_tokens", "cached_tokens", "miss_tokens", "cache_creation_tokens",
    "output_tokens", "reply_tokens", "reasoning_tokens", "hit_rate",
    "last_input_tokens", "step_count", "context_window", "compact_threshold",
    "context_breakdown", "context_quotas",
}


class _ScriptedLLM:
    """两轮剧本：第 1 轮发未注册 tool_call（换取一次工具往返），第 2 轮收尾。

    两轮都回量级真实的 usage，用于区分 footer 的两个 token 口径。
    """

    model_name = MODEL_ID

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def call(self, messages, tools=None, settings=None, **kwargs):
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            return LLMResponse(
                content=None, finish_reason="tool_calls", id="r1",
                model=MODEL_ID, created=1,
                usage={"prompt_tokens": STEP1_IN, "completion_tokens": 5, "total_tokens": 105},
                tool_calls=[{
                    "id": "c1", "type": "function",
                    "function": {"name": "nope", "arguments": "{}"},
                }],
            )
        return LLMResponse(
            content="done", finish_reason="stop", id="r2",
            model=MODEL_ID, created=2,
            usage={
                "prompt_tokens": STEP2_IN,
                "completion_tokens": 7,
                "total_tokens": STEP2_IN + 7,
                "prompt_tokens_details": {"cached_tokens": STEP2_CACHED},
            },
        )


class _CapturingGuard(CostBudgetGuard):
    """捕获 SDK 每步交来的 (run_id, StepUsage)。

    记账键是 SDK 内部生成的 uuid，测试无法预知，只能从守卫这一侧观察。
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.seen: list[tuple[str, object]] = []

    def should_halt(self, *, run_id, usage):  # type: ignore[override]
        self.seen.append((run_id, usage))
        return super().should_halt(run_id=run_id, usage=usage)


def _estimator():
    if not VOCAB.exists():
        pytest.skip(f"未 vendor 词表 {VOCAB}，先跑 scripts/fetch_tiktoken_vocab.py")
    return TiktokenEstimator(vocab_path=VOCAB)


def _budget(
    m: int,
    *,
    output_reserve: int = 8_000,
    sys_measured: int = 19_203,
) -> ContextWindowBudget:
    """照 pandapal/local/run_local.py 的四步装配构造预算对象。"""
    return ContextWindowBudget(
        context_window=compute_cw(m),
        sys_cap=SYSTEM_PROMPT_CAP,
        tool_cap=TOOL_SCHEMA_CAP,
        system_prompt_tokens=sys_measured,
        output_tokens_reserve=output_reserve,
        model_max_context=m,
    )


def _build(llm, guard, budget):
    """照 pandapal/local/run_local.py 的真实接法组装 Agent。"""
    return (
        AgentBuilder()
        .identity(
            agent_id="e2e-ctx", agent_name="e2e", when_to_use="e2e only",
            sensitive_permissions=frozenset(), trust_level=TrustLevel.ORCHESTRATOR,
        )
        .llm(llm)
        .tools([])
        .behavior(max_steps=4, step_guard=guard)
        .system_prompt("You are a test agent.")
        .context_budget(budget)
        .memory(token_estimator=_estimator())
        .build()
    )


# ── E2E-1：配置 → 运行时 → 前端 payload 契约 ─────────────────────────────


@pytest.mark.asyncio
async def test_e2e_footer_payload_matches_frontend_contract():
    budget = _budget(1_000_000)

    guard = _CapturingGuard(
        max_usd=None,
        context_window=budget.context_window,
        compact_threshold=budget.compact_threshold,
        context_quotas={
            "system_prompt": budget.system_prompt_tokens,
            "tool_schema": budget.tool_cap_tokens,
        },
    )
    llm = _ScriptedLLM()
    built = _build(llm, guard, budget)

    result = await built.run("hello", session_id="e2e-footer-1")
    assert result.success, f"意外终止: {result.terminal_reason} / {result.error}"

    # ① 每步用量与上下文组成都真的交到了守卫
    assert len(guard.seen) == 2
    assert all(u.context_breakdown for _, u in guard.seen)
    # ② 同一轮对话共用一个记账键（否则 footer 会"同一条消息里从 0 重来"）
    run_ids = {rid for rid, _ in guard.seen}
    assert len(run_ids) == 1, f"单轮对话出现了多个记账 run_id: {run_ids}"

    payload = guard.summary(run_ids.pop()).to_dict()

    # ③ 契约：键集与前端 ReplyUsage 完全一致
    assert set(payload) == FOOTER_KEYS

    # ④ 两个 token 口径不能混：累计 vs 单次
    assert payload["input_tokens"] == STEP1_IN + STEP2_IN     # 跨步累计（可远超窗口）
    assert payload["last_input_tokens"] == STEP2_IN           # 最后一次调用 = 当前占用
    assert payload["step_count"] == 2

    # ⑤ 进度条分母 = CW（不是模型上限 M）；标记线 / 配额来自同一个预算对象
    assert payload["context_window"] == 800_000
    assert payload["compact_threshold"] == budget.compact_threshold
    assert payload["context_quotas"] == {
        "system_prompt": budget.system_prompt_tokens,
        "tool_schema": budget.tool_cap_tokens,
    }

    # ⑥ 组成明细：四段之和恒等于单次占用，且非负
    bd = payload["context_breakdown"]
    assert set(bd) == {"system", "tools", "attachments", "history"}
    assert sum(bd.values()) == payload["last_input_tokens"]
    assert bd["system"] > 0 and bd["history"] > 0
    assert bd["attachments"] == 0        # 本 run 未压缩 → 无回注

    # ⑦ 派生字段自洽
    assert payload["miss_tokens"] == payload["input_tokens"] - payload["cached_tokens"]
    assert payload["hit_rate"] == round(
        payload["cached_tokens"] / payload["input_tokens"], 4
    )


@pytest.mark.asyncio
async def test_e2e_resolver_budget_reaches_memory():
    """resolver 的档位必须真的落到 Memory（阈值/保留窗口/工具上限），而非停在配置层。"""
    budget = _budget(1_000_000)
    builder = AgentBuilder().context_budget(budget)
    mem = builder._build_memory_factory()()

    threshold = budget.compact_threshold
    policy = mem._short_term._compaction_policy

    assert mem._compact_threshold == threshold
    assert policy._min_tokens == BALANCED.min_keep_tokens(threshold)
    assert policy._max_tokens == BALANCED.max_keep_tokens(threshold)
    # 大窗口下保留窗口必须显著大于旧硬编码的 8K/40K
    assert policy._min_tokens > 8_000 and policy._max_tokens > 40_000


# ── E2E-2：压缩 + 回注 全路径（含组成明细对回注的计量）──────────────────


class _FatReinjectSource:
    """回注一个固定体积的附件，用来验证「压缩 → 回注 → 组成明细 → 不 overflow」全路径。"""

    def __init__(self, content: str) -> None:
        self._content = content

    def collect(self, ctx):  # noqa: ANN001, ANN201 — PostCompactSource Protocol
        return [{
            "source_name": "e2e_fat",
            "title": "e2e fat blob",
            "content": self._content,
            "estimated_tokens": 5_000,
        }]


@pytest.mark.asyncio
async def test_e2e_compaction_with_reinjection_does_not_self_halt():
    """小窗口 + 超阈值输入 + 启用回注 → 压缩发生、回注进上下文、且不自伤停机。

    覆盖 治理前 的故障路径：压缩后 PostCompact 回注把总量重新顶超阈值
    → compact_if_needed 返回 overflow → TerminalReason.CONTEXT_OVERFLOW 直接停机。
    （回注预算 8,000 相对旧默认 50,000 已下调，且 target_tokens 显式预留，双重收口。）
    """
    guard = _CapturingGuard(max_usd=None)
    llm = _ScriptedLLM()
    reinject = _FatReinjectSource("补偿函数说明：" + "空值应返回 None，避免下游除零。" * 300)
    built = (
        AgentBuilder()
        .identity(
            agent_id="e2e-ctx", agent_name="e2e", when_to_use="e2e only",
            sensitive_permissions=frozenset(), trust_level=TrustLevel.ORCHESTRATOR,
        )
        .llm(llm)
        .tools([])
        .behavior(max_steps=4, step_guard=guard)
        .system_prompt("You are a test agent.")
        # 小窗口：CW=20,000；熔断线 sys=min(24k, 5.2k)=5,200 / tool=min(8k, 2k)=2,000，
        # 记账 sys 实占 2,000 → conv=16,000 → T=15,500
        .context_budget(_budget(25_000, output_reserve=2_000, sys_measured=2_000))
        .memory(
            token_estimator=_estimator(),
            post_compact_sources=[reinject],
            post_compact_token_budget=8_000,
        )
        .build()
    )

    # 混合中英文，避免重复片段被 BPE 合并导致估算偏小
    huge_task = "".join(
        f"line {i}: 修正 compute_metric() 的空值处理与边界条件\n" for i in range(4_000)
    )
    result = await built.run(huge_task, session_id="e2e-compact-1")

    assert "context_overflow" not in str(result.terminal_reason), (
        f"压缩后仍溢出 → 自伤停机未修复: {result.error}"
    )
    assert result.success, f"意外终止: {result.terminal_reason} / {result.error}"

    # 压缩确实丢掉了超阈值内容：首轮 LLM 看到的上下文远小于原始任务
    first_ctx_chars = sum(len(str(m.get("content") or "")) for m in llm.calls[0])
    assert first_ctx_chars < len(huge_task) / 2, (
        f"压缩未生效：首轮上下文 {first_ctx_chars} 字符，原始任务 {len(huge_task)} 字符"
    )

    # 回注真的进了上下文，且被新增的「组成明细」计量到（attachments 段 > 0）
    assert len(guard.seen) >= 2
    last_bd = guard.seen[-1][1].context_breakdown
    assert last_bd is not None
    assert last_bd["attachments"] > 0, f"回注未进入上下文: {last_bd}"

    # 回注后依然远低于阈值（未自伤），且明细四段之和 == 该步真实占用
    assert sum(last_bd.values()) == guard.seen[-1][1].input_tokens

    # 真实发出去的 messages（脚本桩报的 usage 是固定假值，不能用来判断上下文大小）：
    # 压缩后的最后一轮 = 回注内容 + system，远小于原始任务，且回注确实在里头。
    last_call_text = "".join(str(m.get("content") or "") for m in llm.calls[-1])
    assert "空值应返回 None" in last_call_text, "回注内容未进入发往 LLM 的 messages"
    assert len(last_call_text) < 10_000, f"压缩后上下文仍过大: {len(last_call_text)} 字符"
