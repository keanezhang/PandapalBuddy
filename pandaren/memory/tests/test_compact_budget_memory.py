"""压缩预算回归：Memory 压缩预留 / 恢复阈值 / 截断 / target 扣减 / overflow 归因。

覆盖用例（设计文档 compact-budget.design.md §6.4）：MEM-1 ~ MEM-8。
"""

from __future__ import annotations

import logging

import pytest

from pandaren.memory.constants import (
    COMPACT_TARGET_RATIO,
    DEFAULT_RESERVED_SUMMARY_TOKENS,
)
from pandaren.memory.memory import Memory
from pandaren.memory.models import CompactionSplit


# ─────────────────────────────────────────────────────────────
# Fakes（隔离 LLM / 网络 / 真实 tokenizer）
# ─────────────────────────────────────────────────────────────

class FakeTokenEstimator:
    """按 rule(messages) -> int 返回 token 估算，可精确控制各段估算值。"""

    def __init__(self, rule=lambda messages: 0):
        self.rule = rule
        self.calls: list[list[dict]] = []

    def estimate(self, messages) -> int:
        self.calls.append(list(messages))
        return self.rule(messages)


class PrefixLenEstimator:
    """estimate = 总字符数（前缀长度单调，1 字符 ≈ 1 token）。"""

    def estimate(self, messages) -> int:
        total = 0
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                total += len(content)
        return total


class FakeDropSummarizer:
    """可审计的 DropSummarizer；result=None 表示本次不产出摘要。"""

    def __init__(self, result=None):
        self.result = result
        self.calls = 0

    async def summarize(self, dropped):
        self.calls += 1
        return self.result


class FakePostCompactSource:
    """返回固定 attachments 列表（默认空）的 PostCompactSource。"""

    def __init__(self, attachments=None):
        self.attachments = list(attachments or [])

    def collect(self, ctx):
        return list(self.attachments)


def _tokens_sequence(*values):
    """按调用次序返回固定序列，多余调用会 StopIteration（暴露意外调用）。"""
    it = iter(values)

    def estimate_tokens() -> int:
        return next(it)

    return estimate_tokens


# ─────────────────────────────────────────────────────────────
# MEM-1：构造期 30% 告警（R7 + inv-5）
# ─────────────────────────────────────────────────────────────

def test_construct_warns_when_fixed_over_30pct(caplog):
    """(a) 固定占用 8512 > 0.3×1000 → warning，且构造成功不拒绝启动。"""
    caplog.set_level(logging.WARNING, logger="pandaren.memory")
    mem = Memory(
        system_prompt="",
        compact_threshold=1_000,
        drop_summarizer=FakeDropSummarizer(),
        post_compact_sources=[FakePostCompactSource()],
        post_compact_token_budget=8_000,
        token_estimator=FakeTokenEstimator(),
    )

    assert mem._reserved_summary_tokens == DEFAULT_RESERVED_SUMMARY_TOKENS
    assert mem._reinject_token_budget == 8_000
    assert "超过阈值的 30%" in caplog.text
    assert "8512" in caplog.text


def test_construct_no_30pct_warning_without_sources(caplog):
    """(b) 无 summarizer / 无 sources → 固定占用 0 → 无 30% warning。"""
    caplog.set_level(logging.WARNING, logger="pandaren.memory")
    Memory(compact_threshold=100_000, token_estimator=FakeTokenEstimator())

    assert "超过阈值的 30%" not in caplog.text


# ─────────────────────────────────────────────────────────────
# MEM-2：init_from_restore 用真实阈值（R13）
# ─────────────────────────────────────────────────────────────

def test_init_from_restore_uses_real_threshold(monkeypatch):
    mem = Memory(compact_threshold=563_000, token_estimator=FakeTokenEstimator())
    captured: dict[str, int] = {}

    def fake_load_for_restore(*, session_id, token_budget):
        captured["token_budget"] = token_budget
        return []

    monkeypatch.setattr(mem._long_term, "load_for_restore", fake_load_for_restore)
    mem.init_from_restore(task="t", session_id="sess-mem2")

    assert captured["token_budget"] == 563_000
    assert captured["token_budget"] == mem._compact_threshold


