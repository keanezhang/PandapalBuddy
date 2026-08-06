"""pandapal/tests/test_degradation.py — 统一降级通道（§5）行为测试。

覆盖：双写（log + counter）、event_code/labels 正确、去重防刷屏、
facade 未注入/后端打点失败均不向业务抛。

注入的是真的 `Metrics` facade（只把最底下的 backend 换成 fake），故这些用例同时
守住「facade 补 agent_id」与「facade 消化后端异常」两条依赖——不是 mock 出来的假绿灯。
"""

from __future__ import annotations

import logging

import pytest
from pandaren.observability.metrics import Metrics

from pandapal import degradation
from pandapal.degradation import (
    DEGRADATION_COUNTER_NAME,
    DegradationEvent,
    report_degradation,
)

_AGENT_ID = "test-agent"


class _FakeMetricsBackend:
    """记录 record_counter 调用，供断言。"""

    def __init__(self) -> None:
        self.counters: list[tuple[str, int, dict]] = []

    def record_counter(self, name: str, value: int, labels: dict) -> None:
        self.counters.append((name, value, labels))

    def record_histogram(self, name, value, labels) -> None: ...
    def record_gauge(self, name, value, labels) -> None: ...
    def flush(self) -> None: ...


class _RaisingBackend(_FakeMetricsBackend):
    def record_counter(self, name, value, labels) -> None:
        raise RuntimeError("backend down")


def _inject(backend) -> _FakeMetricsBackend:
    """把 fake backend 包进真 facade 注入，返回 backend 供断言。"""
    degradation.set_metrics(Metrics(backend=backend, agent_id=_AGENT_ID))
    return backend


@pytest.fixture(autouse=True)
def _reset_channel():
    """每个用例重置注入的 facade 与去重表，避免相互污染。"""
    degradation.set_metrics(None)
    degradation._dedup_last_emit.clear()
    yield
    degradation.set_metrics(None)
    degradation._dedup_last_emit.clear()


def test_double_write_log_and_counter(caplog):
    backend = _inject(_FakeMetricsBackend())

    with caplog.at_level(logging.WARNING, logger="pandapal.degradation"):
        report_degradation(
            DegradationEvent.MODEL_ID_MISSING_IN_RESUME,
            category="id", severity="abort", source="hitl_manager.resume",
            expected="concrete model_id", fallback=None,
            session_id="s1", run_id="r1",
        )

    # counter：name=degradation，+1，labels 含四个低基数维度 + facade 补的 agent_id
    assert backend.counters == [
        (DEGRADATION_COUNTER_NAME, 1, {
            "agent_id": _AGENT_ID,
            "event_code": "model_id_missing_in_resume",
            "category": "id",
            "source": "hitl_manager.resume",
            "severity": "abort",
        }),
    ]
    # log：event_code 作 message 主键 + 结构化字段
    rec = next(r for r in caplog.records if r.name == "pandapal.degradation")
    assert rec.getMessage() == "model_id_missing_in_resume"
    assert rec.event_code == "model_id_missing_in_resume"
    assert rec.category == "id"
    assert rec.severity == "abort"
    assert rec.session_id == "s1" and rec.run_id == "r1"


def test_event_code_is_a_counter_label():
    """event_code 必须进 labels——否则看板只能得到一个无法下钻的总数（本次返工的根因）。"""
    backend = _inject(_FakeMetricsBackend())
    report_degradation(
        DegradationEvent.MODEL_UNPRICED, category="cost",
        source="llm_pricing.cost_of_call",
    )
    _, _, labels = backend.counters[0]
    assert labels["event_code"] == DegradationEvent.MODEL_UNPRICED


def test_counter_labels_exclude_high_cardinality():
    backend = _inject(_FakeMetricsBackend())
    report_degradation(
        DegradationEvent.HITL_DECISION_MISSING, category="decision", severity="abort",
        source="hitl_manager.resume", session_id="sess-xyz", run_id="run-xyz",
    )
    _, _, labels = backend.counters[0]
    # session_id/run_id 绝不能进 labels（会撑爆时间序列）
    assert "session_id" not in labels and "run_id" not in labels


def test_dedup_suppresses_repeat_within_window():
    backend = _inject(_FakeMetricsBackend())
    for _ in range(5):
        report_degradation(
            DegradationEvent.BACKEND_UNAVAILABLE, category="capability",
            source="subsystem_registry.raw_log_backend",
            dedup_key="raw_log_backend", exc_info=False,
        )
    # 同 dedup_key 窗口内只落一次
    assert len(backend.counters) == 1


def test_no_metrics_injected_does_not_raise(caplog):
    # 未注入 facade：counter 为 no-op，log 仍写，绝不抛
    with caplog.at_level(logging.WARNING, logger="pandapal.degradation"):
        report_degradation(
            DegradationEvent.JWT_USER_ID_PARSE_FAILED, category="id",
            source="gateway.extract_user_id", fallback="",
        )
    assert any(r.name == "pandapal.degradation" for r in caplog.records)


def test_backend_failure_does_not_propagate():
    # backend.record_counter 抛异常 → 由 facade 消化，不得反噬业务
    _inject(_RaisingBackend())
    report_degradation(
        DegradationEvent.PLAN_ACTION_MISSING, category="decision", severity="abort",
        source="plan_manager.resume",
    )  # 不抛即通过
