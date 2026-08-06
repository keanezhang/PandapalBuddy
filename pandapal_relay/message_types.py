"""pandapal_relay/message_types.py — 消息类型契约（Relay 本地副本）。

★ 来源：pandapal/messages/types.py（完整复制）
★ 用途：Relay 服务端独立部署，不依赖完整 pandapal 包。
★ 同步约束：修改后必须与 pandapal/messages/types.py 保持完全一致；
  本文件中的所有字符串字面量和类名都是协议层契约，
  一旦与主服务产生偏差会导致消息路由/广播失败。

★ 2026-06-14 D6 修复：补齐 PLAN_APPROVAL_DECISION / STOP_GENERATION；
  删除 OutboundMessageType（D1 修复后已无引用，改为 NormalizedEvent 直通）。
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
    """HITL 审批决策字符串常量（Relay 独立副本）。

    ⚠️  同步约束：本类的字符串字面量必须与
        pandapal/messages/types.py:HITLDecision 保持完全一致。
        若修改 HITLDecision，请同步更新此处，否则 Relay 服务将与主服务产生决策不一致。

    Relay 服务独立部署，不依赖完整 pandapal 包，因此无法直接 import messages 层，
    故维护此本地副本。

    历史问题说明：
        错误写法："approve" / "reject"（动词原形，来自按钮 EventKey）
        正确写法："approved" / "rejected"（过去分词，SDK 期望的决策状态）
    使用本常量可彻底消除此类混用。
    """

    # 用户批准 — 必须与 pandapal.messages.types.HITLDecision.APPROVED 保持一致
    APPROVED = "approved"

    # 用户拒绝 — 必须与 pandapal.messages.types.HITLDecision.REJECTED 保持一致
    REJECTED = "rejected"
