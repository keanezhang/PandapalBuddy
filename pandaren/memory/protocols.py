"""pandaren/memory/protocols.py — Memory 层协议定义

所有 Memory 层使用的 Protocol 类型集中在此文件。

协议遵循原则：
  B3  — SDK 内置实现不调用 LLM
  HC1 — 配置字段初始化后只读
  E4  — 后端失败降级而非崩溃
  O3  — 不静默吞没异常

每个 Protocol 在 docstring 里**明确**标注是否允许实现调 LLM：
  - 不允许：CompactionPolicy（同步、纯函数）、RawLogBackend、FlushPolicy、
            TokenEstimator、WorkingMemoryAccessor、PostCompactSource
  - 允许：  DropSummarizer（应用层注入，异步、可调 LLM）

注：CompactionPolicy 协议本身要求**同步、确定性**——切分是 token 算术，不依赖 LLM。
SDK 内置的 WindowedKeepPolicy 不调 LLM；应用层若需要"对被丢弃的消息做 LLM 摘要"，
请实现 DropSummarizer Protocol，与切分策略正交。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .models import (
    MessageDict,
    CompactBoundaryDict,
    CompactionSplit,
    PostCompactContext,
    ReinjectionAttachment,
)
from ..constants import CHARS_PER_TOKEN


# ─────────────────────────────────────────────
# TokenEstimator（token 估算策略）
# ─────────────────────────────────────────────

@runtime_checkable
class TokenEstimator(Protocol):
    """Token 估算策略协议（不调 LLM）。

    ShortTermMemory、CompactionPolicy、RawLogBackend 均通过此接口
    估算消息的 token 数量，保证三者使用同一把「尺子」。

    应用层可注入真实 tiktoken / 模型计费计算器替换默认实现。
    """

    def estimate(self, messages: list[MessageDict]) -> int:
        """估算消息列表的 token 总数。"""
        ...


class CharBasedTokenEstimator:
    """基于字符数的粗略 token 估算（默认实现，零依赖）。

    估算公式：token ≈ total_chars / CHARS_PER_TOKEN。
    tool_calls 字段的 JSON 序列化字符串也计入估算。
    """

    def estimate(self, messages: list[MessageDict]) -> int:
        """估算消息列表的 token 总数（1 token ≈ CHARS_PER_TOKEN 字符）。"""
        total_chars = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        total_chars += len(str(part.get("text", "")))
                    else:
                        total_chars += len(str(part))
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                total_chars += len(str(tool_calls))
        return max(1, int(total_chars / CHARS_PER_TOKEN))


# ─────────────────────────────────────────────
# WorkingMemoryAccessor（供 tool/models.py 使用）
# ─────────────────────────────────────────────

@runtime_checkable
class WorkingMemoryAccessor(Protocol):
    """工具执行时对工作记忆的只读/写访问接口（不调 LLM）。

    设计约束：工具可以 get/set key，但不能 clear()（clear 是 Loop 的职责）。
    """

    def get(self, key: str) -> Any | None:
        """读取工作记忆中的值，key 不存在时返回 None。"""
        ...

    def set(self, key: str, value: Any) -> None:
        """写入工作记忆中的值。超出容量时抛出 MemoryLimitError。"""
        ...


# ─────────────────────────────────────────────
# WorkingMemoryBackend（工作记忆持久化）
# ─────────────────────────────────────────────

@runtime_checkable
class WorkingMemoryBackend(Protocol):
    """工作记忆持久化后端协议（不调 LLM）。

    将 WorkingMemory 的 KV 条目持久化到外部存储（如 Markdown 文件），
    以便跨 run 恢复或人工查阅。

    与 RawLogBackend 的区别：
      - RawLog：追加写对话消息（不可变日志）
      - WorkingMemoryBackend：KV 增量更新（set/delete），支持按 session 整体加载
    """

    def save(self, key: str, value: Any, session_id: str) -> None:
        """持久化单个 KV 条目（增量更新）。"""
        ...

    def load(self, session_id: str) -> dict[str, Any]:
        """加载指定 session 的所有 KV 条目，返回 {key: value} 字典。"""
        ...

    def delete_key(self, key: str, session_id: str) -> None:
        """删除单个 KV 条目。"""
        ...

    def delete_session(self, session_id: str) -> None:
        """删除指定 session 的所有条目。"""
        ...

    def save_all(self, data: dict[str, Any], session_id: str) -> None:
        """一次性保存整个 WorkingMemory 快照（覆盖写入）。"""
        ...


# ─────────────────────────────────────────────
# RawLogBackend（追加写原始对话日志）
# ─────────────────────────────────────────────

@runtime_checkable
class RawLogBackend(Protocol):
    """原始对话日志后端协议（不调 LLM）。

    职责定位：raw_log 是**离线分析的唯一数据源**——应用层定时任务通过 load_all()
    读取，提炼 User Model / Episodic Archive。

    关键设计约束：
      - 构造参数中**不接受 session_id**。一个 backend 实例为所有 session 服务，
        session_id 在每次运行时方法调用时传入，由实现内部决定如何分桶
        （SQL 表的列、KV 的 key 拼接、文件名的一部分等）。
      - 写入操作的方法是同步的，异步批量写由 FlushPolicy 封装。
    """

    # ── 写入（运行时）──

    def append_raw_message(
        self,
        message: MessageDict,
        session_id: str,
        run_id: str = "",
        step: int | None = None,
    ) -> None:
        """追加一条消息到原始日志。

        run_id / step：该消息所属的 run 与 step，用于把「对话原文的某一轮」与
        traces 的某次 llm_call 做 **key join**（而非顺序/时间戳对齐），从而在
        多 run / 多会话下也不错位。message 本身保持纯净（仅 LLM 消息字段），
        run_id/step 作为独立元数据持久化，不写进 message 以免污染 LLM 上下文。
        缺省值兼容不提供该上下文的调用方（如纯 SDK 单测）。
        """
        ...

    def append_compact_boundary(
        self,
        boundary: CompactBoundaryDict,
        session_id: str,
    ) -> None:
        """追加一条压缩边界标记。"""
        ...

    # ── 运行时恢复读 ──

    def load_within_budget(
        self,
        session_id: str,
        token_budget: int,
    ) -> list[MessageDict]:
        """从最新 compact_boundary 向前读取，直到 token_budget 用尽。

        返回的消息列表按时间从旧到新排列。
        """
        ...

    # ── 离线分析读（应用层用）──

    def load_all(self, session_id: str) -> list[MessageDict]:
        """加载指定 session 的全部历史消息，按时间从旧到新排列。

        供应用层离线任务（User Model / Episodic Archive 提炼）使用。
        """
        ...

    def list_sessions(self) -> list[str]:
        """枚举所有已存在的 session_id。

        user 维度的过滤是应用层在 session_id 命名规则上的关注点
        （如 ``user-123:session-001``），SDK 不感知 user 概念。
        """
        ...


# ─────────────────────────────────────────────
# CompactionPolicy（上下文切分策略）
# ─────────────────────────────────────────────

@runtime_checkable
class CompactionPolicy(Protocol):
    """对话上下文切分策略协议。

    职责：决定"保留 vs 丢弃"的切分线。

    实现约束：
      - **必须是同步、确定性的纯函数**——不允许调 LLM（B3）。
        切分是 token 算术，不需要 LLM 介入。
      - 必须保证不拆分 tool_calls/tool_results 配对（完整性原则）。
        即使实现遗漏，Memory.compact_if_needed() 也会再叠加一次
        ensure_tool_pair_integrity 兜底。
      - 不再负责"摘要生成"。如需对被丢弃的消息生成 LLM 脉络摘要，
        请通过独立的 DropSummarizer 扩展点（应用层注入），与切分正交。
    """

    def split(
        self,
        messages: list[MessageDict],
        max_tokens: int,
    ) -> CompactionSplit:
        """将 messages 按 max_tokens 预算切分为 (kept, dropped)。

        传入的 messages 只含 [user, assistant, tool] 消息，不含 system。
        system 消息由 Memory Facade 管理，实现类无需处理。

        Returns:
            CompactionSplit(kept, dropped)
            必须满足 kept + dropped 是原 messages 的一个划分（不重不漏，时序保持）。
        """
        ...


# ─────────────────────────────────────────────
# DropSummarizer（被丢弃消息的脉络摘要，应用层扩展点）
# ─────────────────────────────────────────────

@runtime_checkable
class DropSummarizer(Protocol):
    """把被切分丢弃的消息总结成一条 system 摘要消息（**应用层可调 LLM**）。

    None    → 不摘要，dropped 直接抛弃（SDK 默认；符合 B3：SDK 不调 LLM）
    实例    → 应用层注入（典型实现：调 LLM 生成 ≤N 字脉络摘要）

    实现要求：
      - 必须 async，允许内部调 LLM
      - 失败时返回 None（视为"摘要不可用"，dropped 静默丢弃）
      - 返回的 MessageDict 必须是 ``role="system"`` 的可序列化消息，
        将被 Memory Facade 插入到保留消息（kept）之前
    """

    async def summarize(
        self,
        dropped: list[MessageDict],
    ) -> MessageDict | None:
        """对被丢弃的消息列表生成脉络摘要。

        Returns:
            一条 role="system" 的 MessageDict —— 将拼到 kept 之前；
            或 None —— 表示本次摘要不可用，dropped 静默丢弃。
        """
        ...


# ─────────────────────────────────────────────
# FlushPolicy（批量写入策略）
# ─────────────────────────────────────────────

@runtime_checkable
class FlushPolicy(Protocol):
    """批量写入策略协议（不调 LLM；纯 IO 编排）。

    调用方通过 enqueue() 将消息放入缓冲区，
    实现负责在合适时机（coalesce 超时或 buffer 溢出）批量写入后端。
    """

    async def enqueue(
        self,
        message: MessageDict,
        session_id: str,
        backend: RawLogBackend,
        run_id: str = "",
        step: int | None = None,
    ) -> None:
        """将消息加入写入队列。可能触发批量写。run_id/step 随消息缓冲，批量落盘时透传给 backend。"""
        ...

    async def flush(
        self,
        session_id: str,
        backend: RawLogBackend,
        *,
        flush_all: bool = False,
    ) -> None:
        """强制将缓冲区所有消息写入后端。run 结束时调用。"""
        ...


# ─────────────────────────────────────────────
# PostCompactSource（压缩后回注源）
# ─────────────────────────────────────────────

@runtime_checkable
class PostCompactSource(Protocol):
    """压缩后主动收集"必须回注"的关键状态片段（不调 LLM）。

    与 DropSummarizer 的本质区别：
      - DropSummarizer：对**被丢弃的消息**做 LLM 脉络摘要（业务可选）
      - PostCompactSource：当前 session 内**可枚举**的关键状态
        （"刚才读过的文件"/"当前激活的技能"/"当前 plan 状态"）

    后者不依赖语义匹配，所以在用户说"继续"这种短指令时仍然能
    精确补回关键上下文，是 PostCompact 不丢上下文的核心机制。

    实现约束：
      - 不允许调 LLM（B3）；只读 WorkingMemory / SkillRegistry / session_meta
        等可枚举状态。
      - 实现失败应返回空列表，不抛异常（E4）。
    """

    def collect(
        self,
        ctx: PostCompactContext,
    ) -> list[ReinjectionAttachment]:
        """收集本次压缩后需要回注的 attachments。

        返回空列表 = 该 source 本次没贡献。

        实现应自己控制每条 attachment 的 token 总量（参考各内置 source 的
        max_tokens_* 参数），最终的总预算控制由 PostCompactReinjector 做。
        """
        ...
