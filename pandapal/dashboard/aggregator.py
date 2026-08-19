"""pandapal/dashboard/aggregator.py — 只读扫描 pandapal_md → DashboardSnapshot（Markdown 源）。

本模块只负责**从 markdown 文件解析出归一结构**；装配（assistant 轮 ↔ llm_call 的
(run_id, step) 精确 join + run 内顺序补配、费用精算、会话/全局聚合）由基类
`BaseDashboardAggregator` 统一承担，与 sqlite 源共用同一套逻辑，杜绝两套实现漂移。

字段出处/口径见 docs/prd/dashboard/dashboard-需求设计.md §3。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from pandapal.dashboard.base import BaseDashboardAggregator, CounterPoint, _f, _i, _rid
from pandapal.dashboard.models import (
    DashboardSnapshot,
    SessionData,
    ToolCall,
)

logger = logging.getLogger("pandapal.dashboard.aggregator")

# traces 属性单元：`key`=value
_ATTR_RE = re.compile(r"`(\w+)`=([^,]+)")
# metrics 指标行：| name{labels} | value |
_METRIC_RE = re.compile(r"^\|\s*([a-z_]+)\{([^}]*)\}\s*\|\s*([\d.]+)\s*\|")
# raw_log 字段行
_FIELD_RE = re.compile(r"^-\s+\*\*(\w+)\*\*:\s*(.*)$")


class DashboardAggregator(BaseDashboardAggregator):
    """扫描单个用户目录（markdown 存储根）→ DashboardSnapshot。只读，不修改任何文件。"""

    def __init__(self, user_dir: str | Path) -> None:
        # user_dir = {data_dir}/pandapal_md/users/{user_id}
        self._dir = Path(user_dir)

    # ── 对外入口 ─────────────────────────────────────────────────
    def build(self) -> DashboardSnapshot:
        points, gauges, last_updated = self._parse_metrics(self._dir / "metrics.md")
        global_ = self._build_global_metrics(last_updated, points, gauges)
        degradations = self._build_degradations(points)
        groups = self._parse_groups(self._dir / "session_groups")
        sessions: list[SessionData] = []
        sessions_root = self._dir / "sessions"
        if sessions_root.is_dir():
            for sdir in sorted(sessions_root.iterdir()):
                if not sdir.is_dir():
                    continue
                try:
                    meta = self._parse_frontmatter(sdir / "session.md")
                    if not meta:
                        continue
                    spans = self._parse_traces(sdir / "traces.md")
                    audit_fin = self._parse_run_finish(sdir / "audit.md")
                    raw_turns = self._parse_raw_log(sdir / "raw_log.md")
                    system_prompt, tools_schema = self._parse_system_prompt(sdir / "logs.md")
                    s = self._assemble_session(
                        meta, spans, audit_fin, raw_turns, system_prompt, groups,
                        tools_schema=tools_schema,
                        fallback_id=sdir.name,
                    )
                    if s is not None:
                        sessions.append(s)
                except Exception as exc:  # 单会话失败不拖垮整体
                    logger.warning("aggregate session failed (%s): %s", sdir.name, exc)
        # 按创建时间倒序（新会话在前）
        sessions.sort(key=lambda s: s.created_at, reverse=True)
        return DashboardSnapshot(
            global_=global_, sessions=sessions, degradations=degradations,
        )

    # ── metrics.md ───────────────────────────────────────────────
    def _parse_metrics(
        self, path: Path,
    ) -> tuple[list[CounterPoint], dict[str, float], str]:
        """纯解析：metrics.md → (counter 采样点, gauge 最新值, last_updated)。

        **不做任何投影**——该看哪些指标、按哪些 label 切档全部交给基类，本方法只保证
        把 labels 原样带出（见 base.CounterPoint 的存在理由）。
        """
        points: list[CounterPoint] = []
        gauges: dict[str, float] = {}
        last_updated = ""
        if not path.is_file():
            return points, gauges, last_updated
        section = ""
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("> Last updated:"):
                last_updated = line.split(":", 1)[1].strip()
                continue
            if line.startswith("## "):
                section = line[3:].strip().lower()
                continue
            m = _METRIC_RE.match(line)
            if not m:
                continue
            name, labels, value = m.group(1), m.group(2), m.group(3)
            lm = dict(kv.split("=", 1) for kv in labels.split(",") if "=" in kv)
            val = _f(value)
            if section.startswith("counter"):
                points.append(CounterPoint(name=name, labels=lm, value=val))
            elif section.startswith("gauge"):
                gauges[name] = val
        return points, gauges, last_updated

    # ── session_groups/*.md → {group_id: name} ───────────────────
    def _parse_groups(self, groups_dir: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        if not groups_dir.is_dir():
            return out
        for f in groups_dir.glob("*.md"):
            fm = self._parse_frontmatter(f)
            if fm and fm.get("id"):
                out[fm["id"]] = fm.get("name", "")
        return out

    # ── raw_log.md → raw_turns ───────────────────────────────────
    def _parse_raw_log(self, path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        text = path.read_text(encoding="utf-8")
        out: list[dict[str, Any]] = []
        # 只按**真正的** Turn 头（行首 `## Turn N` / `## [Compact] Turn N`）切块，
        # 跳过 compact 边界。可读展示正文里的 markdown 标题（如技能全文的 `## 概述`）
        # 不参与切分——否则 message_json 块会与 Turn 头分离而整条丢失
        # （与 markdown_raw_log_backend._parse_sections 同口径）。
        headers = list(re.finditer(r"^## (\[Compact\] )?Turn (\d+)\s*$", text, re.MULTILINE))
        for i, mt in enumerate(headers):
            if mt.group(1):  # [Compact] 压缩边界，无 message
                continue
            turn_index = int(mt.group(2))
            start = mt.end()
            end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
            blk = text[start:end]
            fields: dict[str, str] = {}
            for line in blk.splitlines():
                fm = _FIELD_RE.match(line)
                if fm:
                    fields[fm.group(1)] = fm.group(2).strip()
            # message_json（解析真相源）
            jm = re.search(r"\*\*message_json\*\*:\s*```json\n(.*?)\n```", blk, re.S)
            content, reasoning, tool_calls = "", None, []
            if jm:
                content, reasoning, tool_calls = _parse_message_json(jm.group(1))
            step_raw = fields.get("step", "")
            out.append(
                {
                    "turn": turn_index,
                    "role": fields.get("role", "unknown"),
                    "timestamp": fields.get("timestamp", ""),
                    "run_id": fields.get("run_id", ""),
                    "step": int(step_raw) if step_raw.isdigit() else None,
                    "content": content,
                    "reasoning": reasoning,
                    "tool_calls": tool_calls,
                }
            )
        out.sort(key=lambda t: t["turn"])
        return out

    # ── traces.md → spans ────────────────────────────────────────
    def _parse_traces(self, path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.startswith("| ") or line.startswith("| 时间") or line.startswith("|---"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 9:
                continue
            type_cell, name_cell, status_cell = cells[1], cells[2], cells[3]
            reason_cell = cells[4]  # 结束原因（terminal_reason，run span 才有）
            step_cell, run_cell, attr_cell = cells[6], cells[7], cells[8]
            attrs = {k: v.strip() for k, v in _ATTR_RE.findall(attr_cell)}
            status = "ok" if "ok" in status_cell else ("cancelled" if "cancelled" in status_cell else "error")
            step = int(step_cell) if step_cell.isdigit() else None
            run = run_cell.strip("`")
            dur = _f(attrs.get("duration_ms", "")) or _f(cells[5].strip("*"))

            if "llm_call" in type_cell:
                out.append({
                    "kind": "llm", "run": run, "step": step, "status": status,
                    "model": attrs.get("model", ""),
                    "provider": attrs.get("provider", ""),
                    "input_tokens": _i(attrs.get("input_tokens", "0")),
                    "output_tokens": _i(attrs.get("output_tokens", "0")),
                    "cached_tokens": _i(attrs["cached_tokens"]) if "cached_tokens" in attrs else None,
                    "cache_hit_ratio": _f(attrs["cache_hit_ratio"]) if "cache_hit_ratio" in attrs else None,
                    "tool_calls_count": _i(attrs.get("tool_calls_count", "0")),
                    "duration_ms": dur,
                    # markdown 只写 HH:MM:SS 无日期，无法可靠转本地时区 → None → 保守高峰价
                    "start_time": None,
                })
            elif "tool_call" in type_cell:
                out.append({"kind": "tool", "run": run, "step": step,
                            "tool_name": attrs.get("tool_name", name_cell.strip("`").replace("tool.", "")),
                            "status": status,
                            "duration_ms": dur})
            elif "run" in type_cell:
                out.append({"kind": "run", "run": run, "status": status,
                            "terminal_reason": reason_cell, "duration_ms": dur})
            elif "step" in type_cell:
                out.append({"kind": "step", "run": run, "step": step})
        return out

    # ── audit.md → run_finished 原因 {run_id[:8]: detail} ────────
    def _parse_run_finish(self, path: Path) -> dict[str, str]:
        """run 结束原因（权威）：detail 以 "Completed" 开头视为成功，否则为失败/终止原因。"""
        out: dict[str, str] = {}
        if not path.is_file():
            return out
        for line in path.read_text(encoding="utf-8").splitlines():
            if "run_finished" not in line:
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            # 列：时间 级别 事件 Agent Session Run Step 详情
            if len(cells) >= 8:
                run = cells[5].strip("`")
                out[_rid(run)] = cells[7]  # 最后一次 run_finished 为准
        return out

    # ── logs.md → 生效系统提示词 + 生效工具 schema（纯读，不改数据层）─
    def _parse_system_prompt(self, path: Path) -> tuple[str, list[dict]]:
        """从 logs.md 首个 llm 调用日志的 messages[system] 提取系统提示词，
        同时取该行的 tools_schema（生效工具 schema，同一 extra_json）。"""
        if not path.is_file():
            return "", []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                # extra JSON 开头可能是 {"messages":（旧格式，log_id 不在 extra）
                # 或 {"log_id":（新格式，log_id 是 record 首键）；两者都兼容。
                idx = line.find('{"log_id"')
                if idx < 0:
                    idx = line.find('{"messages"')
                if idx < 0:
                    continue
                frag = line[idx:].rstrip().rstrip("|").rstrip()
                frag = frag.replace("\\|", "|")  # 还原 markdown 表格转义
                try:
                    obj = json.loads(frag)
                except json.JSONDecodeError:
                    end = frag.rfind("}")
                    if end < 0:
                        continue
                    try:
                        obj = json.loads(frag[: end + 1])
                    except json.JSONDecodeError:
                        continue
                sp = _extract_system_prompt(obj)
                if sp:
                    ts = obj.get("tools_schema")
                    return sp, ts if isinstance(ts, list) else []
        except Exception as exc:  # 纯读容错，绝不拖垮聚合
            logger.warning("parse system_prompt failed (%s): %s", path.parent.name, exc)
        return "", []

    # ── frontmatter（--- { json } ---）───────────────────────────
    def _parse_frontmatter(self, path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---", text, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return None


# ── 共享解析工具（markdown / sqlite raw_log 均从 message_json 提取正文）──────
def _parse_message_json(raw: str) -> tuple[str, str | None, list[ToolCall]]:
    """解析一条 MessageDict JSON → (content, reasoning, tool_calls)。两条链路共用。"""
    content, reasoning, tool_calls = "", None, []
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return content, reasoning, tool_calls
    c = obj.get("content")
    content = c if isinstance(c, str) else (json.dumps(c, ensure_ascii=False) if c else "")
    reasoning = obj.get("reasoning_content")
    for tc in obj.get("tool_calls") or []:
        fn = tc.get("function", tc)
        try:
            args = json.loads(fn.get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            args = {}
        tool_calls.append(ToolCall(name=fn.get("name", ""), args=args))
    return content, reasoning, tool_calls


def _extract_system_prompt(obj: dict[str, Any]) -> str:
    """从 {"messages":[...]} 里取 system 消息正文。"""
    for m in obj.get("messages", []):
        if m.get("role") == "system":
            return m.get("content", "") or ""
    return ""
