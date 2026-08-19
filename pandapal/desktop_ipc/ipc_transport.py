"""pandapal.desktop_ipc.ipc_transport — IpcStdoutTransport。

★ 关键设计（5.2.G）：
1. send() 是唯一入口，所有 NormalizedEvent 必经此处
2. ★ Transport 不做任何"事件配对"或"副作用拼接"——
   "HITL_REQUEST 之前先发 REPLY_END" 的配对是 Scheduler 转换层
   的责任，不是 Transport 的事。Transport 只负责"1 个 event → 1 个 IPC 消息"。
3. _to_ipc_schema() 是唯一序列化点（其他模块不允许直接拼 IPC 字符串）
4. O3 Never Throw：所有异常必须内部消化
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from typing import Any

from pandapal.broadcast.transport import Transport
from pandapal.events.normalized import (
    EVENT_SCOPE_KEY,
    SCOPE_GLOBAL,
    EventType,
    NormalizedEvent,
)
from pandapal.desktop_ipc.message_codec import IpcMessageType

logger = logging.getLogger(__name__)

# 会话级离散事件：前端必须按 session_id 分桶，缺失即无声串台/丢失。
# （TASK_NOTIFICATION / PONG / SESSION_CONCURRENCY 等全局或带自身 session 字段的不在内。）
_SESSION_SCOPED_EVENTS = frozenset({
    EventType.REPLY_START, EventType.REPLY_END,
    EventType.LLM_TOKEN, EventType.REASONING_TOKEN,
    EventType.TOOL_START, EventType.TOOL_END,
    EventType.HITL_REQUEST, EventType.INTERACTION_REQUEST,
    EventType.PLAN_APPROVAL_REQUEST, EventType.PERMISSION_DENIED,
    EventType.AGENT_HALTED, EventType.USER_INPUT_ECHO,
    EventType.AGENT_REPLY, EventType.ERROR,
})


class IpcStdoutTransport(Transport):
    """把 NormalizedEvent 转为 IPC:JSON 写 stdout 的 Transport。"""

    def __init__(self) -> None:
        self._closed = False
        self._started = False  # ★ Transport 契约字段
        # 写 stdout 用 _write_lock 保护（多协程并发场景）
        self._write_lock = threading.Lock()

    @property
    def is_started(self) -> bool:
        """★ Transport 契约：start() 后为 True，stop() 后为 False。"""
        return self._started

    async def start(self) -> None:
        """IPC 是 stdin/stdout，启动时无需握手。

        ★ 根本解（2026-06-10）后：start() 不再写 PANDAPAL_READY。
        那个信号是 sidecar 协议层的 side effect（且可能与 run_local.py 重复），
        不属于 Transport 协议的职责。PANDAPAL_READY 的写入由 PandaPalApp 启动流程管理。
        """
        if self._started:
            return  # 幂等
        self._closed = False
        self._started = True
        logger.info("IpcStdoutTransport started")

    async def stop(self) -> None:
        """关闭 transport。"""
        if not self._started:
            return  # 幂等
        self._closed = True
        self._started = False
        logger.info("IpcStdoutTransport stopped")

    async def send(self, event: NormalizedEvent) -> None:
        """唯一的 IPC 发送入口。

        ★ 设计：不做任何"先关流再发"的事件配对。
        一条 event 对应一条 IPC 消息。
        如果需要"先发 REPLY_END 再发 HITL_REQUEST"，
        那是 Scheduler 转换层应该 yield 两条 event 的事。
        """
        if self._closed:
            return

        try:
            ipc_msg = self._to_ipc_schema(event)
            self._write_ipc(ipc_msg)
            # DEBUG: 确认 INTERACTION_REQUEST / HITL_REQUEST / QUICK_APP_DATA 被发送
            if event.event_type in (EventType.INTERACTION_REQUEST, EventType.HITL_REQUEST, EventType.QUICK_APP_DATA):
                logger.info(
                    "[IPC] SENT %s: reply_id=%s run_id=%s payload_keys=%s",
                    event.event_type.value, event.reply_id, event.run_id,
                    list(event.payload.keys()) if event.payload else [],
                )
        except Exception as e:
            # O3 Never Throw：transport 错误必须消化
            logger.exception("IpcStdoutTransport.send failed: %s", e)

    def _to_ipc_schema(self, event: NormalizedEvent) -> dict[str, Any]:
        """NormalizedEvent → IPC:JSON 格式转换。

        这里是 IPC 协议的唯一真相源。所有 IPC 字段名在此处定义。
        前端 types/api.ts 中的 ApiMessageType 枚举值必须与此处的 IpcMessageType 保持一致。
        """
        p = event.payload
        t = event.event_type

        # 通用字段（所有事件都有）
        base: dict[str, Any] = {
            "msg_id":    event.msg_id,
            "timestamp": event.timestamp,
        }
        if event.reply_id:
            base["reply_id"] = event.reply_id
        if event.reply_scope:
            base["reply_scope"] = event.reply_scope
        if event.run_id:
            base["run_id"] = event.run_id
        # ★ v003：透出 session_id 到消息头（若 payload 里有），
        # 前端按 session_id 分发流事件到对应 buffer。
        if isinstance(p, dict) and p.get("session_id"):
            base["session_id"] = p["session_id"]
        # ★ 护栏：会话级离散事件必须携带 session_id，否则前端无法分桶 → 静默串台/丢失。
        #   这里不改变行为（仍照常发出），只把「漏 stamp」的源头暴露成 warning 便于定位。
        #   例外：显式声明 scope=global 的事件（如 dashboard/budget 全局错误）是「明确不带」，
        #   不属于任何会话，豁免告警（SESSION_ID 契约 §八 #4 的显式二分）。
        elif t in _SESSION_SCOPED_EVENTS and not (
            isinstance(p, dict) and p.get(EVENT_SCOPE_KEY) == SCOPE_GLOBAL
        ):
            logger.warning(
                "[IpcTransport] 会话级事件 %s 缺 session_id，前端将无法分桶（可能串台）："
                "reply_id=%s run_id=%s",
                getattr(t, "name", t), event.reply_id, event.run_id,
            )

        # 事件类型映射
        if t == EventType.LLM_TOKEN:
            return {
                "type": IpcMessageType.TOKEN, **base,
                "token": p.get("delta", ""),
                "snapshot": p.get("snapshot", ""),
            }
        if t == EventType.REASONING_TOKEN:
            return {
                "type": IpcMessageType.REASONING_TOKEN, **base,
                "token": p.get("delta", ""),
                "snapshot": p.get("snapshot", ""),
            }
        if t == EventType.REPLY_START:
            return {"type": IpcMessageType.REPLY_START, **base}
        if t == EventType.REPLY_END:
            # usage：本 run 完整用量+费用汇总（net_cost/tokens 明细/命中率/耗时，应用层
            # CostBudgetGuard.summary 精算）。前端在回复末尾直接展示，不重算。
            # 缺失（无 guard / 本 run 无 LLM 调用）→ 省略字段，前端降级不显示。
            out = {
                "type": IpcMessageType.REPLY_END, **base,
                "output": p.get("output", ""),
                "status": p.get("status", "ok"),
            }
            if isinstance(p.get("usage"), dict):
                out["usage"] = p["usage"]
            if p.get("halt_kind"):  # 预算停机等专属停机类型，供前端区分渲染
                out["halt_kind"] = p["halt_kind"]
            return out
        if t == EventType.TOOL_START:
            return {
                "type": IpcMessageType.TOOL_START, **base,
                "tool_name": p["tool_name"],
                "tool_call_id": p["tool_call_id"],
                "tool_args": p.get("tool_args", {}),
            }
        if t == EventType.TOOL_END:
            return {
                "type": IpcMessageType.TOOL_END, **base,
                "tool_name":         p["tool_name"],
                "tool_call_id":      p["tool_call_id"],
                "is_error":          p.get("is_error", False),
                "result_full":       p.get("result_full"),
                "result_error":      p.get("result_error"),
                "result_preview":    p.get("result_preview", ""),
                "result_mime_type":  p.get("result_mime_type", "text/plain"),
                "result_size_bytes": p.get("result_size_bytes", 0),
                "result_truncated":  p.get("result_truncated", False),
                "duration_ms":       p.get("duration_ms"),
                "tool_args":         p.get("tool_args", {}),
                # ToolFeedbackProvider 的反馈：{text, severity, source} 或 None。
                # 契约对侧 = types/api.ts 的 ToolEndMsg.feedback（两处必须同步改）。
                "feedback":          p.get("feedback"),
            }
        if t == EventType.HITL_REQUEST:
            return {
                "type": IpcMessageType.HITL_REQUEST, **base,
                "approval_id": p["approval_id"],
                "tool_name": p["tool_name"],
                "tool_args_summary": p.get("tool_args_summary", {}),
                "session_id": p["session_id"],
                "extra": {k: v for k, v in p.items()
                          if k not in ("approval_id", "tool_name", "tool_args_summary", "session_id")},
            }
        if t == EventType.INTERACTION_REQUEST:
            result: dict[str, Any] = {
                "type": IpcMessageType.INTERACTION_REQUEST, **base,
                "request_id": p["request_id"],
                "questions": p["questions"],
            }
            if p.get("tool_name"):
                result["tool_name"] = p["tool_name"]
            return result
        if t == EventType.USER_INPUT_ECHO:
            return {
                "type": IpcMessageType.USER_INPUT_ECHO, **base,
                "user_id": p["user_id"],
                "content": p["content"],
                "session_id": p["session_id"],
            }
        if t == EventType.AGENT_REPLY:
            return {
                "type": IpcMessageType.AGENT_REPLY, **base,
                "content": p["content"],
                "session_id": p.get("session_id", ""),
            }
        if t == EventType.ERROR:
            return {
                "type": IpcMessageType.ERROR, **base,
                "error_code": p.get("error_code", "unknown"),
                "error_message": p.get("error_message", ""),
                "error_detail": p.get("error_detail", ""),
            }
        if t == EventType.APPROVAL_RESULT:
            return {
                "type": IpcMessageType.APPROVAL_RESULT, **base,
                "approval_id": p["approval_id"],
                "decision": p["decision"],
            }
        if t == EventType.TASK_NOTIFICATION:
            return {
                "type": IpcMessageType.TASK_NOTIFICATION, **base,
                "task_id": p["task_id"],
                "title": p["title"],
                "body": p.get("body", ""),
                "level": p.get("level", "info"),
            }
        if t == EventType.AGENT_HALTED:
            return {
                "type": IpcMessageType.AGENT_HALTED, **base,
                "reason": p.get("reason", ""),
                # halt_kind：区分预算耗尽（"budget_exhausted"）与普通停机，供前端渲染专属文案
                "halt_kind": p.get("halt_kind", ""),
            }
        if t == EventType.PERMISSION_DENIED:
            return {
                "type": IpcMessageType.PERMISSION_DENIED, **base,
                "tool_name": p.get("tool_name", ""),
                "reason": p.get("reason", ""),
            }
        if t == EventType.PLAN_APPROVAL_REQUEST:
            return {
                "type": IpcMessageType.PLAN_APPROVAL_REQUEST, **base,
                "plan_path": p["plan_path"],
                "plan_content": p["plan_content"],
                "run_id": p.get("run_id", event.run_id or ""),
                "session_id": p.get("session_id", ""),
                "user_id": p.get("user_id", ""),
            }
        if t == EventType.AGENT_TASK_EVENT:
            # 透传完整 task 对象，前端 TaskPanel 据此维护实时任务列表；
            # task_id 从 task 内取（payload 顶层无 task_id）。deleted 事件 task 可为 None。
            task = p.get("task") or {}
            return {
                "type": IpcMessageType.AGENT_TASK_EVENT, **base,
                "task_id": task.get("task_id", "") or p.get("task_id", ""),
                "event": p.get("event", ""),
                "task": p.get("task"),
            }
        if t == EventType.SKILL_PROGRESS:
            # 技能/长任务进度心跳：渲染进对话时间线的 skill_progress 段。
            return {
                "type": IpcMessageType.SKILL_PROGRESS, **base,
                "activity": p.get("activity", ""),
                "phase": p.get("phase", ""),
                "status": p.get("status", "running"),
                "detail": p.get("detail", ""),
                "session_id": p.get("session_id", ""),
            }
        if t == EventType.QUICK_APP_DATA:
            return {
                "type": IpcMessageType.QUICK_APP_DATA, **base,
                "app_id": p.get("app_id", ""),
                "data_type": p.get("data_type", ""),
                "data": p.get("data", {}),
                "session_id": p.get("session_id", ""),
            }
        if t == EventType.SCHEDULED_TASK_LIST:
            return {
                "type": IpcMessageType.SCHEDULED_TASK_LIST, **base,
                "tasks": p.get("tasks", []),
            }
        # ── Skill 资源管理 ──
        if t == EventType.SKILL_LIST_RESULT:
            return {
                "type": IpcMessageType.SKILL_LIST_RESULT, **base,
                "skills": p.get("skills", []),
            }
        if t == EventType.SKILL_GET_RESULT:
            return {
                "type": IpcMessageType.SKILL_GET_RESULT, **base,
                "skill_name": p.get("skill_name", ""),
                "description": p.get("description", ""),
                "when_to_use": p.get("when_to_use", ""),
                "content": p.get("content", ""),
                "tags": p.get("tags", []),
                "source": p.get("source", "system"),
                "size": p.get("size", 0),
                "modified_at": p.get("modified_at", ""),
                "from_cache": p.get("from_cache", False),
            }
        if t == EventType.SKILL_SAVED:
            return {
                "type": IpcMessageType.SKILL_SAVED, **base,
                "skill": p.get("skill", {}),
            }
        if t == EventType.SKILL_DELETED:
            return {
                "type": IpcMessageType.SKILL_DELETED, **base,
                "skill_name": p.get("skill_name", ""),
            }
        if t == EventType.SKILL_IMPORTED:
            return {
                "type": IpcMessageType.SKILL_IMPORTED, **base,
                "success": p.get("success", False),
                "skill_name": p.get("skill_name", ""),
                "error": p.get("error"),
            }
        if t == EventType.SKILL_EXPORTED:
            return {
                "type": IpcMessageType.SKILL_EXPORTED, **base,
                "file_path": p.get("file_path", ""),
                "format": p.get("format", "md"),
            }
        if t == EventType.SKILL_ACTIVATED:
            return {
                "type": IpcMessageType.SKILL_ACTIVATED, **base,
                "skill_name": p.get("skill_name", ""),
            }
        if t == EventType.SKILL_CLEARED:
            return {
                "type": IpcMessageType.SKILL_CLEARED, **base,
                "skill_name": p.get("skill_name", ""),
            }
        if t == EventType.SESSION_CONCURRENCY:
            # SessionAgentPool 三态（queued/started/released）+ 排队反馈
            return {
                "type": IpcMessageType.SESSION_CONCURRENCY, **base,
                "session_id":     p.get("session_id", ""),
                "status":         p.get("status", ""),
                "running_count":  p.get("running_count", 0),
                "max_concurrent": p.get("max_concurrent", 0),
                "queue_position": p.get("queue_position", 0),
                "queue_length":   p.get("queue_length", 0),
            }
        # ── 全局搜索 ──
        if t == EventType.SEARCH_RESULT:
            return {
                "type": IpcMessageType.SEARCH_RESULT, **base,
                "query": p.get("query", ""),
                "sessions": p.get("sessions", []),
                "messages": p.get("messages", []),
            }
        # ── Dashboard 看板 ──
        if t == EventType.DASHBOARD_DATA:
            return {
                "type": IpcMessageType.DASHBOARD_DATA, **base,
                "global": p.get("global", {}),
                "sessions": p.get("sessions", []),
                # 降级事件明细（非会话级）。空列表是合法值（无降级），非"数据缺失"。
                "degradations": p.get("degradations", []),
            }
        # ── 预算额度态（按 provider 分账，供额度条）──
        if t == EventType.BUDGET_STATUS:
            return {
                "type": IpcMessageType.BUDGET_STATUS, **base,
                "budgets": p.get("budgets", []),
            }
        # ── 模型选择 ──
        if t == EventType.MODEL_LIST:
            return {
                "type": IpcMessageType.MODEL_LIST, **base,
                "models": p.get("models", []),
                "default_model_id": p.get("default_model_id"),
            }
        # ── LLM 凭据管理（BYOK）──
        if t == EventType.CREDENTIALS_LIST:
            return {
                "type": IpcMessageType.CREDENTIALS_LIST, **base,
                "credentials": p.get("credentials", []),
            }
        if t == EventType.CREDENTIALS_SAVED:
            return {
                "type": IpcMessageType.CREDENTIALS_SAVED, **base,
                "success": p.get("success", False),
                "error": p.get("error"),
            }
        if t == EventType.CREDENTIALS_VERIFIED:
            return {
                "type": IpcMessageType.CREDENTIALS_VERIFIED, **base,
                "success": p.get("success", False),
                "results": p.get("results", []),
            }
        if t == EventType.CREDENTIALS_STATUS:
            return {
                "type": IpcMessageType.CREDENTIALS_STATUS, **base,
                "configured": p.get("configured", False),
                "credential_count": p.get("credential_count", 0),
                "default_model_id": p.get("default_model_id"),
                "legacy_format": p.get("legacy_format", False),
                "default_resolvable": p.get("default_resolvable"),
            }
        # ── 会话列表 ──
        if t == EventType.SESSION_LIST:
            return {
                "type": IpcMessageType.SESSION_LIST, **base,
                "sessions": p.get("sessions", []),
                "has_more": p.get("has_more", False),
                "page": p.get("page", 1),
                "group_id": p.get("group_id", "all"),
            }
        if t == EventType.SESSION_SWITCHED:
            return {
                "type": IpcMessageType.SESSION_SWITCHED, **base,
                "session_id": p.get("session_id", ""),
                "context_status": p.get("context_status", "fresh"),
            }
        if t == EventType.SESSION_UPDATED:
            return {
                "type": IpcMessageType.SESSION_UPDATED, **base,
                "session_info": p.get("session_info", {}),
                "reason": p.get("reason", ""),
            }
        if t == EventType.SESSION_DELETED:
            return {
                "type": IpcMessageType.SESSION_DELETED, **base,
                "session_id": p.get("session_id", ""),
                "routing": p.get("routing", {}),
            }
        if t == EventType.SESSION_GROUP_LIST:
            return {
                "type": IpcMessageType.SESSION_GROUP_LIST, **base,
                "groups": p.get("groups", []),
            }
        if t == EventType.SESSION_HISTORY_LIST:
            return {
                "type": IpcMessageType.SESSION_HISTORY_LIST, **base,
                "session_id": p.get("session_id", ""),
                "messages": p.get("messages", []),
            }
        # RUN_START / RUN_END：IPC schema 不直接暴露（前端通过 REPLY_START/END 感知）
        if t == EventType.RUN_START:
            return {"type": IpcMessageType.REPLY_START, **base}
        if t == EventType.RUN_END:
            out = {"type": IpcMessageType.REPLY_END, **base,
                   "output": p.get("output", ""), "status": p.get("status", "ok")}
            if isinstance(p.get("usage"), dict):
                out["usage"] = p["usage"]
            return out
        # D2 Push：定时任务增量变更
        if t == EventType.SCHEDULED_TASK_CHANGED:
            return {
                "type": IpcMessageType.SCHEDULED_TASK_CHANGED, **base,
                "task": p.get("task", {}),
                "change_type": p.get("change_type", ""),
                "session_id": p.get("session_id", ""),
            }

        # 未知事件类型：走 extra 兜底
        return {
            "type": IpcMessageType.UNKNOWN, **base,
            "event_type": t.value,
            "payload": p,
        }

    def write_raw(self, msg: dict[str, Any]) -> None:
        """直接写一条 IPC 原始消息（非 NormalizedEvent），如 PONG 心跳回包。"""
        self._write_ipc(msg)

    def _write_ipc(self, msg: dict[str, Any]) -> None:
        """写 stdout，加 IPC: 前缀。"""
        line = "IPC:" + json.dumps(msg, ensure_ascii=False)
        with self._write_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
