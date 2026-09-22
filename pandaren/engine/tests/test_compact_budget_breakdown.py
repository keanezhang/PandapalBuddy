"""context_breakdown 观测数据用例：覆盖 BRK-1 / BRK-2 / BRK-3 / BRK-4。

对应设计文档 compact-budget.design.md §6.7：
  - BRK-1 四段之和 == real_tokens（history 残差，property + golden）
  - BRK-2 real_tokens <= 0 → None，且不触发 estimate_text
  - BRK-3 estimator 抛异常 → Fail-Safe None（不炸断 run）
  - BRK-4 缺省段为 0，history 不取负（clamp 0）
"""

from __future__ import annotations

import logging

import pytest

from pandaren.engine.run_core import estimate_context_breakdown


class _FakeMemory:
    """按调用顺序返回可配置估算值；可注入异常；记录 estimate_text 调用次数。"""

    def __init__(self, *, estimates=(), attachments=(), raise_estimate=False):
        self._estimates = list(estimates)
        self._atts = attachments
        self._raise_estimate = raise_estimate
        self.estimate_calls = 0

    def estimate_text(self, text: str) -> int:
        self.estimate_calls += 1
        if self._raise_estimate:
            raise RuntimeError("estimator boom")
        if self._estimates:
            return self._estimates.pop(0)
        return 0

    @property
    def post_compact_attachments(self):
        return self._atts


def _breakdown_with_all_heads(estimates, real_tokens):
    """构造 system 首条 + 2 tools + 1 attachment，并把估算值按调用顺序喂给 Fake。"""
    mem = _FakeMemory(estimates=estimates, attachments=({"content": "att"},))
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    tools = [
        {"type": "function", "function": {"name": "t1"}},
        {"type": "function", "function": {"name": "t2"}},
    ]
    return estimate_context_breakdown(mem, msgs, tools, real_tokens)


def test_brk1_four_segments_sum_to_real_tokens():
    # inv-6：history 为残差，四段之和恒等于真实总量（golden 各段 == Fake 返回值）
    b = _breakdown_with_all_heads(estimates=[100, 50, 25], real_tokens=1000)

    assert b == {"system": 100, "tools": 50, "attachments": 25, "history": 825}
    assert sum(b.values()) == 1000
    assert b["history"] == 1000 - 100 - 50 - 25


@pytest.mark.parametrize(
    "system_est, tools_est, attach_est, real_tokens",
    [
        (100, 50, 25, 1000),
        (0, 0, 0, 1),
        (7, 13, 3, 100),
        (999, 0, 0, 1000),
        (0, 500, 0, 1000),
    ],
)
def test_brk1_property_sum_invariant(system_est, tools_est, attach_est, real_tokens):
    # inv-6 property：对任意（合计 <= real_tokens）的 est 组合，sum 恒等于 real_tokens
    b = _breakdown_with_all_heads(
        estimates=[system_est, tools_est, attach_est], real_tokens=real_tokens,
    )

    assert b is not None
    assert b["system"] == system_est
    assert b["tools"] == tools_est
    assert b["attachments"] == attach_est
    assert b["history"] == real_tokens - system_est - tools_est - attach_est
    assert sum(b.values()) == real_tokens


@pytest.mark.parametrize("real_tokens", [0, -1])
def test_brk2_non_positive_real_tokens_returns_none(real_tokens):
    mem = _FakeMemory()
    assert estimate_context_breakdown(mem, [], None, real_tokens) is None
    assert mem.estimate_calls == 0  # 未触发估算（短路在 first-guard）


def test_brk3_estimator_raises_fail_safe_none(caplog):
    mem = _FakeMemory(raise_estimate=True)
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]

    with caplog.at_level(logging.DEBUG):
        b = estimate_context_breakdown(mem, msgs, [], 1000)

    assert b is None  # 故障隔离：观测数据绝不向上抛异常
    assert "estimate_context_breakdown failed" in caplog.text


def test_brk4_missing_heads_zero_and_history_is_residual():
    # 无 system 首条 / 无 tools / 无 attachments → 三小头全 0，history 全量吸收
    mem = _FakeMemory()
    b = estimate_context_breakdown(mem, [{"role": "user", "content": "x"}], None, 1000)

    assert b == {"system": 0, "tools": 0, "attachments": 0, "history": 1000}
    assert mem.estimate_calls == 0


def test_brk4_history_clamped_to_zero_when_estimates_exceed_real():
    # est 合计 > real_tokens → history 不取负，clamp 到 0
    mem = _FakeMemory(estimates=[500], attachments=())
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]

    b = estimate_context_breakdown(mem, msgs, None, 100)

    assert b is not None
    assert b["system"] == 500
    assert b["history"] == 0
