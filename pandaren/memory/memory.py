"""pandaren/memory/memory.py — Memory Facade（对外唯一接口）

Memory 是 AgentLoop 与记忆层交互的唯一入口，封装：

  - ShortTermMemory      —— 对话历史（不含 system 消息）
  - LongTermMemory       —— RawLog 路由（去 summary 化后唯一职责）
  - WorkingMemory        —— session 级 KV 存储
  - FlushPolicy          —— 异步批量写入
  - MicroCompactor       —— SDK 内置 MicroCompact（清旧工具结果，与切分正交）
  - PostCompactReinjector—— SDK 内置压缩后回注（最近文件 / 激活技能 / plan）

system 消息全部由 Facade 管理：
  - 静态前缀（system_prompt + agent_config_text）由 ``_build_system_msg()`` 构建
  - 压缩后回注的 attachments 通过 ``get_messages()`` 在拼装时插入
    [system 之后, 对话历史之前] 的位置。

──────────────────────────────────────
压缩管线（compact_if_needed）四层结构
──────────────────────────────────────

  estimate(STM + system) ≤ 阈值？
     → return None
  否则（超阈值）：
     [Layer 1] MicroCompact 预清理：清掉早期、白名单内的工具结果正文
        → 重新估算；若已 < 阈值则 return None（省一次切分）
     [Layer 2] CompactionPolicy.split()：切窗口（默认 WindowedKeepPolicy）
        → 返回 CompactionSplit(kept, dropped)
        → 强制 ensure_tool_pair_integrity 兜底（API 硬约束，已在策略内执行）
        → 反扩保护（kept 比 original 还大就丢弃压缩）
     [Layer 3] DropSummarizer.summarize(dropped) [可选]
        → 应用层异步 LLM 摘要，失败/None 则直接抛弃 dropped
     [Layer 4] 写 CompactBoundary 标记 + 通知 LLMClient 冷启动
              + PostCompactReinjector 收集 attachments 暂存
              （由 get_messages() 拼回；下一轮压缩前清空）

──────────────────────────────────────
DropSummarizer vs PostCompact（语义边界）
──────────────────────────────────────

  DropSummarizer  — 对**被丢弃的消息**做 LLM 脉络摘要
                    （应用层注入；默认 None 则不摘要）
                    产物为 role=system 的一条 MessageDict，插入到 kept 之前
  PostCompact     — session 内可枚举的当前状态
                    （"刚读过的文件"/"激活技能"/"plan 进度"）
                    由 PostCompactReinjector 负责，不调 LLM

设计原则：
  B3  — Memory 不调用 LLM；DropSummarizer 是应用层注入的扩展点
  HC1 — 配置字段初始化后只读
  HC2 — 外部返回深拷贝
  HC8 — 使用 TypedDict
  E4  — 后端失败 log warning 不崩溃
  O3  — 错误不静默吞没
"""

from __future__ import annotations

import copy
import logging
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Callable, ClassVar

from .models import (
    MessageDict,
    CompactBoundaryDict,
    MemorySnapshot,
    PostCompactContext,
    ReinjectionAttachment,
)
from .protocols import (
    RawLogBackend,
    CompactionPolicy,
    DropSummarizer,
    FlushPolicy,
    PostCompactSource,
    WorkingMemoryAccessor,
    WorkingMemoryBackend,
    TokenEstimator,
    CharBasedTokenEstimator,
)
from .constants import (
    DEFAULT_COMPACT_THRESHOLD,
    DEFAULT_WORKING_MEMORY_MAX_ENTRIES,
    DEFAULT_RESTORE_TOKEN_BUDGET,
    DEFAULT_POST_COMPACT_TOKEN_BUDGET,
    COMPACT_TARGET_RATIO,
)
from .compaction.windowed import WindowedKeepPolicy
from .compaction.micro_compact import MicroCompactor
from .compaction.tool_pair_integrity import ensure_tool_pair_integrity
from .reinject.coordinator import PostCompactReinjector
from .short_term import ShortTermMemory
from .long_term import LongTermMemory
from .working_memory import WorkingMemory, MemoryLimitError as _WorkingMemoryLimitError
from .flush_policy import AsyncBatchFlushPolicy

logger = logging.getLogger("pandaren.memory")


# ContextVar：Loop 在调用 init_from_restore 前设置 run_id
# 为什么用 ContextVar？→ 协程/异步场景下，run_id 需要在同一个调用链中自动传递，
# 而不用显式参数传递。类似线程局部存储，但是协程安全的。
_run_id_var: ContextVar[str] = ContextVar("pandaren_run_id", default="")

# ── 系统消息分区标签 ──
# 这两个标签用于在 system message 内容中标记 agent_config 区域的起止，
# 方便外部解析/替换
_AGENT_CONFIG_START = "<!-- agent-config-start -->"
_AGENT_CONFIG_END = "<!-- agent-config-end -->"

