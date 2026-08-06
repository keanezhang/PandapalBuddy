"""pandapal_relay/router_models.py — 路由层数据模型与异常（Relay 本地副本）。

★ 来源：pandapal/router/models.py（完整复制，内部 import 已重写为本地 .message_types）
★ 用途：Relay 服务端独立部署，不依赖完整 pandapal 包。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal
from typing import Protocol, runtime_checkable

from .message_types import RouterMessageType


@runtime_checkable
class GatewayProtocol(Protocol):
    """Gateway 依赖的最小接口契约（BL4 DI）。

    MessageRouter 只依赖此 Protocol，不依赖 Gateway 具体实现。
    任何实现了 register_inbound_handler 的对象均可作为 gateway 注入。
    """

    def register_inbound_handler(
        self, handler: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> None:
        """注册入站消息处理回调。"""
        ...


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

    合法的 message_type 值（来自 RouterMessageType）：
    - USER_INSTRUCTION      = "user_instruction"        # 用户来自外部渠道的普通文字/语音指令
    - APPROVAL_DECISION     = "approval_decision"       # HITLBridge 决策完成
    - TASK_INSTRUCTION      = "task_instruction"        # 调度器收到的任务执行指令
    - TASK_RESULT           = "task_result"             # 任务执行结果
    - APPROVAL_NEEDED       = "approval_needed"         # Agent 暂停通知
    - APPROVAL_RESPONSE     = "approval_response"       # 用户审批按钮/文字决策
    - INTERACTION_RESPONSE  = "interaction_response"    # 交互型工具回复
    - PLAN_APPROVAL_DECISION = "plan_approval_decision" # Plan Mode 审批决策
    - STOP_GENERATION       = "stop_generation"         # 停止生成

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

    触发场景：attach_to_gateway() 时路由表为空。
    """

    pass


class RouterStateError(Exception):
    """路由层生命周期/状态错误。

    触发场景：在 attach_to_gateway() 之后再次调用 register_route_handler()。
    设计依据（04 文档 v1.1 失败情况 9）：
    - 运行时动态注册会与正在并发执行的 _dispatch_message_frame 产生竞态
      （查表与写表无锁保护）
    - 同时违反 Bootstrap 启动顺序约定（先 register 再 attach）
    """

    pass


class RouterPermissionError(Exception):
    """路由层权限错误（CLI 短路径专用）。

    触发场景：调用 inject_inbound_message() 时，msg.source_channel_id 不等于 "__cli__"。
    设计依据（04 文档 v1.1 Step 5b、附录 D04-3）：
    - inject_inbound_message 是同进程的本地短路径，绕过了 Gateway 的鉴权
    - 强制要求 source_channel_id == "__cli__" 防止该方法被误用为权限旁路
      （伪造 user_id / message_type 绕过 Gateway 鉴权）
    """

    pass


class PayloadParseError(Exception):
    """消息负荷解析失败。

    触发场景：JSON 解析失败 / 必填字段缺失或非空字符串校验失败 / 类型不匹配。
    """

    pass


class PayloadTooLargeError(Exception):
    """消息负荷超出 _max_payload_bytes 上限。

    触发场景：_parse_inbound_message 在 json.loads 之前检查 len(payload) > 上限。
    设计依据（04 文档 v1.1 Step 5a / 失败情况 8）：
    - JSON 解析的内存峰值可能是 payload 字节数的 2-3 倍
    - 提前拦截避免恶意/异常超大 payload 拖垮进程内存（OOM 防御）
    """

    pass
