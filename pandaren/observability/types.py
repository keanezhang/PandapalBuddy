"""pandaren/observability/types.py — 枚举 + 数据结构

本模块定义可观测性层（Observability Layer）所有共享的枚举类型与不可变数据结构，
作为 logging、tracing、auditing 三大子系统的公共契约。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, IntEnum


# ──────────────────────────────────────────────
# 枚举：日志 / 追踪
# ──────────────────────────────────────────────

class LogLevel(IntEnum):
    """结构化日志的严重级别。

    使用 IntEnum 以便与标准库 logging 模块的数值体系对齐，
    支持直接进行数值比较（如 level >= LogLevel.WARN）。
    """
    DEBUG = 10   # 调试信息，生产环境一般不开启
    INFO  = 20   # 常规运行信息
    WARN  = 30   # 警告：未中断执行，但需关注
    ERROR = 40   # 错误：执行已受影响，需人工介入


class TraceLevel(Enum):
    """Trace 采集的详细程度策略。

    在性能与可观测性之间做权衡：
    - FULL     采集所有 Span（含中间步骤），适合调试与审计。
    - SUMMARY  只保留关键节点 Span，适合生产监控。
    - MINIMAL  仅保留 Run 级别的起止 Span，开销最低。
    """
    FULL    = "full"
    SUMMARY = "summary"
    MINIMAL = "minimal"


class SpanType(Enum):
    """Trace Span 的业务语义类型。

    每种类型对应 Agent 执行链路中一个可识别的执行单元，
    用于在 Trace 视图中按类型聚合、过滤和分析。
    """
    RUN           = "run"           # 一次完整的 Agent.run() 调用
    STEP          = "step"          # 单轮推理步骤（含 LLM 调用 + 工具调用）
    LLM_CALL      = "llm_call"      # 单次大语言模型 API 请求
    TOOL_CALL     = "tool_call"     # 单次工具（函数）调用
    GUARD_CHECK   = "guard_check"   # 安全护栏检查（输入/输出过滤）
    HITL_CHECK    = "hitl_check"    # Human-in-the-Loop 人工审批等待
    MESSAGE_BUILD = "message_build" # 上下文消息列表构建过程
    SKILL_INVOKE  = "skill_invoke"  # Skill 插件调用


class SpanStatus(Enum):
    """Span 执行的最终状态。"""
    OK        = "ok"        # 正常完成
    ERROR     = "error"     # 执行异常（含预期/非预期错误）
    CANCELLED = "cancelled" # 被外部中止（超时、HITL 拒绝等）


# ──────────────────────────────────────────────
# 枚举：审计
# ──────────────────────────────────────────────

class AuditEventType(Enum):
    """审计事件类型（21 种）。

    按业务域分组：
    - Run 生命周期：RUN_STARTED / RUN_FINISHED
    - 权限控制：PERMISSION_DENIED / PERMISSION_GRANTED_HIGH_RISK
    - HITL 流程：HITL_REQUESTED / HITL_APPROVED / HITL_REJECTED
    - Agent 管控：AGENT_TERMINATED / INPUT_BLOCKED / AUDIT_WRITE_FAILED
    - Tool 层：TOOL_EXECUTED
    - Skill 层：SKILL_INVOKED / SKILL_AUTO_TRIGGER_DENIED / SKILL_OVERRIDDEN
    - Agent Registry 层：AGENT_REGISTERED … AGENT_DELEGATE_CYCLE
    """
    # ── Run 生命周期 ──
    RUN_STARTED                  = "run_started"
    RUN_FINISHED                 = "run_finished"

    # ── 权限控制 ──
    PERMISSION_DENIED            = "permission_denied"
    PERMISSION_GRANTED_HIGH_RISK = "permission_granted_high_risk"

    # ── Human-in-the-Loop 流程 ──
    HITL_REQUESTED               = "hitl_requested"
    HITL_APPROVED                = "hitl_approved"
    HITL_REJECTED                = "hitl_rejected"

    # ── Agent 管控 ──
    AGENT_TERMINATED             = "agent_terminated"
    INPUT_BLOCKED                = "input_blocked"
    AUDIT_WRITE_FAILED           = "audit_write_failed"  # 审计写入失败，需告警

    # ── Tool 层审计事件 ──
    TOOL_EXECUTED                = "tool_executed"       # 工具执行完成（含成功与失败）

    # ── Skill 层审计事件 ──
    SKILL_INVOKED                = "skill_invoked"
    SKILL_AUTO_TRIGGER_DENIED    = "skill_auto_trigger_denied"  # 自动触发被拒绝
    SKILL_OVERRIDDEN             = "skill_overridden"           # Skill 被覆盖/替换

    # ── Agent Registry 层审计事件 ──
    AGENT_REGISTERED             = "agent_registered"
    AGENT_UNREGISTERED           = "agent_unregistered"
    AGENT_STATUS_CHANGED         = "agent_status_changed"
    AGENT_DELEGATED              = "agent_delegated"            # 任务委派发起
    AGENT_DELEGATE_COMPLETED     = "agent_delegate_completed"   # 委派任务完成
    AGENT_DELEGATE_DENIED        = "agent_delegate_denied"      # 委派被权限拒绝
    AGENT_DELEGATE_CYCLE         = "agent_delegate_cycle"       # 检测到委派环路


class AuditSeverity(Enum):
    """审计记录的严重程度，用于告警路由和存储分级。"""
    INFO     = "info"     # 正常业务事件，仅作记录
    WARN     = "warn"     # 需关注的异常，可延迟处理
    CRITICAL = "critical" # 高风险事件，需立即告警并人工处理


# ──────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────

@dataclass(frozen=True)
class ObservabilityContext:
    """每次 run 的观测上下文（frozen，不可变）。

    在 Agent.run() 入口处创建，随执行链路向下透传，
    作为所有 Span、Log、AuditRecord 的共同索引键。

    Attributes:
        run_id:        本次 run 的唯一标识（UUID hex）。
        agent_id:      发起本次 run 的 Agent 标识。
        trace_id:      所属 Trace 的标识（多个 run 可共享同一 trace）。
        parent_span_id: 父 Span 的 ID，用于 Trace 树形构建（可选）。
    """
    run_id:         str
    agent_id:       str
    trace_id:       str
    parent_span_id: str | None = None


@dataclass(frozen=True)
class AuditRecord:
    """单条审计记录（不可变）。

    写入后不得修改，保证审计链的完整性与防篡改性。
    持久化后端（文件/DB/SIEM）应将其视为 append-only 记录。

    Attributes:
        timestamp:       事件发生的 UTC 时间戳。
        record_id:       本条记录的唯一 ID（UUID hex）。
        event_type:      审计事件类型，见 AuditEventType。
        severity:        严重程度，见 AuditSeverity。
        agent_id:        产生本事件的 Agent 标识。
        run_id:          关联的 run 标识，便于跨系统关联查询。
        session_id:      产生本事件的会话 ID（多 session 并发下用于路径分片；
                         启动期/无归属事件为空字符串，回落 global 存储）。
        detail:          人类可读的事件描述（自由文本）。
        step_n:          事件发生时的步骤序号（可选）。
        tool_name:       若事件与工具调用相关，填写工具名（可选）。
        terminal_reason: Agent 被终止时的原因说明（可选）。
    """
    timestamp:       datetime
    record_id:       str
    event_type:      AuditEventType
    severity:        AuditSeverity
    agent_id:        str
    run_id:          str
    detail:          str
    session_id:      str = ""
    step_n:          int | None = None
    tool_name:       str | None = None
    terminal_reason: str | None = None


@dataclass(frozen=True)
class Span:
    """Trace span（不可变）。

    代表分布式追踪中的一个执行单元，遵循 OpenTelemetry Span 语义。
    frozen=True 确保 Span 一旦结束（end_time 赋值）便不可变更，
    防止事后篡改 Trace 数据。

    Attributes:
        span_id:        本 Span 的唯一标识（UUID hex）。
        trace_id:       所属 Trace 的标识，用于关联同一请求链路的所有 Span。
        parent_span_id: 父 Span 的 ID；根 Span 为 None。
        span_type:      Span 的业务语义类型，见 SpanType。
        name:           Span 的可读名称（如 "llm_call:gpt-4o"）。
        agent_id:       产生本 Span 的 Agent 标识。
        run_id:         关联的 run 标识。
        session_id:     产生本 Span 的会话 ID（多 session 并发下用于路径分片；
                        无归属事件为空字符串）。
        step_n:         所属步骤的序号（可选）。
        start_time:     Span 开始时间，默认为创建时的 UTC 当前时间。
        end_time:       Span 结束时间；未结束时为 None。
        duration_ms:    执行耗时（毫秒）；由 end_time - start_time 计算后填入。
        status:         执行最终状态，见 SpanStatus。
        attributes:     附加的键值对属性，值类型限定为基本类型以便序列化。
    """
    span_id:        str
    trace_id:       str
    parent_span_id: str | None
    span_type:      SpanType
    name:           str
    agent_id:       str
    run_id:         str
    session_id:     str = ""
    step_n:         int | None = None
    start_time:     datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    end_time:       datetime | None = None
    duration_ms:    float | None = None
    status:         SpanStatus = SpanStatus.OK
    attributes:     dict[str, str | int | float | bool] = field(default_factory=dict)


# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────

def generate_id() -> str:
    """生成唯一 ID（UUID v4 hex）。

    返回 32 位小写十六进制字符串（无连字符），
    作为 run_id、span_id、record_id 等字段的默认生成器。

    Returns:
        str: 32 位 UUID v4 hex 字符串，例如 "a3f2c1d0e4b5..."。
    """
    return uuid.uuid4().hex
