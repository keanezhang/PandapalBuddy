"""pandaren/memory/reinject/sources.py — 内置 PostCompactSource

【背景】
当对话历史被压缩后（这里说的压缩，其实就是删除），AI 可能会"忘记"之前正在处理的关键信息。
"回注"机制就是在压缩后，把重要的上下文信息重新注入回去。
每个 PostCompactSource 负责收集一类特定的重要信息。

三个 SDK 内置的 PostCompactSource：

  - RecentFilesSource:   从 WorkingMemory 拿"最近 N 个 file read"
                         → 压缩后 AI 仍能"看到"最近读过的文件内容
  - ActiveSkillsSource:  从 SkillRegistry 拿当前激活技能内容
                         → 压缩后 AI 仍能"记住"当前激活了哪些技能
  - PlanStateSource:     从 session_meta 拿 plan_file_path 文件正文
                         → 压缩后 AI 仍能"知道"当前正在执行的 plan

应用层可以单独启用/禁用任意 source，也可以实现自己的 PostCompactSource
通过 ``builder.memory(post_compact_sources=[...])`` 注入。

设计原则：
  - 每个 source 只读取**可枚举**的状态，不调 LLM（B3）
  - source 失败时返回空列表（E4），不抛异常
  - 每个 source 自己控制单条 attachment 的截断；总预算由 PostCompactReinjector 统一控制
  - SDK 不内置默认 sources（应用层不传 = 不启用 PostCompact 回注）
"""

from __future__ import annotations

import logging
import os

from ..constants import (
    DEFAULT_POST_COMPACT_FILES_TOKEN_BUDGET,    # 文件回注的总 token 预算
    DEFAULT_POST_COMPACT_MAX_FILES,              # 最多回注多少个文件
    DEFAULT_POST_COMPACT_MAX_TOKENS_PER_FILE,    # 每个文件的 token 上限
    DEFAULT_POST_COMPACT_MAX_TOKENS_PER_SKILL,   # 每个技能的 token 上限
    DEFAULT_POST_COMPACT_PLAN_MAX_TOKENS,        # plan 文件的 token 上限
    DEFAULT_POST_COMPACT_SKILLS_TOKEN_BUDGET,    # 技能回注的总 token 预算
    RECENT_FILE_READS_WM_KEY,                    # WorkingMemory 中记录最近读文件的 key
    CHARS_PER_TOKEN,                             # 估算比例：每个 token ≈ 多少个字符
)
from ..models import PostCompactContext, ReinjectionAttachment

logger = logging.getLogger("pandaren.memory.reinject.sources")


def _truncate_to_tokens(text: str, max_tokens: int) -> tuple[str, int]:
    """按字符数粗略截断到 max_tokens 内，返回 (截断后文本, 估算 token 数)。

    为什么用字符数而不是精确 token 数？
    → 精确 token 化需要调 tokenizer，开销大且引入额外依赖。
      这里用 CHARS_PER_TOKEN（经验值，如 4 字符/token）做粗略估算，
      对于回注场景足够用了，不需要精确计算。

    Args:
        text:       原始文本
        max_tokens: 允许的最大 token 数

    Returns:
        (截断后的文本, 估算的 token 数)
    """
    max_chars = int(max_tokens * CHARS_PER_TOKEN)  # 把 token 上限换算成字符上限
    if len(text) <= max_chars:
        # 文本没有超限，直接返回，token 数按实际长度估算
        return text, max(1, int(len(text) / CHARS_PER_TOKEN))
    # 文本超限，截断并追加提示信息
    truncated = text[:max_chars] + "\n\n[...truncated by PostCompactSource]"
    return truncated, max_tokens


# ─────────────────────────────────────────────
# RecentFilesSource — 回注最近读过的文件
# ─────────────────────────────────────────────

