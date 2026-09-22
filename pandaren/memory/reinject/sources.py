"""pandaren/memory/reinject/sources.py — 内置 PostCompactSource

【背景】
当对话历史被压缩后（这里说的压缩，其实就是删除），AI 可能会"忘记"之前正在处理的关键信息。
"回注"机制就是在压缩后，把重要的上下文信息重新注入回去。

【设计：只回注「指针」，不回注「正文」】
压缩前 AI 真正丢失的是「**我知道有什么**」这类索引信息：
它连"我读过哪些文件""当前 plan 在哪个文件"都不晓得，自然也无从重取。
而正文本身，AI 本来就有工具（``read_file``）能按需重取；且正文进上下文会
**直接挤占 ``kept``**（压缩后保留的对话历史），代价远高于指针。

所以本模块只产出**清单 / 路径**这类几十 token 的指针：

  - RecentFilesSource:  最近读过的**文件清单**（路径 + 大小）
                        → 压缩后 AI 知道"我读过什么"，需要时用 ``read_file`` 重读
  - PlanStateSource:    当前 **plan 文件路径**
                        → 压缩后 AI 知道"计划在哪个文件"，需要时读正文

（技能正文**不回注**：AI 可用 ``search_skills`` 重新加载，且技能目录已在
 ``static_context`` 里常驻、不参与压缩。）

应用层可以单独启用/禁用任意 source，也可以实现自己的 PostCompactSource
通过 ``builder.memory(post_compact_sources=[...])`` 注入。

设计原则：
  - 每个 source 只读取**可枚举**的状态，不调 LLM（B3）
  - source 失败时返回空列表（E4），不抛异常
  - **不读文件正文**（故无"字节上限"问题；清单自身大小由 ``_truncate_to_tokens`` 兜底）
  - 截断用 **Memory 注入的同一把尺子**（``_estimator_of``，见 SPEC §2.5 P0）
  - SDK 不内置默认 sources（应用层不传 = 不启用 PostCompact 回注）
"""

from __future__ import annotations

import logging
import os

from ..constants import (
    DEFAULT_POST_COMPACT_FILES_LIST_MAX_TOKENS,  # 文件清单自身的 token 上限
    DEFAULT_POST_COMPACT_MAX_FILES,              # 清单里最多列几个文件
    DEFAULT_POST_COMPACT_PLAN_MAX_TOKENS,        # plan 指针的 token 上限
    RECENT_FILE_READS_WM_KEY,                    # WorkingMemory 中记录最近读文件的 key
)
from ..models import PostCompactContext, ReinjectionAttachment
from ..protocols import CharBasedTokenEstimator, TokenEstimator

logger = logging.getLogger("pandaren.memory.reinject.sources")


#: 截断标记（旧实现把它内联在函数里，现在提出来以便计入 token 上限）
_TRUNCATED_SUFFIX = "\n\n[...truncated by PostCompactSource]"


def _estimator_of(ctx: PostCompactContext) -> TokenEstimator:
    """取「**与 Memory 同一把尺子**」的 estimator。

    `Memory` 构造 `PostCompactContext` 时会塞入自己的 `_token_estimator`；
    source 被独立使用（测试 / 应用自建）时该字段为 None，回退到 chars/4 粗估。
    """
    return ctx.token_estimator or CharBasedTokenEstimator()


def _estimate_text(estimator: TokenEstimator, text: str) -> int:
    """用同一把尺子估算纯文本 token 数（按 tool 消息估算，与 Memory 口径一致）。"""
    if not text:
        return 0
    return estimator.estimate([{"role": "tool", "content": text}])


def _truncate_to_tokens(
    text: str, max_tokens: int, estimator: TokenEstimator
) -> tuple[str, int]:
    """截断到 ``max_tokens`` 以内，返回 ``(截断后文本, 真实估算 token 数)``。

    ⚠️ 与旧实现的三点区别（SPEC §2.5 P0）：

    1. **用注入的 `estimator`**，不再用 `CHARS_PER_TOKEN` 粗估 ——
       该系数是英文经验值（4 字符/token），中文会**低估约 2 倍**，
       导致实际回注量超过预算 → 压缩后重新超阈值 → `CONTEXT_OVERFLOW` 终止 run。
    2. **后缀计入上限**：旧实现把标记加在 `max_chars` **之外**，
       截断后的文本反而超出 `max_tokens`。
    3. **二分收敛找最长合法前缀**（token 数不线性于字符数），
       与 ``MicroCompactor._converge_prefix_length`` 同一套做法。

    Args:
        text:       原始文本
        max_tokens: 允许的最大 token 数（**含**截断标记）
        estimator:  Token 估算器（应由 `Memory` 注入，保证同尺）

    Returns:
        (截断后的文本, 估算的 token 数) —— 两者都不超过 ``max_tokens``。
    """
    if not text:
        return "", 0
    total = _estimate_text(estimator, text)
    if total <= max_tokens:
        return text, max(1, total)

    # 先扣掉标记自身占用，剩下的才是正文容量
    suffix_tokens = _estimate_text(estimator, _TRUNCATED_SUFFIX)
    if suffix_tokens >= max_tokens:
        # 极端：上限比标记本身还小 → 丢掉标记，硬截到上限（保证"不超限"这条硬约束）
        suffix, body_cap = "", max_tokens
    else:
        suffix, body_cap = _TRUNCATED_SUFFIX, max_tokens - suffix_tokens

    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _estimate_text(estimator, text[:mid]) <= body_cap:
            lo = mid
        else:
            hi = mid - 1

    truncated = text[:lo] + suffix
    return truncated, _estimate_text(estimator, truncated)


