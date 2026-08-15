"""Storage 层数据模型。

所有跨层传递的数据结构使用 @dataclass(frozen=True)，
防止意外状态修改（遵循全局硬约束）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from pandapal.messages.types import HITLDecision


# ──────────────────────────────────────────────
# 枚举
# ──────────────────────────────────────────────


class TaskExecutionStatus(str, Enum):
    """任务执行状态。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApprovalStatus(str, Enum):
    """审批请求的生命周期状态（ApprovalRequest.status 字段的合法值）。

    与 ApprovalDecision 的区别：
      ApprovalStatus = 请求是否仍在处理中（pending → resolved 单向流转）
      ApprovalDecision = 决策结果是什么（approved / rejected / timeout）
    """

    PENDING  = "pending"   # 等待用户决策
    RESOLVED = "resolved"  # 已决策（decision 字段记录具体结果）


class ApprovalDecision(str, Enum):
    """HITL 审批决策（持久化状态）。

    APPROVED / REJECTED 派生自 messages.types.HITLDecision（协议层根定义），
    保证消息协议值与数据库存储值始终一致。
    """

    APPROVED = HITLDecision.APPROVED  # "approved" — 派生自协议层
    REJECTED = HITLDecision.REJECTED  # "rejected" — 派生自协议层


class AgentTaskStatus(str, Enum):
    """AgentTask 状态（AI 自主管理的步骤状态机）。

    不同于 TaskExecutionStatus（TaskScheduler 的调度状态机），
    此处是 AI 在单次会话内对步骤的自主管理。
    """

    PENDING     = "pending"      # 待处理
    IN_PROGRESS = "in_progress"  # 正在执行（依赖检查通过后可并行）
    COMPLETED   = "completed"    # 已完成
    FAILED      = "failed"       # 执行失败
    CANCELLED   = "cancelled"    # 已取消


# ──────────────────────────────────────────────
# Frozen Dataclasses
# ──────────────────────────────────────────────


@dataclass(frozen=True)
class Session:
    """会话（持久化到 sessions 表）。

    ⚠️ 两套语义共用一张表：
    - SessionManager 关注 last_active（消息 session 超时判断）
    - SessionListManager 关注 title/preview/is_empty/is_favorite/is_deleted/group_id
      （UI 会话元数据）
    Repo 写入时 last_active 与 updated_at 保持同步（防漂移）。
    """

    session_id: str
    user_id: str
    device_id: str
    last_active: datetime
    created_at: datetime
    # ── UI 会话元数据（v003 引入）─────────────────────────────
    title: str = ""
    preview: str = ""
    message_count: int = 0
    is_empty: bool = False
    is_favorite: bool = False
    is_deleted: bool = False
    updated_at: datetime | None = None
    group_id: str | None = None


@dataclass(frozen=True)
class SessionGroup:
    """会话分组（持久化到 session_groups 表）。

    - 用户自定义分组名称，1:1 绑定 sessions.group_id
    - UNIQUE(user_id, name) 防重名
    """

    id: str
    user_id: str
    name: str
    created_at: datetime
    session_ids: list[str] = field(default_factory=list)  # 正向记录：组内会话 id 列表


@dataclass(frozen=True)
class TaskDefinition:
    """任务定义（持久化到 task_definitions 表）。"""

    task_id: str
    user_id: str
    name: str
    trigger_rule_json: str
    task_prompt: str
    session_id: str = ""
    sensitivity: str = "medium"
    created_at: datetime | None = None


@dataclass(frozen=True)
class TaskExecution:
    """任务执行记录（持久化到 task_executions 表）。"""

    execution_id: str
    task_id: str
    user_id: str
    status: TaskExecutionStatus = TaskExecutionStatus.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result_json: str | None = None
    source_channel_id: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class DeviceRegistration:
    """设备注册信息（持久化到 device_registrations 表）。"""

    device_id: str
    user_id: str
    channel_type: str
    is_online: bool = False
    registered_at: datetime | None = None
    last_seen: datetime | None = None


@dataclass(frozen=True)
class ApprovalRequest:
    """HITL 审批请求（持久化到 approval_requests 表）。

    BL7 约束：resolve 操作必须为原子 compare-and-update。
    """

    approval_id: str
    user_id: str
    run_id: str
    tool_name: str
    tool_args_summary: str | None = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    decision: str | None = None
    created_at: datetime | None = None
    resolved_at: datetime | None = None
    decision_user_id: str | None = None
    session_id: str | None = None
    source_channel_id: str = ""
    reply_id: str | None = None  # Option C: 原始回复周期 ID，HITL 恢复后关闭同一周期


@dataclass(frozen=True)
class AvatarConfig:
    """Avatar 配置（持久化到 avatar_configs 表）。"""

    user_id: str
    character_name: str
    animation_list_json: str = "[]"
    state_animation_map_json: str = "{}"
    updated_at: datetime | None = None


@dataclass(frozen=True)
class AgentTask:
    """AI 自驱任务（持久化到 agent_tasks 表）。

    设计约束：
    - D1 (Storage Abstraction): blocks/blocked_by 为 list[str]，Repo 负责序列化
    - D5 (No Business Logic in DB): 状态校验在 Repo Python 代码中
    - BL5 (Immutable): frozen=True，跨层传递不可变
    """

    task_id: str
    session_id: str
    user_id: str
    subject: str
    description: str = ""
    active_form: str = ""
    status: AgentTaskStatus = AgentTaskStatus.PENDING
    blocks: list[str] | None = None       # 本任务阻塞哪些任务（task_id 列表）
    blocked_by: list[str] | None = None   # 本任务被哪些任务阻塞（task_id 列表）
    order: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None
    # V2: 代码验证字段 — AI 不能自声明 completed，须经独立验证 Agent 读代码确认
    verify_hint: str = ""       # 验证指引，如 "在 loop.py 中存在 'ContextVar'"；纯查询留空可跳过验证
    verified: bool = False      # 是否已通过验证（只能由 verify_agent_task 工具写入）
    verify_evidence: str = ""   # 验证 Agent 找到的代码证据片段
