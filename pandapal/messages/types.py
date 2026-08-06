"""pandapal/messages/types.py — 消息类型契约。

所有在路由层、调度层、HITL 层、Bridge 层流转的 message_type 字符串
必须在此声明为命名常量，严禁在业务模块中直接使用字符串字面量。

设计原则：
- 使用普通 class 属性而非 Enum，保持与 message_type: str 字段的零摩擦兼容性。
- RouterMessageType 是 Router 路由表 key 常量（入站归一化改造后仅保留 9 种
  Router 词汇；直通词汇的权威来源是 IpcMessageType，见 desktop_ipc/message_codec.py）。
- HITLDecision 单独声明，规范所有审批决策字符串（消除 "approve"/"approved" 混用）。

入站消息（渠道 gate → InboundDispatcher → Router → Handler）：
  RouterMessageType.*

HITL 审批决策字符串（pandaren Agent.run(hitl_decision=...) 要求的值）：
  HITLDecision.*
"""

from __future__ import annotations


class RouterMessageType:
    """Router 路由表 key 常量。

    由路由层 MessageRouter 的路由表 key 使用，必须与 register_route_handler() 的
    message_type 参数保持一致。

    HITL 消息命名规范（统一使用 APPROVAL_* 前缀）：
    - APPROVAL_NEEDED   : Agent 暂停，通知 HITLBridge 需要人工审批（Engine → HITLBridge）
    - APPROVAL_RESPONSE : 用户给出审批决策（设备/文字 → HITLBridge）
    - APPROVAL_DECISION : HITLBridge 决策完成，通知 Scheduler 恢复 Agent（HITLBridge → Scheduler）
    """

    # 用户来自外部渠道的普通文字/语音指令
    USER_INSTRUCTION = "user_instruction"

    # HITLBridge 决策完成后注入，通知 Scheduler 恢复 Agent（原 HITL_DECISION）
    APPROVAL_DECISION = "approval_decision"

    # 调度器收到的任务执行指令（由 TaskScheduler 注入）
    TASK_INSTRUCTION = "task_instruction"

    # 任务执行结果（由 AgentScheduler 注入，TaskScheduler 消费——对称路由）
    TASK_RESULT = "task_result"

    # Agent 暂停通知（由 AgentScheduler 注入给 HITLBridge，原 HITL_REQUEST）
    APPROVAL_NEEDED = "approval_needed"

    # 用户在前端/设备点击审批按钮，或 Scheduler 文字决策转换后注入（→ HITLBridge）
    APPROVAL_RESPONSE = "approval_response"

    # 用户对交互型工具的回复 → Scheduler.handle_interaction_response()
    INTERACTION_RESPONSE = "interaction_response"

    # Plan Mode 审批决策 → PlanModeManager.resume()
    PLAN_APPROVAL_DECISION = "plan_approval_decision"

    # 停止当前正在执行的 Agent 生成
    STOP_GENERATION = "stop_generation"


class HITLDecision:
    """HITL 审批决策字符串常量。

    pandaren SDK Agent.run(hitl_decision=...) 要求的合法值，
    以及 ApprovalRepository.resolve_approval_request() 传入的 decision 字符串。

    协议层根定义：本类是 APPROVED / REJECTED 两个决策值的唯一权威来源。
    storage.models.ApprovalDecision 的对应值派生自此处，而非反向依赖。

    历史问题说明：
        错误写法："approve" / "reject"（动词原形，来自按钮 EventKey）
        正确写法："approved" / "rejected"（过去分词，SDK 期望的决策状态）
    使用本常量可彻底消除此类混用。
    """

    # 用户批准 — 传给 pandaren Agent.run(hitl_decision="approved")
    APPROVED = "approved"

    # 用户拒绝 — 传给 pandaren Agent.run(hitl_decision="rejected")
    REJECTED = "rejected"
