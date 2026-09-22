"""压缩派生单测（``CompactionProfile`` 方法）。

覆盖：max_keep / min_keep / single_result / target 四个派生。
Oracle：golden value（公式白纸黑字）+ property（parametrize 覆盖整类输入）。
纯函数零 mock。
"""

from __future__ import annotations

import logging

import pytest

from pandaren.memory.constants import (
    BALANCED,
    GENTLE,
    TIGHT,
    TOOL_RESULT_CAP_FLOOR,
    TOOL_RESULT_CAP_MAX,
)


# ─────────────────────────────────────────────────────────────
# DER-1: max_keep_tokens = max(1, int(T × max_keep_ratio))
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "T, expected",
    [
        (0, 1),
        (1, 1),
        (10, 4),            # int(10 × 0.45) = 4
        (1_000, 450),
        (772_297, 347_533),  # 1M golden：int(772297 × 0.45)
    ],
)
def test_max_keep_tokens_golden(T: int, expected: int) -> None:
    assert BALANCED.max_keep_tokens(T) == expected


@pytest.mark.parametrize("T", [0, 1, 2, 100, 1_000, 74_697, 772_297, 2_000_000])
def test_max_keep_tokens_property(T: int) -> None:
    assert BALANCED.max_keep_tokens(T) == max(1, int(T * BALANCED.max_keep_ratio))
    assert BALANCED.max_keep_tokens(T) >= 1


# ─────────────────────────────────────────────────────────────
# DER-2: min_keep_tokens = max(1, min(int(T × min_keep_ratio), max_keep))
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "T, expected",
    [
        (1, 1),
        (10, 1),             # min(int(10 × 0.12)=1, max_keep=4) = 1
        (1_000, 120),
        (772_297, 92_675),   # 1M golden：int(772297 × 0.12)
    ],
)
def test_min_keep_tokens_golden(T: int, expected: int) -> None:
    assert BALANCED.min_keep_tokens(T) == expected


@pytest.mark.parametrize("T", [1, 2, 100, 1_000, 74_697, 772_297, 2_000_000])
def test_min_keep_tokens_property(T: int) -> None:
    expected = max(1, min(int(T * BALANCED.min_keep_ratio), BALANCED.max_keep_tokens(T)))
    got = BALANCED.min_keep_tokens(T)
    assert got == expected
    assert 1 <= got <= BALANCED.max_keep_tokens(T)


def test_keep_window_monotonic() -> None:
    thresholds = [1, 2, 10, 100, 1_000, 8_000, 74_697, 772_297, 2_000_000]
    prev_min = prev_max = 0
    for T in thresholds:
        min_keep = BALANCED.min_keep_tokens(T)
        max_keep = BALANCED.max_keep_tokens(T)
        assert min_keep >= prev_min
        assert max_keep >= prev_max
        prev_min, prev_max = min_keep, max_keep


# ─────────────────────────────────────────────────────────────
# DER-3: single_result_max_tokens = clamp(int(T × 0.15), 8_000, 30_000)
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "T, expected",
    [
        (1_000, TOOL_RESULT_CAP_FLOOR),      # ratio×T 低于下限 → floor
        (50_000, TOOL_RESULT_CAP_FLOOR),     # int(50000×0.15)=7500 < 8000 → floor
        (60_000, 9_000),                     # 区间内
        (74_697, 11_204),
        (772_297, TOOL_RESULT_CAP_MAX),      # 超上限 → max
    ],
)
def test_single_result_max_tokens_golden(T: int, expected: int) -> None:
    assert BALANCED.single_result_max_tokens(T) == expected


@pytest.mark.parametrize(
    "T",
    [1, 10, 1_000, 50_000, 53_333, 53_334, 60_000, 74_697, 200_000, 772_297, 1_000_000],
)
def test_single_result_max_tokens_property(T: int) -> None:
    got = BALANCED.single_result_max_tokens(T)
    assert got == max(TOOL_RESULT_CAP_FLOOR, min(int(T * 0.15), TOOL_RESULT_CAP_MAX))
    assert TOOL_RESULT_CAP_FLOOR <= got <= TOOL_RESULT_CAP_MAX