class RecentFilesSource:
    """回注最近读过的若干文件正文。

    【为什么需要这个？】
    压缩对话历史时，AI 之前"读过"的文件内容可能被压缩掉了。
    但这些文件内容对当前任务可能仍然很重要（比如正在编辑的代码文件）。
    这个 source 把最近读过的文件内容重新注入，让 AI 不用重复"读文件"。

    约定：应用层的"读文件"工具应在 ``WorkingMemory[RECENT_FILE_READS_WM_KEY]``
    维护一个列表，每项形如：

        {
            "path": "/abs/path/to/file.py",
            "timestamp": 1700000000.0,    # epoch seconds，可选
            "size_hint": 1234,             # 可选，文件大小提示
        }

    WorkingMemory 是 session 级语义——跨 run 自然保留，所以"上一个 run 读过的
    文件"在下一个 run 触发压缩时仍可见，无需任何特殊豁免逻辑。

    SDK **不强制**任何工具遵守这个约定；只有当应用层启用 RecentFilesSource
    时才需要工具配合。如果约定 key 不存在或格式错误，本 source 返回空列表（不报错）。

    工作流程：
      1. 从 WorkingMemory 拿列表，按 timestamp 倒序、去重路径，取前 max_files
      2. 实际读取文件内容（OS 文件系统）；读不到的跳过
      3. 每个文件截断到 max_tokens_per_file
      4. 累计超 total_token_budget 时不再添加更多文件
    """

    SOURCE_NAME = "recent_files"

    def __init__(
        self,
        max_files: int = DEFAULT_POST_COMPACT_MAX_FILES,
        max_tokens_per_file: int = DEFAULT_POST_COMPACT_MAX_TOKENS_PER_FILE,
        total_token_budget: int = DEFAULT_POST_COMPACT_FILES_TOKEN_BUDGET,
    ) -> None:
        self._max_files = max_files              # 最多回注几个文件
        self._max_tokens_per_file = max_tokens_per_file  # 单个文件的 token 上限
        self._total_token_budget = total_token_budget    # 所有文件合计的 token 上限

    def collect(self, ctx: PostCompactContext) -> list[ReinjectionAttachment]:
        """收集最近读过的文件内容，作为回注附件返回。

        Args:
            ctx: 压缩后的上下文，包含 working_memory 等信息

        Returns:
            回注附件列表，每个附件包含一个文件的内容
        """
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

        # 第二步：去重 + 排序
        # 去重逻辑：同一个路径可能出现多次（多次读取同一文件），
        # 只保留 timestamp 最大的那条记录（即最近一次读取）
        path_to_record: dict[str, dict] = {}
        for r in records:
            if not isinstance(r, dict):
                continue
            path = r.get("path")
            if not isinstance(path, str) or not path:
                continue
            ts = r.get("timestamp", 0)
            existing = path_to_record.get(path)
            # 如果该路径还没有记录，或者新记录的 timestamp 更大，则更新
            if existing is None or (
                isinstance(ts, (int, float))
                and ts > float(existing.get("timestamp", 0) or 0)
            ):
                path_to_record[path] = r

        # 按 timestamp 倒序排列（最近读的在前面），取前 max_files 个，就是最近的几个
        sorted_records = sorted(
            path_to_record.values(),
            key=lambda r: float(r.get("timestamp", 0) or 0),
            reverse=True,
        )[: self._max_files]

        # 第三步：逐个读取文件内容，构建附件
        attachments: list[ReinjectionAttachment] = []
        used_tokens = 0
        for r in sorted_records:
            path = r["path"]
            try:
                # 从文件系统实际读取文件内容
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    raw = f.read()
            except (OSError, IOError) as exc:
                # 文件读不到（可能已被删除），跳过
                logger.info(
                    "RecentFilesSource: failed to read %s, skipping: %s", path, exc
                )
                continue

            # 截断到单文件 token 上限
            content, est_tokens = _truncate_to_tokens(raw, self._max_tokens_per_file)

            # 检查累计 token 是否超出总预算
            if used_tokens + est_tokens > self._total_token_budget and attachments:
                break

            # 构建附件对象
            display_name = self._display_path(path)  # 显示相对路径，更可读
            attachment: ReinjectionAttachment = {
                "source_name": self.SOURCE_NAME,       # 标记来源
                "title": f"Recently read file: {display_name}",  # 附件标题
                "content": content,                     # 文件正文（可能已截断）
                "estimated_tokens": est_tokens,         # 估算的 token 数
            }
            attachments.append(attachment)
            used_tokens += est_tokens

        if attachments:
            logger.info(
                "RecentFilesSource: reinjected %d file(s), ~%d tokens",
                len(attachments),
                used_tokens,
            )
        return attachments

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
# ActiveSkillsSource — 回注当前激活的技能
# ─────────────────────────────────────────────