# ─────────────────────────────────────────────────────────────
# MEM-3：estimate_text 与压缩判据同一把尺子（inv-8）
# ─────────────────────────────────────────────────────────────

def test_estimate_text_same_ruler_and_empty_shortcircuit():
    estimator = FakeTokenEstimator(
        lambda msgs: 5 if msgs and msgs[0].get("content") == "hello" else 0
    )
    mem = Memory(token_estimator=estimator)

    assert mem.estimate_text("") == 0
    assert mem.estimate_text("hello") == 5  # 走 estimator，不落回 chars/4
    assert estimator.calls[-1] == [{"role": "user", "content": "hello"}]


# ─────────────────────────────────────────────────────────────
# MEM-4：truncate_text_to_tokens 二分正确性（R14 + inv-8）
# ─────────────────────────────────────────────────────────────

def test_truncate_empty_and_nonpositive_returns_empty():
    mem = Memory(token_estimator=PrefixLenEstimator())
    assert mem.truncate_text_to_tokens("", 10) == ""
    assert mem.truncate_text_to_tokens("abc", 0) == ""


def test_truncate_within_limit_returns_same_object():
    mem = Memory(token_estimator=PrefixLenEstimator())
    text = "短文本"
    assert mem.truncate_text_to_tokens(text, 1_000) is text


@pytest.mark.parametrize("text", [
    "a" * 500,                            # 英文
    "汉" * 500,                           # 中文
    "def f():\n    return 1\n" * 50,      # 代码
])
def test_truncate_over_limit_is_prefix_within_cap(text):
    mem = Memory(token_estimator=PrefixLenEstimator())
    cap = 100
    result = mem.truncate_text_to_tokens(text, cap)

    # 蜕变关系：截断后 ≤ cap 且为原文前缀（不硬编码截断点）
    assert mem.estimate_text(result) <= cap
    assert text.startswith(result)
    assert len(result) < len(text)


# ─────────────────────────────────────────────────────────────
# MEM-5：未超阈值 → None 基线（R6 基线）
# ─────────────────────────────────────────────────────────────

async def test_compact_returns_none_when_under_threshold(monkeypatch):
    mem = Memory(compact_threshold=1_000, token_estimator=FakeTokenEstimator())
    split_calls = {"n": 0}
    boundary_calls = {"n": 0}

    def fake_split_with(target):
        split_calls["n"] += 1
        return [{"role": "user", "content": "x"}], CompactionSplit(
            kept=[], dropped=[{"role": "user", "content": "x"}]
        )

    def fake_boundary(*args, **kwargs):
        boundary_calls["n"] += 1

    monkeypatch.setattr(mem._short_term, "split_with", fake_split_with)
    monkeypatch.setattr(mem._long_term, "append_compact_boundary", fake_boundary)
    monkeypatch.setattr(mem, "estimate_tokens", lambda: 500)

    result = await mem.compact_if_needed()

    assert result is None
    assert split_calls["n"] == 0
    assert boundary_calls["n"] == 0


# ─────────────────────────────────────────────────────────────
# MEM-6：target_tokens 显式扣减 RESERVED + REINJECT（R6 + inv-5）
# ─────────────────────────────────────────────────────────────

async def test_compact_target_deducts_reserved_and_reinject(monkeypatch):
    system_content = "<!-- agent-config-start -->\n\n<!-- agent-config-end -->"

    def rule(messages):
        total = 0
        for m in messages:
            content = m.get("content", "")
            if content == system_content:
                total += 100          # system_overhead
            elif content == "recent":
                total += 300          # kept
            elif content == "old-msg":
                total += 500          # original[0]
            elif content == "old-reply":
                total += 500          # original[1]
        return total

    estimator = FakeTokenEstimator(rule)
    mem = Memory(
        system_prompt="",
        compact_threshold=100_000,
        drop_summarizer=FakeDropSummarizer(result=None),
        post_compact_sources=[FakePostCompactSource([])],
        post_compact_token_budget=8_000,
        token_estimator=estimator,
    )

    captured: dict[str, int] = {}

    def fake_split_with(target):
        captured["target"] = target
        original = [
            {"role": "user", "content": "old-msg"},
            {"role": "assistant", "content": "old-reply"},
        ]
        return original, CompactionSplit(
            kept=[{"role": "assistant", "content": "recent"}],
            dropped=[{"role": "user", "content": "old-msg"}],
        )

    monkeypatch.setattr(mem._short_term, "split_with", fake_split_with)
    monkeypatch.setattr(
        mem, "estimate_tokens", _tokens_sequence(150_000, 60_000, 61_000)
    )

    result = await mem.compact_if_needed()

    # 独立手算公式：int(T×0.70) − sys − att − (reserved + reinject)
    expected_target = (
        int(100_000 * COMPACT_TARGET_RATIO)
        - 100
        - 0
        - (DEFAULT_RESERVED_SUMMARY_TOKENS + 8_000)
    )
    assert expected_target == 61_388
    assert captured["target"] == expected_target
    assert result is None


