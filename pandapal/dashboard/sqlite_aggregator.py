"""pandapal/dashboard/sqlite_aggregator.py — 只读扫描 SQLite → DashboardSnapshot（SQLite 源）。

storage_mode=sqlite 时的看板数据源。数据分落两个库（均为 user-scoped，同目录内聚）：
  - pandapal.db       ：sessions / session_groups / raw_log（会话元数据 + 对话日志）
  - observability.db  ：spans / audit_records / metrics_points / logs（四大观测支柱）

本模块只负责**从这两个库查询出归一结构**（形状与 markdown 源逐字段一致）；装配
（assistant 轮 ↔ llm_call 的 (run_id, step) join、费用精算、会话/全局聚合）复用基类
`BaseDashboardAggregator`——与 markdown 源共用同一套逻辑，两态结果口径严格一致。

只读、短连接：每次 build() 打开一次连接、查完即关。WAL 下独立读连接可见已提交数据，
故与 sidecar 正在持有的写连接并发安全（写侧每事务即 commit）。O3：解析容错，
单会话失败不拖垮整体，全局失败向上返回空快照由 handler 消化。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from pandapal.dashboard.aggregator import _parse_message_json, _extract_system_prompt
from pandapal.dashboard.base import BaseDashboardAggregator, CounterPoint, _f, _i, _rid
from pandapal.dashboard.models import (
    DashboardSnapshot,
    SessionData,
)

logger = logging.getLogger("pandapal.dashboard.aggregator")

# SpanType.value → 归一 kind（与 markdown _parse_traces 的 kind 对齐）
_SPAN_KIND = {"llm_call": "llm", "tool_call": "tool", "run": "run", "step": "step"}

_MAX_ROWS = 2000  # 单会话 raw_log/logs 读取安全上限，防极端 OOM


class SQLiteDashboardAggregator(BaseDashboardAggregator):
    """从 SQLite（pandapal.db + observability.db）聚合 → DashboardSnapshot。只读。"""

    def __init__(self, pandapal_db_path: str | Path, observability_db_path: str | Path | None = None) -> None:
        # pandapal_db_path = StorageManager._storage_path（sqlite 模式：.../users/{uid}/pandapal.db）
        self._pandapal_db = Path(pandapal_db_path)
        # observability.db 与 pandapal.db 同目录内聚（见 run_local._build_blueprint）
        self._obs_db = (
            Path(observability_db_path)
            if observability_db_path is not None
            else self._pandapal_db.parent / "observability.db"
        )

    # ── 连接工具（只读短连接；缺文件返回 None）───────────────────
    @staticmethod
    def _open(path: Path) -> sqlite3.Connection | None:
        if not path.is_file():
            return None
        try:
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error as exc:
            logger.warning("[sqlite-dashboard] open failed (%s): %s", path, exc)
            return None

    # ── 对外入口 ─────────────────────────────────────────────────
    def build(self) -> DashboardSnapshot:
        obs = self._open(self._obs_db)
        pan = self._open(self._pandapal_db)
        try:
            points, gauges, last_updated = self._load_metrics(obs)
            global_ = self._build_global_metrics(last_updated, points, gauges)
            degradations = self._build_degradations(points)
            groups = self._load_groups(pan)
            sessions: list[SessionData] = []
            for meta in self._load_sessions(pan):
                sid = meta.get("session_id", "")
                try:
                    spans = self._load_spans(obs, sid)
                    audit_fin = self._load_run_finish(obs, sid)
                    raw_turns = self._load_raw_turns(pan, sid)
                    system_prompt = self._load_system_prompt(obs, sid)
                    s = self._assemble_session(
                        meta, spans, audit_fin, raw_turns, system_prompt, groups,
                        fallback_id=sid,
                    )
                    if s is not None:
                        sessions.append(s)
                except Exception as exc:  # 单会话失败不拖垮整体
                    logger.warning("[sqlite-dashboard] aggregate session failed (%s): %s", sid, exc)
            sessions.sort(key=lambda s: s.created_at, reverse=True)
            return DashboardSnapshot(
                global_=global_, sessions=sessions, degradations=degradations,
            )
        finally:
            if obs is not None:
                obs.close()
            if pan is not None:
                pan.close()

    # ── metrics_points → 归一 counter 采样点 + gauge ──────────────
    def _load_metrics(
        self, obs: sqlite3.Connection | None,
    ) -> tuple[list[CounterPoint], dict[str, float], str]:
        """纯查询：metrics_points → (counter 采样点, gauge 最新值, last_updated)。

        **不做任何投影**——该看哪些指标、按哪些 label 切档全部交给基类，本方法只保证
        把 labels 原样带出（见 base.CounterPoint 的存在理由）。SQL 已按
        (name, labels_json) 分组求和，故每行天然就是一个 CounterPoint。
        """
        points: list[CounterPoint] = []
        gauges: dict[str, float] = {}
        last_updated = ""
        if obs is None:
            return points, gauges, last_updated
        try:
            for r in obs.execute(
                "SELECT name, labels_json, SUM(value) AS s FROM metrics_points "
                "WHERE kind = 'counter' GROUP BY name, labels_json"
            ).fetchall():
                points.append(CounterPoint(
                    name=r["name"],
                    labels=_loads(r["labels_json"]),
                    value=_f(r["s"]),
                ))

            # gauges：每 name 取最新值（MAX(id)）
            for r in obs.execute(
                "SELECT m.name, m.value FROM metrics_points m "
                "JOIN (SELECT name, MAX(id) AS mid FROM metrics_points "
                "      WHERE kind = 'gauge' GROUP BY name) g ON m.id = g.mid"
            ).fetchall():
                gauges[r["name"]] = _f(r["value"])

            row = obs.execute("SELECT MAX(ts) AS t FROM metrics_points").fetchone()
            last_updated = (row["t"] or "") if row else ""
        except sqlite3.Error as exc:
            logger.warning("[sqlite-dashboard] metrics query failed: %s", exc)
        return points, gauges, last_updated

    # ── session_groups → {group_id: name} ────────────────────────
    def _load_groups(self, pan: sqlite3.Connection | None) -> dict[str, str]:
        if pan is None:
            return {}
        try:
            return {
                r["id"]: (r["name"] or "")
                for r in pan.execute("SELECT id, name FROM session_groups").fetchall()
            }
        except sqlite3.Error as exc:
            logger.warning("[sqlite-dashboard] groups query failed: %s", exc)
            return {}

    # ── sessions → meta dict 列表（排除已删除）────────────────────
    def _load_sessions(self, pan: sqlite3.Connection | None) -> list[dict[str, Any]]:
        if pan is None:
            return []
        try:
            rows = pan.execute(
                "SELECT session_id, title, preview, created_at, last_active, "
                "       message_count, group_id "
                "FROM sessions WHERE is_deleted = 0"
            ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("[sqlite-dashboard] sessions query failed: %s", exc)
            return []
        return [dict(r) for r in rows]

    # ── spans → 归一 span 列表（形状同 markdown _parse_traces）────
    def _load_spans(self, obs: sqlite3.Connection | None, sid: str) -> list[dict[str, Any]]:
        if obs is None or not sid:
            return []
        rows = obs.execute(
            "SELECT span_type, name, status, run_id, step_n, duration_ms, attributes_json "
            "FROM spans WHERE session_id = ? ORDER BY start_time ASC, id ASC",
            (sid,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            kind = _SPAN_KIND.get(row["span_type"])
            if kind is None:
                continue
            attrs = _loads(row["attributes_json"])
            run = row["run_id"] or ""
            step = row["step_n"]
            status = _norm_status(row["status"])
            dur = _f(row["duration_ms"])
            if kind == "llm":
                out.append({
                    "kind": "llm", "run": run, "step": step, "status": status,
                    "model": attrs.get("model", ""),
                    "provider": attrs.get("provider", ""),
                    "input_tokens": _i(attrs.get("input_tokens", 0)),
                    "output_tokens": _i(attrs.get("output_tokens", 0)),
                    "cached_tokens": _i(attrs["cached_tokens"]) if "cached_tokens" in attrs else None,
                    "cache_hit_ratio": _f(attrs["cache_hit_ratio"]) if "cache_hit_ratio" in attrs else None,
                    "tool_calls_count": _i(attrs.get("tool_calls_count", 0)),
                    "duration_ms": dur,
                })
            elif kind == "tool":
                tool_name = attrs.get("tool_name") or (row["name"] or "").replace("tool.", "")
                out.append({
                    "kind": "tool", "run": run, "step": step,
                    "tool_name": tool_name, "status": status, "duration_ms": dur,
                })
            elif kind == "run":
                out.append({
                    "kind": "run", "run": run, "status": status,
                    "terminal_reason": attrs.get("terminal_reason", ""), "duration_ms": dur,
                })
            else:  # step
                out.append({"kind": "step", "run": run, "step": step})
        return out

    # ── audit_records(run_finished) → {run_id[:8]: detail} ───────
    def _load_run_finish(self, obs: sqlite3.Connection | None, sid: str) -> dict[str, str]:
        if obs is None or not sid:
            return {}
        out: dict[str, str] = {}
        for r in obs.execute(
            "SELECT run_id, detail FROM audit_records "
            "WHERE event_type = 'run_finished' AND session_id = ? ORDER BY id ASC",
            (sid,),
        ).fetchall():
            out[_rid(r["run_id"] or "")] = r["detail"] or ""  # 最后一次为准（升序覆盖）
        return out

    # ── raw_log → raw_turns（形状同 markdown _parse_raw_log）─────
    def _load_raw_turns(self, pan: sqlite3.Connection | None, sid: str) -> list[dict[str, Any]]:
        if pan is None or not sid:
            return []
        rows = pan.execute(
            "SELECT content_json, turn_index, created_at, run_id, step FROM raw_log "
            "WHERE session_id = ? AND entry_type = 'message' "
            "ORDER BY turn_index ASC LIMIT ?",
            (sid, _MAX_ROWS),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            raw = row["content_json"] or "{}"
            role = "unknown"
            try:
                role = (json.loads(raw).get("role") or "unknown")
            except (json.JSONDecodeError, TypeError):
                pass
            content, reasoning, tool_calls = _parse_message_json(raw)
            out.append({
                "turn": _i(row["turn_index"]),
                "role": role,
                "timestamp": row["created_at"] or "",
                "run_id": row["run_id"] or "",
                "step": row["step"],
                "content": content,
                "reasoning": reasoning,
                "tool_calls": tool_calls,
            })
        return out

    # ── logs → 生效系统提示词（首条含 messages 的 llm 日志）──────
    def _load_system_prompt(self, obs: sqlite3.Connection | None, sid: str) -> str:
        if obs is None or not sid:
            return ""
        try:
            rows = obs.execute(
                "SELECT extra_json FROM logs WHERE session_id = ? "
                "AND extra_json LIKE '%messages%' ORDER BY ts ASC, id ASC LIMIT ?",
                (sid, _MAX_ROWS),
            ).fetchall()
            for r in rows:
                obj = _loads(r["extra_json"])
                sp = _extract_system_prompt(obj)
                if sp:
                    return sp
        except sqlite3.Error as exc:
            logger.warning("[sqlite-dashboard] system_prompt query failed (%s): %s", sid, exc)
        return ""


# ── 局部工具 ─────────────────────────────────────────────────────
def _loads(s: str | None) -> dict[str, Any]:
    if not s:
        return {}
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _norm_status(status: str | None) -> str:
    """SpanStatus.value → 归一 ok/cancelled/error（与 markdown 判定同）。"""
    s = status or ""
    if "ok" in s:
        return "ok"
    if "cancelled" in s:
        return "cancelled"
    return "error"
