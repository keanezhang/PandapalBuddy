"""子 Agent 基础设施：蓝图定义、加载、注册、委派。

与 tool/ skill/ 对称的三件套设计：
  Tool  = 我能做什么（原子操作）
  Skill = 我能知道什么（知识注入）
  Agent = 我能委托谁（任务委派）

核心类型：
  SubAgentSummary          — 摘要（注入 system prompt）
  SubAgentDelegateResult   — 委派执行结果
  SubAgentBlueprint        — 蓝图（从 .agent/ 目录加载的中间结构）
  SubAgentSource           — 来源枚举（DIRECTORY < PROGRAMMATIC）
  SubAgentRegistry         — 运行时管理器

辅助工具：
  load_agent_from_file  — 从 Markdown 文件加载单个 SubAgentBlueprint
  load_agents_from_dir  — 从目录批量加载 SubAgentBlueprint
"""

from .models import (
    SubAgentSummary, SubAgentDelegateResult,
    SubAgentBlueprint, SubAgentSource,
)
from .registry import SubAgentRegistry
from .loader import load_agent_from_file, load_agents_from_dir
from .exceptions import SubAgentRegistrationError

__all__ = [
    "SubAgentSummary", "SubAgentDelegateResult",
    "SubAgentBlueprint", "SubAgentSource",
    "SubAgentRegistry",
    "load_agent_from_file", "load_agents_from_dir",
    "SubAgentRegistrationError",
]
