"""pandaren/memory/compaction/micro_compact.py — MicroCompact

**MicroCompact 不是切分策略**——它不切割消息序列，只**就地清空**早期的、
易再生的工具结果正文，把"工具结果可再生"这个性质转化为 token 节省。

两个触发时机（都由 Memory Facade 调度）：

A) **add_tool_result 入口防爆炸**：
   单条工具结果超过 ``single_result_max_tokens`` 立即截断尾部，
   防止单步操作把上下文吃满。

B) **compact_if_needed 入口预清理**：
   所有比 ``keep_recent`` 早的、且工具名在 ``tools`` 白名单的
   tool_result 替换为占位符。如果清理后 token 已 < 阈值，
   可以**跳过 conversation compact**，省一次 LLM 调用。

工具白名单由应用层注入（pandaren SDK 自身没有 Read/Bash 这些固定工具，
不像 claude-code）。SDK 只提供算法，应用决定哪些工具的结果是"可再生的"。

设计要点：
  - 算法纯规则、零依赖、不调 LLM（B3）
  - 只修改 ``role=tool`` 消息的 ``content`` 字段，不动其他字段
  - 不修改入参；返回新列表
"""

from __future__ import annotations

import logging
from typing import Iterable

from ..constants import (
    DEFAULT_MICROCOMPACT_KEEP_RECENT,
    DEFAULT_MICROCOMPACT_SINGLE_RESULT_MAX_TOKENS,
    MICROCOMPACT_CLEARED_PLACEHOLDER,
    MICROCOMPACT_TRUNCATED_SUFFIX,
    CHARS_PER_TOKEN,
)
from ..models import MessageDict
from ..protocols import TokenEstimator, CharBasedTokenEstimator

logger = logging.getLogger("pandaren.memory.compaction.micro_compact")