def _human_size(n: int) -> str:
    """人类可读的文件大小（清单里显示用），如 ``12.3 KB``。"""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _file_size_or_none(path: str, hint: object) -> int | None:
    """取文件大小：优先用记录里的 ``size_hint``（零 I/O），否则本地 stat 一次。

    返回 None = 文件不存在 / 不可访问 —— 调用方应跳过该项：
    指针指向一个读不到的文件没有意义（AI 按它去 ``read_file`` 只会拿到错误）。
    """
    if isinstance(hint, int) and hint > 0:
        return hint
    try:
        return os.path.getsize(path)
    except OSError:
        return None


# ─────────────────────────────────────────────
# RecentFilesSource — 回注「最近读过哪些文件」的清单（指针）
# ─────────────────────────────────────────────

#: ⚠️ 指导语必须写在**头部**：`_truncate_to_tokens` 切的是**尾部**，
#: 放尾部会在清单过长时第一个被切掉 —— 而那恰好是最不该丢的一句。
#: 指针模式的关键是「**给了不等于要读**」：没有这句，模型看到清单很可能
#: 挨个 read_file，反而把上下文重新吃满，违背回注初衷。
_RECENT_FILES_HEADER = (
    "最近读过的文件（**只是索引**，正文已不在上下文中）。"
    "**仅在确实需要某个文件的内容时**才用 read_file 读取它；"
    "不要因为文件出现在上面就逐个重读 —— 那只会白白消耗上下文："
)


class RecentFilesSource:
    """回注「最近读过哪些文件」的**清单**（不注入正文）。

    【为什么需要这个？】
    ``WorkingMemory[RECENT_FILE_READS_WM_KEY]`` **不会进入 prompt** ——
    也就是说压缩前 AI 根本不知道"自己读过哪些文件"，因此也谈不上"自己重读"。
    把这份清单（几十 token）还给它，就等于恢复了"我读过什么"这个索引；
    正文则按需用 ``read_file`` 取。

    约定：应用层的"读文件"工具应在 ``WorkingMemory[RECENT_FILE_READS_WM_KEY]``
    维护一个列表，每项形如::

        {
            "path": "/abs/path/to/file.py",
            "timestamp": 1700000000.0,    # epoch seconds，可选（用于排序）
            "size_hint": 1234,             # 可选；缺失时本地 stat 一次
        }

    WorkingMemory 是 session 级语义——跨 run 自然保留，所以"上一个 run 读过的
    文件"在下一个 run 触发压缩时仍可见，无需任何特殊豁免逻辑。

    SDK **不强制**任何工具遵守这个约定；key 不存在或格式错误时返回空列表（不报错）。

    工作流程：
      1. 从 WorkingMemory 取记录，按 timestamp 倒序、按 path 去重，取前 ``max_files``
      2. 每项取一次大小（优先 ``size_hint``，否则 stat）；取不到（已删）则跳过
      3. 拼成清单文本，超 ``max_tokens`` 时截断（用 Memory 的同一把尺子）
    """

    SOURCE_NAME = "recent_files"

    def __init__(
        self,
        max_files: int = DEFAULT_POST_COMPACT_MAX_FILES,
        max_tokens: int = DEFAULT_POST_COMPACT_FILES_LIST_MAX_TOKENS,
    ) -> None:
        self._max_files = max_files      # 清单里最多列几个文件
        self._max_tokens = max_tokens    # 清单自身的 token 上限

    def collect(self, ctx: PostCompactContext) -> list[ReinjectionAttachment]:
        """收集「最近读过的文件」清单，作为**单个**回注附件返回。"""
        # 第一步：从 WorkingMemory 中获取"最近读文件"的记录列表
        try:
            records = ctx.working_memory.get(RECENT_FILE_READS_WM_KEY)
        except Exception as exc:
            logger.warning(
                "RecentFilesSource: working_memory.get(%s) failed: %s",
                RECENT_FILE_READS_WM_KEY,
                exc,
            )
            return []

        if not records or not isinstance(records, list):
            return []

        # 第二步：去重（同路径只留最近一次）+ 按时间倒序 + 取前 max_files
        path_to_record: dict[str, dict] = {}
        for r in records:
            if not isinstance(r, dict):
                continue
            path = r.get("path")
            if not isinstance(path, str) or not path:
                continue
            ts = r.get("timestamp", 0)
            existing = path_to_record.get(path)
            if existing is None or (
                isinstance(ts, (int, float))
                and ts > float(existing.get("timestamp", 0) or 0)
            ):
                path_to_record[path] = r

        sorted_records = sorted(
            path_to_record.values(),
            key=lambda r: float(r.get("timestamp", 0) or 0),
            reverse=True,
        )[: self._max_files]

        # 第三步：拼清单（只取大小，**不读正文**）
        lines: list[str] = []
        for r in sorted_records:
            path = r["path"]
            size = _file_size_or_none(path, r.get("size_hint"))
            if size is None:
                logger.info(
                    "%s: cannot stat %s, skipping from listing",
                    self.SOURCE_NAME, path,
                )
                continue
            lines.append(
                f"  {len(lines) + 1}. {self._display_path(path)}  ({_human_size(size)})"
            )

        if not lines:
            return []

        raw = _RECENT_FILES_HEADER + "\n" + "\n".join(lines)
        content, est_tokens = _truncate_to_tokens(
            raw, self._max_tokens, _estimator_of(ctx)
        )
        logger.info(
            "%s: listed %d file(s) (~%d tokens)",
            self.SOURCE_NAME, len(lines), est_tokens,
        )
        return [
            {
                "source_name": self.SOURCE_NAME,
                "title": "Recently read files",
                "content": content,
                "estimated_tokens": est_tokens,
            }
        ]

    @staticmethod
    def _display_path(path: str) -> str:
        """返回相对 cwd 的显示路径（更可读）。

        例如：/home/user/project/src/main.py → src/main.py
        如果无法生成相对路径（如不同盘符），返回原路径。
        """
        try:
            return os.path.relpath(path)
        except ValueError:
            return path


