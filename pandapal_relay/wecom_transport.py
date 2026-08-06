"""pandapal_relay.wecom_transport — WeComRestTransport。

★ 关键设计（5.2.G）：
- 把 NormalizedEvent 转为 WeCom 消息推送
- 22 种白名单事件**全部处理**（不吞掉）；非白名单由 W1 闸口 skip
- LLM_TOKEN 仅在 STREAM 策略时被 Broadcast 过滤掉；这里如果进来就累积
- REPLY_END 触发 buffer flush 推送完整文本
- O3 Never Throw：所有异常内部消化
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

# ── 本地副本（Relay 独立部署，不依赖完整 pandapal 包）──
from .transport_protocol import Transport
from .normalized_events import EventType, NormalizedEvent
from pandapal_relay.wecom.sender import WeComSender

logger = logging.getLogger(__name__)

# 微信单条消息字数上限（保守值）
WECOM_TEXT_LIMIT = 4000
# 工具结果"内联显示"上限（< 1.5KB 内联，否则摘要+引导）
WECOM_RESULT_INLINE_LIMIT = 1500

# ── W1 出站白名单（2026-06-14）：WeCom 支持的 21 种事件类型 ──
# ★ 真相源：pandapal/broadcast/policy.py:EVENT_RENDERING_HINTS（wecom 非跳过项）
# ★ W2 已裁决移除 USER_INPUT_ECHO（严格隔离，不参与跨渠道 echo）
# ★ 新增类型时必须同步更新；CI 对账脚本（scripts/check_protocol_sync.py）防漂移
WECOM_SUPPORTED_EVENTS: frozenset[EventType] = frozenset({
    EventType.LLM_TOKEN, EventType.REASONING_TOKEN,
    EventType.REPLY_START, EventType.REPLY_END,
    EventType.TOOL_START, EventType.TOOL_END,
    EventType.HITL_REQUEST, EventType.PLAN_APPROVAL_REQUEST, EventType.INTERACTION_REQUEST,
    EventType.ERROR,
    EventType.AGENT_REPLY, EventType.APPROVAL_RESULT,
    EventType.TASK_NOTIFICATION, EventType.PERMISSION_DENIED,
    EventType.AGENT_HALTED, EventType.RUN_START, EventType.RUN_END,
    EventType.AGENT_TASK_EVENT, EventType.QUICK_APP_DATA,
    EventType.SKILL_PROGRESS,
    EventType.SCHEDULED_TASK_LIST, EventType.SCHEDULED_TASK_CHANGED,
})


class WeComRestTransport(Transport):
    """把 NormalizedEvent 推给 WeCom 用户的 Transport。"""

    def __init__(
        self,
        sender: WeComSender,
        user_id: str = "",
    ) -> None:
        self._sender = sender
        self._user_id = user_id
        self._started = False  # ★ Transport 契约字段
        # reply_id → 累积 LLM_TOKEN 文本
        self._stream_buffer: dict[str, str] = {}
        # tool_call_id → (full_text, ts)，5min TTL
        self._tool_results_cache: dict[str, tuple[str, float]] = {}

    @property
    def is_started(self) -> bool:
        """★ Transport 契约：start() 后为 True，stop() 后为 False。"""
        return self._started

    async def start(self) -> None:
        """启动时校验 access_token（★ 根本解 2026-06-10 后加幂等保护）。"""
        if self._started:
            return  # 幂等
        try:
            await self._sender.verify_access_token()
        except Exception as e:
            # 启动期失败不算致命——记 warning，is_started 仍置 True（已尝试启动）
            # 这样 run_relay 启动自检不会误报，且 send() 内部会走兜底逻辑
            logger.warning("WeComRestTransport: verify_access_token failed: %s", e)
        self._started = True
        logger.info("WeComRestTransport started (user_id=%s)", self._user_id)

    async def stop(self) -> None:
        """关闭 transport。幂等。"""
        if not self._started:
            return
        self._started = False
        logger.info("WeComRestTransport stopped (user_id=%s)", self._user_id)

    async def send(self, event: NormalizedEvent) -> None:
        """把 NormalizedEvent 推给 WeCom 用户。"""
        try:
            t = event.event_type
            p = event.payload
            user_id = self._user_id

            # ★ W1 出站闸口（2026-06-14）：非白名单事件静默跳过（debug 留痕）
            if t not in WECOM_SUPPORTED_EVENTS:
                logger.debug("[WeCom] skip unsupported event: %s", t.value)
                return

            # ── 流式事件：仅 LLM_TOKEN / REASONING_TOKEN ──
            if t == EventType.LLM_TOKEN:
                self._stream_buffer.setdefault(event.reply_id or "", "")
                self._stream_buffer[event.reply_id or ""] += p.get("delta", "")
                return

            if t == EventType.REASONING_TOKEN:
                # 推理过程：WeCom 端不显示（避免刷屏）
                return

            # ── 离散事件：每种类型独立处理 ──
            if t == EventType.REPLY_END:
                # 关键改造：REPLY_END 触发 buffer flush
                full_text = self._stream_buffer.pop(event.reply_id or "", "")
                if full_text:
                    await self._sender.send_text(user_id, full_text)
                status = p.get("status", "ok")
                if status == "halted":
                    await self._sender.send_text(user_id, "[Agent 已停止]")
                if status == "error":
                    await self._sender.send_text(user_id, f"❌ [错误] {p.get('output', '')}")
                if status == "paused_for_hitl":
                    pass  # HITL_REQUEST 会单独发卡片
                return

            if t == EventType.TOOL_START:
                tool_name = p["tool_name"]
                tool_args = p.get("tool_args", {})
                args_preview = json.dumps(tool_args, ensure_ascii=False)[:200]
                await self._sender.send_text(
                    user_id, f"🔧 调用 {tool_name}…\n参数: {args_preview}"
                )
                return

            if t == EventType.TOOL_END:
                await self._handle_tool_end(user_id, p)
                return

            if t == EventType.REPLY_START:
                # WeCom 端无流概念，仅作为标记（不发文本）
                return

            if t == EventType.HITL_REQUEST:
                await self._sender.send_template_card_for_approval(
                    user_id=user_id,
                    approval_id=p["approval_id"],
                    tool_name=p["tool_name"],
                    tool_args_summary=p.get("tool_args_summary", {}),
                    session_id=p["session_id"],  # D6: fail-fast，不降级
                )
                return

            if t == EventType.PLAN_APPROVAL_REQUEST:
                plan_content = p.get("plan_content", "")
                if plan_content:
                    # 截断长文本，企微单条消息建议不超过 2048 字符
                    truncated = plan_content[:1500] + "\n…" if len(plan_content) > 1500 else plan_content
                    await self._sender.send_text(
                        user_id,
                        f"📋 Plan Mode 审批请求\n\n{truncated}",
                    )
                return

            if t == EventType.INTERACTION_REQUEST:
                questions = p.get("questions", [])
                lines = ["❓ 请回答以下问题："]
                for qi, q in enumerate(questions, 1):
                    q_text = q.get("question", "")
                    opts = q.get("options", [])
                    lines.append(f"{qi}. {q_text}")
                    for oi, opt in enumerate(opts):
                        label = opt.get("label", "") if isinstance(opt, dict) else str(opt)
                        lines.append(f"   {chr(65 + oi)}. {label}")
                await self._sender.send_text(user_id, "\n".join(lines))
                return

            if t == EventType.USER_INPUT_ECHO:
                # WeCom 端不用 echo（用户自己发的，自己已看到）
                return

            if t == EventType.ERROR:
                err_msg = (
                    f"❌ [错误 {p.get('error_code', 'unknown')}] "
                    f"{p.get('error_message', '')}"
                )
                await self._sender.send_text(user_id, err_msg)
                return

            if t == EventType.APPROVAL_RESULT:
                decision_emoji = "✅" if p["decision"] == "approved" else "❌"
                decision_text = "已批准" if p["decision"] == "approved" else "已拒绝"
                await self._sender.send_text(
                    user_id,
                    f"{decision_emoji} 审批 {p['approval_id']} {decision_text}",
                )
                return

            if t == EventType.TASK_NOTIFICATION:
                level_emoji = {
                    "info": "ℹ️", "warn": "⚠️", "error": "❌"
                }.get(p.get("level", "info"), "ℹ️")
                await self._sender.send_text(
                    user_id, f"{level_emoji} {p['title']}\n{p.get('body', '')}"
                )
                return

            if t == EventType.PERMISSION_DENIED:
                await self._sender.send_text(
                    user_id, f"🚫 权限被拒：{p.get('reason', '')}"
                )
                return

            if t == EventType.AGENT_HALTED:
                reason = p.get("reason", "")
                text = f"⏹ {reason}" if reason else "⏹ Agent 已停止"
                # 截断保护：WeCom 文本消息上限 2048，留足余量
                if len(text) > 500:
                    text = text[:497] + "..."
                await self._sender.send_text(user_id, text)
                return

            if t == EventType.AGENT_REPLY:
                content = p.get("content", "")
                if content:
                    await self._sender.send_text(user_id, content)
                return

            if t in (EventType.RUN_START, EventType.RUN_END):
                # WeCom 端不展示（运行起止由其他事件承载）
                return

            # ── 简版文本事件（5 种，2026-06-14）──
            if t == EventType.AGENT_TASK_EVENT:
                subject = p.get("subject") or p.get("task_name") or ""
                if subject:
                    await self._sender.send_text(user_id, f"📋 {subject}")
                return

            if t == EventType.QUICK_APP_DATA:
                await self._sender.send_text(
                    user_id,
                    f"📱 {p.get('app_id', '快应用')} 推送",
                )
                return

            if t == EventType.SKILL_PROGRESS:
                activity = p.get("activity", "")
                phase = p.get("phase", "")
                if activity:
                    await self._sender.send_text(user_id, f"⚙️ {activity}: {phase}")
                return

            if t == EventType.SCHEDULED_TASK_LIST:
                # 列表推送，WeCom 端不逐个展示
                return

            if t == EventType.SCHEDULED_TASK_CHANGED:
                task = p.get("task", {})
                change = p.get("change_type", "updated")
                name = task.get("name", "") if isinstance(task, dict) else str(task)
                await self._sender.send_text(
                    user_id,
                    f"⏰ 定时任务{'已删除' if change == 'deleted' else '已更新'}"
                    f"{': ' + name if name else ''}",
                )
                return

            # 白名单内但无显式渲染分支（不应到达，留痕防御）
            logger.warning("[WeCom] whitelisted event has no render branch: %s", t.value)

        except Exception as e:
            logger.exception("WeComRestTransport.send failed: %s", e)

    async def _handle_tool_end(self, user_id: str, p: dict) -> None:
        """WeCom 端 TOOL_END 智能渲染。

        - 失败 → 完整错误信息
        - 小结果（< 1.5KB）→ 完整内容内联
        - 大结果 → 摘要 + 引导去桌面端
        """
        tool_name = p["tool_name"]
        is_error = p.get("is_error", False)
        tool_call_id = p["tool_call_id"]

        if is_error:
            error_text = p.get("result_error", "未知错误")
            await self._sender.send_text(
                user_id, f"❌ **{tool_name} 失败**\n{error_text}"
            )
            return

        result_full = p.get("result_full")
        size = p.get("result_size_bytes", 0)
        preview = p.get("result_preview", "")

        if size <= WECOM_RESULT_INLINE_LIMIT:
            full_text = self._format_result(result_full, p.get("result_mime_type"))
            msg = f"✅ **{tool_name}** {preview}\n\n```\n{full_text[:WECOM_RESULT_INLINE_LIMIT]}\n```"
            if len(msg) <= WECOM_TEXT_LIMIT:
                await self._sender.send_text(user_id, msg)
            else:
                await self._sender.send_template_card_for_result(
                    user_id=user_id, tool_name=tool_name,
                    preview=preview, result_text=full_text, result_url=None,
                )
        else:
            await self._sender.send_text(
                user_id,
                f"✅ **{tool_name}** {preview}\n"
                f"📊 结果大小：{_format_bytes(size)}（已超出 WeCom 显示上限）\n"
                f"👉 请在桌面端查看完整结果（{tool_call_id[:8]}）",
            )
            full_text = self._format_result(result_full, p.get("result_mime_type"))
            self._tool_results_cache[tool_call_id] = (full_text, time.time())

    @staticmethod
    def _format_result(result: Any, mime_type: str | None) -> str:
        if result is None:
            return "(无输出)"
        if isinstance(result, str):
            return result
        if mime_type == "application/json" or isinstance(result, (dict, list)):
            return json.dumps(result, ensure_ascii=False, indent=2)
        return str(result)


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"
