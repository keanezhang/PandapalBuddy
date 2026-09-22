"""pandaren/memory/constants.py — Memory 层专用常量

Memory 层内部使用的常量集中在此。

分层依据见 COMPACT_BUDGET_LAYERING_SPEC.md：
  · D 组（压缩策略旋钮）在本模块，SDK 给默认值 + 注入点；
  · 预算字段（CW / 熔断线 / 记账值 / 输出预留）在 behavior 的
    ContextWindowBudget，本模块不重复定义。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("pandaren.memory.constants")

# ─────────────────────────────────────────────
# D 组 · 压缩策略档（SDK 默认值 + 应用可注入）
# ─────────────────────────────────────────────


@dataclass(frozen=True)
class CompactionProfile:
    """压缩力度：SDK 给预置档，应用可选档或自定义。

    方向说明：越 tight（紧） → 每次压缩丢得越多、保留越少、成本越低。
    窗口比例已固定 0.80（app 侧），本 profile 只决定"压多狠"；
    故刻意不用"保守 / 激进"这类会读反的措辞。

    ⚠️ 压缩派生方法都收 ``T``（= ``ContextWindowBudget.compact_threshold``）作输入，
      因为它们是"压多狠"的事，与窗口预算无关（见 SPEC §2.7）。
    """

    target_ratio: float
    min_keep_ratio: float
    max_keep_ratio: float
    min_keep_text_messages: int

    # ── 压缩派生（依赖 T）───────────────────────────────────────────────────

    def min_keep_tokens(self, T: int) -> int:
        """保留下限（实际即"保留目标"：split() 满足它即停）。"""
        return max(1, min(int(T * self.min_keep_ratio), self.max_keep_tokens(T)))

    def max_keep_tokens(self, T: int) -> int:
        """保留硬上限（常规只兜"单条超大消息"）。"""
        return max(1, int(T * self.max_keep_ratio))

    def single_result_max_tokens(self, T: int) -> int:
        """单条工具结果上限 = clamp(T × RATIO, FLOOR, MAX)。"""
        return max(
            TOOL_RESULT_CAP_FLOOR,
            min(int(T * TOOL_RESULT_CAP_RATIO), TOOL_RESULT_CAP_MAX),
        )

    def target_tokens(
        self,
        T: int,
        *,
        system_overhead: int,
        old_attachments: int,
        reserved_summary: int,
        reinject: int,
    ) -> int:
        """压缩后目标保留 = T × target_ratio − 各项固定开销。"""
        return (
            int(T * self.target_ratio)
            - system_overhead
            - old_attachments
            - reserved_summary
            - reinject
        )

    def keep_window(
        self,
        T: int,
        *,
        sys_acct: int,
        target_ceiling: int,
    ) -> tuple[int, int]:
        """返回经 **I4 / I5 收敛**后的 ``(min_keep, max_keep)``。

        违反处置 = 「收敛到最近合法值 + WARNING」（SPEC §4），不拒绝启动：

        - **I5** `min_keep ≤ T − sys_acct`：否则压缩后总量仍 > `T`，会反复触发压缩；
        - **I4** `min_keep ≤ max_keep ≤ max(target_ceiling, min_keep)`：区间非空且不超过压缩目标。

        预置三档在正常窗口下都不会触发收敛（`max_keep_ratio < target_ratio`），
        收敛只对**自定义档 / 极端窗口**生效。
        """
        max_keep = self.max_keep_tokens(T)
        min_keep = max(1, min(int(T * self.min_keep_ratio), max_keep))
        converged: list[str] = []

        i5_cap = max(1, T - sys_acct)
        if min_keep > i5_cap:
            converged.append(f"I5 min_keep {min_keep}→{i5_cap} (T={T}, sys_acct={sys_acct})")
            min_keep = i5_cap

        i4_cap = max(target_ceiling, min_keep)
        if max_keep > i4_cap:
            converged.append(f"I4 max_keep {max_keep}→{i4_cap} (target={target_ceiling})")
            max_keep = i4_cap

        if min_keep > max_keep:
            converged.append(f"I4 min_keep {min_keep}→{max_keep}")
            min_keep = max_keep

        min_keep = max(1, min_keep)
        if converged:
            logger.warning(
                "CompactionProfile: 保留窗口偏离目标，已收敛到最近合法值 —— %s",
                "; ".join(converged),
            )
        return min_keep, max_keep


# 预置档（比例来自 SDK 现行默认值）target_ratio（水位）、min_keep_ratio（下限）、max_keep_ratio（上限）、min_keep_text_messages（条数）
GENTLE = CompactionProfile(0.80, 0.25, 0.55, 6)      # 压得轻：保留多、成本高
BALANCED = CompactionProfile(0.70, 0.12, 0.45, 4)    # 默认
TIGHT = CompactionProfile(0.55, 0.08, 0.35, 3)       # 压得紧：保留少、成本低
DEFAULT_COMPACTION_PROFILE = BALANCED


# ─────────────────────────────────────────────
# D 组 · 同组常量（不随模型变、无注入点，SDK 写死）
# ─────────────────────────────────────────────

# 触发提前量：全档固定。T = conversation − COMPACT_BUFFER_TOKENS
COMPACT_BUFFER_TOKENS: int = 500

# 单条工具结果上限三段式：clamp(T × RATIO, FLOOR, MAX)
TOOL_RESULT_CAP_RATIO: float = 0.15
TOOL_RESULT_CAP_FLOOR: int = 8_000
TOOL_RESULT_CAP_MAX: int = 30_000

# 入口预清理时，最近 N 条工具结果不动
MICROCOMPACT_KEEP_RECENT: int = 3

# 压缩后固定占用超阈值的告警线：这里的固定指的是摘要+回注的固定值，这里的阈值指的是压缩后剩下的余量headroom，当这个固定值大于余量，那就说明压缩完之后还是没有给会话留空间，压缩没意义
FIXED_AFTER_COMPACT_WARN_RATIO: float = 0.30

# 占位符文本（替换被清空的工具结果正文）
MICROCOMPACT_CLEARED_PLACEHOLDER: str = (
    "[Old tool result content cleared - re-run the tool if you need this content]"
)

# 单条工具结果超长截断时的尾部提示
MICROCOMPACT_TRUNCATED_SUFFIX: str = (
    "\n\n[...truncated by MicroCompact: tool result exceeded single-message limit]"
)

# 摘要输出预留兜底（app 未传 reserved_summary_tokens 时用；唯一事实来源是摘要器的
# max_output_tokens，见 SPEC §2.4）
DEFAULT_RESERVED_SUMMARY_TOKENS: int = 1_000


# ─────────────────────────────────────────────
# PostCompact 回注默认参数（**指针模式**：只注入索引，不注入正文）
# ─────────────────────────────────────────────
# 回注只产出"清单 / 路径"这类几十 token 的指针；正文由 AI 用 read_file 按需重取。
# 设计理由见 reinject/sources.py 模块文档。

# 所有 source 合计的 token 预算。按"各 source cap 之和"定，不按"实际用量"。
# ⚠️ 这个值会**直接从压缩目标里扣掉**（target_tokens −= post_compact_token_budget），
# ⚠️ 但它必须 **≥ 各 source cap 之和**（1,000 + 512 = 1,512）：编排器超总预算时是
#    **整体丢弃**后续 attachment（不是截断），否则会出现"清单占满 → plan 被饿死"。
DEFAULT_POST_COMPACT_TOKEN_BUDGET: int = 1_600

# RecentFilesSource：文件清单里最多列几个文件
DEFAULT_POST_COMPACT_MAX_FILES: int = 10

# 两个 source 各自产物的 token 上限（防路径极多 / 超长）——命名与 source 一一对应：
#   · FILES_LIST —— **文件清单**（RecentFilesSource 那份列表）的 token 上限
#                  实测：指导语头部 ~110 + 10 行 × ~20-40 ≈ 310~510 → 1,000 留约 2x 余量
#   · PLAN       —— **plan 指针**（PlanStateSource 的路径 + 使用说明）的 token 上限
#                  只有"1 行路径 + 2 行说明" ≈ 120 → 512 留约 4x 余量
DEFAULT_POST_COMPACT_FILES_LIST_MAX_TOKENS: int = 1_000
DEFAULT_POST_COMPACT_PLAN_MAX_TOKENS: int = 512

# WorkingMemory 中 RecentFilesSource 约定的 key（应用层 file 工具向此 key 写记录）
RECENT_FILE_READS_WM_KEY: str = "recent_file_reads"


# ─────────────────────────────────────────────
# 工作记忆
# ─────────────────────────────────────────────

DEFAULT_WORKING_MEMORY_MAX_ENTRIES: int = 1000


# ─────────────────────────────────────────────
# FlushPolicy
# ─────────────────────────────────────────────

# 批量写入合并窗口（毫秒）
DEFAULT_FLUSH_COALESCE_MS: int = 100
# 写入缓冲区条数上限，超出时立即触发写入（溢出保护）
DEFAULT_FLUSH_BUFFER_MAX_ENTRIES: int = 50