# ─────────────────────────────────────────────
# PlanStateSource — 回注当前 plan 文件的路径（指针）
# ─────────────────────────────────────────────

#: 同 _RECENT_FILES_HEADER：指导语放**头部**（截断切尾部）；
#: 且"给了路径 ≠ 要读"，避免无谓重读消耗上下文。
_PLAN_HEADER = (
    "当前 plan 文件（**只是路径**，正文已不在上下文中）。"
    "**需要回顾计划细节时**才用 read_file 读取该文件；"
    "若当前任务与计划无关，无需读取："
)


class PlanStateSource:
    """回注「当前 plan 文件的**路径**」（不注入正文）。

    约定：``ctx.session_meta`` 中存在 key ``plan_file_path``（由 ``run_core`` 在
    ``enter_plan_mode`` 成功后写入；``exit_plan_mode`` 提交时同样写入）。

    计划是任务主线，但往往很长；逐字注入会挤占 ``kept``。
    给"路径 + 明确指引"更划算：AI 需要正文时用 ``read_file`` 读一次即可。

    路径缺失、或指向的文件已不存在时返回空列表（E4 降级，不报错）。
    """

    SOURCE_NAME = "plan_state"
    META_KEY = "plan_file_path"  # session_meta 中存放 plan 文件路径的 key

    def __init__(self, max_tokens: int = DEFAULT_POST_COMPACT_PLAN_MAX_TOKENS) -> None:
        self._max_tokens = max_tokens    # plan 指针的 token 上限

    def collect(self, ctx: PostCompactContext) -> list[ReinjectionAttachment]:
        """返回单个「plan 文件路径」附件（或空列表）。"""
        path = ctx.session_meta.get(self.META_KEY)
        if not isinstance(path, str) or not path:
            # 没有 plan 文件路径 → 当前不在 plan 模式
            return []

        if not os.path.exists(path):
            logger.info(
                "%s: plan file %s not found, skipping", self.SOURCE_NAME, path
            )
            return []

        content, est_tokens = _truncate_to_tokens(
            _PLAN_HEADER + f"\n  {path}",
            self._max_tokens,
            _estimator_of(ctx),
        )
        logger.info(
            "%s: reinjected plan pointer (~%d tokens)", self.SOURCE_NAME, est_tokens
        )
        return [
            {
                "source_name": self.SOURCE_NAME,
                "title": f"Current plan: {os.path.basename(path)}",
                "content": content,
                "estimated_tokens": est_tokens,
            }
        ]
