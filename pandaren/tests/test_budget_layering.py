"""预算分层护栏（COMPACT_BUDGET_LAYERING_SPEC §7.1 / §10 验收）。

跨层一致性只能靠测试锁死（A2 禁止 SDK 依赖应用层，所以两处必然各有一份）：

 ① SDK 兜底 CW == floor(toml [default].max_context × CONTEXT_WINDOW_RATIO)
 ② SDK 兜底熔断线 == app SYSTEM_PROMPT_CAP / TOOL_SCHEMA_CAP
 ③ 1M 金标 T == 772,297
 ④ MIN_CONVERSATION_TOKENS 与 I6
 ⑤ I1：固定 0.80 下等价于「输出预留 ≤ 8% × M」
 ⑥ 数值口径：[default] 与「按名字猜」的 pattern 必须是 10 的整数幂
 ⑦ 记账口径：conv 用实占 sys + 熔断线 tool
"""

from __future__ import annotations

import ast
import inspect
import logging
import math
import tomllib
from pathlib import Path

import pytest

from pandapal.config.llm.context_budget import (
    CONTEXT_WINDOW_RATIO,
    DEFAULT_MAX_OUTPUT_TOKENS,
    ESTIMATOR_ERROR_RATIO,
    MIN_CONVERSATION_TOKENS,
    SYSTEM_PROMPT_CAP,
    TOOL_SCHEMA_CAP,
    check_i1,
    check_i6,
    compute_cw,
    output_reserve_hard_limit,
    resolve_output_tokens_reserve,
)
from pandapal.config.llm.context_window_resolver import _TOML_PATH
from pandaren.behavior.context_window_budget import (
    SDK_FALLBACK_BUDGET,
    ContextWindowBudget,
)
from pandaren.builder import AgentBuilder

# 真值层白名单：厂商 + 版本明确、照抄官方的条目（可以是 2 的幂，也可以不是）
_TRUE_VALUE_GLOBS = {
    "gemini-1.5*",
    "gemini-2*",
    "llama-3.1*",
    "llama-3.2*",
    "llama-3.3*",
    "deepseek-v4*",
    "deepseek-v3*",
    "claude-*",
}

TOML = tomllib.loads(_TOML_PATH.read_text(encoding="utf-8"))


def _is_decimal_notation(value: int) -> bool:
    """约定层写法：**K 的整数倍**（如 128,000 = 128×1000、1,000,000）。

    判别目标是「十进制约定」而非「2 的幂」（如 131,072 = 2¹⁷）。
    """
    return value > 0 and value % 1_000 == 0


# ── ① / ② 跨层锁定 ────────────────────────────────────────────────────────


def test_sdk_fallback_context_window_locked_to_toml_default():
    expected = math.floor(TOML["default"]["max_context"] * CONTEXT_WINDOW_RATIO)
    assert expected == 102_400
    assert SDK_FALLBACK_BUDGET.context_window == expected


def test_sdk_fallback_caps_locked_to_app_constants():
    assert SDK_FALLBACK_BUDGET.sys_cap == SYSTEM_PROMPT_CAP
    assert SDK_FALLBACK_BUDGET.tool_cap == TOOL_SCHEMA_CAP


def test_sdk_fallback_estimator_ratio_locked_to_app():
    assert SDK_FALLBACK_BUDGET.estimator_error_ratio == ESTIMATOR_ERROR_RATIO


def test_default_compaction_profile_is_balanced():
    """默认压缩档必须是 BALANCED（改成 GENTLE 会让保留窗口静默放大一倍）。"""
    from pandaren.memory.constants import BALANCED, DEFAULT_COMPACTION_PROFILE

    assert DEFAULT_COMPACTION_PROFILE is BALANCED
    assert (BALANCED.min_keep_ratio, BALANCED.max_keep_ratio, BALANCED.target_ratio) == (
        0.12,
        0.45,
        0.70,
    )


# ── ③ 1M 金标 ─────────────────────────────────────────────────────────────


def test_1m_golden_threshold():
    budget = ContextWindowBudget(
        context_window=compute_cw(1_000_000),
        sys_cap=SYSTEM_PROMPT_CAP,
        tool_cap=TOOL_SCHEMA_CAP,
        system_prompt_tokens=19_203,
        output_tokens_reserve=32_000,
        model_max_context=1_000_000,
    )
    assert budget.context_window == 800_000
    assert budget.conversation_tokens == 772_797
    assert budget.compact_threshold == 772_297


# ── ④ I6 防御下限 ────────────────────────────────────────────────────────


def test_min_conversation_tokens_matches_spec():
    assert MIN_CONVERSATION_TOKENS == 8_000


