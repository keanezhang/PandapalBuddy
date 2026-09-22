"""pandapal/config/tests/test_compact_budget_guard.py

覆盖用例 ID：GRD-1 ~ GRD-5（CostBudgetGuard 进度条字段 / 覆盖式记账 /
context_breakdown 保留 / summary 透传 / 计价异常 Fail-Safe）。
"""

from __future__ import annotations

import logging

import pytest

from pandapal.config.budget import guard as guard_mod
from pandapal.config.budget.guard import CostBudgetGuard
from pandapal.config.budget.pricing import CallCost
from pandaren.behavior.step_guard import GuardDecision, StepUsage


def _usage(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    model: str = "m",
    step: int = 1,
    context_breakdown: dict[str, int] | None = None,
) -> StepUsage:
    return StepUsage(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        step=step,
        context_breakdown=context_breakdown,
    )


def _zero_cost(model, input_tokens, output_tokens, cached_tokens=0, *, at=None):
    return CallCost(0.0, 0.0, 0.0, 0.0, 0.0)


# R9 [P1] last_input_tokens 覆盖式记账（非累加）
def test_grd01_last_input_tokens_overwrite(monkeypatch):
    monkeypatch.setattr(guard_mod, "cost_of_call", _zero_cost)
    guard = CostBudgetGuard()
    guard.should_halt(run_id="r1", usage=_usage(input_tokens=100))
    guard.should_halt(run_id="r1", usage=_usage(input_tokens=250))

    s = guard.summary("r1")
    assert s.input_tokens == 350          # 跨步累加
    assert s.last_input_tokens == 250     # 覆盖为最后一次输入


# R9 [P1] step_count 累加
def test_grd02_step_count_accumulates(monkeypatch):
    monkeypatch.setattr(guard_mod, "cost_of_call", _zero_cost)
    guard = CostBudgetGuard()
    for _ in range(3):
        guard.should_halt(run_id="r1", usage=_usage())

    assert guard.summary("r1").step_count == 3


# R9 [P1] context_breakdown 保留上次（None / 空 dict 不覆盖）
def test_grd03_context_breakdown_keeps_last(monkeypatch):
    monkeypatch.setattr(guard_mod, "cost_of_call", _zero_cost)
    guard = CostBudgetGuard()
    first = {"system": 100, "tools": 50, "attachments": 0, "history": 250}
    guard.should_halt(run_id="r1", usage=_usage(context_breakdown=first))
    guard.should_halt(run_id="r1", usage=_usage(context_breakdown=None))
    guard.should_halt(run_id="r1", usage=_usage(context_breakdown={}))

    assert guard.summary("r1").context_breakdown == first


# R9 [P1] 反向：非空 dict 覆盖为最新值
def test_grd03_context_breakdown_overwrites(monkeypatch):
    monkeypatch.setattr(guard_mod, "cost_of_call", _zero_cost)
    guard = CostBudgetGuard()
    guard.should_halt(run_id="r1", usage=_usage(context_breakdown={"system": 1}))
    second = {"system": 2, "tools": 3}
    guard.should_halt(run_id="r1", usage=_usage(context_breakdown=second))

    assert guard.summary("r1").context_breakdown == second


# R1/R9 summary 透传 context_window / compact_threshold / context_quotas
def test_grd04_summary_transparent_fields(monkeypatch):
    monkeypatch.setattr(guard_mod, "cost_of_call", _zero_cost)
    guard = CostBudgetGuard(
        context_window=1_000_000,
        compact_threshold=563_000,
        context_quotas={"system_prompt": 24_000, "tool_schema": 8_000},
    )
    guard.should_halt(run_id="r1", usage=_usage())

    s = guard.summary("r1")
    assert s.context_window == 1_000_000
    assert s.compact_threshold == 563_000
    assert s.context_quotas == {"system_prompt": 24_000, "tool_schema": 8_000}


# R1/R9 未注入 quotas → None
def test_grd04_quotas_none_when_not_injected(monkeypatch):
    monkeypatch.setattr(guard_mod, "cost_of_call", _zero_cost)
    guard = CostBudgetGuard()
    guard.should_halt(run_id="r1", usage=_usage())

    assert guard.summary("r1").context_quotas is None


# O3 基线：计价异常 Fail-Safe（不停机）
def test_grd05_cost_exception_failsafe(monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("pricing down")

    monkeypatch.setattr(guard_mod, "cost_of_call", boom)
    guard = CostBudgetGuard()
    with caplog.at_level(logging.ERROR, logger="pandapal.config.budget.guard"):
        decision = guard.should_halt(run_id="r1", usage=_usage())

    assert decision == GuardDecision(halt=False)
    assert any("计价异常" in r.getMessage() for r in caplog.records)
