"""pandaren/memory/__init__.py — Memory 层公共导出

应用层从此模块导入常用类型与默认实现。

Backend 具体实现（SQLite / Markdown 等）由应用层显式构造或通过 builder 注入。
SDK 只定义 Protocol 与默认切分 / 回注算法。
"""

# ── 核心 Facade ──
from .memory import Memory, MemoryLimitError, MemoryStateError

# ── 数据模型 ──
from .models import (
    MessageDict,
    CompactBoundaryDict,
    CompactionSplit,
    MemorySnapshot,
    PostCompactContext,
    ReinjectionAttachment,
)

# ── 协议 ──
from .protocols import (
    WorkingMemoryAccessor,
    WorkingMemoryBackend,
    RawLogBackend,
    CompactionPolicy,
    DropSummarizer,
    FlushPolicy,
    PostCompactSource,
    TokenEstimator,
    CharBasedTokenEstimator,
)

# ── 估算器实现 ──
# 注意：estimators 模块顶层不 import tiktoken（构造期惰性 import），
# 此处导出不会让 SDK 产生硬依赖；未装 tiktoken 时仅构造 TiktokenEstimator 才抛 ImportError。
from .estimators import TiktokenEstimator

# ── 切分 / 清理 ──
from .compaction import (
    WindowedKeepPolicy,
    MicroCompactor,
    ensure_tool_pair_integrity,
)

# ── 压缩后回注 ──
from .reinject import (
    PostCompactReinjector,
    RecentFilesSource,
    ActiveSkillsSource,
    PlanStateSource,
)

# ── 写入策略 ──
from .flush_policy import AsyncBatchFlushPolicy

# ── 常量 ──
from .constants import (
    DEFAULT_COMPACT_THRESHOLD,
    COMPACT_TARGET_RATIO,
    DEFAULT_RESERVED_OUTPUT_TOKENS,
    DEFAULT_COMPACT_BUFFER_TOKENS,
    DEFAULT_MIN_KEEP_TOKENS,
    DEFAULT_MIN_KEEP_TEXT_MESSAGES,
    DEFAULT_MAX_KEEP_TOKENS,
    DEFAULT_MICROCOMPACT_KEEP_RECENT,
    DEFAULT_MICROCOMPACT_SINGLE_RESULT_MAX_TOKENS,
    MICROCOMPACT_CLEARED_PLACEHOLDER,
    DEFAULT_POST_COMPACT_TOKEN_BUDGET,
    DEFAULT_POST_COMPACT_MAX_FILES,
    DEFAULT_POST_COMPACT_MAX_TOKENS_PER_FILE,
    DEFAULT_POST_COMPACT_FILES_TOKEN_BUDGET,
    DEFAULT_POST_COMPACT_MAX_TOKENS_PER_SKILL,
    DEFAULT_POST_COMPACT_SKILLS_TOKEN_BUDGET,
    DEFAULT_POST_COMPACT_PLAN_MAX_TOKENS,
    RECENT_FILE_READS_WM_KEY,
    DEFAULT_WORKING_MEMORY_MAX_ENTRIES,
    DEFAULT_RESTORE_TOKEN_BUDGET,
    DEFAULT_FLUSH_COALESCE_MS,
    DEFAULT_FLUSH_BUFFER_MAX_ENTRIES,
)

__all__ = [
    # Facade
    "Memory",
    "MemoryLimitError",
    "MemoryStateError",
    # Models
    "MessageDict",
    "CompactBoundaryDict",
    "CompactionSplit",
    "MemorySnapshot",
    "PostCompactContext",
    "ReinjectionAttachment",
    # Protocols
    "WorkingMemoryAccessor",
    "WorkingMemoryBackend",
    "RawLogBackend",
    "CompactionPolicy",
    "DropSummarizer",
    "FlushPolicy",
    "PostCompactSource",
    "TokenEstimator",
    "CharBasedTokenEstimator",
    "TiktokenEstimator",
    # Compaction
    "WindowedKeepPolicy",
    "MicroCompactor",
    "ensure_tool_pair_integrity",
    # PostCompact reinject
    "PostCompactReinjector",
    "RecentFilesSource",
    "ActiveSkillsSource",
    "PlanStateSource",
    # Flush
    "AsyncBatchFlushPolicy",
    # Constants
    "DEFAULT_COMPACT_THRESHOLD",
    "COMPACT_TARGET_RATIO",
    "DEFAULT_RESERVED_OUTPUT_TOKENS",
    "DEFAULT_COMPACT_BUFFER_TOKENS",
    "DEFAULT_MIN_KEEP_TOKENS",
    "DEFAULT_MIN_KEEP_TEXT_MESSAGES",
    "DEFAULT_MAX_KEEP_TOKENS",
    "DEFAULT_MICROCOMPACT_KEEP_RECENT",
    "DEFAULT_MICROCOMPACT_SINGLE_RESULT_MAX_TOKENS",
    "MICROCOMPACT_CLEARED_PLACEHOLDER",
    "DEFAULT_POST_COMPACT_TOKEN_BUDGET",
    "DEFAULT_POST_COMPACT_MAX_FILES",
    "DEFAULT_POST_COMPACT_MAX_TOKENS_PER_FILE",
    "DEFAULT_POST_COMPACT_FILES_TOKEN_BUDGET",
    "DEFAULT_POST_COMPACT_MAX_TOKENS_PER_SKILL",
    "DEFAULT_POST_COMPACT_SKILLS_TOKEN_BUDGET",
    "DEFAULT_POST_COMPACT_PLAN_MAX_TOKENS",
    "RECENT_FILE_READS_WM_KEY",
    "DEFAULT_WORKING_MEMORY_MAX_ENTRIES",
    "DEFAULT_RESTORE_TOKEN_BUDGET",
    "DEFAULT_FLUSH_COALESCE_MS",
    "DEFAULT_FLUSH_BUFFER_MAX_ENTRIES",
]
