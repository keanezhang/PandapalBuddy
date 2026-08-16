"""pandapal/desktop_ipc/inbound_adapter.py — IPC 渠道入站适配器（方言翻译）。

入站是出站 broadcast 的镜像：类比出站的 IpcStdoutTransport（每渠道一份，渲染方言），
入站侧每渠道一份适配器，负责把 IPC 方言帧翻译成规范 InboundEnvelope。

职责（§1.5 分层职责边界）：
- 方言→规范翻译（词汇归一：5 条 Router 方言映射 + 直通 identity）
- 渠道白名单（allowed_types）：不在集合内的类型 WARN + drop，永远到不了 dispatcher
- 渠道特有的 InboundMessage 构造校验（user_id/session_id 0 容忍）

不决定消息给谁（无注册表——那是 InboundDispatcher 的职责）。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from pandapal import session_id as session_id_mod
from pandapal.desktop_ipc.message_codec import IpcMessageType
from pandapal.dispatch.types import ChannelContext, InboundEnvelope
from pandapal.messages.types import HITLDecision, RouterMessageType
from pandapal.router.models import InboundMessage

logger = logging.getLogger(__name__)


# ── 渠道白名单（方言全集，去 PING；原 stdio_ipc._ALLOWED_INBOUND_TYPES 搬家）──
# 连接层消息 PING 由 gate（_handle_line）自处理，不进 normalize，故不在此表。
_ALLOWED_TYPES: frozenset[str] = frozenset({
    # Router 方言（5 条，需词汇映射）
    IpcMessageType.SEND_MESSAGE,
    IpcMessageType.HITL_DECISION,
    IpcMessageType.INTERACTION_RESPONSE,
    IpcMessageType.PLAN_APPROVAL_DECISION,
    IpcMessageType.STOP_GENERATION,
    # 直通（24 种，identity：IPC 直通字符串即规范词汇）
    IpcMessageType.MODEL_LIST_REQUEST,
    IpcMessageType.REQUEST_SCHEDULED_TASKS,
    IpcMessageType.DELETE_SCHEDULED_TASK,
    IpcMessageType.SKILL_LIST,
    IpcMessageType.SKILL_GET,
    IpcMessageType.SKILL_SAVE,
    IpcMessageType.SKILL_DELETE,
    IpcMessageType.SKILL_IMPORT,
    IpcMessageType.SKILL_EXPORT,
    IpcMessageType.SESSION_LIST_REQUEST,
    IpcMessageType.SESSION_CREATE,
    IpcMessageType.SESSION_SWITCH,
    IpcMessageType.SESSION_DELETE,
    IpcMessageType.SESSION_RENAME,
    IpcMessageType.SESSION_GROUP_MUTATE,
    IpcMessageType.SESSION_HISTORY_REQUEST,
    IpcMessageType.SEARCH,
    IpcMessageType.DASHBOARD_REQUEST,
    IpcMessageType.SET_BUDGET,
    IpcMessageType.BUDGET_QUERY,
    IpcMessageType.LOAD_CREDENTIALS,
    IpcMessageType.SAVE_LLM_CREDENTIALS,
    IpcMessageType.VERIFY_CREDENTIALS,
    IpcMessageType.GET_CREDENTIALS_STATUS,
})


# ── 词汇映射：IPC 方言 → Router 规范词汇（5 条；直通 identity 不入表）──
_ROUTER_TYPE_MAP: dict[str, str] = {
    IpcMessageType.SEND_MESSAGE: RouterMessageType.USER_INSTRUCTION,
    IpcMessageType.HITL_DECISION: RouterMessageType.APPROVAL_RESPONSE,
    IpcMessageType.INTERACTION_RESPONSE: RouterMessageType.INTERACTION_RESPONSE,
    IpcMessageType.PLAN_APPROVAL_DECISION: RouterMessageType.PLAN_APPROVAL_DECISION,
    IpcMessageType.STOP_GENERATION: RouterMessageType.STOP_GENERATION,
}


class IpcInboundAdapter:
    """IPC 渠道入站适配器（InboundChannelAdapter Protocol 实现）。

    归一发生在渠道入口（逐条即时翻译，非集中批处理）。
    """

    def __init__(
        self,
        config_user_id: str = "",
        channel_id: str = "__desktop_ipc__",
    ) -> None:
        self._channel_id = channel_id
        self._config_user_id = config_user_id

    @property
    def channel_id(self) -> str:
        return self._channel_id

    @property
    def allowed_types(self) -> frozenset[str]:
        return _ALLOWED_TYPES

    def normalize(self, raw: dict[str, Any]) -> InboundEnvelope | None:
        """方言帧 → 规范信封（结构归一 + 词汇归一，两级一次完成）。

        返回 None = 非法/不放行的消息（已 WARN 留痕）。
        """
        ipc_type = raw.get("type", "")
        if ipc_type not in _ALLOWED_TYPES:
            logger.warning(
                "IpcInboundAdapter: type=%r not in allowed_types, dropped", ipc_type,
            )
            return None

        # 词汇归一：Router 方言映射 + 直通 identity
        msg_type = _ROUTER_TYPE_MAP.get(ipc_type, ipc_type)
        ctx = ChannelContext(
            channel_id=self._channel_id,
            # 直通类 user_id 缺省回落 config（保持现状，session handler 本就闭包 config
            # user_id）；Router 类的严格校验在 build 阶段（_require_user_id 0 容忍）。
            user_id=raw.get("user_id") or self._config_user_id,
            session_id=raw.get("session_id"),
            msg_id=raw.get("msg_id") or str(uuid.uuid4()),
        )
        return InboundEnvelope(msg_type=msg_type, data=raw, ctx=ctx)

    def build_inbound_message(self, env: InboundEnvelope) -> InboundMessage:
        """规范信封 → InboundMessage（仅 Router 类型；按 env.msg_type 规范词汇分支）。

        渠道特有校验语义：user_id/session_id 0 容忍（缺失抛 → dispatcher 兜底 WARN drop）。
        原 StdioIpcServer._build_inbound_message 整体搬家。
        """
        data = env.data
        msg_id = env.ctx.msg_id
        msg_type = env.msg_type

        # ── 严格校验：user_id 必须由前端提供，不做 fallback ──
        def _require_user_id() -> str:
            uid = data.get("user_id")
            if not uid:
                raise ValueError(f"{msg_type}: user_id is required (not in payload)")
            return uid

        def _require_session_id() -> str:
            # 经由命根子模块做 0 容忍校验（非空 + 格式），单一真相源。
            return session_id_mod.require(
                data.get("session_id"), where=f"ipc_inbound_adapter.{msg_type}",
            )

        if msg_type == RouterMessageType.USER_INSTRUCTION:
            # ★ 提取 active_app_id，通过 content 透传给下游
            active_app_id = data.get("active_app_id", "")
            content_dict: dict[str, Any] = {
                "text": data.get("content", ""),
                "raw": data,
            }
            if active_app_id and isinstance(active_app_id, str) and active_app_id.strip():
                content_dict["active_app_id"] = active_app_id.strip()

            # ★ 提取 mode（coding/office），缺省/非法交由 SessionAgentPool 处理
            mode = data.get("mode", "")
            if isinstance(mode, str) and mode.strip():
                content_dict["mode"] = mode.strip()

            # ★ 提取 model_id（InputBar 选择的模型），缺省/空 → executor 走 default
            model_id = data.get("model_id", "")
            if isinstance(model_id, str) and model_id.strip():
                content_dict["model_id"] = model_id.strip()

            return InboundMessage(
                msg_id=msg_id,
                message_type=RouterMessageType.USER_INSTRUCTION,
                source_channel_id=self._channel_id,
                user_id=_require_user_id(),
                session_id=_require_session_id(),
                content=content_dict,
            )

        if msg_type == RouterMessageType.APPROVAL_RESPONSE:
            decision_raw = data.get("decision", "")
            decision = (
                HITLDecision.APPROVED if decision_raw in ("approved", "approve")
                else HITLDecision.REJECTED
            )
            uid = _require_user_id()
            return InboundMessage(
                msg_id=msg_id,
                message_type=RouterMessageType.APPROVAL_RESPONSE,
                source_channel_id=self._channel_id,
                user_id=uid,
                session_id=_require_session_id(),
                content={
                    "approval_id": data.get("approval_id"),
                    "run_id":      data.get("run_id"),
                    "decision":    decision,
                    "user_id":     uid,
                    "source_channel_id": self._channel_id,
                },
            )

        if msg_type == RouterMessageType.INTERACTION_RESPONSE:
            return InboundMessage(
                msg_id=msg_id,
                message_type=RouterMessageType.INTERACTION_RESPONSE,
                source_channel_id=self._channel_id,
                user_id=_require_user_id(),
                session_id=_require_session_id(),
                content={
                    "request_id": data.get("request_id"),
                    "run_id":     data.get("run_id"),
                    "response":   data.get("response", ""),
                    "raw":        data,
                },
            )

        if msg_type == RouterMessageType.PLAN_APPROVAL_DECISION:
            uid = _require_user_id()
            sid = _require_session_id()
            return InboundMessage(
                msg_id=msg_id,
                message_type=RouterMessageType.PLAN_APPROVAL_DECISION,
                source_channel_id=self._channel_id,
                user_id=uid,
                session_id=sid,
                content={
                    # ★ 决策类字段：不给默认值。plan_action 是纯 UI→后端的单向决策，
                    #   缺失时留空，交由 plan_manager.resume 做 fail-fast（静默降级审计 §1.1）。
                    "plan_action":         data.get("plan_action", ""),
                    "run_id":              data.get("run_id", ""),
                    "session_id":          sid,
                    "user_id":             uid,
                    "user_text":           data.get("user_text", ""),
                    "edited_plan_content": data.get("edited_plan_content"),
                },
            )

        if msg_type == RouterMessageType.STOP_GENERATION:
            uid = _require_user_id()
            # ★ 必须携带要停止的 session_id：缺失即拒绝，避免下游按空/错 session 误杀。
            return InboundMessage(
                msg_id=msg_id,
                message_type=RouterMessageType.STOP_GENERATION,
                source_channel_id=self._channel_id,
                user_id=uid,
                session_id=_require_session_id(),
                content={"raw": data},
            )

        raise ValueError(
            f"build_inbound_message: unsupported Router msg_type={msg_type!r}"
        )
