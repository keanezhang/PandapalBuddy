"""pandaren/engine/types.py — 枚举与基础类型定义"""

from enum import Enum


class NextStep(str, Enum):
    """Loop 每步执行后的决策类型。

    每个 step 结束后，Loop 根据执行结果决定下一步动作：

      CONTINUE  — 正常继续，进入下一个 step（最常见路径）
      RETRY     — 当前 step 执行失败但可重试（如 LLM 临时错误），重新执行本 step
      FINAL     — LLM 返回了最终答案（无 tool_call），Loop 正常结束
      HALT      — 遇到不可恢复的终止条件（超时、超预算、工具强制停止等），立即中断
      PAUSE     — HITL（Human-In-The-Loop）场景，暂停等待人工审批后再继续
      HANDOFF   — 将控制权移交给另一个 Agent（多 Agent 协作场景）
    """
    CONTINUE = "continue"
    RETRY = "retry"
    FINAL = "final"
    HALT = "halt"
    PAUSE = "pause"
    HANDOFF = "handoff"


class RunStatus(str, Enum):
    """Run 生命周期状态。

    一次完整 run 的状态流转：

      PENDING   — 已提交但尚未开始执行（排队中）
      RUNNING   — 正在执行 step 循环
      PAUSED    — HITL 暂停，等待人工审批
      COMPLETED — 正常完成（LLM 给出最终答案）
      FAILED    — 异常终止（LLM 错误、超时、溢出等）
      CANCELLED — 被外部主动取消（用户中断或超时强杀）
    """
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TerminalReason(str, Enum):
    """Loop 终止原因。

    记录在 RunResult 和 AuditLog 中，用于监控、告警和问题排查。

    ── 正常终止 ──
      COMPLETED            — LLM 返回最终答案，任务完成

    ── 预算 / 限制超出 ──
      MAX_STEPS_EXCEEDED   — 达到最大 step 数限制，强制停止
      STEP_TIMEOUT         — 单个 step 执行超时
      TOTAL_TIMEOUT        — 整个 run 总时长超时
      HALTED_BY_GUARD      — 应用层 StepGuard 每步裁决停机（如费用超限；具体理由见 error）

    ── 上下文问题 ──
      CONTEXT_OVERFLOW     — 压缩后 token 数仍超过阈值，无法继续

    ── 工具 / 权限问题 ──
      TOOL_HALT            — 工具执行返回强制停止信号（工具主动终止 run）
      TOOLS_EXHAUSTED      — 工具预算耗尽（调用次数或费用超限）
      PERMISSION_EXHAUSTED — HITL 权限申请被拒绝次数超限

    ── 安全 / 质量问题 ──
      CIRCUIT_BREAKER      — 熔断器触发（连续失败次数过多）
      LLM_LOOP_DETECTED    — 检测到 LLM 陷入重复循环（输出雷同）
      AUDIT_FAILURE        — 审计检查不通过，run 被强制中断

    ── 外部中断 ──
      HITL_PAUSED          — HITL 触发，run 挂起等待人工审批
      HITL_REJECTED        — HITL 审批被人工拒绝
      INTERACTION_PAUSED   — 交互型工具触发，run 挂起等待用户回复
      CANCELLED            — 外部主动取消（用户中断或系统强杀）

    ── 规划完成 ──
      PLAN_COMPLETE        — Plan Mode 规划阶段正常完成，等待用户批准

    ── LLM 错误 ──
      LLM_ERROR            — LLM 调用失败（网络错误、API 报错、响应解析失败等）
    """
    COMPLETED = "completed"
    PLAN_COMPLETE = "plan_complete"
    MAX_STEPS_EXCEEDED = "max_steps_exceeded"
    STEP_TIMEOUT = "step_timeout"
    TOTAL_TIMEOUT = "total_timeout"
    HALTED_BY_GUARD = "halted_by_guard"
    CIRCUIT_BREAKER = "circuit_breaker"
    PERMISSION_EXHAUSTED = "permission_exhausted"
    TOOL_HALT = "tool_halt"
    TOOLS_EXHAUSTED = "tools_exhausted"
    HITL_REJECTED = "hitl_rejected"
    HITL_PAUSED = "hitl_paused"
    INTERACTION_PAUSED = "interaction_paused"
    LLM_ERROR = "llm_error"
    LLM_LOOP_DETECTED = "llm_loop_detected"
    CONTEXT_OVERFLOW = "context_overflow"
    AUDIT_FAILURE = "audit_failure"
    CANCELLED = "cancelled"


class MessageTrust(str, Enum):
    """消息信任标签（S4 原则）。

    标记消息来源的可信程度，影响 Loop 对该消息的处理策略：

      HIGH   — 系统消息、用户直接输入，完全信任，不做额外校验
      MEDIUM — LLM 生成的消息，可能产生幻觉或被 prompt injection，使用时需谨慎
      LOW    — 外部工具返回、第三方数据源等不可信来源，使用前需额外校验
    """
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