def _content_to_text(content: str | list) -> str:
    """把消息 content（可能是字符串或 multimodal 列表）转成 text 估算。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    parts.append(str(part.get("text", "")))
                else:
                    # 非 text 块（image/document）按估算成本占位（不实际复制内容）
                    parts.append(f"[{part.get('type', 'unknown')}]")
            else:
                parts.append(str(part))
        return "\n".join(parts)
    return ""


class MicroCompactor:
    """MicroCompact 算法实现（无状态，可复用）。

    Args:
        compactable_tools: 哪些工具的 tool_result 可以被清空（应用层提供）
        keep_recent:       compact_if_needed 入口预清理时，最近 N 条工具结果不动
        single_result_max_tokens: add_tool_result 入口单条结果上限
        token_estimator:   Token 估算器
    """

    def __init__(
        self,
        compactable_tools: Iterable[str] | None = None,
        keep_recent: int = DEFAULT_MICROCOMPACT_KEEP_RECENT,
        single_result_max_tokens: int = DEFAULT_MICROCOMPACT_SINGLE_RESULT_MAX_TOKENS,
        token_estimator: TokenEstimator | None = None,
    ) -> None:
        self._tools: frozenset[str] = frozenset(compactable_tools or ())
        self._keep_recent: int = max(1, keep_recent)  # 至少保留 1 条
        self._single_result_max_tokens: int = single_result_max_tokens
        self._token_estimator: TokenEstimator = (
            token_estimator or CharBasedTokenEstimator()
        )

    # ─── 时机 A: add_tool_result 防爆炸 ───

    def truncate_single_result_if_needed(
        self,
        content: str | list,
    ) -> str | list:
        """单条工具结果超过单条上限时截断尾部并加截断标记。

        若未超过返回原 content；超过返回截断后的字符串
        （不保留 multimodal 结构 —— 截断意味着已经超过预算，丢掉非 text 内容是合理的）。
        """
        msg_for_estimate: MessageDict = {"role": "tool", "content": content}
        tokens = self._token_estimator.estimate([msg_for_estimate])
        if tokens <= self._single_result_max_tokens:
            return content

        text = _content_to_text(content)
        # 按字符数粗略对齐到 token 上限（CHARS_PER_TOKEN 是 float，必须 int 转换）
        max_chars = int(self._single_result_max_tokens * CHARS_PER_TOKEN)
        # 留 ~200 字给截断标记
        cut_at = max(0, max_chars - 200)
        truncated = text[:cut_at] + MICROCOMPACT_TRUNCATED_SUFFIX
        logger.info(
            "MicroCompact.truncate_single_result: %d tokens → ~%d tokens (cap %d)",
            tokens,
            self._single_result_max_tokens,
            self._single_result_max_tokens,
        )
        return truncated

    # ─── 时机 B: compact_if_needed 入口预清理 ───

    def clear_old_tool_results(
        self,
        messages: list[MessageDict],
        assistant_tool_lookup: dict[str, str] | None = None,
    ) -> tuple[list[MessageDict], int]:
        """清空旧 tool_result 正文，保留最近 ``keep_recent`` 条。

        Args:
            messages: 完整消息列表（不含 system）
            assistant_tool_lookup: tool_call_id → tool_name 映射；
                若为 None，会从 messages 自己重建。

        Returns:
            (新消息列表, 节省的 token 估算值)

        清空规则：
          - 只处理 role=tool 消息
          - 该 tool_result 对应的 tool_name（通过 tool_call_id 反查 assistant.tool_calls）
            必须在 ``compactable_tools`` 白名单中
          - 该 tool_result 不在最近 ``keep_recent`` 条之内
          - content 不已经是 ``MICROCOMPACT_CLEARED_PLACEHOLDER``（避免重复处理）
        """
        if not messages or not self._tools:
            # 没有白名单 = 应用层没指定可清工具 = 不动任何东西
            return list(messages), 0

        # 重建 tool_call_id → tool_name 映射
        if assistant_tool_lookup is None:
            assistant_tool_lookup = self._build_tool_lookup(messages)

        # 收集所有 tool_result 的 index（按时间序）
        tool_result_indices: list[int] = [
            i for i, msg in enumerate(messages) if msg.get("role") == "tool"
        ]
        if len(tool_result_indices) <= self._keep_recent:
            return list(messages), 0

        # 最近 keep_recent 条不动
        protected_indices: set[int] = set(tool_result_indices[-self._keep_recent:])
        candidates: list[int] = [
            i for i in tool_result_indices if i not in protected_indices
        ]

        new_messages: list[MessageDict] = []
        tokens_saved = 0
        cleared_count = 0

        for i, msg in enumerate(messages):
            if i in candidates:
                tc_id = msg.get("tool_call_id")
                tool_name = (
                    assistant_tool_lookup.get(tc_id, "")
                    if isinstance(tc_id, str)
                    else ""
                )
                if tool_name in self._tools:
                    # 已经是占位符则不重复处理
                    if msg.get("content") == MICROCOMPACT_CLEARED_PLACEHOLDER:
                        new_messages.append(msg)
                        continue
                    before = self._token_estimator.estimate([msg])
                    new_msg = dict(msg)
                    new_msg["content"] = MICROCOMPACT_CLEARED_PLACEHOLDER
                    after = self._token_estimator.estimate([new_msg])
                    tokens_saved += max(0, before - after)
                    cleared_count += 1
                    new_messages.append(new_msg)
                    continue
            new_messages.append(dict(msg))

        if cleared_count:
            logger.info(
                "MicroCompact.clear_old_tool_results: cleared %d tool_result(s), "
                "saved ~%d tokens (whitelist=%d tools, keep_recent=%d)",
                cleared_count,
                tokens_saved,
                len(self._tools),
                self._keep_recent,
            )
        return new_messages, tokens_saved

    # ─── 内部 ───

    @staticmethod
    def _build_tool_lookup(messages: list[MessageDict]) -> dict[str, str]:
        """从 messages 中重建 tool_call_id → tool_name 映射。"""
        lookup: dict[str, str] = {}
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            tool_calls = msg.get("tool_calls") or []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tc_id = tc.get("id")
                fn = tc.get("function") or {}
                name = fn.get("name") if isinstance(fn, dict) else None
                if isinstance(tc_id, str) and isinstance(name, str):
                    lookup.setdefault(tc_id, name)
        return lookup

    @property
    def has_whitelist(self) -> bool:
        """是否配置了 compactable_tools 白名单（无白名单时 clear_old_tool_results 是 no-op）。"""
        return bool(self._tools)