# ── PostCompact attachment 拼接外壳 ──
_ATTACHMENT_HEADER = "<post-compact-context source=\"{src}\" title=\"{title}\">"
_ATTACHMENT_FOOTER = "</post-compact-context>"


# ─────────────────────────────────────────────
# 异常类
# ─────────────────────────────────────────────

# Re-export from working_memory（避免应用层多导入路径，但不二次定义）
MemoryLimitError = _WorkingMemoryLimitError


class MemoryStateError(Exception):
    """Memory 状态非法时抛出。例如在未初始化 session 时就追加消息。"""


# ─────────────────────────────────────────────
# Memory Facade
# ─────────────────────────────────────────────

class Memory:
    """Memory Facade —— Loop 的唯一记忆接口。

    【整体架构】

    Memory 是一个"门面模式"（Facade），对外暴露简单接口，对内协调多个子系统：

    ┌──────────────────────────────────────────────────────┐
    │                    Memory Facade                      │
    │                                                      │
    │  ┌─────────────┐  ┌─────────────┐  ┌──────────────┐ │
    │  │ShortTermMem │  │LongTermMem  │  │WorkingMemory │ │
    │  │ (对话历史)  │  │ (RawLog 路由)│  │  (KV 存储)   │ │
    │  └─────────────┘  └─────────────┘  └──────────────┘ │
    │                                                      │
    │  ┌─────────────┐  ┌─────────────┐  ┌──────────────┐ │
    │  │MicroCompact │  │FlushPolicy  │  │Reinjector    │ │
    │  │(清工具结果) │  │(异步批量写) │  │(压缩后回注)  │ │
    │  └─────────────┘  └─────────────┘  └──────────────┘ │
    └──────────────────────────────────────────────────────┘

    消息流生命周期（5 个 Phase，去 summary 化后）：
      Phase 1: init_from_restore   → 初始化 session，恢复历史
      Phase 2: append_*            → 追加消息（user/assistant/tool）
      Phase 3: compact_if_needed   → 四层压缩管线
      Phase 4: flush_raw_messages  → 强制刷盘
      Phase 5: end_session         → 会话结束（仅 flush + reset；不再生成摘要）

    Args:
        # ── 必传参数 ──
        system_prompt:              系统提示词

        # ── 持久化（应用层注入）──
        raw_log_backend:            原始日志后端（None = 不持久化原始日志）

        # ── 压缩切分策略 ──
        compaction_policy:          自定义 CompactionPolicy（None = 默认 WindowedKeepPolicy）
        compact_threshold:          token 压缩阈值（None = DEFAULT_COMPACT_THRESHOLD；
                                    builder 若配置了 ContextWindowBudget 则传入对应值）

        # ── 摘要扩展点（应用层注入，可调 LLM）──
        drop_summarizer:            被丢弃消息的脉络摘要策略（None = 不摘要，默认）

        # ── MicroCompact（SDK 算法 + 应用白名单）──
        microcompact_tools:         工具白名单（None / set() = 不启用清理）
        microcompact_keep_recent:   compact_if_needed 入口预清理时保留最近 N 条
        microcompact_single_result_max_tokens: 单条工具结果上限（add_tool_result 入口截断）

        # ── PostCompact 回注 ──
        post_compact_sources:       PostCompactSource 列表（默认空 = 不启用）
        post_compact_token_budget:  回注 attachment 总 token 预算

        # ── 其他 ──
        session_mode:               "multi_turn"（默认）| "single_turn"
        working_memory_backend:     工作记忆持久化后端（None = 纯内存，不持久化）
        flush_policy:               异步批量写策略（默认 AsyncBatchFlushPolicy）
        token_estimator:            Token 估算器（默认 CharBasedTokenEstimator）
        agent_config_text:          Agent Config 字符串（拼到 system message 末尾）
        skill_registry:             SkillRegistry 引用（PostCompact ActiveSkillsSource 用；
                                    无 skill 层时传 None）

    运行时隔离：session_id 每次 init_from_restore 时传入。
    """

    def __init__(
        self,
        system_prompt: str = "You are a helpful assistant.",
        # ── 持久化 ──
        raw_log_backend: RawLogBackend | None = None,
        # ── 压缩切分 ──
        compaction_policy: CompactionPolicy | None = None,
        compact_threshold: int = DEFAULT_COMPACT_THRESHOLD,
        # ── 摘要扩展点 ──
        drop_summarizer: DropSummarizer | None = None,
        # ── MicroCompact ──
        microcompact_tools: frozenset[str] | set[str] | None = None,
        microcompact_keep_recent: int | None = None,
        microcompact_single_result_max_tokens: int | None = None,
        # ── PostCompact ──
        post_compact_sources: list[PostCompactSource] | None = None,
        post_compact_token_budget: int = DEFAULT_POST_COMPACT_TOKEN_BUDGET,
        # ── 其他 ──
        session_mode: str = "multi_turn",
        working_memory_backend: WorkingMemoryBackend | None = None,
        flush_policy: FlushPolicy | None = None,
        token_estimator: TokenEstimator | None = None,
        agent_config_text: str | None = None,
        skill_registry: Any | None = None,
    ) -> None:
        # HC1: 配置字段初始化后只读（_FROZEN_ATTRS 守护）
        self._system_prompt: str = system_prompt
        self._compact_threshold: int = compact_threshold
        self._session_mode: str = session_mode
        self._agent_config_text: str | None = agent_config_text
        self._skill_registry: Any | None = skill_registry

        # 共享同一个 token estimator（所有组件共用一把尺子，保证估算一致性）
        _token_estimator: TokenEstimator = token_estimator or CharBasedTokenEstimator()
        self._token_estimator: TokenEstimator = _token_estimator

        # 切分策略（默认 WindowedKeepPolicy：保留最近窗口内的消息）
        _compaction_policy: CompactionPolicy = (
            compaction_policy
            or WindowedKeepPolicy(token_estimator=_token_estimator)
        )

        # ShortTermMemory（不含 system 消息，只存对话轮次）
        self._short_term = ShortTermMemory(
            compaction_policy=_compaction_policy,
            token_estimator=_token_estimator,
        )

        # LongTermMemory（瘦身：只剩 RawLog 路由）
        self._long_term = LongTermMemory(
            raw_log_backend=raw_log_backend,
        )

        # 应用层注入的"被丢弃消息脉络摘要"扩展点（异步、可调 LLM）
        self._drop_summarizer: DropSummarizer | None = drop_summarizer

        # WorkingMemory（KV 存储，用于保存运行时状态，如最近读过的文件列表）
        self._working = WorkingMemory(
            max_entries=DEFAULT_WORKING_MEMORY_MAX_ENTRIES,
            backend=working_memory_backend,
        )
        self._working_memory_backend: WorkingMemoryBackend | None = working_memory_backend

        # FlushPolicy（异步批量写入策略，避免每条消息都触发 IO）
        self._flush_policy: FlushPolicy = flush_policy or AsyncBatchFlushPolicy()

        # MicroCompactor（轻量级清理器：清掉早期工具结果）
        # 应用层无白名单时算法仍存在，但 clear_old_tool_results 是 no-op
        from .constants import (
            DEFAULT_MICROCOMPACT_KEEP_RECENT,
            DEFAULT_MICROCOMPACT_SINGLE_RESULT_MAX_TOKENS,
        )
        self._micro_compactor = MicroCompactor(
            compactable_tools=microcompact_tools,
            keep_recent=(
                microcompact_keep_recent
                if microcompact_keep_recent is not None
                else DEFAULT_MICROCOMPACT_KEEP_RECENT
            ),
            single_result_max_tokens=(
                microcompact_single_result_max_tokens
                if microcompact_single_result_max_tokens is not None
                else DEFAULT_MICROCOMPACT_SINGLE_RESULT_MAX_TOKENS
            ),
            token_estimator=_token_estimator,
        )

        # PostCompactReinjector（压缩后回注编排器）
        self._reinjector = PostCompactReinjector(
            sources=post_compact_sources,
            token_budget=post_compact_token_budget,
        )

        # ── Run / Session 级状态 ──

        # 压缩后回注 attachments（每次 compact 写入，下一次 compact 前清空）
        self._post_compact_attachments: list[ReinjectionAttachment] = []

        # session 状态
        self._session_id: str | None = None        # 当前 session ID
        self._current_session_id: str = ""          # 用于持久化的 session ID
        self._stm_session_id: str = ""              # STM 当前对应的 session ID

        # run 上下文：由 AgentLoop 每个 run/step 边界通过 set_run_context() 更新，
        # 写 raw_log 时随消息一起落盘（run_id + step），供离线分析按 (run_id, step)
        # 与 traces 的 llm_call 做 key join（多 run/多会话不错位）。
        self._current_run_id: str = ""
        self._current_step: int | None = None

        # 缓存冷启动回调（compact 后通知 LLMClient）
        # 为什么需要？→ 压缩后对话历史发生了"断崖"，LLM 的 cache 失效，
        # 需要通知 LLMClient 重新预热
        self._on_compact_callback: Callable[[], None] | None = None

        # session-persistent meta（跨 run 同 session）
        # 与 WorkingMemory 的区别：WorkingMemory 是 session 级别的（同 session 跨 run 保留），
        # session_meta 也是 session 级别的（典型用途：保存 plan_wip_path）
        self._session_meta: dict[str, Any] = {}
        self._session_meta_id: str = ""  # 用于检测 session 切换

        # HC1：构造完成标记
        self._initialized = True

    # ─────────────────────────────────────────
    # HC1：配置字段冻结保护
    # ─────────────────────────────────────────

    _FROZEN_ATTRS: ClassVar[frozenset[str]] = frozenset({
        "_system_prompt",
        "_compact_threshold",
        "_session_mode",
        "_agent_config_text",
        "_skill_registry",
        "_token_estimator",
        "_short_term",
        "_long_term",
        "_drop_summarizer",
        "_working",
        "_working_memory_backend",
        "_flush_policy",
        "_micro_compactor",
        "_reinjector",
        "_initialized",
    })

    def __setattr__(self, name: str, value: Any) -> None:
        """HC1 保护：构造完成后，_FROZEN_ATTRS 中的字段不允许被修改。

        这防止了运行时意外修改配置字段（如把 compact_threshold 从 100000 改成 10），
        这种 bug 很难排查。如果确实需要修改，应重新构造 Memory 实例。
        """
        if (
            name not in ("_initialized",)
            and getattr(self, "_initialized", False)
            and name in Memory._FROZEN_ATTRS
        ):
            raise AttributeError(
                f"Memory.{name} is frozen after initialization (HC1). "
                f"Cannot modify configuration field after construction."
            )
        object.__setattr__(self, name, value)

    # ─────────────────────────────────────────
    # System 消息构建
    # ─────────────────────────────────────────

    def _build_system_content(self) -> str:
        """构建 system 消息内容（仅静态区：system_prompt + agent_config）。

        输出格式：
            <!-- agent-config-start -->
            {system_prompt}

            {agent_config_text}    （可选）
            <!-- agent-config-end -->

        为什么用 XML 注释标签？→ 方便外部解析器识别和替换特定区域，
        而不影响 system_prompt 的正文内容。
        """
        if self._agent_config_text:
            config_body = f"{self._system_prompt}\n\n{self._agent_config_text}"
        else:
            config_body = self._system_prompt
        return (
            f"{_AGENT_CONFIG_START}\n"
            f"{config_body}\n"
            f"{_AGENT_CONFIG_END}"
        )

    def _build_system_msg(self) -> MessageDict:
        """构建完整的 system 消息 dict。"""
        return {"role": "system", "content": self._build_system_content()}

    def _build_attachment_messages(self) -> list[MessageDict]:
        """把 _post_compact_attachments 拼成 role=user 消息列表。

        每条 attachment 单独成一条消息（便于阅读和调试），
        外壳用 ``<post-compact-context>`` XML 标记。

        为什么用 role=user 而不是 role=system？
        → system 消息只有一条（在对话开头），而 attachments 可能有多条。
          用 user 消息可以灵活地插入多条，且 LLM 对 user 消息的注意力更高。
        """
        result: list[MessageDict] = []
        for att in self._post_compact_attachments:
            header = _ATTACHMENT_HEADER.format(
                src=att.get("source_name", ""),
                title=att.get("title", ""),
            )
            content = (
                f"{header}\n"
                f"{att.get('content', '')}\n"
                f"{_ATTACHMENT_FOOTER}"
            )
            result.append({"role": "user", "content": content})
        return result

    # ─────────────────────────────────────────
    # Phase 1: Session 初始化
    # ─────────────────────────────────────────

    def init_from_restore(self, task: str, session_id: str) -> list[dict]:
        """初始化 session，智能选择历史来源，再追加新 user 消息。

        这是每次用户发起新请求时，AgentLoop 调用的第一个方法。
        它负责"恢复上下文"——决定从哪里拿对话历史，然后追加新的用户消息。

        三档恢复优先级：
          1. STM 非空且 session_id 匹配 → 直接追加，零 IO（最快）
          2. raw_log 有历史 → 从持久化存储恢复到 STM，再追加
          3. 全新对话 → 直接追加

        single_turn 模式直接走档位 3。
        """
        if not session_id or not session_id.strip():
            raise ValueError("session_id is required and cannot be empty.")

        # 重置 session 级状态
        self._reset_session_state()
        self._current_session_id = session_id
        self._session_id = session_id

        # 通知 WorkingMemory 当前 session_id（用于持久化 + 恢复）
        self._working.set_session_id(session_id)

        # session meta 切换检测：如果 session_id 变了，清空 meta
        if self._session_meta_id != session_id:
            self._session_meta.clear()
            self._session_meta_id = session_id
            # 跨 run 保留的 PostCompact attachments 也必须按 session 隔离
            self._post_compact_attachments = []

        # single_turn 模式：不恢复历史，直接追加
        if self._session_mode == "single_turn":
            self._stm_session_id = session_id
            self._short_term.reset()
            self._short_term.append_user_message(task)
            self._enqueue_last_message()
            return self.get_messages()

        # 档位 1：STM 非空，检查 session 是否匹配
        if not self._short_term.is_empty:
            if self._stm_session_id != session_id:
                logger.debug(
                    "Memory.init_from_restore: STM session mismatch, resetting STM "
                    "(stm=%s, new=%s)",
                    self._stm_session_id, session_id,
                )
                self._short_term.reset()
            else:
                self._stm_session_id = session_id
                self._short_term.append_user_message(task)
                self._enqueue_last_message()
                return self.get_messages()

        # 档位 2 或 3：STM 为空，尝试从 LongTermMemory 恢复
        self._stm_session_id = session_id

        restored = self._long_term.load_for_restore(
            session_id=session_id,
            token_budget=DEFAULT_RESTORE_TOKEN_BUDGET,
        )

        if restored:
            # 工具对完整性兜底（API 硬约束）——恢复路径无压缩管线的保护，
            # 但 load_within_budget 的 token 截断可能切在 assistant/tool_result 之间，
            # 导致 tool_call 缺少对应 result（见 sess-f537efb5 案例）。
            restored = ensure_tool_pair_integrity(restored)
            self._short_term.load_messages(restored)
            self._short_term.append_user_message(task)
            self._enqueue_last_message()
            logger.debug(
                "Memory.init_from_restore: restored %d messages + new user msg "
                "(session_id=%s)",
                len(restored), session_id,
            )
        else:
            self._short_term.reset()
            self._short_term.append_user_message(task)
            self._enqueue_last_message()
            logger.debug(
                "Memory.init_from_restore: fresh init (session_id=%s)", session_id,
            )

        return self.get_messages()

    # ─────────────────────────────────────────
    # Phase 2: 消息追加
    # ─────────────────────────────────────────

    def append_user_message(self, task: str) -> list[dict]:
        """追加用户消息（同一 session 内的连续对话）。"""
        if self._short_term.is_empty:
            raise MemoryStateError(
                "Memory.append_user_message: session not initialized. "
                "Call init_from_restore() first."
            )
        self._short_term.append_user_message(task)
        self._enqueue_last_message()
        return self.get_messages()

    async def add_assistant_message(
        self,
        content: str | list | None,
        tool_calls: list | None = None,
        reasoning_content: str | None = None,
    ) -> None:
        """追加 assistant 消息，异步写入 RawLogBackend。"""
        self._short_term.add_assistant_message(
            content, tool_calls=tool_calls, reasoning_content=reasoning_content
        )
        await self._enqueue_message_async()

    async def add_tool_result(
        self,
        tool_call_id: str,
        tool_name: str,
        content: str | list,
    ) -> None:
        """追加工具调用结果，异步写入 RawLogBackend。

        **MicroCompact 时机 A**：单条工具结果超过 single_result_max_tokens 立即截断。

        为什么要在"入口处"就截断？
        → 防止巨大的工具结果（如 read_file 返回整个大文件）直接撑爆 STM。
        """
        truncated_content = self._micro_compactor.truncate_single_result_if_needed(content)
        self._short_term.add_tool_result(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            content=truncated_content,
        )
        await self._enqueue_message_async()

    def inject_user_hint(self, content: str) -> None:
        """注入 SDK 内部引导消息（如 Skill hint），走 STM + raw_log 双写。"""
        self._short_term.append_user_message(content)
        self._enqueue_last_message()

    def set_run_context(self, run_id: str, step: int | None) -> None:
        """更新当前 run 上下文。AgentLoop 在每个 run 开始（step=None）与每个
        step 开始（step=step_n）时调用；此后写入 raw_log 的消息都会带上该 run_id/step。
        纯状态更新，不做 I/O；raw_log 关闭时同样安全（值只在写入时被读取）。"""
        self._current_run_id = run_id
        self._current_step = step

    @staticmethod
    def _with_timestamp(msg: "MessageDict") -> "MessageDict":
        """给待持久化消息补 timestamp（raw_log 时间戳，不污染 STM/LLM 请求）。

        get_messages() 返回深拷贝（HC2），此处 {**msg, ...} 生成新 dict，
        STM 内部消息不受影响；MessageBuilder 只消费 STM 的快照，
        因此 timestamp 不会进入发给 LLM 的 payload。
        """
        if "timestamp" not in msg:
            return {**msg, "timestamp": datetime.now(timezone.utc).isoformat()}
        return msg

    async def _enqueue_message_async(self) -> None:
        """STM 最后一条消息异步写入 RawLogBackend（运行时路径）。"""
        if (
            self._session_mode == "single_turn"
            or self._long_term.raw_log_backend is None
        ):
            return
        messages = self._short_term.get_messages()
        if not messages:
            return
        msg = self._with_timestamp(messages[-1])
        try:
            await self._flush_policy.enqueue(
                msg,
                session_id=self._current_session_id,
                backend=self._long_term.raw_log_backend,
                run_id=self._current_run_id,
                step=self._current_step,
            )
        except Exception as exc:
            logger.warning("Memory._enqueue_message_async: enqueue failed: %s", exc)

    def _enqueue_last_message(self) -> None:
        """STM 最后一条消息同步直写（init / append_user 路径专用）。

        为什么 init/append_user 要同步写？
        → 因为这两条路径之后要立即返回 get_messages()，
          如果异步写可能还没落盘，进程崩溃就丢了。
        """
        if (
            self._session_mode == "single_turn"
            or self._long_term.raw_log_backend is None
        ):
            return
        messages = self._short_term.get_messages()
        if not messages:
            return
        msg = self._with_timestamp(messages[-1])
        try:
            self._long_term.raw_log_backend.append_raw_message(
                msg,
                session_id=self._current_session_id,
                run_id=self._current_run_id,
                step=self._current_step,
            )
        except Exception as exc:
            logger.warning("Memory._enqueue_last_message: append failed: %s", exc)

    # ─────────────────────────────────────────
    # Phase 3: 估算 & 四层压缩管线
    # ─────────────────────────────────────────

    def set_on_compact_callback(self, callback: Callable[[], None] | None) -> None:
        """注入 STM→LTM 摘要通知回调（AgentLoop 在绑定 LLMClient 后调用）。

        回调的用途：压缩发生后，LLM 的 KV cache 会失效（因为历史被改了），
        需要通知 LLMClient 重新预热 cache。
        """
        self._on_compact_callback = callback

    def estimate_tokens(self) -> int:
        """估算当前完整消息列表的 token 数（含 system，含 attachments）。"""
        system_tokens = self._token_estimator.estimate([self._build_system_msg()])
        attachment_msgs = self._build_attachment_messages()
        attachment_tokens = (
            self._token_estimator.estimate(attachment_msgs) if attachment_msgs else 0
        )
        return system_tokens + attachment_tokens + self._short_term.estimate_tokens()

    async def compact_if_needed(self) -> int | None:
        """四层压缩管线——核心方法。

        ┌─────────────────────────────────────────────────────┐
        │ Layer 1: MicroCompact 预清理（不调 LLM）              │
        ├─────────────────────────────────────────────────────┤
        │ Layer 2: CompactionPolicy.split() 切窗口             │
        │   → 返回 CompactionSplit(kept, dropped)              │
        │   → 反扩保护                                         │
        ├─────────────────────────────────────────────────────┤
        │ Layer 3: DropSummarizer.summarize(dropped)（可选）    │
        │   → 应用层异步 LLM 摘要；失败/None 则直接抛弃 dropped  │
        │   → 摘要消息插入到 kept 之前                          │
        ├─────────────────────────────────────────────────────┤
        │ Layer 4: 写 boundary + 通知冷启动 + PostCompact 回注  │
        └─────────────────────────────────────────────────────┘

        Returns:
            None — 无需压缩，或压缩成功且 < 阈值
            int  — 压缩后仍超过阈值的 token 数（Context Overflow，调用方应终止）
        """
        # 先估算当前 token 数
        current_tokens = self.estimate_tokens()
        if current_tokens <= self._compact_threshold:
            return None

        # ── Layer 1: MicroCompact 预清理 ──
        if self._micro_compactor.has_whitelist:
            stm_messages = self._short_term.get_messages()
            cleared, saved = self._micro_compactor.clear_old_tool_results(stm_messages)
            if saved > 0:
                self._short_term.replace_messages(cleared)
                after_micro = self.estimate_tokens()
                logger.info(
                    "Memory.compact: MicroCompact saved ~%d tokens, "
                    "now ~%d (threshold %d)",
                    saved, after_micro, self._compact_threshold,
                )
                if after_micro <= self._compact_threshold:
                    # MicroCompact 自己解决了；不写 boundary，不通知 LLMClient
                    return None
                current_tokens = after_micro

        # ── Layer 2: CompactionPolicy.split() 切窗口 ──
        # 计算"对话消息"的目标 token 数
        system_overhead = self._token_estimator.estimate([self._build_system_msg()])
        attachment_overhead = self._token_estimator.estimate(
            self._build_attachment_messages()
        ) if self._post_compact_attachments else 0
        target_tokens = (
            int(self._compact_threshold * COMPACT_TARGET_RATIO)
            - system_overhead
            - attachment_overhead
        )

        # 极端情况：system + attachments 自己就超了，压缩也没用
        if target_tokens <= 0:
            logger.warning(
                "Memory.compact: system+attachments overhead (%d tokens) alone exceeds "
                "compact target, skipping compression.",
                system_overhead + attachment_overhead,
            )
            return current_tokens

        # 切分：返回 (原始, CompactionSplit(kept, dropped))
        original, split_result = self._short_term.split_with(target_tokens)
        kept = split_result.kept
        dropped = split_result.dropped

        # 工具对完整性兜底（API 硬约束）
        # WindowedKeepPolicy.split() 内部已调用 ensure_tool_pair_integrity，
        # 但若用户自定义 CompactionPolicy 实现遗漏，这里再叠一次保护。
        kept = ensure_tool_pair_integrity(kept, full=original)

        # 反扩保护：压缩后比原始还大就丢弃
        original_tokens = self._token_estimator.estimate(original)
        kept_tokens = self._token_estimator.estimate(kept)
        if kept_tokens >= original_tokens:
            logger.warning(
                "Memory.compact: kept (%d tokens) >= original (%d tokens), "
                "discarding compaction.",
                kept_tokens, original_tokens,
            )
            return current_tokens

        # ── Layer 3: DropSummarizer.summarize(dropped)（可选） ──
        summary_msg: MessageDict | None = None
        if self._drop_summarizer is not None and dropped:
            try:
                summary_msg = await self._drop_summarizer.summarize(dropped)
            except Exception as exc:
                logger.warning(
                    "Memory.compact: drop_summarizer.summarize failed: %s", exc,
                )
                summary_msg = None

        # 拼装最终 STM：[summary_msg?] + kept
        final_messages: list[MessageDict] = []
        if summary_msg is not None:
            # 防御：忽略非 system 角色的摘要消息（实现违反契约时降级）
            if summary_msg.get("role") == "system":
                final_messages.append(summary_msg)
            else:
                logger.warning(
                    "Memory.compact: drop_summarizer returned non-system message, ignored."
                )
        final_messages.extend(kept)

        # 写回 STM
        self._short_term.replace_messages(final_messages)

        # ── Layer 4: 写 boundary + 通知冷启动 + PostCompact 回注 ──
        new_total = self.estimate_tokens()
        if self._session_mode != "single_turn":
            boundary: CompactBoundaryDict = {
                "type": "compact_boundary",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tokens_before": current_tokens,
                "tokens_after": new_total,
                "kept_message_count": len(final_messages),
                "summary": (
                    str(summary_msg.get("content", ""))[:1000]
                    if summary_msg is not None else None
                ),
            }
            self._long_term.append_compact_boundary(
                boundary, session_id=self._current_session_id,
            )

        # 通知 LLMClient：压缩发生了，cache 失效，需要冷启动
        if self._on_compact_callback is not None:
            try:
                self._on_compact_callback()
            except Exception as exc:
                logger.warning(
                    "Memory.compact: on_compact_callback failed: %s", exc,
                )

        # PostCompact 回注（清空旧 attachments → 收集新的）
        self._post_compact_attachments = []
        if self._reinjector.has_sources:
            ctx = PostCompactContext(
                session_id=self._current_session_id,
                run_id=_run_id_var.get(""),
                working_memory=self._working.accessor,
                skill_registry=self._skill_registry,
                session_meta=copy.deepcopy(self._session_meta),
            )
            try:
                attachments = self._reinjector.collect_all(ctx)
            except Exception as exc:
                logger.warning("Memory.compact: reinjector.collect_all failed: %s", exc)
                attachments = []
            self._post_compact_attachments = attachments

        # 重新估算（含 attachments）；仍超阈值则返回 overflow
        final_total = self.estimate_tokens()
        logger.info(
            "Memory.compact: %d → %d tokens (threshold %d, kept %d msgs, "
            "dropped %d msgs, summary=%s, %d attachments)",
            current_tokens, final_total, self._compact_threshold,
            len(kept), len(dropped),
            "yes" if summary_msg else "no",
            len(self._post_compact_attachments),
        )
        return final_total if final_total > self._compact_threshold else None

    # ─────────────────────────────────────────
    # Phase 4: Raw Log Flush
    # ─────────────────────────────────────────

    async def flush_raw_messages(self) -> None:
        """强制将缓冲消息写入 RawLogBackend。

        通常在 session 结束前调用，确保所有消息都落盘。
        """
        if self._session_mode == "single_turn":
            return
        if self._long_term.raw_log_backend is None:
            return
        try:
            await self._flush_policy.flush(
                session_id=self._current_session_id,
                backend=self._long_term.raw_log_backend,
                flush_all=True,
            )
        except Exception as exc:
            logger.warning("Memory.flush_raw_messages: flush failed: %s", exc)

    # ─────────────────────────────────────────
    # Phase 5: Session 结束（去 summary 化后简化）
    # ─────────────────────────────────────────

    async def end_session(self) -> None:
        """结束当前 run，强制落盘并重置 session 状态。

        v1.4 重构：去 summary 化后此方法只做 flush + reset，不再调用 LLM。
        会话摘要 / 知识抽取等业务由应用层定时任务消费 raw_log 完成。
        """
        await self.flush_raw_messages()
        self._reset_session_state()

    def _reset_session_state(self) -> None:
        """重置 session 级状态（但不重置配置字段，也不清 STM / attachments）。

        在 init_from_restore 和 end_session 时调用。

        **不清空** ``_session_meta``（跨 run 保持）、``_stm_session_id``（用于恢复
        判断）、``_post_compact_attachments``（跨 run 保留，下一轮 LLM 调用仍要看到
        回注内容；只在下一次压缩或 session 切换时清空）。
        """
        self._session_id = None
        self._current_session_id = ""

    # ─────────────────────────────────────────
    # Phase 6: 工作记忆管理
    # ─────────────────────────────────────────

    def set_working(self, key: str, value: Any) -> None:
        """设置 WorkingMemory 的 KV 条目。"""
        try:
            self._working.set(key, value)
        except Exception as exc:
            raise MemoryLimitError(str(exc)) from exc

    def get_working(self, key: str) -> Any | None:
        """获取 WorkingMemory 的 KV 条目。"""
        return self._working.get(key)

    def clear_working(self) -> None:
        """显式清空 WorkingMemory。"""
        self._working.clear()

    # ─────────────────────────────────────────
    # Session Meta（跨 run 同 session）
    # ─────────────────────────────────────────

    def set_session_meta(self, key: str, value: Any) -> None:
        """设置 session 级元数据。

        与 WorkingMemory 的区别：
        - WorkingMemory：session 级 KV，跨 run 自然保留（同 session 切换时清空）
        - session_meta：session 级元数据，典型用途是保存 plan_wip_path
        """
        self._session_meta[key] = value

    def get_session_meta(self, key: str) -> Any | None:
        """获取 session 级元数据。"""
        return self._session_meta.get(key)

    def clear_session_meta(self) -> None:
        """清空 session 级元数据。"""
        self._session_meta.clear()

    # ─────────────────────────────────────────
    # Properties
    # ─────────────────────────────────────────

    @property
    def working_memory_accessor(self) -> WorkingMemoryAccessor:
        """获取 WorkingMemory 的访问器（供 PostCompactSource 使用）。"""
        return self._working.accessor

    @property
    def compact_threshold(self) -> int:
        """压缩阈值（只读）。"""
        return self._compact_threshold

    @property
    def system_prompt(self) -> str:
        """系统提示词（读取；运行时替换请用 set_system_prompt）。"""
        return self._system_prompt

    def set_system_prompt(self, prompt: str) -> None:
        """运行时替换 system prompt（下一次消息构建即生效）。

        这是 ``_system_prompt`` 唯一被允许的运行时写入路径：应用层可据此在
        同一 Memory（保留对话历史）上切换 Agent 人格 / 领域。

        HC1 例外说明：``_system_prompt`` 仍留在 ``_FROZEN_ATTRS`` 中，
        因此 ``mem._system_prompt = x`` 这类**误改**依旧被 __setattr__ 拦截；
        本方法用 ``object.__setattr__`` 绕过冻结，构成一条**具名、受控**的
        修改入口——只挪动 system_prompt 这一项，其余配置字段仍然只读。

        与 compact_threshold 等真正的配置不同，system_prompt 在双层 Prompt
        设计（CORE + PERSONA）下本就是运行时可变状态，而非固定配置。

        ``_build_system_content()`` 每次动态读取 ``self._system_prompt``，
        故无需重建 Memory / Agent；调用方负责 delta 判断以保护 prompt cache
        （相同字节不重复写入）。
        """
        object.__setattr__(self, "_system_prompt", prompt)

    @property
    def post_compact_attachments(self) -> tuple[ReinjectionAttachment, ...]:
        """当前暂存的回注 attachments（只读视图，深拷贝防外部修改）。"""
        return tuple(copy.deepcopy(a) for a in self._post_compact_attachments)

    # ─────────────────────────────────────────
    # HITL Pause / Resume
    # ─────────────────────────────────────────

    def snapshot_for_pause(self) -> MemorySnapshot:
        """生成 HITL（Human-In-The-Loop）暂停快照。

        快照包含：
        - messages: STM 的对话历史
        - post_compact_attachments: 压缩后回注的附件
        """
        return MemorySnapshot(
            messages=self._short_term.snapshot(),
            post_compact_attachments=tuple(
                copy.deepcopy(a) for a in self._post_compact_attachments
            ),
        )

    def resume_context(
        self,
        snapshot: MemorySnapshot,
        *,
        session_id: str = "",
    ) -> None:
        """从 HITL 快照恢复。"""
        if session_id:
            self._current_session_id = session_id
            self._session_id = session_id
        self._short_term.resume_from_snapshot(snapshot.messages)
        self._post_compact_attachments = [
            copy.deepcopy(a) for a in snapshot.post_compact_attachments
        ]

    # ─────────────────────────────────────────
    # 通用读取
    # ─────────────────────────────────────────

    def get_messages(self) -> list[dict]:
        """返回当前完整消息列表（深拷贝，HC2）。

        消息拼接顺序：
            [system] + [post_compact_attachment_1, ...] + [conversation...]
        """
        system_msg = copy.deepcopy(self._build_system_msg())
        attachment_msgs = self._build_attachment_messages()
        conv_msgs = self._short_term.get_messages()
        # 出口守卫：会话消息段（不含 system，与守卫契约匹配）返回前套
        # ensure_tool_pair_integrity，把"压缩时才校验"扩大为"每次出站都校验"，
        # 拦截任何路径产生的孤儿 tool_call / tool_result（API 硬约束：
        # assistant(tool_calls) 之后必须跟齐 tool 消息，否则 OpenAI 兼容 API 400）。
        # 守卫不修改入参、返回新列表，此处仅用于读取出口，不影响内部状态。
        conv_msgs = ensure_tool_pair_integrity(conv_msgs)
        return [system_msg] + attachment_msgs + conv_msgs

    # ─────────────────────────────────────────
    # ContextVar
    # ─────────────────────────────────────────

    @staticmethod
    def set_run_id(run_id: str) -> None:
        """设置当前 run_id（ContextVar，协程安全）。"""
        _run_id_var.set(run_id)

    @staticmethod
    def get_run_id() -> str:
        """获取当前 run_id。"""
        return _run_id_var.get("")
