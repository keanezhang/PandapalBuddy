"""Router 层数据模型与异常。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pandapal.messages.types import RouterMessageType


@dataclass(frozen=True)
class InboundMessage:
    """路由层的入站消息（不可变跨层传递）。

    字段说明：
    - msg_id: 消息唯一 ID（用于去重）
    - message_type: 消息类型（必须是 RouterMessageType.* 常量之一）
    - source_channel_id: 来源渠道 ID（如 "web_xxx", "wechat_xxx"）
    - user_id: 用户 ID
    - session_id: 会话 ID（远程渠道由发起方 relay 渠道 bridge mint 稳定渠道会话 id，
      必填；缺失将在 Gateway adapter 被拒——契约零兜底，下游绝不创建）
    - content: 消息内容（透明传递，路由层不校验内部结构）

    合法的 message_type 值（来自 pandapal.messages.types.RouterMessageType）：
    - USER_INSTRUCTION  = "user_instruction"   # 用户来自外部渠道的普通文字/语音指令
    - APPROVAL_RESPONSE = "approval_response"  # 用户在前端/设备点击审批按钮
    - INTERACTION_RESPONSE = "interaction_response"
    - TASK_INSTRUCTION  = "task_instruction"   # 调度器收到的任务执行指令
    - STOP_GENERATION   = "stop_generation"
    （其余见 RouterMessageType 定义）

    类型约束：
    - message_type 类型为 Literal，只能取 RouterMessageType 中定义的值
    - 运行时会在 __post_init__ 中校验值合法性
    """

    msg_id: str
    message_type: Literal[
        RouterMessageType.USER_INSTRUCTION,
        RouterMessageType.APPROVAL_DECISION,
        RouterMessageType.TASK_INSTRUCTION,
        RouterMessageType.TASK_RESULT,
        RouterMessageType.APPROVAL_NEEDED,
        RouterMessageType.APPROVAL_RESPONSE,
        RouterMessageType.INTERACTION_RESPONSE,
        RouterMessageType.PLAN_APPROVAL_DECISION,
        RouterMessageType.STOP_GENERATION,
    ]  # ⚠️ 必须是 RouterMessageType.* 常量之一
    source_channel_id: str
    user_id: str
    session_id: str | None = None
    content: Any = None

    def __post_init__(self) -> None:
        """运行时校验 message_type 合法性。"""
        valid_types = {
            RouterMessageType.USER_INSTRUCTION,
            RouterMessageType.APPROVAL_DECISION,
            RouterMessageType.TASK_INSTRUCTION,
            RouterMessageType.TASK_RESULT,
            RouterMessageType.APPROVAL_NEEDED,
            RouterMessageType.APPROVAL_RESPONSE,
            RouterMessageType.INTERACTION_RESPONSE,
            RouterMessageType.PLAN_APPROVAL_DECISION,
            RouterMessageType.STOP_GENERATION,
        }
        if self.message_type not in valid_types:
            raise ValueError(
                f"Invalid message_type: '{self.message_type}'. "
                f"Must be one of RouterMessageType.* constants: {valid_types}"
            )


class RouterConfigError(Exception):
    """路由层配置错误（I1 快速失败）。

    触发场景：freeze() 时路由表为空。
    """

    pass


class RouterStateError(Exception):
    """路由层生命周期/状态错误。

    触发场景：在 freeze() 之后再次调用 register_route_handler()。
    设计依据（04 文档 v1.1 失败情况 9）：
    - 运行时动态注册会与正在并发执行的 inject/_route_and_execute 产生竞态
      （查表与写表无锁保护）
    - 同时违反 Bootstrap 启动顺序约定（先 register 再 freeze）
    """

    pass


class RouterPermissionError(Exception):
    """路由层权限错误（inject 短路径专用）。

    触发场景：调用 inject_inbound_message() 时，msg.source_channel_id 不在白名单内。
    设计依据（04 文档 v1.1 Step 5b、附录 D04-3）：
    - inject_inbound_message 是同进程的本地短路径，绕过了 Gateway 的鉴权
    - 强制校验 source_channel_id 必须在 channel_ids 白名单内，防止该方法被误用为权限旁路
      （伪造 user_id / message_type 绕过 Gateway 鉴权）
    """

    pass
