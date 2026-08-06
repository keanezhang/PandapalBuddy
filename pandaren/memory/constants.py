"""pandaren/memory/constants.py — Memory 层专用常量

Memory 层内部使用的常量集中在此。
跨层共用常量（如 CHARS_PER_TOKEN）从顶层 constants.py 导入并重新导出。
"""

from ..constants import (
    CHARS_PER_TOKEN as CHARS_PER_TOKEN,  # re-export
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_CONVERSATION_RATIO,
)

# ─────────────────────────────────────────────
# 压缩阈值
# ─────────────────────────────────────────────

# 估算 token 超过此值时触发 compact_if_needed()。
# 默认 = context_window × conversation_ratio，与 ContextWindowBudget 一致。
# 应用层可通过 AgentBuilder.context_budget() 或 .memory() 覆盖。
DEFAULT_COMPACT_THRESHOLD: int = int(DEFAULT_CONTEXT_WINDOW * DEFAULT_CONVERSATION_RATIO)

# compact 后目标保留比例（保留原始估算 token 的 70%）
COMPACT_TARGET_RATIO: float = 0.70

# 摘要输出预算：触发压缩前预留给 LLM 摘要响应的 token 数。
# 跟 claude-code MAX_OUTPUT_TOKENS_FOR_SUMMARY=20K 不同：pandaren 默认目标更小，
# 适合短任务 Agent；应用层若调 LLM 摘要可在 builder 时按需上调。
DEFAULT_RESERVED_OUTPUT_TOKENS: int = 8_000

# 触发提前量：实际触发阈值 = 配额 - DEFAULT_COMPACT_BUFFER_TOKENS
# 留缓冲是为了避免压缩后稍微偏高就立即再次触发循环。
DEFAULT_COMPACT_BUFFER_TOKENS: int = 5_000

# ─────────────────────────────────────────────
# WindowedKeepPolicy 默认参数
# ─────────────────────────────────────────────

# 保留窗口最少 token 数（确保上下文深度）
DEFAULT_MIN_KEEP_TOKENS: int = 8_000

# 保留窗口最少含 text 块的消息数（确保对话连续性，避免窗口里全是 tool result）
DEFAULT_MIN_KEEP_TEXT_MESSAGES: int = 4

# 保留窗口最多 token 数（硬上限，避免压缩后立即又触发）
DEFAULT_MAX_KEEP_TOKENS: int = 40_000

# ─────────────────────────────────────────────
# RoundBasedPolicy 默认参数（保留为可选实现）
# ─────────────────────────────────────────────

# 已删除：DEFAULT_KEEP_ROUNDS——仅服务于已废弃的 compress_every_n_turns 糖参数

# ─────────────────────────────────────────────
# MicroCompact 默认参数
# ─────────────────────────────────────────────

# add_tool_result 时单条结果超过此值立即截断
DEFAULT_MICROCOMPACT_SINGLE_RESULT_MAX_TOKENS: int = 20_000

# compact_if_needed 入口预清理时，最近 N 条工具结果不动
DEFAULT_MICROCOMPACT_KEEP_RECENT: int = 3

# 占位符文本（替换被清空的工具结果正文）
MICROCOMPACT_CLEARED_PLACEHOLDER: str = (
    "[Old tool result content cleared - re-run the tool if you need this content]"
)

# 单条工具结果超长截断时的尾部提示
MICROCOMPACT_TRUNCATED_SUFFIX: str = (
    "\n\n[...truncated by MicroCompact: tool result exceeded single-message limit]"
)

# ─────────────────────────────────────────────
# PostCompact 回注默认参数
# ─────────────────────────────────────────────

# PostCompactReinjector 总 token 预算
DEFAULT_POST_COMPACT_TOKEN_BUDGET: int = 50_000

# RecentFilesSource 默认参数
DEFAULT_POST_COMPACT_MAX_FILES: int = 5
DEFAULT_POST_COMPACT_MAX_TOKENS_PER_FILE: int = 5_000
DEFAULT_POST_COMPACT_FILES_TOKEN_BUDGET: int = 25_000

# ActiveSkillsSource 默认参数
DEFAULT_POST_COMPACT_MAX_TOKENS_PER_SKILL: int = 5_000
DEFAULT_POST_COMPACT_SKILLS_TOKEN_BUDGET: int = 25_000

# PlanStateSource 默认参数
DEFAULT_POST_COMPACT_PLAN_MAX_TOKENS: int = 5_000

# WorkingMemory 中 RecentFilesSource 约定的 key（应用层 file 工具向此 key 写记录）
RECENT_FILE_READS_WM_KEY: str = "recent_file_reads"

# ─────────────────────────────────────────────
# 长期记忆召回（已废弃）
# ─────────────────────────────────────────────

# v1.4 重构（去 summary 化）：跨 session 召回路径整体废弃。
# 已删除常量：DEFAULT_RECALL_TOP_K / RECALL_QUERY_MIN_CHARS /
#             RECALL_QUERY_AGGREGATE_TURNS / RECALL_QUERY_AGGREGATE_MAX_CHARS

# ─────────────────────────────────────────────
# 工作记忆
# ─────────────────────────────────────────────

DEFAULT_WORKING_MEMORY_MAX_ENTRIES: int = 1000

# ─────────────────────────────────────────────
# Session restore
# ─────────────────────────────────────────────

# load_for_restore() 最多加载 token 数
DEFAULT_RESTORE_TOKEN_BUDGET: int = DEFAULT_COMPACT_THRESHOLD

# ─────────────────────────────────────────────
# FlushPolicy
# ─────────────────────────────────────────────

# 批量写入合并窗口（毫秒）
DEFAULT_FLUSH_COALESCE_MS: int = 100
# 写入缓冲区条数上限，超出时立即触发写入（溢出保护）
DEFAULT_FLUSH_BUFFER_MAX_ENTRIES: int = 50
