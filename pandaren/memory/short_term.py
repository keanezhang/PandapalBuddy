"""pandaren/memory/short_term.py — 短期记忆（对话历史管理）

ShortTermMemory：管理 in-memory 对话历史列表。

**职责瘦身**（重构后）：
  - 仅负责对话消息的追加、切分接口、快照/恢复
  - 不再有独立的 compact_if_needed() 方法
    （所有压缩入口由 Memory Facade 统一调度，跑三层管线）
  - 不感知 system 消息（由 Facade 管理）

设计原则：
  HC1 — 配置初始化后只读
  HC2 — get_messages() / snapshot() 返回深拷贝
  HC8 — 使用 MessageDict TypedDict
"""

from __future__ import annotations

import copy
import logging

from .models import MessageDict, CompactionSplit
from .protocols import CompactionPolicy, TokenEstimator, CharBasedTokenEstimator

logger = logging.getLogger("pandaren.memory.short_term")


class ShortTermMemory:
    """短期记忆，管理 in-memory 对话历史（user / assistant / tool 消息）。

    Args:
        compaction_policy:   切分策略（CompactionPolicy）
        token_estimator:     token 估算器
        max_entries:         最大消息条数（仅用于 log 警告，不强制截断）
    """

    def __init__(
        self,
        compaction_policy: CompactionPolicy,
        token_estimator: TokenEstimator | None = None,
        max_entries: int = 10_000,
    ) -> None:
        self._compaction_policy = compaction_policy
        self._token_estimator: TokenEstimator = (
            token_estimator or CharBasedTokenEstimator()
        )
        self._max_entries: int = max_entries  # HC1
        self._messages: list[MessageDict] = []

    # ── 消息追加 ──

    def append_user_message(self, task: str) -> None:
        """追加用户消息，这种情况一般出现在ask_user等情况下，用户输入了选项，但是没有对话，所以这里占位一下"""
        if not task:
            task = "[user message]"
        msg: MessageDict = {"role": "user", "content": task}
        self._messages.append(msg)

    def add_assistant_message(
        self,
        content: str | list | None,
        tool_calls: list | None = None,
        reasoning_content: str | None = None,
    ) -> None:
        """追加 assistant 消息。

        对于 DeepSeek 等需要 reasoning_content 回传的思考模型，
        上一轮 assistant 消息中的 reasoning_content 必须在后续请求中保留，
        否则 API 会返回 400。
        """
        # 大模型调用工具时很多时候返回空，content为空，但是因为不同模型严格程度不同，所以这里特意赋值为空格：" "
        # 注意必须兜住空字符串 "": HITL resume 路径会把 paused_assistant_content="" 传进来
        # （run_core.py 暂停时 parsed.content or ""），Kimi K3 等模型拒绝 content 为空的 assistant 消息。
        msg: MessageDict = {
            "role": "assistant",
            "content": content if content else " "
        }
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if reasoning_content:
            msg["reasoning_content"] = reasoning_content
        self._messages.append(msg)

    def add_tool_result(
        self,
        tool_call_id: str,
        tool_name: str,
        content: str | list,
    ) -> None:
        """追加工具调用结果消息。"""
        # 防御：个别工具结果可能产出空字符串，Kimi K3 等模型拒绝
        # tool role 消息的 content 为空。
        if isinstance(content, str) and content == "":
            content = "[OK]"
        msg: MessageDict = {
            "role": "tool",
            "content": content,
            "tool_call_id": tool_call_id,
        }
        self._messages.append(msg)

    # ── 估算 & 切分 ──

    def estimate_tokens(self) -> int:
        """估算当前消息列表的 token 数（不含 system）。"""
        return self._token_estimator.estimate(self._messages)

    def replace_messages(self, messages: list[MessageDict]) -> None:
        """完整替换 STM 内容（由 Facade 在压缩管线中使用）。

        会接受深拷贝输入，避免外部事后修改。
        """
        self._messages = [copy.deepcopy(m) for m in messages]

    def split_with(
        self,
        max_tokens: int,
    ) -> tuple[list[MessageDict], CompactionSplit]:
        """调用 compaction_policy 切分，返回 (原始, CompactionSplit)。

        **不修改 STM 自身状态**——切分后的应用由 Memory Facade 决定
        （可能还要叠加 DropSummarizer 摘要、PostCompact 回注等），
        最终通过 ``replace_messages`` 写回。

        Returns:
            (original_messages_deepcopy, CompactionSplit(kept, dropped))
        """
        original = [copy.deepcopy(m) for m in self._messages]
        split_result = self._compaction_policy.split(
            messages=copy.deepcopy(self._messages),
            max_tokens=max_tokens,
        )
        return original, split_result

    # ── 加载（session restore）──

    def load_messages(self, messages: list[MessageDict]) -> None:
        """从外部加载消息列表（session restore 时使用）。

        仅保留 [user, assistant, tool] 消息，system 消息由 Facade 管理。
        """
        if not messages:
            return
        non_system = [m for m in messages if m.get("role") != "system"]
        self._messages = [copy.deepcopy(m) for m in non_system]

    # ── 外部读取 ──

    def get_messages(self) -> list[MessageDict]:
        """返回当前对话消息列表的深拷贝（HC2）。不含 system 消息。"""
        return [copy.deepcopy(m) for m in self._messages]

    def snapshot(self) -> tuple[MessageDict, ...]:
        """返回当前对话消息的不可变快照（深拷贝，HC1/HC2）。"""
        return tuple(copy.deepcopy(m) for m in self._messages)

    def resume_from_snapshot(self, messages: tuple[MessageDict, ...]) -> None:
        """从消息快照恢复状态（HITL resume）。"""
        self._messages = [copy.deepcopy(m) for m in messages]

    def reset(self) -> None:
        """重置为初始状态（新 session 时由 Memory.init_context 调用）。"""
        self._messages = []

    @property
    def is_empty(self) -> bool:
        """是否没有任何对话消息（还没有用户消息）。"""
        return len(self._messages) == 0

    @property
    def message_count(self) -> int:
        """当前消息条数（含工具消息）。"""
        return len(self._messages)
