"""pandapal/local/llm_policies.py — 应用层 LLM 策略实现

B3 原则：SDK 层（pandaren/）不持有 LLM 引用、不直接调用 LLM。
所有依赖 LLM 的策略实现放在应用层，通过 Protocol 注入到 SDK 中。

v1.4 重构变更：
  - CompressionPolicy → CompactionPolicy（SDK 内置 WindowedKeepPolicy，应用层不再自定义切分）
  - SessionSummaryPolicy → 已删除（end_session 不再调 LLM，摘要/知识抽取由应用层定时任务处理）
  - 新增 DropSummarizer Protocol：对被丢弃的消息做 LLM 脉络摘要
  - LLMSummaryCompressionPolicy → 重构为 LLMDropSummarizer

本模块现在只包含：
  LLMDropSummarizer: 基于 LLM 的脉络摘要（实现 DropSummarizer Protocol）
"""

from __future__ import annotations

import json
import logging

from pandaren.llm.protocol import LLMClient
from pandaren.llm.types import ModelSettings
from pandaren.memory.models import MessageDict

logger = logging.getLogger("pandapal.local.llm_policies")


# ═══════════════════════════════════════════════════════
#  LLMDropSummarizer
# ═══════════════════════════════════════════════════════

class LLMDropSummarizer:
    """基于 LLM 的 DropSummarizer：对被压缩丢弃的消息做脉络摘要。

    v1.4 重构后，压缩管线的职责边界重新划分：
      - CompactionPolicy.split()：纯同步切分（SDK 内置 WindowedKeepPolicy）
      - DropSummarizer.summarize()：异步 LLM 摘要（应用层注入）
      - PostCompactReinjector：回注当前状态（不调 LLM）

    本类负责 Layer 3：对 split() 产出的 dropped 消息，调 LLM 生成脉络摘要，
    作为一条 role=system 消息插入到 kept 之前，保留被丢弃对话的关键信息。

    失败时返回 None（降级为静默丢弃），不影响 compact 主流程。

    Args:
        llm_client: LLM 客户端，用于生成摘要。
        max_summary_tokens: 摘要最大 token 数提示（默认 300 字）。
        system_prompt: 生成摘要时使用的系统提示词。
    """

    DEFAULT_SUMMARY_PROMPT = (
        "以下是一段被压缩丢弃的对话历史，请用简洁的中文概括其中的关键信息、"
        "用户意图和已完成的操作，作为后续对话的脉络摘要。"
    )

    def __init__(
        self,
        llm_client: LLMClient,
        max_summary_tokens: int = 500,
        system_prompt: str | None = None,
    ) -> None:
        self._llm_client = llm_client
        self._max_summary_tokens = max_summary_tokens
        self._summary_prompt = system_prompt or self.DEFAULT_SUMMARY_PROMPT

    async def summarize(
        self,
        dropped: list[MessageDict],
    ) -> MessageDict | None:
        """对被丢弃的消息生成脉络摘要。

        Args:
            dropped: 被 CompactionPolicy.split() 切分出的待丢弃消息列表。

        Returns:
            role=system 的摘要消息，或 None（失败/降级时）。
        """
        if not dropped:
            return None

        # 拼装摘要请求：把被丢弃消息序列化为 prompt
        conversation_lines: list[str] = []
        for i, m in enumerate(dropped):
            role = m.get("role", "?")
            content = m.get("content", "")
            # tool_calls 也需要序列化
            tool_calls = m.get("tool_calls")
            tool_call_id = m.get("tool_call_id")

            if tool_calls:
                tc_str = "; ".join(
                    f"{tc.get('function', {}).get('name', '?')}({tc.get('function', {}).get('arguments', '')[:100]})"
                    for tc in tool_calls
                )
                content_str = str(content)[:200].replace("\n", "↵") if content else ""
                line = f"[{i}] {role}: [tool_calls: {tc_str}]"
                if content_str:
                    line += f" {content_str}"
                conversation_lines.append(line)
            elif role == "tool" and tool_call_id:
                content_str = str(content)[:300].replace("\n", "↵") if content else ""
                conversation_lines.append(f"[{i}] {role}({tool_call_id}): {content_str}")
            else:
                if isinstance(content, list):
                    content_str = json.dumps(content, ensure_ascii=False)[:300].replace("\n", "↵")
                else:
                    content_str = str(content)[:300].replace("\n", "↵") if content else ""
                conversation_lines.append(f"[{i}] {role}: {content_str}")

        prompt = (
            f"{self._summary_prompt}\n"
            f"控制在 {self._max_summary_tokens} 字以内。\n\n"
            + "\n".join(conversation_lines)
        )

        try:
            resp = await self._llm_client.call(
                messages=[{"role": "user", "content": prompt}],
                settings=ModelSettings(
                    max_tokens=512,
                    temperature=0.3,
                ),
            )
            summary_text = resp.get("content") or ""
            if not summary_text.strip():
                return None
            body = f"【对话脉络摘要】{summary_text.strip()}"
            return {"role": "system", "content": body}
        except Exception as e:
            logger.warning("LLM 脉络摘要生成失败: %s", e)
            # 降级：摘要不可用时静默丢弃
            return None
