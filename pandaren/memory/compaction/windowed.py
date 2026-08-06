"""pandaren/memory/compaction/windowed.py — 三维度窗口保留切分策略

从对话尾部向前扩展保留窗口，扩展终止条件：

  1. **同时满足两个下限**：
     - ``min_keep_tokens``         — 保留的总 token 数下限（确保上下文深度）
     - ``min_keep_text_messages``  — 保留的"含 text 块"消息条数下限
                                     （确保对话连续性，避免窗口里全是 tool result）
  2. **撞到上限**：
     - ``max_keep_tokens`` — 硬上限，防止保留过多导致压缩后立刻又触发

设计参考：claude-code ``SessionMemoryCompactConfig`` (10K/5/40K)。
pandaren 默认使用 8K/4/40K，更保守（任务通常更短）。

切分完毕后会强制调 ``ensure_tool_pair_integrity()``：API 硬约束不可违反；
被该兜底进一步丢弃的消息也算入 ``dropped``。
"""

from __future__ import annotations

import logging

from ..models import MessageDict, CompactionSplit
from ..constants import (
    DEFAULT_MIN_KEEP_TOKENS,
    DEFAULT_MIN_KEEP_TEXT_MESSAGES,
    DEFAULT_MAX_KEEP_TOKENS,
)
from ..protocols import TokenEstimator, CharBasedTokenEstimator
from .tool_pair_integrity import ensure_tool_pair_integrity

logger = logging.getLogger("pandaren.memory.compaction")


def _has_text_block(msg: MessageDict) -> bool:
    """判断消息是否含有"真对话文本"。

    定义：
      - role=user 或 role=assistant
      - content 为非空字符串，或 content 是 list 且至少有一个 type='text' 的非空块
      - **role=tool 永远不算**（tool result 是工具产物，不算对话连续性）
      - 带 tool_calls 的 assistant 若 content 非空仍算（解释性文本）；content 空则不算
    """
    role = msg.get("role")
    if role not in ("user", "assistant"):
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text" and str(part.get("text", "")).strip():
                    return True
            elif isinstance(part, str) and part.strip():
                return True
        return False
    return False


def _msg_identity(msg: MessageDict) -> tuple:
    """生成可哈希的消息身份键，用于在 ensure_tool_pair_integrity 修剪后
    识别哪些消息被进一步丢弃。

    role + tool_call_id（若有）+ content 的字符串化已足够区分；
    完美哈希不是目标，仅用于"两次结果对比找差集"。
    """
    return (
        msg.get("role"),
        msg.get("tool_call_id"),
        str(msg.get("content")),
        str(msg.get("tool_calls", "")),
    )


class WindowedKeepPolicy:
    """三维度窗口保留切分策略（实现 CompactionPolicy Protocol）。

    Args:
        min_keep_tokens:        保留窗口最少 token 数（默认 8K）
        min_keep_text_messages: 保留窗口最少"含 text 块"消息数（默认 4）
        max_keep_tokens:        保留窗口最多 token 数（默认 40K）
        token_estimator:        Token 估算器（默认 CharBasedTokenEstimator）

    传入的 messages 不含 system 消息（由 Memory Facade 管理）。
    """

    def __init__(
        self,
        min_keep_tokens: int = DEFAULT_MIN_KEEP_TOKENS,
        min_keep_text_messages: int = DEFAULT_MIN_KEEP_TEXT_MESSAGES,
        max_keep_tokens: int = DEFAULT_MAX_KEEP_TOKENS,
        token_estimator: TokenEstimator | None = None,
    ) -> None:
        if min_keep_tokens <= 0:
            raise ValueError(f"min_keep_tokens must be > 0, got {min_keep_tokens}")
        if min_keep_text_messages <= 0:
            raise ValueError(
                f"min_keep_text_messages must be > 0, got {min_keep_text_messages}"
            )
        if max_keep_tokens < min_keep_tokens:
            raise ValueError(
                f"max_keep_tokens ({max_keep_tokens}) must be >= "
                f"min_keep_tokens ({min_keep_tokens})"
            )
        self._min_tokens = min_keep_tokens
        self._min_text_messages = min_keep_text_messages
        self._max_tokens = max_keep_tokens
        self._token_estimator: TokenEstimator = (
            token_estimator or CharBasedTokenEstimator()
        )

    def split(
        self,
        messages: list[MessageDict],
        max_tokens: int,
    ) -> CompactionSplit:
        """从尾向前扩展窗口，返回 CompactionSplit(kept, dropped)。

        ``max_tokens`` 参数语义为 Facade 期望的目标预算（COMPACT_TARGET_RATIO 应用后）。
        WindowedKeepPolicy 的内部上限 ``max_keep_tokens`` 与之共同生效，**取较小值**
        作为本次扩展的硬上限——既尊重 Facade 的预算，又保障策略自身的稳定性。

        Returns:
            CompactionSplit(kept, dropped)
            kept + dropped 必然是原 messages 的一个划分（不重不漏，时序保持）。
        """
        if not messages:
            return CompactionSplit(kept=[], dropped=[])

        # 实际硬上限：取 Facade 预算与策略上限的较小值
        hard_cap = min(self._max_tokens, max_tokens) if max_tokens > 0 else self._max_tokens

        accumulated_tokens = 0
        text_count = 0
        kept_indices_reversed: list[int] = []  # 以倒序入栈

        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            msg_tokens = self._token_estimator.estimate([msg])

            # 即将加入这条消息后是否突破 hard_cap？
            # 注意：不做"至少保留一条"豁免——超大消息应进入 dropped → L2 摘要管线，
            # 否则保留一条超大消息会导致压缩后仍超出 compact_threshold（ContextOverflowError）。
            if accumulated_tokens + msg_tokens > hard_cap:
                break

            kept_indices_reversed.append(i)
            accumulated_tokens += msg_tokens
            if _has_text_block(msg):
                text_count += 1

            # 同时满足两个下限 → 立即停（即使没到上限）
            if (
                accumulated_tokens >= self._min_tokens
                and text_count >= self._min_text_messages
            ):
                break

        # 转回正序
        kept_indices_set = set(kept_indices_reversed)
        kept_pre = [messages[i] for i in range(len(messages)) if i in kept_indices_set]

        # 工具对完整性兜底（API 硬约束）——可能从 kept_pre 中再丢一些违反配对的消息
        kept = ensure_tool_pair_integrity(kept_pre, full=messages)

        # dropped = 原列表中不在最终 kept 里的全部消息（保持时序，含 ensure 兜底丢的）
        kept_id_counts: dict[tuple, int] = {}
        for m in kept:
            key = _msg_identity(m)
            kept_id_counts[key] = kept_id_counts.get(key, 0) + 1

        dropped: list[MessageDict] = []
        for m in messages:
            key = _msg_identity(m)
            if kept_id_counts.get(key, 0) > 0:
                kept_id_counts[key] -= 1
            else:
                dropped.append(m)

        logger.info(
            "WindowedKeepPolicy.split: %d → kept=%d dropped=%d, ~%d tokens "
            "(min=%d, min_text=%d, hard_cap=%d, got_text=%d)",
            len(messages),
            len(kept),
            len(dropped),
            self._token_estimator.estimate(kept),
            self._min_tokens,
            self._min_text_messages,
            hard_cap,
            text_count,
        )
        return CompactionSplit(kept=kept, dropped=dropped)
