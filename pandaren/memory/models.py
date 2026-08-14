"""pandaren/memory/models.py — Memory 层核心数据模型

TypedDicts + frozen dataclasses。

所有供外部消费的 TypedDict 都基于 HC8 原则（禁止裸 dict）。
frozen=True dataclass 保证跨层传递的不可变性（HC1）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired, TypedDict, TYPE_CHECKING

if TYPE_CHECKING:
    from .protocols import WorkingMemoryAccessor


# ─────────────────────────────────────────────
# 消息相关 TypedDicts
# ─────────────────────────────────────────────

class MessageDict(TypedDict):
    """对话历史中的单条消息，对应 OpenAI chat message 格式。"""
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list
    tool_call_id: NotRequired[str]
    tool_calls: NotRequired[list]
    reasoning_content: NotRequired[str]
    timestamp: NotRequired[str]


class CompactBoundaryDict(TypedDict):
    """写入 raw_log 的压缩边界标记（非 MessageDict，type 字段区分）。"""
    type: Literal["compact_boundary"]
    timestamp: str
    tokens_before: int
    tokens_after: int
    kept_message_count: int
    summary: NotRequired[str | None]


# ─────────────────────────────────────────────
# CompactionSplit（CompactionPolicy.split() 的返回类型）
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class CompactionSplit:
    """切分策略对消息列表的切分结果（一等公民数据类，HC1 不可变）。

    kept    保留进上下文的消息（按时间序）
    dropped 被丢弃的消息（按时间序，可能为空）

    切分必然满足：kept + dropped 是原 messages 的一个划分（不重不漏，保持时序）。
    若实现违反该不变量，Memory Facade 不会显式校验，但下游 ensure_tool_pair_integrity
    兜底与 token 估算会出现可观察的异常。
    """
    kept: list[MessageDict]
    dropped: list[MessageDict] = field(default_factory=list)


# ─────────────────────────────────────────────
# Memory Snapshot（HITL pause/resume）
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class MemorySnapshot:
    """Memory 在 HITL 暂停时的快照，frozen=True 保证不可变（HC1 原则）。

    messages:                暂停时 short-term 的消息列表（纯对话消息，不含 system，tuple 保证不可变）
    post_compact_attachments: 压缩后待回注的 attachments（HITL 暂停时也要保留）
    """
    messages: tuple[MessageDict, ...]
    post_compact_attachments: tuple["ReinjectionAttachment", ...] = ()


# ─────────────────────────────────────────────
# PostCompact 回注相关
# ─────────────────────────────────────────────

class ReinjectionAttachment(TypedDict):
    """单条压缩后回注消息。

    最终由 Memory.get_messages() 拼装成一条 role=user 消息插入到
    [system 之后, 对话历史之前] 的位置。

    source_name:      产生此 attachment 的 source 标识（"recent_files" / "active_skills" / ...）
    title:            一行标题，用于在拼接消息中标识此片段
    content:          实际正文（已经过截断，不再做二次处理）
    estimated_tokens: 估算的 token 数（供 PostCompactReinjector 做总预算控制）
    """
    source_name: str
    title: str
    content: str
    estimated_tokens: int


@dataclass(frozen=True)
class PostCompactContext:
    """传给 PostCompactSource.collect 的运行时上下文。

    SDK 内置 source 与应用层自定义 source 都通过此对象访问运行时状态。
    frozen=True 保证 source 不能反向修改 ctx 内容。

    session_id / run_id:  当前 run 的标识
    working_memory:       run 级 KV 存储读取接口（最近文件 read 记录约定写在此处）
    skill_registry:       Skill 注册表（None = Agent 没启用 skill 层）
    session_meta:         跨 run 同 session 的状态（plan_wip_path 等），传深拷贝
    """
    session_id: str
    run_id: str
    working_memory: "WorkingMemoryAccessor"
    skill_registry: Any | None  # 类型用 Any 避免在 memory 层引入对 skill 层的强依赖
    session_meta: dict[str, Any]
