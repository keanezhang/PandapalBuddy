"""pandapal_relay.wecom_bridge — 企微 Bridge Gateway（5.2 重写版）。

★ 5.2 关键改造：
- 出站消息统一通过 WeComRestTransport（替代原来散落的 _handle_agent_reply）
- 文本消息、template_card_event、approval_request 都通过 Transport 发送
- 移除 envelope 解包 hack（上游已扁平化）
- 修复 WeCom 端 HITL 按钮事件转发（之前只 log 不转发）

设计约束：
- Bridge 层是纯协议翻译层，零业务逻辑
- HITL 审批通过 WeCom template_card（button_interaction）发送
- 用户点击按钮 → template_card_event 回调 → 直接提取 TaskId/EventKey → 转发
- 无关键词匹配，无 _pending_approvals，无 session_id 反推
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Query, Request, Response

# ── 本地副本（Relay 独立部署，不依赖完整 pandapal 包）──
from .message_types import HITLDecision

from .wecom.crypto import WeComCrypto, parse_wecom_xml
from .wecom.sender import WeComSender
from .wecom_transport import WeComRestTransport
from . import server as relay_server

logger = logging.getLogger("pandapal_relay.wecom_bridge")

router = APIRouter(tags=["WeCom Bridge"])

# ── 全局引用，在 run_relay 中注入 ──
_crypto: Optional[WeComCrypto] = None
_sender: Optional[WeComSender] = None
_transport: Optional[WeComRestTransport] = None
# D6 白名单（fail-closed）：None（未配置）或空集合都拒绝所有人；仅非空白名单内用户放行。
# 决策/门禁类配置零默认——未配置绝不等于"不限制"（健壮性与降级契约 §九）。
_allowed_userids: Optional[set[str]] = None

# ── 渠道会话 id 前缀（跨部署契约字符串，就近归属）──
# 企微渠道作为发起方，为每个企微用户 mint 稳定渠道会话 id "wecom-{user_id}"，
# 与 xiaozhi 渠道的 "xiaozhi-{device_id}" 先例对称（见 xiaozhi_bridge.py）。
# pandapal 侧前缀表见 pandapal/session_id.py（sess- / task-），两侧互相引用。
_WECOM_SESSION_PREFIX = "wecom-"

# ── 消息去重（内存，TTL 60s）──
_seen_msg_ids: dict[str, float] = {}
_DEDUP_TTL_SECONDS = 60.0


def init_wecom_bridge(
    crypto: WeComCrypto,
    sender: WeComSender,
    user_id: str = "",
    allowed_userids: list[str] | None = None,
) -> WeComRestTransport:
    """初始化企微 Bridge（在 relay 启动时调用）。

    ★ 5.2 改造：构造 WeComRestTransport 持有 sender。
    ★ 根本解（2026-06-10）：返回 transport 引用，让 run_relay.py 调度 start()/stop()。
    ★ D6: allowed_userids — None（未配置）与空列表都拒绝所有人（fail-closed），
      仅非空白名单内用户放行。未配置不等于"不限制"——门禁类配置零默认。

    Returns:
        WeComRestTransport 实例（供 run_relay.py 调 start()/stop()）。
    """
    global _crypto, _sender, _transport, _allowed_userids
    _crypto = crypto
    _sender = sender
    _transport = WeComRestTransport(sender=sender, user_id=user_id)
    _allowed_userids = set(allowed_userids) if allowed_userids is not None else None
    logger.info(
        "[WeComBridge] Initialized (allowed_users=%s)",
        f"{len(_allowed_userids)} users" if _allowed_userids else "REJECT ALL (not configured or empty)",
    )

    # 注册 Agent 回复处理器（→ 通过 Transport 发送）
    relay_server.register_agent_reply_handler(_handle_agent_reply)
    logger.info("[WeComBridge] Initialized (with WeComRestTransport)")
    return _transport


def _is_duplicate(msg_id: str) -> bool:
    """检查消息是否重复。"""
    now = time.time()
    expired = [k for k, t in _seen_msg_ids.items() if now - t > _DEDUP_TTL_SECONDS]
    for k in expired:
        del _seen_msg_ids[k]

    if msg_id in _seen_msg_ids:
        return True
    _seen_msg_ids[msg_id] = now
    return False


# ==================== 企微回调端点 ====================


@router.get("/assistant/wecom/callback")
async def wecom_verify(
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
):
    """企业微信 URL 验证（GET）。"""
    if not _crypto:
        return Response(content="not ready", status_code=500)

    plain = _crypto.decrypt_echostr(msg_signature, timestamp, nonce, echostr)
    if plain is None:
        return Response(content="verify failed", status_code=403)
    return Response(content=plain, media_type="text/plain")


@router.post("/assistant/wecom/callback")
async def wecom_receive(
    request: Request,
    msg_signature: str = Query(""),
    timestamp: str = Query(""),
    nonce: str = Query(""),
):
    """企业微信消息接收（POST）→ 转发给 Agent。

    设计要点：
    - 必须在 5s 内返回 "success"
    - 转发给 Agent 通过 asyncio.create_task 异步执行
    - msg_id 去重防止重复转发
    - template_card_event：用户点击审批按钮 → 直接提取 TaskId/EventKey → 转发 APPROVAL_RESPONSE
    """
    body = await request.body()
    if isinstance(body, bytes):
        body = body.decode("utf-8")

    # 解析外层 XML
    outer = parse_wecom_xml(body)
    encrypt_str = outer.get("Encrypt", "")
    if not encrypt_str:
        return Response(content="success", media_type="text/plain")

    # 签名校验
    if not _crypto.verify_signature(msg_signature, timestamp, nonce, encrypt_str):
        logger.warning("[WeComBridge] Signature verification failed")
        return Response(content="success", media_type="text/plain")

    # AES 解密
    try:
        xml_content, _ = _crypto.decrypt(encrypt_str)
    except Exception as e:
        logger.warning("[WeComBridge] Decrypt failed: %s", e)
        return Response(content="success", media_type="text/plain")

    # 解析消息字段
    msg_dict = parse_wecom_xml(xml_content)
    user_id = msg_dict.get("FromUserName", "")
    msg_type = msg_dict.get("MsgType", "text")
    msg_id = msg_dict.get("MsgId", "")
    content = msg_dict.get("Content", "")

    # D6: 白名单校验（fail-closed）— None（未配置）/空集合都拒绝所有人；
    # 仅在非空白名单内的用户放行。user_id 为空同样被拒（ID 类零兜底）。
    if not _allowed_userids or not user_id or user_id not in _allowed_userids:
        logger.warning(
            "[WeComBridge] Message rejected: user='%s' not in allowed_userids "
            "(whitelist %s)", user_id,
            "not configured" if _allowed_userids is None
            else "empty" if not _allowed_userids
            else f"has {len(_allowed_userids)} users",
        )
        return Response(content="success", media_type="text/plain")

    logger.info(
        "[WeComBridge] Received: user=%s type=%s msg_id=%s",
        user_id, msg_type, msg_id,
    )

    # 去重（event 类型通常无 MsgId，不参与去重）
    if msg_id and _is_duplicate(msg_id):
        logger.info("[WeComBridge] Duplicate msg_id=%s, skip", msg_id)
        return Response(content="success", media_type="text/plain")

    # ★ 5.2 修复：text 消息
    if msg_type == "text" and content:
        asyncio.create_task(
            _forward_text_to_agent(user_id, content, msg_id)
        )
    # ★ 5.2 修复：template_card_event 真正转发 APPROVAL_RESPONSE
    elif msg_type == "event":
        event_type = msg_dict.get("Event", "")
        event_key = msg_dict.get("EventKey", "")
        task_id = msg_dict.get("TaskId", "")  # 模板卡片 TaskId = approval_id
        logger.info(
            "[WeComBridge] Event: user=%s event=%s key=%s task_id=%s",
            user_id, event_type, event_key, task_id,
        )
        if event_type == "template_card_event" and task_id:
            asyncio.create_task(
                _forward_approval_to_agent(
                    user_id=user_id, approval_id=task_id, event_key=event_key
                )
            )

    return Response(content="success", media_type="text/plain")


async def _forward_text_to_agent(user_id: str, content: str, msg_id: str) -> None:
    """异步转发文本消息给 Agent。

    ★ 防线0·发起方创建（SESSION_ID 契约：创建权专属发起方）：
    企微渠道在此 mint 稳定渠道会话 id "wecom-{user_id}"——同一企微用户的所有
    消息归入同一会话（与 xiaozhi-{device_id} 先例对称）。pandapal 全链路只读
    透传 + 校验，绝不创建/替代/兜底。
    """
    # 空 user_id fail-fast：防止 mint 出畸形的 "wecom-"（零兜底，没有就报错）
    if not user_id:
        logger.warning(
            "[WeComBridge] empty user_id, refusing to forward (msg_id=%s)", msg_id
        )
        return

    # 发起方 mint：稳定派生，同一企微用户 → 同一渠道会话
    session_id = f"{_WECOM_SESSION_PREFIX}{user_id}"

    logger.info(
        "[WeComBridge] Forwarding text to agent: user=%s content='%s' msg_id=%s session=%s",
        user_id, content[:100], msg_id, session_id,
    )
    success = await relay_server.forward_to_agent(
        user_id=user_id,
        content=content,
        source_channel_id="wecom",
        session_id=session_id,
    )
    if success:
        logger.info("[WeComBridge] Forward success: msg_id=%s", msg_id)
    else:
        logger.warning(
            "[WeComBridge] Forward buffered or failed: msg_id=%s", msg_id
        )


async def _forward_approval_to_agent(
    user_id: str, approval_id: str, event_key: str
) -> None:
    """转发 WeCom 审批按钮事件给 Agent Backend。

    走 forward_approval_response_to_agent() 通道，发送 type="message" 帧（D2 修复）。

    D6 修复（session_id 编码）：
    按钮 key 格式为 "approve:<session_id>" / "reject:<session_id>"，
    本函数解析 EventKey 还原 decision 和 session_id。
    不兼容旧格式（旧格式无冒号，session_id 缺失，拒绝处理）。
    """
    # 解析 EventKey: "approve:<session_id>" 或 "reject:<session_id>"
    if ":" not in event_key:
        logger.error(
            "[WeComBridge] Invalid EventKey format (missing session_id): '%s'. "
            "Rejecting approval — SESSION_ID 契约 0 容忍空值.",
            event_key,
        )
        return

    action, session_id = event_key.split(":", 1)
    if not session_id:
        logger.error(
            "[WeComBridge] Empty session_id in EventKey: '%s'. Rejecting approval.",
            event_key,
        )
        return

    decision = (
        HITLDecision.APPROVED if action == "approve" else HITLDecision.REJECTED
    )
    try:
        await relay_server.forward_approval_response_to_agent(
            user_id=user_id,
            approval_id=approval_id,
            decision=decision,
            session_id=session_id,
            source_channel_id="wecom",
        )
        logger.info(
            "[WeComBridge] approval_response forwarded: approval_id=%s "
            "decision=%s user=%s session_id=%s",
            approval_id, decision, user_id, session_id,
        )
    except Exception as e:
        logger.exception(
            "[WeComBridge] Failed to forward approval: %s", e
        )


async def _handle_agent_reply(frame: dict) -> None:
    """处理 Agent 回复帧 → 通过 WeComRestTransport 发送给用户。

    ★ D1 修复（2026-06-14）：端到端对齐 5.2 契约 —— frame 顶层携带
      `event_type`（EventType.value）+ `payload`，直接反序列化为 NormalizedEvent
      交给 Transport（Transport 按 EventType 渲染）。
      旧实现按 OutboundMessageType（"approval_request"/"error_reply" 等）映射，
      与上游 EventType 值（"hitl_request"/"error" 等）不匹配，导致 HITL 模板卡片、
      TOOL_END 智能截断等全部落入 default(agent_reply) 分支失效。本实现删除该错误映射。
    """
    if not _transport:
        logger.error("[WeComBridge] Transport not initialized")
        return

    # ── 本地副本（Relay 独立部署，不依赖完整 pandapal 包）──
    from .normalized_events import EventType, NormalizedEvent

    # 提取关键字段（兼容旧 envelope：payload 内再嵌套一层 payload）
    msg_id = frame.get("msg_id", "")
    payload = frame.get("payload", {}) or {}
    event_type_str = frame.get("event_type") or ""
    if isinstance(payload, dict) and "payload" in payload and isinstance(payload["payload"], dict):
        logger.debug("[WeComBridge] Legacy envelope format detected, unwrapping")
        if not event_type_str and payload.get("event_type"):
            event_type_str = payload["event_type"]
        payload = payload["payload"]

    # 跳过 __hitl_bridge__ 内部目标
    target_ids = frame.get("target_channel_ids", []) or []
    if "__hitl_bridge__" in target_ids:
        return

    # ★ origin 白名单（2026-06 渠道策略重构）：
    #   仅 None/""/"wecom" 放行——全局事件（定时任务）与 wecom 自有事件才投递企微。
    #   其余 origin（__desktop_ipc__ / xiaozhi:{device} / 未来渠道）一律拒收：
    #   桌面会话串到企微是串话事故；xiaozhi 事件归 xiaozhi_bridge 认领，
    #   设备离线时链式落到此处也必须拒（不能错投企微）。
    origin = frame.get("origin_channel_id") or ""
    if origin not in ("", "wecom"):
        logger.info(
            "[WeComBridge] origin=%r not in whitelist, dropping: msg_id=%s",
            origin, msg_id,
        )
        return

    # 解析 EventType（未知类型直接丢弃并留痕，不强转 —— 不该到达 WeCom 的不硬塞）
    try:
        event_type = EventType(event_type_str)
    except ValueError:
        logger.warning(
            "[WeComBridge] unknown event_type=%r, dropping: msg_id=%s",
            event_type_str, msg_id,
        )
        return

    # 回包路由 user_id 零兜底：仅取 payload（pandapal executor stamp）或显式配置的
    # WECOM_DEFAULT_USER_ID；两者都缺失 → 拒绝发送 + error 留痕，绝不猜测收件人。
    user_id = payload.get("user_id", "")
    if not user_id and _transport._user_id:
        user_id = _transport._user_id
    if not user_id:
        logger.error(
            "[WeComBridge] frame has no user_id and WECOM_DEFAULT_USER_ID not configured, "
            "refusing to send (user_id zero-fallback): msg_id=%s type=%s",
            msg_id, event_type_str,
        )
        return

    # 直接从 frame 反序列化 NormalizedEvent（event_type + payload 透传）。
    # 异常（如 HITL_REQUEST 缺 reply_id）内部消化，不向外抛。
    try:
        event = NormalizedEvent(
            event_type=event_type,
            reply_id=frame.get("reply_id"),
            run_id=frame.get("run_id"),
            payload=payload,
            msg_id=msg_id or uuid.uuid4().hex,
            origin_channel_id=frame.get("origin_channel_id"),
        )
    except Exception as e:
        logger.exception("[WeComBridge] failed to build NormalizedEvent: %s", e)
        return

    try:
        await _transport.send(event)
    except Exception as e:
        logger.exception("[WeComBridge] transport.send failed: %s", e)
