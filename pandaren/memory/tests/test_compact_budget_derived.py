"""compact-budget.design.md §6.1 — 派生函数单测。

覆盖用例：DER-1 / DER-2 / DER-3 / DER-4
风险映射：R3 [P1] + inv-4（派生边界）
Oracle：golden value（公式白纸黑字，独立手算）+ property（parametrize 覆盖整类输入）。
纯函数零 mock。
"""

from __future__ import annotations

import pytest

from pandaren.memory.constants import (
    derive_buffer_tokens,
    derive_compact_threshold,
    derive_keep_window,
    derive_single_result_max_tokens,
)


# ─────────────────────────────────────────────────────────────
# DER-1: derive_buffer_tokens — min(5000, slot//4) 与 max(0, ...) 两分支
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "slot, expected",
    [
        (1_000_000, 5_000),  # 大窗口：buffer 饱和于 5000
        (8_000, 2_000),      # 中窗口：slot//4 生效
        (4, 1),              # 临界：4//4 == 1
        (3, 0),              # 临界：3//4 == 0
        (1, 0),              # 极小
        (0, 0),              # 零
        (-5, 0),             # 非法/负值：下限兜底 0
    ],
)
def test_derive_buffer_tokens_golden(slot: int, expected: int) -> None:
    assert derive_buffer_tokens(slot) == expected


@pytest.mark.parametrize(
    "slot",
    [-5, -1, 0, 1, 2, 3, 4, 5, 7, 8, 100, 8_000, 19_999, 20_000, 20_001,
     568_000, 1_000_000, 2_000_000],
)
def test_derive_buffer_tokens_bounds(slot: int) -> None:
    # inv-4: 0 ≤ buffer ≤ min(5000, max(0, slot)//4)
    upper = min(5_000, max(0, slot) // 4)
    assert 0 <= derive_buffer_tokens(slot) <= upper


# ─────────────────────────────────────────────────────────────
# DER-2: derive_compact_threshold — slot − buffer，下限 1
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "slot, expected",
    [
        (568_000, 563_000),  # 大窗口：buffer 饱和 5000
        (70_400, 65_400),    # 中窗口：buffer 5000
        (8_000, 6_000),      # 小窗口：buffer 2000
        (1, 1),              # 极小：下限 1
        (0, 1),              # 零：下限 1
    ],
)
def test_derive_compact_threshold_golden(slot: int, expected: int) -> None:
    assert derive_compact_threshold(slot) == expected


@pytest.mark.parametrize(
    "slot",
    [0, 1, 4, 8_000, 19_999, 20_000, 70_400, 568_000, 1_000_000, 2_000_000],
)
def test_derive_compact_threshold_property(slot: int) -> None:
    # inv-4: T == max(1, slot − derive_buffer_tokens(slot))，且 T ≥ 1
    expected = max(1, slot - max(0, min(5_000, slot // 4)))
    result = derive_compact_threshold(slot)
    assert result == expected
    assert result >= 1


# ─────────────────────────────────────────────────────────────
# DER-3: derive_keep_window — 边界与单调性
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "threshold, expected",
    [
        (563_000, (67_560, 253_350)),  # 1M 档 golden
        (65_400, (7_848, 29_430)),     # 中档
        (10, (1, 4)),                  # 极小：min 夹逼为 1
        (1, (1, 1)),                   # 极小：max 下限 1
    ],
)
def test_derive_keep_window_golden(threshold: int, expected: tuple[int, int]) -> None:
    assert derive_keep_window(threshold) == expected


@pytest.mark.parametrize(
    "threshold",
    [1, 2, 10, 100, 1_000, 8_000, 65_400, 563_000, 1_000_000, 2_000_000],
)
def test_derive_keep_window_property(threshold: int) -> None:
    # inv-4: 1 ≤ min_keep ≤ max_keep；max_keep == max(1, int(T×0.45))
    expected_max = max(1, int(threshold * 0.45))
    expected_min = max(1, min(int(threshold * 0.12), expected_max))
    assert derive_keep_window(threshold) == (expected_min, expected_max)
    assert 1 <= expected_min <= expected_max


def test_derive_keep_window_monotonic() -> None:
    # inv-4 单调性：threshold 增大时 min_keep / max_keep 均不下降
    thresholds = [1, 2, 10, 100, 1_000, 8_000, 65_400, 563_000, 1_000_000, 2_000_000]
    prev_min = prev_max = 0
    for t in thresholds:
        min_keep, max_keep = derive_keep_window(t)
        assert min_keep >= prev_min
        assert max_keep >= prev_max
        prev_min, prev_max = min_keep, max_keep


# ─────────────────────────────────────────────────────────────
# DER-4: derive_single_result_max_tokens — 夹逼 [8000, 30000]
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "threshold, expected",
    [
        (563_000, 30_000),  # ratio×T 超上限 → 30000
        (65_400, 9_810),    # 区间内
        (60_000, 9_000),    # 区间内
        (1_000, 8_000),     # 低于下限 → floor 8000
        (50_000, 8_000),    # ratio×T == 7500 < 8000 → floor 8000
    ],
)
def test_derive_single_result_max_tokens_golden(threshold: int, expected: int) -> None:
    assert derive_single_result_max_tokens(threshold) == expected


@pytest.mark.parametrize(
    "threshold",
    [1, 10, 1_000, 50_000, 53_333, 53_334, 60_000, 65_400, 200_000, 563_000,
     1_000_000],
)
def test_derive_single_result_max_tokens_property(threshold: int) -> None:
    # inv-4: 8000 ≤ cap ≤ 30000；cap == max(8000, min(int(T×0.15), 30000))
    expected = max(8_000, min(int(threshold * 0.15), 30_000))
    result = derive_single_result_max_tokens(threshold)
    assert result == expected
    assert 8_000 <= result <= 30_000