# ─────────────────────────────────────────────────────────────
# MEM-7：target ≤ 0 → 放弃压缩返回 current_tokens（R6 + inv-5）
# ─────────────────────────────────────────────────────────────

async def test_compact_skips_when_target_nonpositive(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="pandaren.memory")
    summarizer = FakeDropSummarizer(result=None)
    mem = Memory(
        system_prompt="",
        compact_threshold=1_000,
        drop_summarizer=summarizer,
        post_compact_sources=[FakePostCompactSource([])],
        post_compact_token_budget=8_000,
        token_estimator=FakeTokenEstimator(lambda msgs: 0),
    )

    split_calls = {"n": 0}
    boundary_calls = {"n": 0}

    def fake_split_with(target):
        split_calls["n"] += 1
        return [{"role": "user", "content": "x"}], CompactionSplit(
            kept=[], dropped=[{"role": "user", "content": "x"}]
        )

    def fake_boundary(*args, **kwargs):
        boundary_calls["n"] += 1

    monkeypatch.setattr(mem._short_term, "split_with", fake_split_with)
    monkeypatch.setattr(mem._long_term, "append_compact_boundary", fake_boundary)
    monkeypatch.setattr(mem, "estimate_tokens", lambda: 1_500)

    result = await mem.compact_if_needed()

    assert result == 1_500
    assert split_calls["n"] == 0
    assert boundary_calls["n"] == 0
    assert summarizer.calls == 0
    assert "跳过压缩" in caplog.text


# ─────────────────────────────────────────────────────────────
# MEM-8：压缩后 overflow 归因（回注推超 vs 压缩不足）（R6）
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("attachment_tokens, expected_cause", [
    (900, "post-compact reinjection pushed over threshold"),
    (100, "compaction itself insufficient"),
])
async def test_overflow_attribution(monkeypatch, caplog, attachment_tokens, expected_cause):
    caplog.set_level(logging.WARNING, logger="pandaren.memory")

    def rule(messages):
        total = 0
        for m in messages:
            content = m.get("content", "")
            if (
                m.get("role") == "user"
                and isinstance(content, str)
                and content.startswith("<post-compact-context")
            ):
                total += attachment_tokens
            elif content == "recent":
                total += 10
            elif content == "old":
                total += 100
        return total

    estimator = FakeTokenEstimator(rule)
    attachment = {
        "source_name": "fake",
        "title": "t",
        "content": "attachment-content",
        "estimated_tokens": 1,
    }
    mem = Memory(
        system_prompt="",
        compact_threshold=1_000,
        post_compact_sources=[FakePostCompactSource([attachment])],
        post_compact_token_budget=100,
        token_estimator=estimator,
    )

    captured: dict[str, int] = {}

    def fake_split_with(target):
        captured["target"] = target
        original = [{"role": "user", "content": "old"}]
        return original, CompactionSplit(
            kept=[{"role": "assistant", "content": "recent"}],
            dropped=[{"role": "user", "content": "old"}],
        )

    monkeypatch.setattr(mem._short_term, "split_with", fake_split_with)
    monkeypatch.setattr(mem, "estimate_tokens", _tokens_sequence(2_000, 500, 1_500))

    result = await mem.compact_if_needed()

    assert result == 1_500
    assert captured["target"] == 600
    assert expected_cause in caplog.text