def test_i6_check(caplog):
    """I6：conv < MIN_CONVERSATION_TOKENS → WARNING（无档位可升，只告警）。"""
    with caplog.at_level(logging.WARNING, logger="pandapal.config.llm.context_budget"):
        assert check_i6(MIN_CONVERSATION_TOKENS) is True        # 恰好触线 → 通过
        assert check_i6(MIN_CONVERSATION_TOKENS - 1) is False   # 低于下限 → 告警

    assert sum(r.levelno == logging.WARNING for r in caplog.records) == 1


def test_i6_signals_on_squeezed_window(caplog):
    """极小窗口下固定开销挤死对话区 → 该预算对象的 conv 会被 I6 判为越界。"""
    budget = ContextWindowBudget(
        context_window=10_000,
        sys_cap=SYSTEM_PROMPT_CAP,
        tool_cap=TOOL_SCHEMA_CAP,
        system_prompt_tokens=1_000,
        output_tokens_reserve=1_000,
        model_max_context=12_500,
    )
    # conv = 10,000 − 1,000 − 1,000 = 8,000 → 恰好触线
    assert budget.conversation_tokens == 8_000

    with caplog.at_level(logging.WARNING, logger="pandapal.config.llm.context_budget"):
        assert check_i6(budget.conversation_tokens - 1) is False
    assert any("I6" in r.getMessage() for r in caplog.records)


# ─────────────────────────────────────────────────────────────
# 输出预留：三层来源 + 「用户值不得高于 toml 的厂商上限」夹取（SPEC §2.3.3）
# ─────────────────────────────────────────────────────────────


def test_reserve_source1_within_vendor_cap_is_verbatim():
    """① llm_settings(max_tokens) 在厂商上限内 → 原样采用。"""
    assert resolve_output_tokens_reserve(
        llm_max_tokens=8_000, model_max_output_tokens=64_000
    ) == (8_000, 1)


def test_reserve_source1_clamped_to_vendor_cap(caplog):
    """① 高于 toml 的 max_output（厂商事实）→ 夹到上限 + WARNING。"""
    with caplog.at_level(logging.WARNING, logger="pandapal.config.llm.context_budget"):
        reserve, priority = resolve_output_tokens_reserve(
            llm_max_tokens=200_000, model_max_output_tokens=64_000
        )

    assert (reserve, priority) == (64_000, 1)
    assert any("夹到上限" in rec.getMessage() for rec in caplog.records)


def test_reserve_source1_not_clamped_when_vendor_cap_unverified():
    """厂商上限未核实（None）→ 无可夹取的上界，原样采用。"""
    assert resolve_output_tokens_reserve(
        llm_max_tokens=200_000, model_max_output_tokens=None
    ) == (200_000, 1)


def test_reserve_source2_uses_toml_max_output():
    """未设 max_tokens → 用 toml 的 max_output（来源②，不告警）。"""
    assert resolve_output_tokens_reserve(
        llm_max_tokens=None, model_max_output_tokens=64_000
    ) == (64_000, 2)


def test_reserve_source3_falls_back_with_warning(caplog):
    """两者皆无 → 兜底 32,000（来源③）+ WARNING。"""
    with caplog.at_level(logging.WARNING, logger="pandapal.config.llm.context_budget"):
        reserve, priority = resolve_output_tokens_reserve(
            llm_max_tokens=None, model_max_output_tokens=None
        )

    assert (reserve, priority) == (DEFAULT_MAX_OUTPUT_TOKENS, 3)
    assert any("兜底" in rec.getMessage() for rec in caplog.records)


# ─────────────────────────────────────────────────────────────
# 技能摘要的 1% 预算：必须用注入的 estimator（与压缩判据同一把尺子）
#
# 技能摘要会进 static_context（属 system prompt），它占多少 token 直接影响
# sys_acct → conv → I1。用 chars/4 粗估中文会低估约 2 倍，使 1% 预算形同虚设。
# ─────────────────────────────────────────────────────────────


class _TenXEstimator:
    """1 字符 = 10 token（远大于 chars/4），用于证明"用的是**注入的**尺子"。"""

    def estimate(self, messages) -> int:
        return sum(len(str(m.get("content") or "")) for m in messages) * 10


class _RaisingEstimator:
    def estimate(self, messages) -> int:
        raise RuntimeError("estimator boom")


class _ZeroEstimator:
    def estimate(self, messages) -> int:
        return 0


def _mk_skill(registry, name: str = "s1") -> None:
    from pandaren.skill.models import Skill

    registry.register_skill(
        Skill(name=name, description="d", when_to_use="x" * 100, content="# c")
    )


