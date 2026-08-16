"""pandapal/desktop_ipc/message_codec.py — IPC 消息类型与结构体。

设计约束：
- HC6: 禁止裸 dict 跨层传递，所有消息使用 frozen dataclass
- BL3: 入站 / 出站方向分离
- E1: 枚举值与前端 types/api.ts 保持一致（协议契约）

串行启动架构：sidecar 在登录成功后由 Rust 携带 --user-id / --token 参数启动，
无需 AUTH_READY / AUTH_VERIFIED / SYSTEM_READY 等 Phase 2 初始化消息。

入站消息（前端 → Python）：
    SEND_MESSAGE           : 用户发送对话消息
    HITL_DECISION          : HITL 审批决策（approve/reject）
    PING                   : 心跳探测
    INTERACTION_RESPONSE   : 问卷回复
    PLAN_APPROVAL_DECISION : Plan Mode 审批决策
    STOP_GENERATION        : 停止当前 Agent 生成
    LOAD_CREDENTIALS       : 加载已有凭据列表（设置页回填 / 向导回显）
    SAVE_LLM_CREDENTIALS   : 保存 LLM 凭据列表（整体覆写）
    VERIFY_CREDENTIALS     : 连通性校验（探测服务商凭据是否有效）
    GET_CREDENTIALS_STATUS : 查询凭据配置状态（门禁轻量查询）

出站消息（Python → 前端）：
    REPLY_START           : 回复流开始
    TOKEN                 : 流式 LLM token
    REASONING_TOKEN       : 推理 token（o1/o3）
    TOOL_START            : 工具调用开始
    TOOL_END              : 工具调用结束
    HITL_REQUEST          : 触发人工审批
    PERMISSION_DENIED     : 权限被拒
    REPLY_END             : 回复流结束
    AGENT_HALTED          : Agent 被强制停止
    PLAN_APPROVAL_REQUEST : Plan Mode 审批请求
    ERROR                 : 错误通知
    PONG                  : 心跳回应
    CREDENTIALS_LIST      : 凭据列表（api_key 脱敏）
    CREDENTIALS_SAVED     : 凭据保存结果
    CREDENTIALS_VERIFIED  : 凭据校验结果
    CREDENTIALS_STATUS    : 凭据配置状态
    AUTH_TOKEN_REFRESHED  : JWT 刷新成功（带新 token，前端回写 auth_store.json）
    AUTH_EXPIRED          : 登录态彻底失效（前端登出并跳登录页）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class IpcMessageType:
    """IPC 消息类型字符串常量（入站 + 出站）。

    前端 types/api.ts 中的 ApiMessageType 枚举值必须与此处保持一致。
    """

    # ── 入站（前端 → Python）──────────────────────────────
    SEND_MESSAGE            = "SEND_MESSAGE"
    HITL_DECISION           = "HITL_DECISION"
    PING                    = "PING"
    INTERACTION_RESPONSE    = "INTERACTION_RESPONSE"
    PLAN_APPROVAL_DECISION  = "PLAN_APPROVAL_DECISION"
    STOP_GENERATION         = "STOP_GENERATION"
    MODEL_LIST_REQUEST      = "MODEL_LIST_REQUEST"   # 入站：请求可选模型清单
    REQUEST_SCHEDULED_TASKS = "REQUEST_SCHEDULED_TASKS"
    DELETE_SCHEDULED_TASK   = "DELETE_SCHEDULED_TASK"
    SKILL_LIST              = "SKILL_LIST"
    SKILL_GET               = "SKILL_GET"
    SKILL_SAVE              = "SKILL_SAVE"
    SKILL_DELETE            = "SKILL_DELETE"
    SKILL_IMPORT            = "SKILL_IMPORT"
    SKILL_EXPORT            = "SKILL_EXPORT"
    SEARCH                  = "SEARCH"

    # ── 出站（Python → 前端）──────────────────────────────
    REPLY_START           = "REPLY_START"
    TOKEN                 = "TOKEN"
    REASONING_TOKEN       = "REASONING_TOKEN"
    TOOL_START            = "TOOL_START"
    TOOL_END              = "TOOL_END"
    HITL_REQUEST          = "HITL_REQUEST"
    APPROVAL_RESULT       = "APPROVAL_RESULT"
    TASK_NOTIFICATION     = "TASK_NOTIFICATION"
    USER_INPUT_ECHO       = "USER_INPUT_ECHO"
    PERMISSION_DENIED     = "PERMISSION_DENIED"
    REPLY_END             = "REPLY_END"
    AGENT_REPLY           = "AGENT_REPLY"
    AGENT_HALTED          = "AGENT_HALTED"
    PLAN_APPROVAL_REQUEST = "PLAN_APPROVAL_REQUEST"
    ERROR                 = "ERROR"
    PONG                  = "PONG"
    INTERACTION_REQUEST   = "INTERACTION_REQUEST"
    AGENT_TASK_EVENT      = "AGENT_TASK_EVENT"
    QUICK_APP_DATA        = "QUICK_APP_DATA"
    SKILL_PROGRESS        = "SKILL_PROGRESS"
    SCHEDULED_TASK_LIST   = "SCHEDULED_TASK_LIST"
    SCHEDULED_TASK_CHANGED = "SCHEDULED_TASK_CHANGED"  # 增量：单个任务变更（D2 Push 增量通道）
    SKILL_LIST_RESULT     = "SKILL_LIST_RESULT"
    SKILL_GET_RESULT      = "SKILL_GET_RESULT"
    SKILL_SAVED           = "SKILL_SAVED"
    SKILL_DELETED         = "SKILL_DELETED"
    SKILL_ACTIVATED       = "SKILL_ACTIVATED"
    SKILL_CLEARED         = "SKILL_CLEARED"
    SKILL_IMPORTED        = "SKILL_IMPORTED"
    SKILL_EXPORTED        = "SKILL_EXPORTED"

    # ── 全局搜索（命令面板 ⌘K）──────────────────────────────
    SEARCH_RESULT         = "SEARCH_RESULT"

    # ── Dashboard 看板 ──────────────────────────────────────
    DASHBOARD_REQUEST     = "DASHBOARD_REQUEST"    # 入站：请求看板快照
    DASHBOARD_DATA        = "DASHBOARD_DATA"       # 出站：看板快照（global + sessions + degradations）

    # ── 预算额度（按 provider 分账）────────────────────────
    SET_BUDGET            = "SET_BUDGET"            # 入站：设/改某 provider 额度
    BUDGET_QUERY          = "BUDGET_QUERY"          # 入站：查询全部 provider 额度态
    BUDGET_STATUS         = "BUDGET_STATUS"         # 出站：每 provider 额度视图（额度条）

    # ── 多 Session 并发 ─────────────────────────────────────
    SESSION_CONCURRENCY   = "SESSION_CONCURRENCY"  # 三态：queued/started/released

    # ── 模型选择 ────────────────────────────────────────────
    MODEL_LIST            = "MODEL_LIST"           # 出站：可选模型清单 + default

    # ── 会话列表（UI 会话管理）─────────────────────────────
    # 入站
    SESSION_LIST_REQUEST      = "SESSION_LIST_REQUEST"
    SESSION_CREATE            = "SESSION_CREATE"
    SESSION_SWITCH            = "SESSION_SWITCH"
    SESSION_DELETE            = "SESSION_DELETE"
    SESSION_RENAME            = "SESSION_RENAME"
    SESSION_GROUP_MUTATE      = "SESSION_GROUP_MUTATE"
    SESSION_HISTORY_REQUEST   = "SESSION_HISTORY_REQUEST"
    # 出站
    SESSION_LIST         = "SESSION_LIST"
    SESSION_SWITCHED     = "SESSION_SWITCHED"
    SESSION_UPDATED      = "SESSION_UPDATED"
    SESSION_DELETED      = "SESSION_DELETED"
    SESSION_GROUP_LIST   = "SESSION_GROUP_LIST"
    SESSION_HISTORY_LIST = "SESSION_HISTORY_LIST"

    # ── LLM 凭据管理（BYOK）─────────────────────────────────
    # 入站
    LOAD_CREDENTIALS       = "LOAD_CREDENTIALS"        # 入站：加载已有凭据（设置页回填）
    SAVE_LLM_CREDENTIALS   = "SAVE_LLM_CREDENTIALS"    # 入站：保存凭据列表（整体覆写）
    VERIFY_CREDENTIALS     = "VERIFY_CREDENTIALS"      # 入站：连通性校验
    GET_CREDENTIALS_STATUS = "GET_CREDENTIALS_STATUS"  # 入站：查询配置状态（门禁）
    # 出站
    CREDENTIALS_LIST      = "CREDENTIALS_LIST"         # 出站：已有凭据列表（api_key 脱敏）
    CREDENTIALS_SAVED     = "CREDENTIALS_SAVED"        # 出站：保存结果
    CREDENTIALS_VERIFIED  = "CREDENTIALS_VERIFIED"     # 出站：校验结果
    CREDENTIALS_STATUS    = "CREDENTIALS_STATUS"       # 出站：配置状态

    # ── 认证会话（JWT 自动续期）────────────────────────────
    # 真相源：本节与 pandapal_desktop/src/types/api.ts 的 ApiMessageType 必须同步更新
    AUTH_TOKEN_REFRESHED  = "AUTH_TOKEN_REFRESHED"   # 出站：token 刷新成功（带新 token，前端回写 store）
    AUTH_EXPIRED          = "AUTH_EXPIRED"           # 出站：登录态彻底失效（前端登出并跳登录页）

    # ── 未识别事件兜底 ──────────────────────────────────────
    UNKNOWN               = "UNKNOWN"                # 出站：未知事件类型兜底


@dataclass(frozen=True)
class InboundIpcMessage:
    """前端发来的 IPC 消息结构体。

    JSON 格式：
        {"type": "SEND_MESSAGE", "msg_id": "...", "content": "..."}
        {"type": "HITL_DECISION", "msg_id": "...", "run_id": "...", "decision": "approved"}
        {"type": "PING", "msg_id": "..."}
    """

    type: str
    msg_id: str
    # 可选字段（依 type 而定）
    content: str | None = None
    run_id: str | None = None
    decision: str | None = None
    response: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "InboundIpcMessage":
        """从已解析的 JSON dict 构造（忽略未知字段，Fail-Safe）。"""
        return cls(
            type=str(d.get("type", "")),
            msg_id=str(d.get("msg_id", "")),
            content=d.get("content"),
            run_id=d.get("run_id"),
            decision=d.get("decision"),
            response=d.get("response"),
            extra=d.get("extra", {}),
        )


@dataclass(frozen=True)
class OutboundIpcMessage:
    """Python 推送到前端的 IPC 消息结构体。

    JSON 格式：
        {"type": "TOKEN",       "msg_id": "...", "token": "字"}
        {"type": "REPLY_START", "msg_id": "...", "reply_id": "..."}
        {"type": "REPLY_END",   "msg_id": "...", "reply_id": "...", "output": "完整回复"}
        {"type": "TOOL_START",  "msg_id": "...", "tool_name": "...", "tool_call_id": "..."}
        {"type": "TOOL_END",    "msg_id": "...", "tool_call_id": "...", "result_summary": "..."}
        {"type": "HITL_REQUEST","msg_id": "...", "run_id": "...", "tool_name": "...", ...}
        {"type": "ERROR",       "msg_id": "...", "message": "..."}
        {"type": "PONG",        "msg_id": "..."}
    """

    type: str
    msg_id: str
    # 流式 token
    token: str | None = None
    # 推理 token
    reasoning_token: str | None = None
    # 回复 ID（一轮对话的唯一标识）
    reply_id: str | None = None
    # 回复结束时的完整 output
    output: str | None = None
    # 工具调用
    tool_name: str | None = None
    tool_call_id: str | None = None
    tool_args_summary: str | None = None
    progress_label: str | None = None
    result_summary: str | None = None
    # HITL 审批
    run_id: str | None = None
    # 错误
    message: str | None = None
    # Plan Mode 审批
    plan_action: str | None = None
    edited_plan_content: str | None = None
    plan_path: str | None = None
    plan_content: str | None = None
    # 通用扩展（其余字段以 dict 传递）
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON-safe dict（None 字段跳过）。"""
        d: dict[str, Any] = {
            "type": self.type,
            "msg_id": self.msg_id,
        }
        if self.token is not None:
            d["token"] = self.token
        if self.reasoning_token is not None:
            d["reasoning_token"] = self.reasoning_token
        if self.reply_id is not None:
            d["reply_id"] = self.reply_id
        if self.output is not None:
            d["output"] = self.output
        if self.tool_name is not None:
            d["tool_name"] = self.tool_name
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_args_summary is not None:
            d["tool_args_summary"] = self.tool_args_summary
        if self.progress_label is not None:
            d["progress_label"] = self.progress_label
        if self.result_summary is not None:
            d["result_summary"] = self.result_summary
        if self.run_id is not None:
            d["run_id"] = self.run_id
        if self.message is not None:
            d["message"] = self.message
        if self.extra:
            d.update(self.extra)
        return d


@dataclass(frozen=True)
class ScheduledTaskItem:
    """定时任务列表项（frozen dataclass，所有字段有默认值，防缺失崩溃）。

    在 push_task_list 闭包中从 TaskDefinition + last execution 构建，
    经 NormalizedEvent(SCHEDULED_TASK_LIST) → IpcStdoutTransport → IPC:JSON 到前端。
    """

    task_id: str = ""
    name: str = ""
    trigger_type: str = ""          # recurring / oneshot / event / manual
    cron_expression: str = ""
    task_prompt: str = ""
    session_id: str = ""
    sensitivity: str = "medium"
    created_at: str = ""            # ISO
    last_status: str = ""           # pending / running / completed / failed / cancelled
    last_run_at: str = ""           # ISO，空=从未执行
