"""pandaren/tool/builtin — 内置工具 Factory 子包。

设计原则：
  - Factory 无状态，不持有 Registry 引用（消除循环依赖）
  - executor 的运行时依赖通过 ToolContext.metadata 传递
  - 条件判断由调用方（Assembler / Builder）负责，Factory 只管构建
"""

from .protocol import BuiltinToolFactory
from .search import SearchToolFactory
from .plan import PlanToolFactory
from .skill import SkillToolFactory
from .agent import AgentToolFactory

__all__ = [
    "BuiltinToolFactory",
    "SearchToolFactory",
    "PlanToolFactory",
    "SkillToolFactory",
    "AgentToolFactory",
]
