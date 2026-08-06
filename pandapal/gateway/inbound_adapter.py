"""pandapal/gateway/inbound_adapter.py — Gateway 渠道入站适配器（方言翻译）。

入站是出站 broadcast 的镜像：类比出站的 WSSGateway Transport（每渠道一份，渲染方言），
入站侧每渠道一份适配器，负责把 Gateway frame 翻译成规范 InboundEnvelope。

职责（§1.5 分层职责边界）：
- 方言→规范翻译（Gateway frame → InboundEnvelope）
- 渠道白名单（allowed_types）：仅 Router 词汇 9 种；不在集合内 WARN + drop，
  永远到不了 dispatcher
- 渠道特有的安全校验（payload 大小上限防 OOM、必填字段 0 容忍）
- 渠道特有的 InboundMessage 构造（session_id 必填 0 容忍——SESSION_ID 契约）

与 IPC 侧的差异：
- IPC 是桌面专属，直通 + Router 混合，user_id/session_id 0 容忍；
- Gateway 汇聚远程渠道（wecom / xiaozhi:xxx），relay 端只发业务消息（Router 词汇），
  无桌面直通；远程渠道由发起方（relay 渠道 bridge）创建稳定渠道会话 id 并随帧携带，
  session_id 与 msg_id/user_id 同级必填 0 容忍，缺失即 drop
  （契约：零兜底、创建权专属发起方，下游绝不创建/替代/默认值）。
- IPC 单渠道（channel_id 恒定 __desktop_ipc__）；Gateway 多渠道汇聚，消息真正渠道在
  payload.source_channel_id，故 ChannelContext.channel_id 取消息级来源（回包定向依据）。

不决定消息给谁（无注册表——那是 InboundDispatcher 的职责）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pandapal import session_id as session_id_mod
from pandapal.dispatch.types import ChannelContext, InboundEnvelope
from pandapal.messages.types import RouterMessageType
from pandapal.router.models import InboundMessage

logger = logging.getLogger(__name__)


# ── 渠道白名单：Gateway 仅放行 Router 词汇 9 种 ──
# relay 端（wecom_bridge / xiaozhi_bridge）产出的 message_type 已是规范 Router 词汇，
# 不做方言映射；直通（SKILL_LIST 等）是桌面专属，远程渠道不存在。
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


class GatewayInboundAdapter:
    """Gateway 渠道入站适配器（InboundChannelAdapter Protocol 实现）。

    归一发生在渠道入口（逐条即时翻译，非集中批处理）。
    """

    def __init__(
        self,
        default_channel_id: str = "gateway",
        max_payload_bytes: int = 1_048_576,
    ) -> None:
        # 汇聚兜底渠道 ID：消息未携带 source_channel_id 时使用（正常路径不会，
        # relay 端 wecom_bridge / xiaozhi_bridge 均会填入真实渠道）。
        self._default_channel_id = default_channel_id
        # payload 大小上限（防 OOM / 内存放大攻击）：在 json.loads 之前检查。
        self._max_payload_bytes = max_payload_bytes

    @property
    def channel_id(self) -> str:
        return self._default_channel_id

    @property
    def allowed_types(self) -> frozenset[str]:
        return _ROUTER_TYPES

    def normalize(self, raw: dict[str, Any]) -> InboundEnvelope | None:
        """Gateway frame → 规范信封（结构归一 + 安全校验）。

        raw 结构（type="message" 帧）：
          {
            "type": "message",
            "msg_id": "...",                # 帧级 msg_id（gate 已校验非空）
            "source_channel_id": "...",     # 可选，可能在 payload 内
            "payload": {                    # dict / str / bytes
              "message_type": "user_instruction",
              "user_id": "...",
              "session_id": "...",          # 必填（发起方 relay bridge mint）
              "content": ...,
              "source_channel_id": "..."    # 可能在 payload 内
            }
          }

        返回 None = 非法/不放行的消息（已 WARN 留痕）。
        """
        payload = raw.get("payload")

        # ── ① payload 大小检查（仅 bytes/str 原始形态，json.loads 之前，防 OOM）──
        if isinstance(payload, (bytes, str)):
            payload_size = len(payload)
            if payload_size > self._max_payload_bytes:
                logger.warning(
                    "GatewayInboundAdapter: payload too large (%d bytes, max=%d) "
                    "from channel '%s', dropped",
                    payload_size, self._max_payload_bytes,
                    raw.get("source_channel_id", "unknown"),
                )
                return None

        # ── ② payload → data（bytes/str→json.loads；dict 直接用）──
        if isinstance(payload, (bytes, str)):
            try:
                data = json.loads(payload)
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                logger.warning(
                    "GatewayInboundAdapter: invalid JSON in payload from channel "
                    "'%s': %s, dropped",
                    raw.get("source_channel_id", "unknown"), e,
                )
                return None
        elif isinstance(payload, dict):
            data = payload
        else:
            logger.warning(
                "GatewayInboundAdapter: unsupported payload type %s, dropped",
                type(payload).__name__,
            )
            return None

        # ── ③ 词汇白名单：message_type 必须是规范 Router 词汇 ──
        msg_type = data.get("message_type")
        if msg_type not in _ROUTER_TYPES:
            logger.warning(
                "GatewayInboundAdapter: message_type=%r not in allowed_types, dropped",
                msg_type,
            )
            return None

        # ── ④ 必填字段 0 容忍：msg_id / user_id / session_id（缺失即丢，fail-closed）──
        msg_id = data.get("msg_id") if data.get("msg_id") is not None else raw.get("msg_id")
        if not self._is_non_empty_str(msg_id):
            logger.warning(
                "GatewayInboundAdapter: missing/invalid msg_id (type=%s), dropped",
                msg_type,
            )
            return None
        user_id = data.get("user_id")
        if not self._is_non_empty_str(user_id):
            logger.warning(
                "GatewayInboundAdapter: missing/invalid user_id (type=%s), dropped",
                msg_type,
            )
            return None
        # ★ 防线2·adapter 校验（SESSION_ID 契约：零兜底、创建权专属发起方）：
        #   远程渠道的 session_id 由发起方（relay 渠道 bridge）mint 并随帧携带，
        #   与 msg_id/user_id 同级必填；缺失/非法即 WARN + drop，下游绝不创建/替代/兜底。
        session_id = data.get("session_id")
        if not session_id_mod.is_wellformed(session_id):
            logger.warning(
                "GatewayInboundAdapter: missing/invalid session_id (type=%s), dropped",
                msg_type,
            )
            return None

        # ── ⑤ 渠道 ID：消息级 source_channel_id 优先（回包定向依据），缺失回落汇聚兜底 ──
        source_channel_id = (
            data.get("source_channel_id")
            or raw.get("source_channel_id")
            or self._default_channel_id
        )

        ctx = ChannelContext(
            channel_id=str(source_channel_id),
            user_id=str(user_id),
            # session_id 已在 ④ 必填校验过（is_wellformed 保证非空 str）
            session_id=session_id,
            msg_id=str(msg_id),
        )
        # data 为已解析 payload（方言字段原样保留，由 handler/构造器解释）
        return InboundEnvelope(msg_type=str(msg_type), data=data, ctx=ctx)

    def build_inbound_message(self, env: InboundEnvelope) -> InboundMessage:
        """规范信封 → InboundMessage（仅 Router 类型）。

        必填字段（msg_id/user_id/session_id）已在 normalize 阶段 0 容忍校验过
        （远程渠道 session_id 由发起方 relay bridge mint，本层只读透传），此处透明构造。
        """
        try:
            return InboundMessage(
                msg_id=env.ctx.msg_id,
                message_type=env.msg_type,  # 已是规范 Router 词汇
                source_channel_id=env.ctx.channel_id,
                user_id=env.ctx.user_id,
                session_id=env.ctx.session_id,
                content=env.data.get("content"),
            )
        except ValueError as e:
            # InboundMessage.__post_init__ 白名单校验（理论不会触发，msg_type 已过白名单）
            raise ValueError(
                f"GatewayInboundAdapter.build_inbound_message: {e}"
            ) from e

    @staticmethod
    def _is_non_empty_str(value: Any) -> bool:
        """合法 = 非 None 且 isinstance(str) 且 strip() 非空（防空白字符串）。"""
        return isinstance(value, str) and bool(value.strip())
