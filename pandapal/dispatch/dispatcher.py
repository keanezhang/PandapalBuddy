"""pandapal/dispatch/dispatcher.py — 入站分发中枢（渠道无关核心）。

对标出站的 MessageBroadcast：
- 直通类型：查注册表 → 渠道作用域检查 → await handler(msg_type, data, ctx)
  → 统一 broadcast.send(返回事件, origin_channel_id=ctx.channel_id)
- Router 类型：adapter.build_inbound_message() → router.inject_inbound_message()
- 未知/越权：WARN + drop（O3 永不抛异常）

直通 handler 只构建并返回 NormalizedEvent（或事件列表 / None），由本类统一
broadcast.send() 并注入 origin_channel_id；handler 禁止持有/触碰任何 Transport。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from pandapal.broadcast.broadcaster import MessageBroadcast
from pandapal.dispatch.adapter import InboundChannelAdapter
from pandapal.dispatch.types import ChannelContext, InboundEnvelope
from pandapal.events.normalized import NormalizedEvent
from pandapal.messages.types import RouterMessageType
from pandapal.router.router import MessageRouter

logger = logging.getLogger(__name__)

# 直通 handler 签名：async (msg_type, data, ctx) -> NormalizedEvent | list | None
#   handler 只构建事件不发送；返回 None 表示回包由其他路径负责（豁免路径）。
DirectHandler = Callable[
    [str, dict, ChannelContext],
    Awaitable[NormalizedEvent | list[NormalizedEvent] | None],
]


@dataclass(frozen=True)
class _DirectEntry:
    handler: DirectHandler
    channels: frozenset[str] | None  # None=全渠道放行；否则仅白名单渠道


# Router 词汇全集（9 种）。dispatch 判定 ∈ 此集合 → 走 build_inbound_message + inject。
_ROUTER_TYPES: frozenset[str] = frozenset({
    RouterMessageType.USER_INSTRUCTION,
    RouterMessageType.APPROVAL_DECISION,
    RouterMessageType.TASK_INSTRUCTION,
    RouterMessageType.TASK_RESULT,
    RouterMessageType.APPROVAL_NEEDED,
    RouterMessageType.APPROVAL_RESPONSE,
    RouterMessageType.INTERACTION_RESPONSE,
    RouterMessageType.PLAN_APPROVAL_DECISION,
    RouterMessageType.STOP_GENERATION,
})

# 已知直通词汇全集（24 种）。⚠️ 与 IpcMessageType（desktop_ipc/message_codec.py）
# 的直通常量保持同值同步——dispatch 核心包不反向 import 渠道包（依赖方向 §1.8），
# 故此处以字面量声明；改动 IpcMessageType 直通常量时必须同步本集合（B5）。
_KNOWN_DIRECT_TYPES: frozenset[str] = frozenset({
    # 定时任务
    "REQUEST_SCHEDULED_TASKS",
    "DELETE_SCHEDULED_TASK",
    # 技能管理
    "SKILL_LIST",
    "SKILL_GET",
    "SKILL_SAVE",
    "SKILL_DELETE",
    "SKILL_IMPORT",
    "SKILL_EXPORT",
    # 模型清单
    "MODEL_LIST_REQUEST",
    # 凭据管理
    "LOAD_CREDENTIALS",
    "SAVE_LLM_CREDENTIALS",
    "VERIFY_CREDENTIALS",
    "GET_CREDENTIALS_STATUS",
    # 会话管理
    "SESSION_LIST_REQUEST",
    "SESSION_CREATE",
    "SESSION_SWITCH",
    "SESSION_DELETE",
    "SESSION_RENAME",
    "SESSION_GROUP_MUTATE",
    "SESSION_HISTORY_REQUEST",
    # 看板预算
    "DASHBOARD_REQUEST",
    "SET_BUDGET",
    "BUDGET_QUERY",
    # 全局搜索
    "SEARCH",
})


class InboundDispatcher:
    """入站分发中枢（渠道无关核心）。对标 MessageBroadcast。

    - 直通类型：查注册表 → 渠道作用域检查 → await handler(msg_type, data, ctx)
    - Router 类型：adapter.build_inbound_message() → router.inject_inbound_message()
    - 未知/越权：WARN + drop（O3 永不抛异常）
    """

    def __init__(self, router: MessageRouter, broadcast: MessageBroadcast) -> None:
        if router is None:
            raise ValueError("router cannot be None")
        if broadcast is None:
            raise ValueError("broadcast cannot be None")
        self._router = router
        self._broadcast = broadcast
        self._direct: dict[str, _DirectEntry] = {}
        self._frozen: bool = False

    def register(
        self,
        msg_type: str,
        handler: DirectHandler,
        *,
        channels: frozenset[str] | None = None,
    ) -> None:
        """注册直通 handler。freeze() 之后调用 raise（对齐 Router 注册冻结语义）。"""
        if self._frozen:
            raise RuntimeError(
                f"InboundDispatcher is frozen; cannot register {msg_type!r} "
                "(register all handlers before freeze)"
            )
        if msg_type in self._direct:
            raise ValueError(f"duplicate direct handler registration: {msg_type!r}")
        self._direct[msg_type] = _DirectEntry(handler=handler, channels=channels)

    def freeze(self) -> None:
        """运行期只读。app 接线完成后调用。"""
        self._frozen = True
        logger.info(
            "InboundDispatcher frozen (%d direct handlers registered)",
            len(self._direct),
        )

    async def dispatch(
        self, adapter: InboundChannelAdapter, env: InboundEnvelope
    ) -> None:
        """唯一分类决策点。O3：永不向外抛异常。"""
        try:
            entry = self._direct.get(env.msg_type)
            if entry is not None:
                if (
                    entry.channels is not None
                    and env.ctx.channel_id not in entry.channels
                ):
                    logger.warning(
                        "InboundDispatcher: %s not allowed on channel %s",
                        env.msg_type, env.ctx.channel_id,
                    )
                    return
                result = await entry.handler(env.msg_type, env.data, env.ctx)
                if result is not None:
                    events = result if isinstance(result, list) else [result]
                    for ev in events:
                        await self._broadcast.send(
                            ev, origin_channel_id=env.ctx.channel_id
                        )
                return

            if env.msg_type in _ROUTER_TYPES:
                # 渠道校验失败会抛 → 由外层兜底 WARN + drop
                msg = adapter.build_inbound_message(env)
                await self._router.inject_inbound_message(msg)
                return

            if env.msg_type in _KNOWN_DIRECT_TYPES:
                logger.warning(
                    "InboundDispatcher: no handler registered for %s",
                    env.msg_type,
                )
            else:
                logger.warning(
                    "InboundDispatcher: unknown msg_type=%r, dropped",
                    env.msg_type,
                )
        except Exception:
            logger.exception("InboundDispatcher.dispatch() unhandled error (O3)")
