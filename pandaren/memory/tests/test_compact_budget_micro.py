"""MicroCompact 单条工具结果截断回归：中文二分收敛 / 密度 / suffix 预留 / 二分早退。

覆盖用例（设计文档 compact-budget.design.md §6.5）：MIC-1 ~ MIC-5。
"""

from __future__ import annotations

import math

import pytest

from pandaren.memory.compaction.micro_compact import MicroCompactor, _content_to_text
from pandaren.memory.constants import MICROCOMPACT_TRUNCATED_SUFFIX


# ─────────────────────────────────────────────────────────────
# Fakes（不同字符-token 密度的注入估算器）
# ─────────────────────────────────────────────────────────────

class FakeMicroEstimator:
    """按 chars_per_token 折算 token；str 直接算，list 走 _content_to_text。"""

    def __init__(self, chars_per_token: float = 1.0):
        self.chars_per_token = chars_per_token

    def estimate(self, messages) -> int:
        total = 0
        for m in messages:
            content = m.get("content", "")
            text = content if isinstance(content, str) else _content_to_text(content)
            total += math.ceil(len(text) / self.chars_per_token)
        return total


class SuffixFixedEstimator:
    """suffix 固定 N token，其余文本按 1 字符 1 token。"""

    def __init__(self, suffix_tokens: int = 3):
        self.suffix_tokens = suffix_tokens

    def estimate(self, messages) -> int:
        total = 0
        for m in messages:
            content = m.get("content", "")
            text = content if isinstance(content, str) else _content_to_text(content)
            if text == MICROCOMPACT_TRUNCATED_SUFFIX:
                total += self.suffix_tokens
            else:
                total += len(text)
        return total


# ─────────────────────────────────────────────────────────────
# MIC-1：中文长结果二分收敛 ≤ cap（R10 核心回归）
# ─────────────────────────────────────────────────────────────

def test_chinese_long_result_converges_below_cap():
    compactor = MicroCompactor(
        single_result_max_tokens=20_000,
        token_estimator=FakeMicroEstimator(chars_per_token=1.0),
    )
    text = "汉" * 64_000
    result = compactor.truncate_single_result_if_needed(text)

    assert isinstance(result, str)
    # 蜕变关系：截断后 estimate ≤ cap（原 bug：64_000 → 64_015 越截越大）
    assert compactor._estimate_text(result) <= 20_000
    assert len(result) < len(text)
    assert result.endswith(MICROCOMPACT_TRUNCATED_SUFFIX)


# ─────────────────────────────────────────────────────────────
# MIC-2：英文/代码不同密度均 ≤ cap（R10）
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("chars_per_token, text", [
    (4.0, "a" * 400_000),                 # 英文 ≈ 4 chars/token
    (2.5, "x = x + 1\n" * 40_000),        # 代码 ≈ 2.5 chars/token
])
def test_dense_text_truncates_within_cap(chars_per_token, text):
    compactor = MicroCompactor(
        single_result_max_tokens=20_000,
        token_estimator=FakeMicroEstimator(chars_per_token=chars_per_token),
    )
    result = compactor.truncate_single_result_if_needed(text)

    assert isinstance(result, str)
    assert compactor._estimate_text(result) <= 20_000
    assert result.endswith(MICROCOMPACT_TRUNCATED_SUFFIX)


# ─────────────────────────────────────────────────────────────
# MIC-3：suffix token 预留（body_cap = cap − suffix_tokens）（R10）
# ─────────────────────────────────────────────────────────────

def test_suffix_tokens_reserved_from_body_cap():
    compactor = MicroCompactor(
        single_result_max_tokens=20_000,
        token_estimator=SuffixFixedEstimator(suffix_tokens=3),
    )
    text = "汉" * 100_000
    result = compactor.truncate_single_result_if_needed(text)

    assert isinstance(result, str)
    assert result.endswith(MICROCOMPACT_TRUNCATED_SUFFIX)

    body = result[: -len(MICROCOMPACT_TRUNCATED_SUFFIX)]
    body_tokens = compactor._estimate_text(body)
    suffix_tokens = compactor._estimate_text(MICROCOMPACT_TRUNCATED_SUFFIX)

    assert len(body) == 20_000 - 3            # body_cap == cap − suffix_tokens
    assert body_tokens <= 20_000 - 3
    assert body_tokens + suffix_tokens <= 20_000


# ─────────────────────────────────────────────────────────────
# MIC-4：_converge_prefix_length 二分极端与早退（R10）
# ─────────────────────────────────────────────────────────────

def test_converge_prefix_length_early_exit_and_tiny_cap():
    compactor = MicroCompactor(
        single_result_max_tokens=20_000,
        token_estimator=FakeMicroEstimator(chars_per_token=1.0),
    )

    assert compactor._converge_prefix_length("abc", 1_000) == 3  # 早退：全文 ≤ cap
    assert compactor._converge_prefix_length("abc", 1) == 1       # body_cap=1 极端


# ─────────────────────────────────────────────────────────────
# MIC-5：未超限原样返回 + list content 处理（R10 基线）
# ─────────────────────────────────────────────────────────────

def test_under_limit_returns_same_object():
    compactor = MicroCompactor(
        single_result_max_tokens=100,
        token_estimator=FakeMicroEstimator(chars_per_token=1.0),
    )
    content = "短文本"
    assert compactor.truncate_single_result_if_needed(content) is content


def test_list_content_over_limit_returns_str_within_cap():
    compactor = MicroCompactor(
        single_result_max_tokens=100,
        token_estimator=FakeMicroEstimator(chars_per_token=1.0),
    )
    content = [{"type": "text", "text": "a" * 500}, {"type": "image"}]
    result = compactor.truncate_single_result_if_needed(content)

    assert isinstance(result, str)
    assert compactor._estimate_text(result) <= 100