class ActiveSkillsSource:
    """回注当前已激活技能的正文。

    【为什么需要这个？】
    "技能"（Skill）是预定义的专业知识/指令集，激活后会影响 AI 的行为。
    压缩后，AI 可能忘记自己激活了哪些技能，导致行为偏离。
    这个 source 把激活技能的正文重新注入，确保 AI 仍然遵循技能的指引。

    约定：``ctx.skill_registry`` 提供方法 ``get_invoked_skills() -> list[skill_obj]``，
    每个 skill_obj 至少有：
      - ``.name: str``
      - ``.content: str``   （已渲染好的 skill 正文）
      - ``.path: str``      （可选，文件路径用于显示）
      - ``.invoked_at: float`` （可选，激活时间戳，用于排序）

    若 ``ctx.skill_registry`` 为 None 或缺少 ``get_invoked_skills``，返回空列表。

    截断规则：
      - 每个 skill 截到 ``max_tokens_per_skill``
      - 累计超 ``total_token_budget`` 时停止添加
      - 按 invoked_at 倒序（最近激活的优先）
    """

    SOURCE_NAME = "active_skills"

    def __init__(
        self,
        max_tokens_per_skill: int = DEFAULT_POST_COMPACT_MAX_TOKENS_PER_SKILL,
        total_token_budget: int = DEFAULT_POST_COMPACT_SKILLS_TOKEN_BUDGET,
    ) -> None:
        self._max_tokens_per_skill = max_tokens_per_skill   # 单个技能的 token 上限
        self._total_token_budget = total_token_budget       # 所有技能合计的 token 上限

    def collect(self, ctx: PostCompactContext) -> list[ReinjectionAttachment]:
        """收集当前激活技能的内容，作为回注附件返回。

        Args:
            ctx: 压缩后的上下文，包含 skill_registry 等信息

        Returns:
            回注附件列表，每个附件包含一个技能的内容
        """
        # 获取技能注册表
        registry = ctx.skill_registry
        if registry is None:
            return []

        # 安全地获取 get_invoked_skills 方法（防御性编程）
        get_invoked = getattr(registry, "get_invoked_skills", None)
        if not callable(get_invoked):
            logger.debug(
                "ActiveSkillsSource: skill_registry has no get_invoked_skills(); skipping"
            )
            return []

        try:
            skills = get_invoked()  # 调用方法获取已激活技能列表
        except Exception as exc:
            logger.warning("ActiveSkillsSource: get_invoked_skills() failed: %s", exc)
            return []

        if not skills:
            return []

        # 按 invoked_at 倒序排列（最近激活的技能优先回注）
        try:
            sorted_skills = sorted(
                skills,
                key=lambda s: float(getattr(s, "invoked_at", 0) or 0),
                reverse=True,
            )
        except (ValueError, TypeError):
            # 排序失败就用原始顺序（收窄到排序键 float() 可能抛的类型，不吞无关异常）
            sorted_skills = list(skills)

        # 逐个构建技能附件
        attachments: list[ReinjectionAttachment] = []
        used_tokens = 0
        for skill in sorted_skills:
            name = getattr(skill, "name", None)
            content = getattr(skill, "content", None)
            if not name or not content:
                # 技能缺少必要字段，跳过
                continue
            content_str = str(content)
            # 截断到单技能 token 上限
            truncated, est_tokens = _truncate_to_tokens(
                content_str, self._max_tokens_per_skill
            )
            # 检查累计 token 是否超出总预算
            if used_tokens + est_tokens > self._total_token_budget and attachments:
                break

            # 构建附件标题（如果有路径信息就附上）
            path = getattr(skill, "path", None)
            title = (
                f"Active skill: {name}" + (f" ({path})" if path else "")
            )
            attachments.append(
                {
                    "source_name": self.SOURCE_NAME,
                    "title": title,
                    "content": truncated,
                    "estimated_tokens": est_tokens,
                }
            )
            used_tokens += est_tokens

        if attachments:
            logger.info(
                "ActiveSkillsSource: reinjected %d skill(s), ~%d tokens",
                len(attachments),
                used_tokens,
            )
        return attachments


# ─────────────────────────────────────────────
# PlanStateSource — 回注当前 plan 文件
# ─────────────────────────────────────────────

class PlanStateSource:
    """回注当前 plan 文件的正文。

    【为什么需要这个？】
    当 AI 进入"plan 模式"时，会创建一个 plan 文件来记录任务分解和执行步骤。
    压缩后，AI 可能忘记当前的 plan 内容，导致后续执行偏离计划。
    这个 source 把 plan 文件的正文重新注入，确保 AI 继续按计划执行。

    约定：``ctx.session_meta`` 中存在 key ``plan_file_path``，值为 plan 文件的绝对路径。
    （由 run_core 在 ``enter_plan_mode`` 工具成功后通过 ``Memory.set_session_meta``
    写入；``exit_plan_mode`` 提交审批时同样写入该 key。）

    若 key 不存在或文件读不到，返回空列表。
    """

    SOURCE_NAME = "plan_state"
    META_KEY = "plan_file_path"  # session_meta 中存放 plan 文件路径的 key

    def __init__(
        self,
        max_tokens: int = DEFAULT_POST_COMPACT_PLAN_MAX_TOKENS,
    ) -> None:
        self._max_tokens = max_tokens  # plan 文件的 token 上限

    def collect(self, ctx: PostCompactContext) -> list[ReinjectionAttachment]:
        """收集当前 plan 文件的内容，作为回注附件返回。

        与前两个 source 不同，这个 source 最多只返回一个附件（一个 plan 文件）。

        Args:
            ctx: 压缩后的上下文，包含 session_meta 等信息

        Returns:
            包含单个附件的列表（或空列表）
        """
        # 从 session_meta 中获取 plan 文件路径
        path = ctx.session_meta.get(self.META_KEY)
        if not isinstance(path, str) or not path:
            # 没有 plan 文件路径，说明当前不在 plan 模式，返回空
            return []

        # 读取 plan 文件内容
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
        except (OSError, IOError) as exc:
            logger.info(
                "PlanStateSource: failed to read plan file %s: %s", path, exc
            )
            return []

        # plan 文件内容为空，不需要回注
        if not raw.strip():
            return []

        # 截断到 token 上限
        content, est_tokens = _truncate_to_tokens(raw, self._max_tokens)
        attachment: ReinjectionAttachment = {
            "source_name": self.SOURCE_NAME,
            "title": f"Current plan: {os.path.basename(path)}",  # 只显示文件名
            "content": content,
            "estimated_tokens": est_tokens,
        }
        logger.info(
            "PlanStateSource: reinjected plan file (~%d tokens)", est_tokens
        )
        return [attachment]
