"""Skill 层：按需知识注入 + 动作执行机制。

用一次工具调用换取精准的上下文注入，让 LLM 在需要时获得正确的专项知识。
Action Skill 更进一步：自动生成带完整参数 schema 的 Tool，LLM 可一步直调。

核心类型：
  Skill          — 知识包定义（frozen，SK1 不可变）
  SkillResult    — 调用结果（frozen，allowed_tools 有安全意义）
  SkillSummary   — 摘要（注入 system prompt）
  SkillSource    — 来源枚举（BUILTIN < PROJECT < USER < PROGRAMMATIC）
  SkillType      — 类型枚举（KNOWLEDGE / ACTION）
  SkillRegistry  — 运行时管理器

桥接组件：
  SkillToolBridge — Action Skill → Tool 自动转换器

辅助工具：
  load_skill_from_file  — 从 Markdown 文件加载单个 Skill
  load_skills_from_dir  — 从目录批量加载 Skill
"""

from .models import Skill, SkillResult, SkillSummary, SkillSource, SkillType
from .registry import SkillRegistry
from .bridge import SkillToolBridge
from .loader import load_skill_from_file, load_skills_from_dir
from .exceptions import SkillRegistrationError, SkillScriptError

__all__ = [
    # 核心模型
    "Skill", "SkillResult", "SkillSummary", "SkillSource", "SkillType",
    # Registry
    "SkillRegistry",
    # 桥接
    "SkillToolBridge",
    # 加载器
    "load_skill_from_file", "load_skills_from_dir",
    # 异常
    "SkillRegistrationError", "SkillScriptError",
]
