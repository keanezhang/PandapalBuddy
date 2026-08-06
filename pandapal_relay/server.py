"""pandapal_relay/server.py — Relay Server（单端口 FastAPI 版）

职责：
- WebSocket 端点 /relay/ws，接受 Agent（Gateway Client）连接
- JWT 验签：握手时验证 token，无效则拒绝连接
- 维护 agent_ws 引用（Phase 0 只支持单 Agent）
- 提供 forward_to_agent() 方法供 Bridge 调用
- 处理 Agent 回复消息（多渠道路由分发）
- ACK 回复：收到 Agent message 帧后发送 {"type": "ack", "msg_id": "..."}
- 离线消息 ACK 确认：追踪发送给 Agent 的消息是否被确认
- 心跳超时检测：90s 无活动主动断开连接
- 活跃渠道追踪 + GET /relay/channels 端点

设计：
- HTTP 和 WebSocket 共用同一 FastAPI app / 同一端口
- nginx 配置 /relay/ → proxy_pass 到本进程
- 企微回调 /assistant/wecom/callback 同样在本进程
- XiaoZhi WebSocket /xiaozhi/ws 同样在本进程
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Optional, Callable, Awaitable

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

logger = logging.getLogger("pandapal_relay.server")

router = APIRouter(tags=["Relay WebSocket"])

# ── 全局状态 ──
_agent_ws: Optional[WebSocket] = None

# AuthService 引用（由 run_relay 注入）
_auth_service: Optional[object] = None  # 避免循环 import，用 object 类型

# 待发消息缓冲（Agent 未连接时暂存，连接后立即 drain）
_pending_frames: list[dict] = []
_MAX_PENDING = 50  # 防止内存无限增长

# 离线消息 ACK 确认表（msg_id → frame，等待 Agent ACK）
_pending_ack: dict[str, dict] = {}
_PENDING_ACK_TTL = 300  # 5分钟超时，超时未确认则丢弃

# 心跳超时检测
_last_activity: float = 0.0  # 最后活动时间戳
_HEARTBEAT_TIMEOUT = 90.0  # 90秒超时

# 多渠道回复处理器列表（按优先级尝试，第一个处理成功的终止链）
_reply_handlers: list[Callable[[dict], Awaitable[bool]]] = []

# 兜底回复处理器（向后兼容，当所有渠道处理器都未处理时调用）
_fallback_reply_handler: Optional[Callable[[dict], Awaitable[None]]] = None

# 活跃渠道追踪（channel_id → last_seen timestamp）
_active_channels: dict[str, float] = {}
_CHANNEL_TTL_SECONDS = 86400  # 24h，渠道超过此时间无消息则视为离线


def get_agent_connected() -> bool:
    """Agent 是否在线。"""
    return _agent_ws is not None


# ★ 根本解 2026-06-10（第四轮同类反模式）：
#   旧名 get_agent_connected() 语义模糊（"是否已注册" vs "是否已连接"），
#   改名 is_agent_connected() 让契约更明确（is_xxx 暗示瞬时状态）。
def is_agent_connected() -> bool:
    """Agent 是否在线（★ 公共 accessor，替代直接访问私有 _agent_ws）。

    之前 wecom_bridge.py:231 写 `if relay_server.relay_ws and not relay_server.relay_ws.is_closed:`，
    但 server.py 实际只有 `_agent_ws`（私有），没有 `relay_ws`。`if` 表达式求值时直接抛 AttributeError，
    被 try/except 静默吞掉 → 用户每次点审批按钮都崩。

    此函数是 wecom_bridge 等 Bridge 层判断 Agent 状态的唯一公共入口。
    """
    return _agent_ws is not None


def init_relay_server(auth_service: object) -> None:
    """初始化 Relay Server（注入 AuthService）。

    Args:
        auth_service: AuthService 实例（避免循环 import，用 object 类型）
    """
    global _auth_service
    _auth_service = auth_service
    logger.info("[Relay] AuthService injected for JWT verification")


async def send_to_agent(text: str) -> bool:
    """发送文本帧到 Agent（★ 公共通道，替代直接访问 _agent_ws）。

    行为约定：
      - Agent 在线：直接发送，返回 True
      - Agent 离线：返回 False（不抛异常，不入队 — 入队由 forward_to_agent 负责）
      - 发送失败：返回 False，且将 _agent_ws 重置为 None（触发重连）

    使用场景：Bridge 层把 inbound_message 帧转发给 Agent Backend。
    与 forward_to_agent 的区别：forward_to_agent 入队未连接消息；send_to_agent 不入队。
    """
    global _agent_ws
    if _agent_ws is None:
        return False
    try:
        await _agent_ws.send_text(text)
        return True
    except Exception as e:
        logger.error("[Relay] send_to_agent failed: %s", e)
        _agent_ws = None
        return False


def register_reply_handler(handler: Callable[[dict], Awaitable[bool]]) -> None:
    """注册一个渠道回复处理器。

    handler 签名: async def handler(frame: dict) -> bool
    返回 True 表示已处理该帧，后续处理器不再调用。
    返回 False 表示未处理（非本渠道目标）。
    """
    _reply_handlers.append(handler)


def register_agent_reply_handler(handler: Callable[[dict], Awaitable[None]]) -> None:
    """注册 Agent 回复消息的兜底处理器（向后兼容 WeCom Bridge）。

    此处理器在所有渠道处理器都返回 False 时被调用。
    """
    global _fallback_reply_handler
    _fallback_reply_handler = handler


async def forward_to_agent(
    user_id: str,
    content: str,
    source_channel_id: str,
    session_id: str,
    msg_id: str | None = None,
) -> bool:
    """将消息帧转发给 Agent（Gateway Client）。

    Args:
        user_id: 消息来源用户
        content: 消息内容
        source_channel_id: 来源渠道 ID
        session_id: 渠道会话 ID（★ 必填，发起方创建——SESSION_ID 契约：
            创建权专属发起方 relay 渠道 bridge，本函数只透传，空值即 fail-fast）
        msg_id: 消息 ID（可选，不传则自动生成）

    Returns:
        是否成功发送

    Raises:
        ValueError: session_id 为空（零兜底，绝不代建/默认值）
    """
    global _agent_ws

    # 防线1·relay 校验：session_id 必填 0 容忍（契约：没有就报错，不兜底）
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError(
            "forward_to_agent: session_id 为空——发起方必须创建（契约零兜底）"
        )

    # 先构造帧，这样缓冲和发送路径都能使用同一个对象
    frame = {
        "type": "message",
        "msg_id": msg_id or str(uuid.uuid4()),
        "payload": {
            "message_type": "user_instruction",
            "user_id": user_id,
            "session_id": session_id,
            "content": content,
            "content_type": "text",
            "source_channel_id": source_channel_id,
        },
    }

    if _agent_ws is None:
        if len(_pending_frames) < _MAX_PENDING:
            _pending_frames.append(frame)
            logger.warning(
                "[Relay] Agent not connected, buffered msg_id=%s (buffer=%d)",
                frame["msg_id"], len(_pending_frames),
            )
        else:
            logger.error(
                "[Relay] Agent not connected and buffer full (%d), dropping msg_id=%s",
                _MAX_PENDING, frame["msg_id"],
            )
        return False

    # 记录活跃渠道（供 GET /relay/channels 使用）
    _active_channels[source_channel_id] = time.time()

    try:
        frame_json = json.dumps(frame, ensure_ascii=False)
        logger.info("[Relay] Forwarding to agent: user=%s content='%s' channel=%s msg_id=%s",
                    user_id, content[:100], source_channel_id, frame["msg_id"])
        logger.debug("[Relay] Frame payload: %s", frame_json)
        await _agent_ws.send_text(frame_json)
        # 记录到 _pending_ack 等待 Agent 确认
        frame["_sent_at"] = time.time()
        _pending_ack[frame["msg_id"]] = frame
        logger.info("[Relay] Forwarded successfully: msg_id=%s", frame["msg_id"])
        return True
    except Exception as e:
        logger.error("[Relay] Forward failed: %s", e)
        _agent_ws = None
        return False


async def forward_approval_response_to_agent(
    user_id: str,
    approval_id: str,
    decision: str,
    session_id: str,
    source_channel_id: str,
) -> bool:
    """将用户审批决策转发给 Agent（approval_response 消息类型）。

    Args:
        user_id: 审批用户 ID
        approval_id: 审批请求 ID
        decision: "approve" 或 "reject"
        session_id: 会话 ID（用于路由层 session 定位）
        source_channel_id: 来源渠道 ID

    Returns:
        是否成功发送
    """
    global _agent_ws

    # 先构造帧，缓冲和发送路径共用同一对象
    frame = {
        "type": "message",
        "msg_id": str(uuid.uuid4()),
        "payload": {
            "message_type": "approval_response",
            "user_id": user_id,
            "content": {"approval_id": approval_id, "decision": decision},
            "session_id": session_id,
            "source_channel_id": source_channel_id,
        },
    }

    if _agent_ws is None:
        if len(_pending_frames) < _MAX_PENDING:
            _pending_frames.append(frame)
            logger.warning(
                "[Relay] Agent not connected, buffered approval_response approval_id=%s (buffer=%d)",
                approval_id, len(_pending_frames),
            )
        else:
            logger.error(
                "[Relay] Agent not connected and buffer full (%d), dropping approval_id=%s",
                _MAX_PENDING, approval_id,
            )
        return False

    _active_channels[source_channel_id] = time.time()

    try:
        frame_json = json.dumps(frame, ensure_ascii=False)
        logger.info(
            "[Relay] Forwarding approval_response: user=%s approval_id=%s decision=%s",
            user_id, approval_id, decision,
        )
        await _agent_ws.send_text(frame_json)
        # 记录到 _pending_ack 等待 Agent 确认
        frame["_sent_at"] = time.time()
        _pending_ack[frame["msg_id"]] = frame
        logger.info("[Relay] Approval response forwarded: approval_id=%s", approval_id)
        return True
    except Exception as e:
        logger.error("[Relay] Forward approval_response failed: %s", e)
        _agent_ws = None
        return False


@router.get("/relay/channels")
async def get_channels(user_id: str = Query(default="")):
    """返回当前活跃渠道列表（供 Gateway 查询在线渠道）。

    路由路径为 /relay/channels，与 WebSocket 端点 /relay/ws 同属 /relay 前缀。
    Gateway 构造的查询 URL 形如：
        https://domain/relay/channels?user_id=<user_id>
    """
    now = time.time()
    # 驱逐超时渠道
    expired = [ch for ch, ts in _active_channels.items() if now - ts > _CHANNEL_TTL_SECONDS]
    for ch in expired:
        del _active_channels[ch]

    active = list(_active_channels.keys())
    logger.debug("[Relay] GET /relay/channels user_id=%s → %d channels", user_id, len(active))
    return {"channel_ids": active}


def get_pending_ack_count() -> int:
    """返回当前等待 Agent ACK 的消息数量。

    用于 Gateway 或监控系统检测消息丢失。
    """
    # 驱逐超时的 ACK（超过 _PENDING_ACK_TTL 秒未确认）
    now = time.time()
    expired = []
    for msg_id, frame in _pending_ack.items():
        # 用 frame 中 timestamp 或估算
        sent_at = frame.get("_sent_at", 0)
        if sent_at and now - sent_at > _PENDING_ACK_TTL:
            expired.append(msg_id)
    for msg_id in expired:
        del _pending_ack[msg_id]
        logger.info("[Relay] Pending ACK expired: msg_id=%s", msg_id)
    return len(_pending_ack)


@router.websocket("/relay/ws")
async def relay_ws_endpoint(websocket: WebSocket, token: str = Query(default="")):
    """WebSocket 端点：Agent（Gateway Client）连接入口。

    握手时验证 JWT token，无效则拒绝连接（code=4003）。
    nginx 配置 /relay/ → proxy_pass 到此进程，
    本地 Gateway Client 连接 wss://domain/relay/ws?token=<jwt>
    """
    global _agent_ws, _last_activity

    # JWT 验签：握手阶段失败则拒绝连接
    if not token:
        logger.warning("[Relay] WebSocket connection rejected: missing token")
        await websocket.close(code=4003, reason="Missing token")
        return

    user_id = None
    if _auth_service is not None:
        try:
            # 通过反射调用 verify_jwt_token（避免循环 import）
            verify_fn = getattr(_auth_service, "verify_jwt_token", None)
            if callable(verify_fn):
                user_id = verify_fn(token)
        except Exception as e:
            logger.warning("[Relay] JWT verification error: %s", e)

    if user_id is None:
        logger.warning("[Relay] WebSocket connection rejected: invalid token")
        await websocket.close(code=4003, reason="Invalid token")
        return

    logger.info("[Relay] JWT verified: user_id=%s", user_id)

    await websocket.accept()
    logger.info("[Relay] Agent connected from %s (user=%s)", websocket.client, user_id)

    _last_activity = time.time()
    # Phase 0：单 Agent，新连接覆盖旧连接
    old_ws = _agent_ws
    _agent_ws = websocket

    if old_ws is not None:
        logger.warning("[Relay] Replacing old agent connection")
        try:
            await old_ws.close()
        except Exception:
            pass

    # 连接后立即 drain 缓冲帧 + 重发未 ACK 的消息
    if _pending_frames or _pending_ack:
        # 先发送缓冲帧
        if _pending_frames:
            drained = list(_pending_frames)
            _pending_frames.clear()
            logger.info("[Relay] Draining %d buffered frames after agent connect", len(drained))
            for pending_frame in drained:
                msg_id = pending_frame.get("msg_id", "")
                try:
                    await websocket.send_text(json.dumps(pending_frame, ensure_ascii=False))
                    # 记录到 _pending_ack 等待确认
                    pending_frame["_sent_at"] = time.time()
                    _pending_ack[msg_id] = pending_frame
                    logger.info("[Relay] Drained buffered msg_id=%s, waiting ACK", msg_id)
                except Exception as e:
                    logger.error("[Relay] Failed to drain msg_id=%s: %s", msg_id, e)
                    break

        # 重发未 ACK 的消息
        if _pending_ack:
            unacked = {mid: f for mid, f in _pending_ack.items()}
            logger.info("[Relay] Resending %d unacked messages", len(unacked))
            for msg_id, frame in unacked.items():
                try:
                    await websocket.send_text(json.dumps(frame, ensure_ascii=False))
                    logger.info("[Relay] Resent unacked msg_id=%s", msg_id)
                except Exception as e:
                    logger.error("[Relay] Failed to resend msg_id=%s: %s", msg_id, e)
                    break

    # 心跳超时检测后台任务
    heartbeat_task = asyncio.create_task(_heartbeat_monitor())

    try:
        while True:
            raw = await websocket.receive_text()
            _last_activity = time.time()
            await _handle_agent_message(raw)
    except WebSocketDisconnect:
        logger.info("[Relay] Agent disconnected")
    except Exception as e:
        logger.error("[Relay] Agent connection error: %s", e)
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        if _agent_ws is websocket:
            _agent_ws = None
        logger.info("[Relay] Agent connection cleaned up")


async def _heartbeat_monitor() -> None:
    """心跳超时检测后台任务。

    每 10s 检查一次，如果超过 _HEARTBEAT_TIMEOUT 秒无活动则主动断开连接。
    """
    global _agent_ws
    while True:
        await asyncio.sleep(10)
        if _agent_ws is not None:
            elapsed = time.time() - _last_activity
            if elapsed > _HEARTBEAT_TIMEOUT:
                logger.warning(
                    "[Relay] Heartbeat timeout: no activity for %.0fs, closing connection",
                    elapsed,
                )
                try:
                    await _agent_ws.close(code=4001, reason="Heartbeat timeout")
                except Exception:
                    pass
                _agent_ws = None
                break


async def _handle_agent_message(raw: str) -> None:
    """处理 Agent 发来的消息帧（回复消息）。

    路由策略：
    1. 依次尝试所有已注册的渠道处理器（XiaoZhi、WeCom 等）
    2. 如果某个处理器返回 True，表示已处理，终止
    3. 如果所有处理器都返回 False，调用兜底处理器
    """
    try:
        frame = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("[Relay] Invalid JSON from agent: %s", raw[:100])
        return

    frame_type = frame.get("type", "")
    msg_id = frame.get("msg_id", "")
    # ★ D1 修复（2026-06-14）：上游 5.2 契约字段为 event_type（EventType.value）。
    message_type = frame.get("event_type", "")
    
    # logger.info(
    #     "[Relay] Received frame from Agent: frame_type=%s, msg_id=%s, message_type=%s, keys=%s",
    #     frame_type, msg_id, message_type, list(frame.keys()),
    # )

    if frame_type == "message":
        # 立即回复 ACK，防止 Gateway 超时重发
        if msg_id and _agent_ws is not None:
            try:
                await _agent_ws.send_text(json.dumps({"type": "ack", "msg_id": msg_id}))
                logger.debug("[Relay] ACK sent: msg_id=%s", msg_id)
            except Exception as e:
                logger.warning("[Relay] Failed to send ACK for msg_id=%s: %s", msg_id, e)

        # 尝试多渠道路由
        # logger.info("[Relay] Routing message: msg_id=%s, message_type=%s, handler_count=%d", 
        #             msg_id, message_type, len(_reply_handlers))
        handled = False
        for handler in _reply_handlers:
            try:
                # logger.info("[Relay] Trying handler: %s", handler.__name__ if hasattr(handler, '__name__') else str(handler))
                if await handler(frame):
                    # logger.info("[Relay] Handler processed: msg_id=%s, handler=%s", 
                    #             msg_id, handler.__name__ if hasattr(handler, '__name__') else str(handler))
                    handled = True
                    break
            except Exception as e:
                logger.error("[Relay] Reply handler error: %s", e)

        # 兜底处理器（向后兼容 WeCom Bridge）
        if not handled and _fallback_reply_handler:
            # logger.info("[Relay] Using fallback handler for msg_id=%s", msg_id)
            try:
                await _fallback_reply_handler(frame)
                # logger.info("[Relay] Fallback handler completed: msg_id=%s", msg_id)
            except Exception as e:
                logger.error("[Relay] Fallback reply handler error: %s", e)
        elif not handled:
            logger.warning("[Relay] No handler processed reply frame")

    elif frame_type == "ping":
        # Gateway 客户端发来的 ping → 立即回 pong，透传 ts
        ping_ts = frame.get("ts")
        if _agent_ws is not None:
            try:
                pong_frame = {"type": "pong"}
                if ping_ts is not None:
                    pong_frame["ts"] = ping_ts
                await _agent_ws.send_text(json.dumps(pong_frame))
            except Exception as e:
                logger.warning("[Relay] Failed to send pong reply: %s", e)

    elif frame_type == "pong":
        # Gateway 对 Relay ping 的回复（当前 Relay 不主动 ping，忽略即可）
        pass

    elif frame_type == "ack":
        # Agent 确认收到消息，从 _pending_ack 中移除
        ack_msg_id = frame.get("msg_id", "")
        if ack_msg_id and ack_msg_id in _pending_ack:
            del _pending_ack[ack_msg_id]
            logger.debug("[Relay] ACK received and cleared: msg_id=%s", ack_msg_id)

    elif frame_type == "close":
        # Gateway 主动关闭
        logger.info("[Relay] Agent requested close: reason=%s", frame.get("reason", ""))

    else:
        logger.debug("[Relay] Unknown frame type: %s", frame_type)