# ─────────────────────────────────────────────────────────────
# DER-4: target_tokens = int(T × target_ratio) − 各项固定开销
# ─────────────────────────────────────────────────────────────


def test_target_tokens_golden_1m() -> None:
    """1M 口径：SPEC §5 的 target_tokens = 520,404。"""
    got = BALANCED.target_tokens(
        772_297,
        system_overhead=19_203,
        old_attachments=0,
        reserved_summary=1_000,
        reinject=0,
    )
    assert got == 540_607 - 19_203 - 1_000
    assert got == 520_404


@pytest.mark.parametrize("T", [1, 1_000, 74_697, 772_297, 2_000_000])
def test_target_tokens_property(T: int) -> None:
    got = BALANCED.target_tokens(
        T,
        system_overhead=100,
        old_attachments=50,
        reserved_summary=1_000,
        reinject=200,
    )
    assert got == int(T * BALANCED.target_ratio) - 100 - 50 - 1_000 - 200


# ─────────────────────────────────────────────────────────────
# DER-5: keep_window —— I4 / I5 收敛（「收敛到最近合法值 + WARNING」，SPEC §4）
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("profile", [GENTLE, BALANCED, TIGHT])
@pytest.mark.parametrize("T", [74_697, 772_297])
def test_keep_window_no_convergence_in_normal_window(profile, T, caplog):
    """预置档正常窗口下不触发收敛（max_keep_ratio < target_ratio），且不告警。"""
    with caplog.at_level(logging.WARNING, logger="pandaren.memory.constants"):
        min_keep, max_keep = profile.keep_window(
            T, sys_acct=19_203, target_ceiling=int(T * profile.target_ratio)
        )

    assert (min_keep, max_keep) == (
        profile.min_keep_tokens(T),
        profile.max_keep_tokens(T),
    )
    assert not caplog.records


def test_keep_window_i5_converges_when_sys_acct_squeezes_T(caplog):
    """I5: min_keep + sys_acct > T → 收到 T − sys_acct（否则压缩后仍超 T，反复触发）。"""
    T, sys_acct = 10_000, 9_500
    with caplog.at_level(logging.WARNING, logger="pandaren.memory.constants"):
        min_keep, _max_keep = BALANCED.keep_window(
            T, sys_acct=sys_acct, target_ceiling=7_000
        )

    assert min_keep == T - sys_acct == 500
    assert "I5" in caplog.text


def test_keep_window_i4_converges_max_keep_to_target(caplog):
    """I4: max_keep > max(target, min_keep) → 收到该上界。"""
    T = 100_000
    with caplog.at_level(logging.WARNING, logger="pandaren.memory.constants"):
        min_keep, max_keep = BALANCED.keep_window(
            T, sys_acct=0, target_ceiling=1_000
        )

    assert min_keep == BALANCED.min_keep_tokens(T) == 12_000
    assert max_keep == max(1_000, min_keep)
    assert "I4" in caplog.text


def test_keep_window_interval_never_empty():
    """任意极端输入下都返回合法的非空区间（I4 的结构保证）。"""
    for T in (1, 10, 1_000, 74_697):
        for sys_acct in (0, T // 2, T, T * 2):
            for ceiling in (0, -5_000, T):
                min_keep, max_keep = BALANCED.keep_window(
                    T, sys_acct=sys_acct, target_ceiling=ceiling
                )
                assert 1 <= min_keep <= max_keep


# ─────────────────────────────────────────────────────────────
# 档位方向：GENTLE 保留最多、TIGHT 保留最少
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("T", [1_000, 74_697, 772_297])
def test_profile_direction(T: int) -> None:
    assert GENTLE.min_keep_tokens(T) >= BALANCED.min_keep_tokens(T)
    assert BALANCED.min_keep_tokens(T) >= TIGHT.min_keep_tokens(T)
    assert GENTLE.max_keep_tokens(T) >= BALANCED.max_keep_tokens(T)
    assert BALANCED.max_keep_tokens(T) >= TIGHT.max_keep_tokens(T)
    assert GENTLE.target_tokens(
        T, system_overhead=0, old_attachments=0, reserved_summary=0, reinject=0
    ) >= TIGHT.target_tokens(
        T, system_overhead=0, old_attachments=0, reserved_summary=0, reinject=0
    )
