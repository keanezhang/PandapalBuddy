"""
企业微信消息发送器

WeComSender — 通过企微 API 发送文本/Markdown/图片/模板卡片消息
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

logger = logging.getLogger("relay_server.wecom.sender")


class WeComSender:
    """企业微信消息发送器"""

    WECOM_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"

    def __init__(self, corp_id: str, agent_id: str, app_secret: str):
        self.corp_id = corp_id
        self.agent_id = agent_id
        self.app_secret = app_secret
        self._token = ""
        self._token_expires_at = 0.0
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def verify_access_token(self) -> bool:
        """5.2 新增：校验 access_token 是否可用（Transport.start 调用）。"""
        token = await self._get_access_token()
        return bool(token)

    async def _get_access_token(self) -> str:
        import time

        now = time.time()
        if self._token_expires_at > now + 300:
            return self._token

        url = (
            f"{self.WECOM_API_BASE}/gettoken"
            f"?corpid={self.corp_id}&corpsecret={self.app_secret}"
        )
        try:
            client = await self._get_client()
            resp = await client.get(url)
            data = resp.json()
        except Exception as e:
            logger.warning("[WeComSender] 获取 access_token 异常: %s", e)
            return ""

        if data.get("errcode", 0) != 0:
            logger.warning("[WeComSender] 获取 access_token 失败: %s", data)
            return ""

        self._token = data["access_token"]
        self._token_expires_at = now + int(data.get("expires_in", 7200))
        return self._token

    # 企微文本消息单条上限 2048 字节；中文 UTF-8 一个字 3 字节，
    # 保守按 680 字符切片（680×3=2040 < 2048），确保不超限。
    _MAX_CHUNK_CHARS = 680

    async def send_text(self, user_id: str, content: str) -> bool:
        """发送文本消息，超长时自动分片。"""
        if len(content.encode("utf-8")) <= 2048:
            # 短消息直接发
            return await self._send_text_single(user_id, content)

        # 超长 → 分片发送
        chunks = self._split_content(content)
        total = len(chunks)
        logger.info(
            "[WeComSender] 超长消息分片: user=%s total_len=%d chunks=%d",
            user_id, len(content), total,
        )
        all_ok = True
        for i, chunk in enumerate(chunks, 1):
            if total > 1:
                header = f"【{i}/{total}】\n"
                chunk = header + chunk
            ok = await self._send_text_single(user_id, chunk)
            if not ok:
                all_ok = False
            # 企微 API 频率限制：同一用户不超过 30 条/秒，稍作间隔
            if i < total:
                await asyncio.sleep(0.2)
        return all_ok

    def _split_content(self, content: str) -> list[str]:
        """按 UTF-8 字节安全切分内容，尽量在换行处断开。"""
        # 预留 header "【xx/xx】\n" 占位（最多 12 字符 = ~36 字节）
        effective_limit = self._MAX_CHUNK_CHARS - 15
        chunks: list[str] = []
        remaining = content

        while remaining:
            if len(remaining.encode("utf-8")) <= 2048:
                chunks.append(remaining)
                break

            # 找一个安全的切分点
            cut = effective_limit
            # 尝试在最近的换行处切
            newline_pos = remaining.rfind("\n", 0, cut)
            if newline_pos > cut // 2:
                cut = newline_pos + 1  # 包含换行符

            chunks.append(remaining[:cut])
            remaining = remaining[cut:]

        return chunks

    async def _send_text_single(self, user_id: str, content: str) -> bool:
        """发送单条文本消息（不超过 2048 字节）。"""
        token = await self._get_access_token()
        if not token:
            return False

        url = f"{self.WECOM_API_BASE}/message/send?access_token={token}"
        body = {
            "touser": user_id,
            "msgtype": "text",
            "agentid": int(self.agent_id),
            "text": {"content": content[:2048]},
        }
        try:
            client = await self._get_client()
            resp = await client.post(url, json=body)
            result = resp.json()
            if result.get("errcode") != 0:
                logger.warning("[WeComSender] 发送失败: %s", result)
                return False
            return True
        except Exception as e:
            logger.warning("[WeComSender] 发送异常: %s", e)
            return False

    async def send_markdown(self, user_id: str, content: str) -> bool:
        """发送 Markdown 消息，超长时降级为分片纯文本。"""
        # Markdown 也有 2048 字节限制；超长直接降级为分片文本
        if len(content.encode("utf-8")) > 2048:
            logger.info(
                "[WeComSender] Markdown 超长(%d bytes)，降级为分片文本",
                len(content.encode("utf-8")),
            )
            return await self.send_text(user_id, content)

        token = await self._get_access_token()
        if not token:
            return False

        url = f"{self.WECOM_API_BASE}/message/send?access_token={token}"
        body = {
            "touser": user_id,
            "msgtype": "markdown",
            "agentid": int(self.agent_id),
            "markdown": {"content": content},
        }
        try:
            client = await self._get_client()
            resp = await client.post(url, json=body)
            result = resp.json()
            if result.get("errcode") != 0:
                # 降级为纯文本（分片）
                return await self.send_text(user_id, content)
            return True
        except Exception as e:
            logger.warning("[WeComSender] Markdown 发送异常: %s", e)
            return False

    async def send_template_card(
        self,
        user_id: str,
        approval_id: str,
        tool_name: str,
        tool_args_summary: str,
        timeout_seconds: int,
        session_id: str,
    ) -> bool:
        """发送 button_interaction 类型模板卡片（用于 HITL 审批）。

        TaskId 设为 approval_id，用户点击按钮后企微回调中原样返回。
        ButtonKey 格式: "approve:<session_id>" / "reject:<session_id>"，
        回调时 wecom_bridge 解析还原 decision 和 session_id。

        session_id 必传，不退化——SESSION_ID 契约 0 容忍空值。
        """
        token = await self._get_access_token()
        if not token:
            return False

        if not session_id:
            logger.error("[WeComSender] session_id is empty, refusing to send approval card")
            return False

        # 按钮 key 编码 session_id（D6 修复）
        approve_key = f"approve:{session_id}"
        reject_key = f"reject:{session_id}"

        url = f"{self.WECOM_API_BASE}/message/send?access_token={token}"
        body = {
            "touser": user_id,
            "msgtype": "template_card",
            "agentid": int(self.agent_id),
            "template_card": {
                "card_type": "button_interaction",
                "task_id": approval_id,
                "source": {"desc": "操作审批请求"},
                "main_title": {
                    "title": "【Agent 操作审批】",
                    "desc": f"工具：{tool_name}",
                },
                "sub_title_text": (
                    f"参数：{tool_args_summary[:200]}\n\n"
                    f"超时：{timeout_seconds}s 后自动拒绝"
                ),
                "button_list": [
                    {"text": "✅ 同意", "key": approve_key, "type": 1, "style": 4},
                    {"text": "❌ 拒绝", "key": reject_key,  "type": 1, "style": 2},
                ],
            },
        }
        try:
            client = await self._get_client()
            resp = await client.post(url, json=body)
            result = resp.json()
            if result.get("errcode") != 0:
                logger.warning("[WeComSender] template_card 发送失败: %s", result)
                return False
            return True
        except Exception as e:
            logger.warning("[WeComSender] template_card 发送异常: %s", e)
            return False

    async def send_template_card_for_approval(
        self,
        user_id: str,
        approval_id: str,
        tool_name: str,
        tool_args_summary: dict | str,
        session_id: str,
    ) -> bool:
        """5.2 改造后版本：参数 tool_args_summary 接受 dict | str。

        兼容原 send_template_card（接受 str）。
        D6: 新增 session_id 参数，编码到按钮 EventKey 中解决审批回调缺失问题。
        """
        if isinstance(tool_args_summary, dict):
            try:
                import json as _json
                tool_args_summary = _json.dumps(
                    tool_args_summary, ensure_ascii=False
                )
            except (TypeError, ValueError):
                tool_args_summary = str(tool_args_summary)
        # 默认 300s 超时
        return await self.send_template_card(
            user_id=user_id,
            approval_id=approval_id,
            tool_name=tool_name,
            tool_args_summary=tool_args_summary,
            timeout_seconds=300,
            session_id=session_id,
        )

    async def send_template_card_for_result(
        self,
        user_id: str,
        tool_name: str,
        preview: str,
        result_text: str,
        result_url: str | None = None,
    ) -> bool:
        """5.2 新增：工具结果超长时用模板卡片展示（替代文本）。"""
        token = await self._get_access_token()
        if not token:
            return False

        url = f"{self.WECOM_API_BASE}/message/send?access_token={token}"
        body = {
            "touser": user_id,
            "msgtype": "template_card",
            "agentid": int(self.agent_id),
            "template_card": {
                "card_type": "text_notice",
                "source": {"desc": "工具结果"},
                "main_title": {
                    "title": f"✅ {tool_name}",
                    "desc": preview[:200],
                },
                "sub_title_text": result_text[:2000],
                "horizontal_content_list": [],
                "jump_list": (
                    [{"type": 1, "title": "查看完整结果", "url": result_url}]
                    if result_url else []
                ),
            },
        }
        try:
            client = await self._get_client()
            resp = await client.post(url, json=body)
            result = resp.json()
            if result.get("errcode") != 0:
                logger.warning(
                    "[WeComSender] send_template_card_for_result 失败: %s", result
                )
                return False
            return True
        except Exception as e:
            logger.warning(
                "[WeComSender] send_template_card_for_result 异常: %s", e
            )
            return False

    async def send_image(
        self,
        user_id: str,
        image_base64: str,
        filename: str = "image.png",
    ) -> bool:
        """通过 base64 数据发送图片（本地系统将图片编码后回传）"""
        token = await self._get_access_token()
        if not token:
            return False

        # 上传临时素材
        upload_url = (
            f"{self.WECOM_API_BASE}/media/upload?access_token={token}&type=image"
        )
        try:
            import base64 as b64

            image_bytes = b64.b64decode(image_base64)
            client = await self._get_client()
            files = {"media": (filename, image_bytes, "image/png")}
            resp = await client.post(upload_url, files=files)
            upload_result = resp.json()
            if upload_result.get("errcode", 0) != 0:
                logger.warning("[WeComSender] 上传图片失败: %s", upload_result)
                return False
            media_id = upload_result.get("media_id", "")
            if not media_id:
                return False
        except Exception as e:
            logger.warning("[WeComSender] 上传图片异常: %s", e)
            return False

        # 发送图片消息
        send_url = f"{self.WECOM_API_BASE}/message/send?access_token={token}"
        body = {
            "touser": user_id,
            "msgtype": "image",
            "agentid": int(self.agent_id),
            "image": {"media_id": media_id},
        }
        try:
            resp = await client.post(send_url, json=body)
            result = resp.json()
            if result.get("errcode") != 0:
                logger.warning("[WeComSender] 图片发送失败: %s", result)
                return False
            return True
        except Exception as e:
            logger.warning("[WeComSender] 图片发送异常: %s", e)
            return False
