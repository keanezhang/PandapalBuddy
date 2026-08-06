"""pandapal/dashboard/tests/test_degradation_aggregation.py — 降级 counter → 看板明细。

覆盖基类 `_build_degradations` / `_sum_counters` 的投影行为，以及**两条存储链路
（markdown / sqlite）口径严格一致**——后者是把归一契约从 `dict[name, float]` 改成
保 label 的 `CounterPoint` 之后最该守住的不变量：只要两态还共用同一个投影点，
它们的 degradations 就必须逐字段相等。

同时守住 `run_total{status=...}` 的分档：该特例从两个子类里删掉后，改由基类的
`_sum_counters(name, status=...)` 通用表达承担，不能因此丢档。
"""

from __future__ import annotations

from pathlib import Path

from pandaren.observability.backend.markdown import MarkdownMetricsBackend
from pandaren.observability.backend.sqlite import SQLiteMetricsBackend
from pandaren.observability.metrics import Metrics

from pandapal import degradation
from pandapal.dashboard.aggregator import DashboardAggregator
from pandapal.dashboard.sqlite_aggregator import SQLiteDashboardAggregator
from pandapal.degradation import DegradationEvent

_AGENT = "pandapal"


def _emit_fixture() -> None:
    """一批有代表性的降级 + 普通 counter（跨 severity / 同 code 多次 / 多 source）。"""
    r = degradation.report_degradation
    for _ in range(3):
        r(DegradationEvent.MODEL_UNPRICED, category="cost",
          source="llm_pricing.cost_of_call")
    r(DegradationEvent.MODEL_ID_MISSING_IN_RESUME, category="id", severity="abort",
      source="hitl_manager.resume", session_id="s1", run_id="r1")
    r(DegradationEvent.HITL_DECISION_MISSING, category="decision", severity="abort",
      source="hitl_manager.resume")
    r(DegradationEvent.BACKEND_UNAVAILABLE, category="capability",
      source="subsystem_registry.raw_log")
    m = degradation._metrics
    assert m is not None
    m.inc_run_total("success")
    m.inc_run_total("success")
    m.inc_run_total("failed")


def _build_sqlite(tmp_path: Path):
    backend = SQLiteMetricsBackend(tmp_path / "observability.db")
    degradation.set_metrics(Metrics(backend=backend, agent_id=_AGENT))
    _emit_fixture()
    backend.flush()
    return SQLiteDashboardAggregator(tmp_path / "pandapal.db").build()


def _build_markdown(tmp_path: Path):
    backend = MarkdownMetricsBackend(tmp_path)
    degradation.set_metrics(Metrics(backend=backend, agent_id=_AGENT))
    _emit_fixture()
    backend.flush()
    return DashboardAggregator(tmp_path).build()


def _reset() -> None:
    degradation.set_metrics(None)
    degradation._dedup_last_emit.clear()


def test_event_code_survives_to_snapshot(tmp_path: Path):
    """event_code 必须完整抵达快照——它是趋势下钻的唯一主键，丢了等于只剩总数。"""
    _reset()
    snap = _build_sqlite(tmp_path)
    by_code = {d.event_code: d for d in snap.degradations}
    assert set(by_code) == {
        "model_unpriced", "model_id_missing_in_resume",
        "hitl_decision_missing", "backend_unavailable",
    }
    # 同 event_code 多次 → 求和而非覆盖
    assert by_code["model_unpriced"].count == 3
    assert by_code["model_unpriced"].category == "cost"
    assert by_code["model_unpriced"].source == "llm_pricing.cost_of_call"
    _reset()


def test_sorted_by_severity_then_count(tmp_path: Path):
    """abort 类必须置顶——看板一眼要看到「拒绝放行」的那些。"""
    _reset()
    snap = _build_sqlite(tmp_path)
    sevs = [d.severity for d in snap.degradations]
    assert sevs[:2] == ["abort", "abort"], sevs
    # 同严重度内按次数降序：log_only 段里 model_unpriced(3) 在 backend_unavailable(1) 前
    log_only = [d for d in snap.degradations if d.severity == "log_only"]
    assert [d.count for d in log_only] == sorted(
        [d.count for d in log_only], reverse=True,
    )
    _reset()


def test_markdown_and_sqlite_agree(tmp_path: Path):
    """两条链路逐字段相等——共用同一投影点的核心不变量。"""
    _reset()
    sqlite_snap = _build_sqlite(tmp_path / "sq")
    _reset()
    md_snap = _build_markdown(tmp_path / "md")
    _reset()
    assert sqlite_snap.degradations == md_snap.degradations


def test_run_total_status_split_survives_refactor(tmp_path: Path):
    """run_total 按 status 分档不得因删掉子类特例而丢失（改由基类通用投影承担）。"""
    _reset()
    snap = _build_sqlite(tmp_path)
    assert snap.global_.run_total_success == 2
    assert snap.global_.run_total_failed == 1
    assert snap.global_.agent_id == _AGENT
    _reset()


def test_no_degradation_yields_empty_list(tmp_path: Path):
    """无降级时是空列表（正常态），不是 None、也不该伪造占位行。"""
    _reset()
    backend = SQLiteMetricsBackend(tmp_path / "observability.db")
    metrics = Metrics(backend=backend, agent_id=_AGENT)
    metrics.inc_run_total("success")  # 只有普通 counter，无 degradation
    backend.flush()
    snap = SQLiteDashboardAggregator(tmp_path / "pandapal.db").build()
    assert snap.degradations == []
    assert snap.global_.run_total_success == 1
    _reset()


def test_snapshot_to_dict_carries_degradations(tmp_path: Path):
    """to_dict() 必须带出 degradations 段——IPC 出站就是从它取值。"""
    _reset()
    snap = _build_sqlite(tmp_path)
    d = snap.to_dict()
    assert "degradations" in d
    assert len(d["degradations"]) == len(snap.degradations)
    assert d["degradations"][0]["event_code"]  # 序列化后 event_code 仍在
    _reset()