def test_skill_summary_budget_uses_injected_estimator():
    """注入大密度尺子 → 同一条摘要被判超预算；用 chars/4 则能装下。"""
    from pandaren.skill.registry import SkillRegistry

    # ① 注入 estimator：entry ≈ (6+2+1+13+100) × 10 = 1,220 token > 1% × 10,000 = 100
    reg = SkillRegistry(token_estimator=_TenXEstimator())
    _mk_skill(reg)
    assert reg.build_skill_summaries(context_window=10_000) == []

    # ② 无 estimator（兜底 chars/4）：(2 + 100)//4 + 5 ≈ 30 ≤ 100 → 装得下
    reg_fallback = SkillRegistry()
    _mk_skill(reg_fallback)
    assert len(reg_fallback.build_skill_summaries(context_window=10_000)) == 1


def test_skill_summary_budget_falls_back_when_estimator_unusable(caplog):
    """estimator 抛异常 / 返回非法值（≤0）→ 走字符兜底，不崩也不静默失真。"""
    from pandaren.skill.registry import SkillRegistry

    for estimator in (_RaisingEstimator(), _ZeroEstimator()):
        reg = SkillRegistry(token_estimator=estimator)
        _mk_skill(reg)
        with caplog.at_level(logging.WARNING, logger="pandaren.skill.registry"):
            assert len(reg.build_skill_summaries(context_window=10_000)) == 1

    # 抛异常那次必须留 WARNING（返回非法值是静默兜底，不告警）
    assert any("估算失败" in rec.getMessage() for rec in caplog.records)


# ─────────────────────────────────────────────────────────────
# app → SDK 装配契约：run_local 传给 AgentBuilder 的参数必须都在签名里
#
# 回归（真实崩溃）：run_local 曾把 reserved_summary_tokens 塞进 memory_kwargs，
# 而 AgentBuilder.memory() 当时没有该形参 →
#   TypeError: AgentBuilder.memory() got an unexpected keyword argument
# → 打包后 sidecar **启动即退出**（App 显示 backend-crashed）。
# 当时全量单测绿灯：run_local 是脚本入口，不在 pytest 收集范围，装配路径无人覆盖。
# ─────────────────────────────────────────────────────────────

_RUN_LOCAL = Path(__file__).resolve().parents[2] / "pandapal" / "local" / "run_local.py"


def _memory_kwargs_keys(src: str) -> set[str]:
    """静态提取 run_local 写入 ``memory_kwargs`` 的全部 key（3 种写法）。"""
    keys: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        # ① memory_kwargs["k"] = ...
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Subscript)
            and isinstance(node.targets[0].value, ast.Name)
            and node.targets[0].value.id == "memory_kwargs"
        ):
            _sl = node.targets[0].slice
            if isinstance(_sl, ast.Constant) and isinstance(_sl.value, str):
                keys.add(_sl.value)
            continue
        # ② memory_kwargs.update(k=...)（**memory_kwargs 的 kw.arg 为 None，天然跳过）
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "memory_kwargs"
        ):
            keys.update(kw.arg for kw in node.keywords if kw.arg)
            continue
        # ③ memory_kwargs: dict = {...}（含条件表达式里的 dict 字面量）
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "memory_kwargs"
        ):
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Dict):
                    keys.update(
                        k.value
                        for k in sub.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)
                    )
    return keys


def test_run_local_memory_kwargs_are_accepted_by_builder_api():
    """``memory(**memory_kwargs)`` 的每个 key，``AgentBuilder.memory()`` 都必须认。"""
    keys = _memory_kwargs_keys(_RUN_LOCAL.read_text(encoding="utf-8"))
    # 前置断言：确实扫到了东西（否则 AST 结构一变就静默变空集、护栏失效）
    assert {
        "token_estimator",
        "post_compact_sources",
        "drop_summarizer",
        "reserved_summary_tokens",
    } <= keys, f"未扫到预期的 memory_kwargs key，实际={sorted(keys)}"

    accepted = set(inspect.signature(AgentBuilder.memory).parameters)
    assert keys <= accepted, (
        f"run_local 传了 AgentBuilder.memory() 不接受的参数：{sorted(keys - accepted)}"
        " —— app 装配时会 TypeError，打包后表现为 sidecar 启动即退出"
    )


