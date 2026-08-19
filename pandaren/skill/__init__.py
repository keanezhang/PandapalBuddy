"""Skill 层：按需知识注入机制。

用一次工具调用换取精准的上下文注入，让 LLM 在需要时获得正确的专项知识。
技能脚本作为普通文件留在技能目录，由 SKILL.md content 指引 LLM 用已有
工具（bash / read_file 等）读取执行——SDK 不再代为加载脚本。

核心类型：
  Skill          — 知识包定义（frozen，SK1 不可变）
  SkillResult    — 调用结果（frozen，allowed_tools 有安全意义）
  SkillSummary   — 摘要（注入 system prompt）
  SkillSource    — 来源枚举（BUILTIN < PROJECT < USER < PROGRAMMATIC）
  SkillRegistry  — 运行时管理器

辅助工具：
  load_skill_from_file  — 从 Markdown 文件加载单个 Skill
  load_skills_from_dir  — 从目录批量加载 Skill
"""

from .models import Skill, SkillResult, SkillSummary, SkillSource
from .registry import SkillRegistry
from .loader import load_skill_from_file, load_skills_from_dir
from .exceptions import SkillRegistrationError

__all__ = [
    # 核心模型
    "Skill", "SkillResult", "SkillSummary", "SkillSource",
    # Registry
    "SkillRegistry",
    # 加载器
    "load_skill_from_file", "load_skills_from_dir",
    # 异常
    "SkillRegistrationError",
]
