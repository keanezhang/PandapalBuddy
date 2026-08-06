"""pandapal_relay/normalized_events.py — 归一化事件模型（Relay 本地副本）。

★ 来源：pandapal/events/normalized.py（完整复制，45 种 EventType 全量同步）
★ 用途：Relay 服务端独立部署，不依赖完整 pandapal 包。
★ 同步约束：与主服务保持 100% 一致；NormalizedEvent 是跨渠道边界契约，
  任何字段/枚举值变化都会破坏 Transport 兼容。
  由 scripts/check_protocol_sync.py 在 CI 中对账，防 D4 复发。

★ 设计原则：
1. 业务层用各自强类型 dataclass（InboundMessage、AgentResult、ApprovalRequest…）
2. 跨渠道边界（Broadcast → Transport）一律用 NormalizedEvent
3. NormalizedEvent 是 frozen=True 的不可变对象，可哈希、可放心跨线程

★ 与 OutboundMessage 的关系：
  OutboundMessage(message_type, payload: bytes, origin_channel_id)
    ↓ 改造后
  NormalizedEvent(event_type, reply_id, run_id, payload: dict, timestamp)
  去除 bytes，message_type 改名 event_type，加 reply_id/run_id/timestamp

★ msg_id 单一来源约定：
  - 事件级 msg_id 放在 NormalizedEvent 顶层，由 dataclass 默认生成
  - payload 内**禁止**再放 msg_id 字段（避免双源导致 dedup 二义性）
  - 工厂方法如需 msg_id 透传，用 msg_id 参数覆盖默认值；不写进 payload
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """统一事件类型枚举（**45 种**全保留，不合并）。

    命名空间划分：
    - 流式生命周期 **4** 种（REPLY_START/REPLY_END/RUN_START/RUN_END）
    - LLM 输出 2 种
    - 工具调用 2 种
    - 暂停/恢复 4 种（HITL_REQUEST/INTERACTION_REQUEST/PERMISSION_DENIED/AGENT_HALTED）
    - 终端 2 种（ERROR/APPROVAL_RESULT）
    - 系统 5 种（USER_INPUT_ECHO/TASK_NOTIFICATION/AGENT_TASK_EVENT/AGENT_REPLY/QUICK_APP_DATA）

    ★ 注意：HITL_REQUEST 是事件类型之一，HITL 不是独立通道。
    ★ 流式分类（见 EVENT_CATEGORY）：2 种 STREAMING（LLM_TOKEN/REASONING_TOKEN）+ 其余 DISCRETE。
    """

    # ── 流式生命周期 ──
    REPLY_START = "reply_start"
    REPLY_END   = "reply_end"
    RUN_START   = "run_start"
    RUN_END     = "run_end"

    # ── LLM 输出 ──
    LLM_TOKEN         = "llm_token"
    REASONING_TOKEN   = "llm_reasoning_token"

    # ── 工具调用 ──
    TOOL_START = "tool_start"
    TOOL_END   = "tool_end"

    # ── 暂停/恢复 ──
    HITL_REQUEST         = "hitl_request"           # ← Agent 暂停，要求审批
    INTERACTION_REQUEST  = "interaction_request"    # ← 交互型工具问卷
    PERMISSION_DENIED    = "permission_denied"
    AGENT_HALTED         = "agent_halted"

    # ── 终端 ──
    ERROR         = "error"            # error_code, error_message
    APPROVAL_RESULT = "approval_result"  # decision="approved"/"rejected"

    # ── 系统 ──
    USER_INPUT_ECHO   = "user_input_echo"
    TASK_NOTIFICATION = "task_notification"
    AGENT_TASK_EVENT  = "agent_task_event"
    AGENT_REPLY       = "agent_reply"    # 非流式完整回复（旧 `_publish_agent_reply` 的替代）

    # ── Plan Mode ──
    PLAN_APPROVAL_REQUEST = "plan_approval_request"  # Plan Mode 规划完成，等待用户审批

    # ── Quick App ──
    QUICK_APP_DATA = "quick_app_data"  # 快应用数据推送（app_id + data_type + data）

    # 技能进度（LLM 主动上报的长任务/技能内部进度心跳；渲染进对话时间线）
    SKILL_PROGRESS = "skill_progress"  # activity + phase + status(running/completed/failed)

    # ── 定时任务 ──
    SCHEDULED_TASK_LIST    = "scheduled_task_list"    # 定时任务列表推送（pull/push D1）
    SCHEDULED_TASK_CHANGED = "scheduled_task_changed" # 增量：单个任务变更（D2 Push 增量）

    # ── Skill 资源管理 ──
    SKILL_LIST_RESULT = "skill_list_result"   # Skill 摘要列表响应
    SKILL_GET_RESULT  = "skill_get_result"    # Skill 详情响应
    SKILL_SAVED       = "skill_saved"         # Skill 保存成功确认
    SKILL_DELETED     = "skill_deleted"       # Skill 删除成功确认
    SKILL_IMPORTED    = "skill_imported"      # Skill 导入成功确认
    SKILL_EXPORTED    = "skill_exported"      # Skill 导出成功确认

    # ── Skill 生命周期 ──
    SKILL_ACTIVATED = "skill_activated"  # Skill 已激活（search_skills 成功）
    SKILL_CLEARED   = "skill_cleared"    # Skill 已清除（Turn 结束）

    # ── 并发池状态（多 Session 排队反馈）──
    SESSION_CONCURRENCY = "session_concurrency"  # 三态：queued / started / released

    # ── 会话列表（UI 会话管理，前端 SessionListPanel）──
    SESSION_LIST         = "session_list"          # 会话列表响应
    SESSION_SWITCHED     = "session_switched"      # 切换应答 + context_status
    SESSION_UPDATED      = "session_updated"       # 增量元数据变更
    SESSION_DELETED      = "session_deleted"       # 删除完成 + 路由决策
    SESSION_GROUP_LIST   = "session_group_list"    # 分组列表广播
    SESSION_HISTORY_LIST = "session_history_list"  # 历史消息回补

    # ── 全局搜索（命令面板 ⌘K）──
    SEARCH_RESULT        = "search_result"         # 搜索结果响应（会话标题 + 消息全文）

    # ── 模型选择 ──
    MODEL_LIST           = "model_list"            # 可选模型清单 + default（拉取回复）

    # ── LLM 凭据管理（BYOK）──
    CREDENTIALS_LIST     = "credentials_list"      # 已有凭据列表（api_key 脱敏）
    CREDENTIALS_SAVED    = "credentials_saved"     # 保存结果确认
    CREDENTIALS_VERIFIED = "credentials_verified"  # 连通性校验结果
    CREDENTIALS_STATUS   = "credentials_status"    # 门禁配置状态

    # ── Dashboard 看板 ──
    DASHBOARD_DATA       = "dashboard_data"        # 看板快照响应（global + sessions + degradations）

    # ── 预算额度（按 provider 分账）──
    BUDGET_STATUS        = "budget_status"         # 每 provider 额度视图（额度条）


# ── 事件作用域标记 ──────────────────────────────────────────────────────────
# SESSION_ID 契约 §八 #4「显式二分」：会话级事件必带 session_id；全局级事件
# 「明确不带」——在 payload 里显式声明 scope=global，宣告「我不属于任何会话，
# 无 session_id 是正确的」。Transport 护栏据此区分「真·全局」与「漏 stamp 的
# 会话级事件」，避免把 dashboard/budget 这类全局错误误报成串台风险。
# 该 key 仅供后端护栏识别，不进入发往前端的 IPC schema（ERROR 分支只挑固定字段）。
EVENT_SCOPE_KEY = "scope"
SCOPE_GLOBAL = "global"


# ── REPLY_END.status 完整取值表 ─────────────────────────────────────────────
REPLY_END_STATUSES: frozenset[str] = frozenset({
    "ok",                        # 正常完成
    "paused_for_hitl",           # HITL 暂停
    "paused_for_interaction",    # 交互型工具暂停
    "paused_for_plan_approval",  # Plan Mode 审批暂停
    "permission_denied",         # 工具权限被拒
    "halted",                    # Agent 显式停止
    "error",                     # 执行异常
})


# ── payload schema 速查表 ──────────────────────────────────────────────────
# | event_type             | 类别 | payload 必填字段                              |
# |------------------------|------|----------------------------------------------|
# | REPLY_START            | 离散 | (无)                                         |
# | REPLY_END              | 离散 | output: str, status: 见 REPLY_END_STATUSES   |
# | LLM_TOKEN              | 流式 | delta: str, snapshot: str                    |
# | REASONING_TOKEN        | 流式 | delta: str, snapshot: str                    |
# | TOOL_START             | 离散 | tool_name, tool_call_id, tool_args: dict     |
# | TOOL_END               | 离散 | tool_name, tool_call_id, result_full,        |
# |                        |      | result_preview, result_mime_type,            |
# |                        |      | result_size_bytes, result_truncated,         |
# |                        |      | is_error, duration_ms                        |
# | HITL_REQUEST           | 离散 | approval_id, tool_name, tool_args_summary,   |
# |                        |      | session_id                                   |
# | INTERACTION_REQUEST    | 离散 | request_id, questions: list[dict],           |
# |                        |      | tool_name                                      |
# | PLAN_APPROVAL_REQUEST  | 离散 | plan_path: str, plan_content: str             |
# | QUICK_APP_DATA         | 离散 | app_id: str, data_type: str, data: dict,      |
# |                        |      | session_id: str                              |
# | USER_INPUT_ECHO        | 离散 | user_id, content, session_id                 |
# | AGENT_REPLY            | 离散 | content, session_id                          |
# | ERROR                  | 离散 | error_code, error_message, error_detail      |
# | APPROVAL_RESULT        | 离散 | approval_id, decision, reason                |
# | TASK_NOTIFICATION      | 离散 | task_id, title, body, level                  |


@dataclass(frozen=True)
class NormalizedEvent:
    """跨渠道归一化事件。

    字段约束（frozen=True 后由 @dataclass 自动保证）：
    - event_type:  必填，决定 Transport 如何渲染
    - reply_id:    同一回复周期内所有事件共享（开始→结束）
    - reply_scope: reply 的语义范围（来自 ReplyIdManager.scope.value），
                  normal / hitl_resume / system / task / error
    - run_id:      整个 Agent 执行周期的 ID（可能包含多个 reply）
    - payload:     业务数据（与 event_type 配套的 schema，见上表）
    - timestamp:   事件产生时间（毫秒精度，方便排序）
    - msg_id:      事件唯一 ID（去重、重传、关联 key），不传则自动生成
    """

    event_type: EventType
    reply_id:   str | None = None
    reply_scope: str | None = None
    run_id:     str | None = None
    payload:    dict[str, Any] = field(default_factory=dict)
    timestamp:  float = field(default_factory=lambda: time.time() * 1000)
    msg_id:     str = field(default_factory=lambda: uuid.uuid4().hex)
    origin_channel_id: str | None = None

    def to_dict(self) -> dict:
        """序列化为线协议 dict（仅 1 次，由 Transport 出口调用）。"""
        d: dict[str, Any] = {
            "event_type": self.event_type.value,
            "msg_id":     self.msg_id,
            "timestamp":  self.timestamp,
        }
        if self.reply_id:
            d["reply_id"] = self.reply_id
        if self.reply_scope:
            d["reply_scope"] = self.reply_scope
        if self.run_id:
            d["run_id"]   = self.run_id
        if self.payload:
            d["payload"]  = self.payload
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "NormalizedEvent":
        """从线协议 dict 解析（仅 1 次，由 Transport 入口调用）。"""
        return cls(
            event_type=EventType(d["event_type"]),
            reply_id=d.get("reply_id"),
            reply_scope=d.get("reply_scope"),
            run_id=d.get("run_id"),
            payload=d.get("payload", {}),
            timestamp=d.get("timestamp", time.time() * 1000),
            msg_id=d.get("msg_id") or uuid.uuid4().hex,
        )

    # ── 不变量校验 ─────────────────────────────────────────────
    def __post_init__(self) -> None:
        # frozen 模式下 __post_init__ 仍可运行（只读 self）
        if self.event_type == EventType.HITL_REQUEST and not self.reply_id:
            raise ValueError(f"HITL_REQUEST must have reply_id, got {self}")
        if self.event_type == EventType.HITL_REQUEST and self.reply_id != self.run_id:
            raise ValueError(
                f"HITL_REQUEST requires reply_id == run_id (Option C), "
                f"got reply_id={self.reply_id!r}, run_id={self.run_id!r}"
            )
        if self.event_type in (EventType.LLM_TOKEN, EventType.REASONING_TOKEN,
                               EventType.REPLY_START) and not self.reply_id:
            raise ValueError(f"{self.event_type.value} must have reply_id, got {self}")
        # REPLY_END status 校验
        if self.event_type == EventType.REPLY_END:
            status = self.payload.get("status")
            if status is not None and status not in REPLY_END_STATUSES:
                raise ValueError(
                    f"REPLY_END.status must be one of {sorted(REPLY_END_STATUSES)}, "
                    f"got {status!r}"
                )

    # ── 工厂方法（让调用方更直观）────────────────────────────────
    #
    # ★ msg_id 单一来源约定：
    #   - 事件级 msg_id 放在 NormalizedEvent 顶层（line 1442），由 dataclass 默认生成
    #   - payload 内**禁止**再放 msg_id 字段（避免双源导致 dedup 二义性）
    #   - 工厂方法如需 msg_id 透传，用 msg_id 参数覆盖默认值；不写进 payload
    @classmethod
    def llm_token(cls, delta: str, snapshot: str, reply_id: str,
                  run_id: str | None = None, msg_id: str | None = None) -> "NormalizedEvent":
        return cls(
            event_type=EventType.LLM_TOKEN,
            reply_id=reply_id, run_id=run_id, msg_id=msg_id or uuid.uuid4().hex,
            payload={"delta": delta, "snapshot": snapshot},
        )

    @classmethod
    def reasoning_token(cls, delta: str, snapshot: str, reply_id: str,
                        run_id: str | None = None) -> "NormalizedEvent":
        return cls(
            event_type=EventType.REASONING_TOKEN,
            reply_id=reply_id, run_id=run_id,
            payload={"delta": delta, "snapshot": snapshot},
        )

    @classmethod
    def reply_start(cls, reply_id: str, run_id: str | None = None,
                    reply_scope: str | None = None) -> "NormalizedEvent":
        return cls(
            event_type=EventType.REPLY_START,
            reply_id=reply_id, reply_scope=reply_scope, run_id=run_id,
        )

    @classmethod
    def reply_end(cls, reply_id: str, output: str = "",
                  status: str = "ok", run_id: str | None = None) -> "NormalizedEvent":
        return cls(
            event_type=EventType.REPLY_END,
            reply_id=reply_id, run_id=run_id,
            payload={"output": output, "status": status},
        )

    @classmethod
    def tool_start(cls, tool_name: str, tool_call_id: str, tool_args: dict,
                   reply_id: str, run_id: str | None = None) -> "NormalizedEvent":
        return cls(
            event_type=EventType.TOOL_START,
            reply_id=reply_id, run_id=run_id,
            payload={"tool_name": tool_name, "tool_call_id": tool_call_id, "tool_args": tool_args},
        )

    @classmethod
    def tool_end(cls, tool_name: str, tool_call_id: str,
                 result_full: Any = None, result_error: str | None = None,
                 is_error: bool = False, duration_ms: int | None = None,
                 tool_args: dict | None = None,
                 feedback: dict | None = None,
                 reply_id: str = "", run_id: str | None = None) -> "NormalizedEvent":
        """构造 TOOL_END 事件。

        ★ 关键：result_full 携带工具返回的**完整结果**（任意 JSON-serializable）。
          - IPC 端：可折叠展开显示完整内容
          - WeCom 端：按 result_size_bytes 智能截断（< 1.5KB 内联，否则摘要+引导）
          - 单事件 > 5MB 时 result_full=None, result_truncated=True

        ★ feedback：ToolFeedbackProvider 贡献的反馈，`{text, severity, source}` 或 None。
          与 result_full 是**两码事** —— result_full 是工具自己说的（"文件已创建"），
          feedback 是第三方对这次调用的评价（"但它有 15 个 lint error"）。
          不参与 result_full 的 5MB 截断：反馈由 provider 自行限长（门控是 20 条），
          且它恰恰是长结果下最不能丢的那部分（同理见 run_core 渲染时的前置）。
        """
        MAX_INLINE_BYTES = 5 * 1024 * 1024
        if is_error:
            size = len(result_error.encode("utf-8")) if result_error else 0
            payload = {
                "tool_name":    tool_name,
                "tool_call_id": tool_call_id,
                "is_error":     True,
                "result_full":  None,
                "result_error": result_error or "未知错误",
                "result_preview": f"❌ {tool_name} 失败",
                "result_size_bytes": size,
                "result_truncated":   False,
                "duration_ms":  duration_ms,
                "tool_args":    tool_args or {},
            }
        else:
            size = _calc_payload_size(result_full)
            truncated = size > MAX_INLINE_BYTES
            payload = {
                "tool_name":    tool_name,
                "tool_call_id": tool_call_id,
                "is_error":     False,
                "result_full":  None if truncated else result_full,
                "result_mime_type": _infer_mime_type(result_full, tool_name),
                "result_size_bytes": size,
                "result_truncated":   truncated,
                "result_preview":     _generate_tool_preview(tool_name, result_full, truncated),
                "duration_ms":  duration_ms,
                "tool_args":    tool_args or {},
            }
        # 两分支共用：feedback 与工具成败正交（provider 对失败的工具也可能有话说）。
        # 统一挂在 if/else **之后** —— 分别塞进两个 dict 迟早漏一个，
        # 且漏掉的那个只在「工具失败 + 有反馈」时才现形，最难复现。
        payload["feedback"] = feedback
        return cls(
            event_type=EventType.TOOL_END,
            reply_id=reply_id, run_id=run_id,
            payload=payload,
        )

    @classmethod
    def hitl_request(cls, approval_id: str, tool_name: str, tool_args_summary: dict,
                     session_id: str, run_id: str) -> "NormalizedEvent":
        """构造 HITL_REQUEST 事件。

        ★ reply_id == run_id 是硬约定（见 Option C：HITL 暂停的是"当前回复"，
          后续恢复执行时前端通过同一 reply_id 关联原气泡）。
        工厂方法不接受 reply_id 参数；如需自定义 reply_id（如 ns: 前缀），
        请直接用 dataclass 构造，但自行保证 reply_id == run_id。
        """
        return cls(
            event_type=EventType.HITL_REQUEST,
            reply_id=run_id,  # ★ 强制 reply_id == run_id
            run_id=run_id,
            payload={
                "approval_id": approval_id,
                "tool_name": tool_name,
                "tool_args_summary": tool_args_summary,
                "session_id": session_id,
            },
        )

    @classmethod
    def interaction_request(cls, request_id: str, questions: list[dict],
                            tool_name: str | None,
                            reply_id: str, run_id: str) -> "NormalizedEvent":
        """构造 INTERACTION_REQUEST 事件。

        questions 是 list[dict]，每个 dict 含:
          - question:    str
          - header:      str
          - options:     list[dict] (label + description)
          - multiSelect: bool
        tool_name 一并下发，前端 MessageBubble 用作 header 标签。
        """
        payload: dict[str, Any] = {
            "request_id": request_id,
            "questions":  questions,
        }
        if tool_name:
            payload["tool_name"] = tool_name
        return cls(
            event_type=EventType.INTERACTION_REQUEST,
            reply_id=reply_id, run_id=run_id,
            payload=payload,
        )

    @classmethod
    def user_input_echo(cls, user_id: str, content: str, session_id: str) -> "NormalizedEvent":
        return cls(
            event_type=EventType.USER_INPUT_ECHO,
            reply_id=None, run_id=None,
            payload={"user_id": user_id, "content": content, "session_id": session_id},
        )

    @classmethod
    def agent_reply(cls, content: str, session_id: str = "",
                    reply_id: str = "", run_id: str | None = None) -> "NormalizedEvent":
        return cls(
            event_type=EventType.AGENT_REPLY,
            reply_id=reply_id, run_id=run_id,
            payload={"content": content, "session_id": session_id},
        )

    @classmethod
    def error(cls, error_code: str, error_message: str,
              error_detail: str = "",
              reply_id: str | None = None, run_id: str | None = None) -> "NormalizedEvent":
        return cls(
            event_type=EventType.ERROR,
            reply_id=reply_id, run_id=run_id,
            payload={
                "error_code":    error_code,
                "error_message": error_message,
                "error_detail":  error_detail,
            },
        )

    @classmethod
    def global_error(cls, error_code: str, error_message: str,
                     error_detail: str = "") -> "NormalizedEvent":
        """全局级错误：不属于任何会话（如 dashboard/budget 请求失败）。

        与 error() 的区别：显式声明 scope=global（SESSION_ID 契约 §八 #4 的
        「全局级明确不带」），因此不携带 session_id / reply_id / run_id。
        前端不按 session 分桶，Transport 护栏也不会误报「缺 session_id」。
        """
        return cls(
            event_type=EventType.ERROR,
            payload={
                "error_code":    error_code,
                "error_message": error_message,
                "error_detail":  error_detail,
                EVENT_SCOPE_KEY: SCOPE_GLOBAL,
            },
        )

    @classmethod
    def approval_result(cls, approval_id: str, decision: str, reason: str = "",
                        reply_id: str = "", run_id: str | None = None) -> "NormalizedEvent":
        return cls(
            event_type=EventType.APPROVAL_RESULT,
            reply_id=reply_id, run_id=run_id,
            payload={"approval_id": approval_id, "decision": decision, "reason": reason},
        )

    @classmethod
    def task_notification(cls, task_id: str, title: str, body: str = "",
                          level: str = "info") -> "NormalizedEvent":
        return cls(
            event_type=EventType.TASK_NOTIFICATION,
            reply_id=None, run_id=None,
            payload={"task_id": task_id, "title": title, "body": body, "level": level},
        )

    @classmethod
    def permission_denied(cls, tool_name: str, reason: str,
                          reply_id: str = "", run_id: str | None = None) -> "NormalizedEvent":
        return cls(
            event_type=EventType.PERMISSION_DENIED,
            reply_id=reply_id, run_id=run_id,
            payload={"tool_name": tool_name, "reason": reason},
        )

    @classmethod
    def agent_halted(cls, reason: str = "",
                     reply_id: str = "", run_id: str | None = None) -> "NormalizedEvent":
        return cls(
            event_type=EventType.AGENT_HALTED,
            reply_id=reply_id, run_id=run_id,
            payload={"reason": reason},
        )

    @classmethod
    def plan_approval_request(cls, plan_path: str, plan_content: str,
                               run_id: str, session_id: str = "",
                               user_id: str = "") -> "NormalizedEvent":
        """构造 PLAN_APPROVAL_REQUEST 事件。"""
        return cls(
            event_type=EventType.PLAN_APPROVAL_REQUEST,
            reply_id=run_id,
            run_id=run_id,
            payload={
                "plan_path": plan_path,
                "plan_content": plan_content,
                "run_id": run_id,
                "session_id": session_id,
                "user_id": user_id,
            },
        )

    @classmethod
    def quick_app_data(cls, app_id: str, data_type: str, data: dict,
                       session_id: str = "",
                       reply_id: str = "", run_id: str | None = None,
                       msg_id: str | None = None) -> "NormalizedEvent":
        """构造 QUICK_APP_DATA 事件。

        Args:
            app_id: 目标快应用 ID，如 "test-pipeline"、"stock-query"
            data_type: 数据类型标签，如 "step_output"、"quote"、"history"
            data: 任意 JSON-serializable dict，建议 < 4KB，大内容走文件路径
            session_id: 会话 ID
            reply_id: 关联的回复 ID（可选）
            run_id: 关联的运行 ID（可选）
        """
        return cls(
            event_type=EventType.QUICK_APP_DATA,
            reply_id=reply_id or None,
            run_id=run_id,
            msg_id=msg_id or uuid.uuid4().hex,
            payload={
                "app_id": app_id,
                "data_type": data_type,
                "data": data,
                "session_id": session_id,
            },
        )

    @staticmethod
    def skill_activated(
        skill_name: str,
        skill_type: str,
        tools: list[str] | None = None,
        run_id: str | None = None,
        msg_id: str | None = None,
    ) -> "NormalizedEvent":
        """Skill 已激活事件（search_skills 成功后触发）。

        Args:
            skill_name: Skill 名称
            skill_type: "KNOWLEDGE" 或 "ACTION"
            tools: ACTION Skill 暴露的工具名列表，KNOWLEDGE Skill 为空
            run_id: 关联的运行 ID
            msg_id: 事件唯一 ID
        """
        return NormalizedEvent(
            event_type=EventType.SKILL_ACTIVATED,
            run_id=run_id,
            msg_id=msg_id or uuid.uuid4().hex,
            payload={
                "skill_name": skill_name,
                "skill_type": skill_type,
                "tools": tools or [],
            },
        )

    @staticmethod
    def skill_cleared(
        skill_name: str,
        run_id: str | None = None,
        msg_id: str | None = None,
    ) -> "NormalizedEvent":
        """Skill 已清除事件（Turn 结束时触发）。

        Args:
            skill_name: 被清除的 Skill 名称
            run_id: 关联的运行 ID
            msg_id: 事件唯一 ID
        """
        return NormalizedEvent(
            event_type=EventType.SKILL_CLEARED,
            run_id=run_id,
            msg_id=msg_id or uuid.uuid4().hex,
            payload={
                "skill_name": skill_name,
            },
        )

    @classmethod
    def scheduled_task_changed(
        cls,
        *,
        task: dict,
        change_type: str,  # "created" | "updated" | "deleted"
    ) -> "NormalizedEvent":
        """D2 Push 增量：单个定时任务变更。"""
        return cls(
            event_type=EventType.SCHEDULED_TASK_CHANGED,
            payload={
                "task": task,
                "change_type": change_type,
            },
        )

    @classmethod
    def session_concurrency(
        cls,
        *,
        session_id: str,
        status: str,
        running_count: int,
        max_concurrent: int,
        queue_position: int = 0,
        queue_length: int = 0,
    ) -> "NormalizedEvent":
        """构造 SESSION_CONCURRENCY 事件（SessionAgentPool 广播排队状态）。

        三态语义：
          - queued    → 该 session 拿不到 slot，正在排队；queue_position 表示位次
          - started   → 该 session 拿到 slot，开始执行
          - released  → 该 session 归还 slot（无论正常结束 / 异常 / 取消）

        Args:
            session_id:     哪个 session 的状态变化（前端按 session_id 分桶）
            status:         "queued" | "started" | "released"
            running_count:  当前正在执行的 session 数（0..max_concurrent）
            max_concurrent: 并发上限（用于前端显示 "N/max"）
            queue_position: 仅 queued 时有意义，0=队首
            queue_length:   仅 queued 时有意义，含当前 session 的总排队人数
        """
        return cls(
            event_type=EventType.SESSION_CONCURRENCY,
            payload={
                "session_id": session_id,
                "status": status,
                "running_count": running_count,
                "max_concurrent": max_concurrent,
                "queue_position": queue_position,
                "queue_length": queue_length,
            },
        )

    # ══════════════════════════════════════════════════════════
    # 会话列表事件工厂（UI 会话管理）
    # ══════════════════════════════════════════════════════════

    @classmethod
    def session_list(
        cls,
        *,
        sessions: list[dict],
        has_more: bool,
        page: int,
        group_id: str | None,
    ) -> "NormalizedEvent":
        """SessionListPanel 首屏 / 分页应答 / 分组切换后的列表广播。"""
        return cls(
            event_type=EventType.SESSION_LIST,
            payload={
                "sessions": sessions,
                "has_more": has_more,
                "page": page,
                "group_id": group_id if group_id is not None else "all",
            },
        )

    @classmethod
    def dashboard_data(cls, *, snapshot: dict) -> "NormalizedEvent":
        """看板快照响应。snapshot = DashboardSnapshot.to_dict()（global + sessions + degradations）。"""
        return cls(event_type=EventType.DASHBOARD_DATA, payload=snapshot)

    @classmethod
    def budget_status(cls, *, budgets: list[dict]) -> "NormalizedEvent":
        """预算额度态。budgets = [BudgetView.to_dict(), ...]（每 provider 一条，供额度条）。"""
        return cls(event_type=EventType.BUDGET_STATUS, payload={"budgets": budgets})

    @classmethod
    def search_result(
        cls,
        *,
        query: str,
        sessions: list[dict],
        messages: list[dict],
    ) -> "NormalizedEvent":
        """命令面板全局搜索应答 —— 会话标题命中 + 消息全文命中。"""
        return cls(
            event_type=EventType.SEARCH_RESULT,
            payload={
                "query": query,
                "sessions": sessions,
                "messages": messages,
            },
        )

    @classmethod
    def session_switched(
        cls,
        *,
        session_id: str,
        context_status: str,
    ) -> "NormalizedEvent":
        """SESSION_SWITCH 应答 —— 携带 context_status（fresh/restored/degraded）。"""
        return cls(
            event_type=EventType.SESSION_SWITCHED,
            payload={
                "session_id": session_id,
                "context_status": context_status,
            },
        )

    @classmethod
    def session_updated(
        cls,
        *,
        session_info: dict,
        reason: str,
    ) -> "NormalizedEvent":
        """会话元数据变更（created/first_message/activity/favorite/group_changed）。"""
        return cls(
            event_type=EventType.SESSION_UPDATED,
            payload={
                "session_info": session_info,
                "reason": reason,
            },
        )

    @classmethod
    def session_deleted(
        cls,
        *,
        session_id: str,
        routing: dict,
    ) -> "NormalizedEvent":
        """会话删除完成 + 后端计算的路由决策。"""
        return cls(
            event_type=EventType.SESSION_DELETED,
            payload={
                "session_id": session_id,
                "routing": routing,
            },
        )

    @classmethod
    def session_group_list(
        cls,
        *,
        groups: list[dict],
    ) -> "NormalizedEvent":
        """分组列表广播。"""
        return cls(
            event_type=EventType.SESSION_GROUP_LIST,
            payload={"groups": groups},
        )

    @classmethod
    def session_history_list(
        cls,
        *,
        session_id: str,
        messages: list[dict],
    ) -> "NormalizedEvent":
        """会话历史消息回补（LRU 淘汰后切回该 session 时补齐 buffer）。"""
        return cls(
            event_type=EventType.SESSION_HISTORY_LIST,
            payload={
                "session_id": session_id,
                "messages": messages,
            },
        )


# ── 模块级辅助函数（TOOL_END payload 构造用）─────────────────────────

def _calc_payload_size(value: Any) -> int:
    """计算任意 JSON-serializable 值序列化后的字节数。"""
    if value is None:
        return 0
    try:
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _infer_mime_type(value: Any, tool_name: str) -> str:
    """根据结果形态推断 MIME 类型。"""
    if isinstance(value, str):
        return "text/plain"
    if isinstance(value, (dict, list)):
        return "application/json"
    if isinstance(value, bytes):
        return "application/octet-stream"
    return "text/plain"


def _generate_tool_preview(tool_name: str, result: Any, truncated: bool) -> str:
    """根据工具名 + 结果类型生成一句话摘要。"""
    if result is None:
        return f"{tool_name} 执行完成（无输出）"
    if truncated:
        size = _calc_payload_size(result)
        return f"{tool_name} 执行完成（{size // 1024} KB，结果过大）"

    if isinstance(result, dict):
        if "content" in result and isinstance(result["content"], str):
            line_count = result.get("line_count") or result["content"].count("\n") + 1
            size = len(result["content"].encode("utf-8"))
            path = result.get("path", "?")
            return f"已读取 {path}（{line_count} 行，{size // 1024} KB）"
        if "rows" in result and isinstance(result["rows"], list):
            return f"查询到 {len(result['rows'])} 行"
        if "url" in result and "status_code" in result:
            return f"HTTP {result['status_code']} · {result['url']}"
    if isinstance(result, list):
        return f"找到 {len(result)} 条结果"
    if isinstance(result, str):
        size = len(result.encode("utf-8"))
        return f"{tool_name} 输出 {size} 字符"
    return f"{tool_name} 执行完成"
