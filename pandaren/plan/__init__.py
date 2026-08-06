"""pandaren/plan/ — Plan Mode 模块

提供 Plan Mode 的完整实现：
  - tools.py:     enter_plan_mode / write_plan / exit_plan_mode 三个内置工具
  - manager.py:   PlanManager 类（单一管理入口）
  - files.py:     文件操作纯函数
  - prompt.py:    所有提示文案常量

设计原则：
  - 工具只读 ToolContext（B2），不直接做 IO 副作用
  - 副作用（写 Memory meta、发 StreamEvent、终止 run）由 run_core 统一处理
  - PlanManager 封装所有规划状态，run_core 只通过其公开接口交互
"""

from .tools import (
    build_plan_mode_tools,
    ENTER_PLAN_MODE_NAME,
    WRITE_PLAN_NAME,
    EXIT_PLAN_MODE_NAME,
    PLAN_MODE_BUILTIN_TOOLS,
)
from .files import (
    generate_plan_file_path,
    validate_plan_file_path,
    write_plan_content,
    read_plan,
    plan_exists,
)
from .manager import PlanManager

__all__ = [
    # tools
    "build_plan_mode_tools",
    "ENTER_PLAN_MODE_NAME",
    "WRITE_PLAN_NAME",
    "EXIT_PLAN_MODE_NAME",
    "PLAN_MODE_BUILTIN_TOOLS",
    # files
    "generate_plan_file_path",
    "validate_plan_file_path",
    "write_plan_content",
    "read_plan",
    "plan_exists",
    # manager
    "PlanManager",
]