def test_run_local_direct_builder_calls_match_builder_api():
    """run_local 里 ``agent_builder.X(...)`` 的关键字参数必须都在 ``X`` 签名内。"""
    tree = ast.parse(_RUN_LOCAL.read_text(encoding="utf-8"))
    checked = 0
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "agent_builder"
        ):
            continue
        name = node.func.attr
        method = getattr(AgentBuilder, name, None)
        assert callable(method), f"run_local 调用了 AgentBuilder 上不存在的方法：{name}"
        accepted = set(inspect.signature(method).parameters)
        passed = {kw.arg for kw in node.keywords if kw.arg}
        assert passed <= accepted, (
            f"agent_builder.{name}() 不接受的参数：{sorted(passed - accepted)}"
        )
        checked += 1
    # 前置断言：run_local 必然对 builder 发起过调用（否则本测试名存实亡）
    assert checked > 0


def test_memory_reserved_summary_tokens_forwarded():
    """``memory(reserved_summary_tokens=)`` 必须落到 Memory（与摘要器 max_tokens 同源）。"""
    from pandaren.memory.constants import DEFAULT_RESERVED_SUMMARY_TOKENS

    class _FakeSummarizer:
        """最小 DropSummarizer 哨兵：装配测试不调用摘要。"""

        max_output_tokens = 1_000

        async def summarize(self, *args, **kwargs):  # pragma: no cover
            raise NotImplementedError("fake summarizer, never invoked at build time")

    # ① 显式传入 → 原样进 Memory
    mem = (
        AgentBuilder()
        .memory(drop_summarizer=_FakeSummarizer(), reserved_summary_tokens=2_048)
        ._build_memory_factory()()
    )
    assert mem._reserved_summary_tokens == 2_048

    # ② 不传 → 落到 Memory 内默认（不是 0，也不是 None）
    mem_default = (
        AgentBuilder()
        .memory(drop_summarizer=_FakeSummarizer())
        ._build_memory_factory()()
    )
    assert mem_default._reserved_summary_tokens == DEFAULT_RESERVED_SUMMARY_TOKENS


# ── ⑤ I1 ────────────────────────────────────────────────────────────────


def test_i1_hard_limit_is_eight_percent_of_model_window():
    assert output_reserve_hard_limit(1_000_000) == 80_000
    assert output_reserve_hard_limit(400_000) == 32_000


@pytest.mark.parametrize(
    "m, reserve, ok",
    [(1_000_000, 32_000, True), (400_000, 32_000, True),
     (200_000, 32_000, False), (128_000, 32_000, False)],
)
def test_i1_check(m: int, reserve: int, ok: bool) -> None:
    assert check_i1(m, reserve) is ok


# ── ⑥ 数值口径 ───────────────────────────────────────────────────────────


def test_default_and_guessed_patterns_use_decimal_notation():
    assert _is_decimal_notation(TOML["default"]["max_context"])

    offenders = [
        entry["glob"]
        for entry in TOML["patterns"]
        if entry["glob"] not in _TRUE_VALUE_GLOBS
        and not _is_decimal_notation(entry["max_context"])
    ]
    assert not offenders, f"约定层必须是十进制（K 的整数倍，禁 2ⁿ）：{offenders}"


def test_two_power_values_only_in_true_value_whitelist():
    """非十进制（2ⁿ）只允许出现在真值条目的白名单里。"""
    non_decimal = {
        entry["glob"]
        for entry in TOML["patterns"]
        if not _is_decimal_notation(entry["max_context"])
    }
    assert non_decimal <= _TRUE_VALUE_GLOBS


# ── ⑦ 记账口径回归 ───────────────────────────────────────────────────────


def test_accounting_uses_measured_system_and_cap_tool():
    budget = ContextWindowBudget(
        context_window=102_400,
        sys_cap=SYSTEM_PROMPT_CAP,
        tool_cap=TOOL_SCHEMA_CAP,
        system_prompt_tokens=19_203,      # 实占（不是熔断线 24,000）
        output_tokens_reserve=8_000,
        model_max_context=128_000,
    )
    # 若误用熔断线记账，会得到 102,400 − 24,000 − 8,000 = 70,400
    assert budget.conversation_tokens == 102_400 - 19_203 - 8_000
    assert budget.conversation_tokens != 102_400 - budget.sys_cap_tokens - 8_000


def test_tool_accounting_uses_cap_not_measured():
    """tool 用熔断线记账：小窗口下 cap 被占比收紧，记账随之收紧（同尺）。"""
    budget = ContextWindowBudget(
        context_window=50_000,
        sys_cap=SYSTEM_PROMPT_CAP,
        tool_cap=TOOL_SCHEMA_CAP,
        system_prompt_tokens=1_000,
        output_tokens_reserve=2_000,
        model_max_context=62_500,
    )
    assert budget.tool_cap_tokens == 5_000          # floor(50,000 × 0.10)
    assert budget.conversation_tokens == 50_000 - 1_000 - 5_000
