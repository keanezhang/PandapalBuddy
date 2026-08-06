"""pandaren.tools — SDK 内置通用工具集

提供开箱即用的纯本地工具，不依赖外部服务或 API Key：

    from pandaren.tools import get_builtin_tools

    agent = (
        AgentBuilder()
        .identity(...)
        .llm(...)
        .tools(get_builtin_tools())
        .build()
    )

内置工具列表（共 11 个）：
  文件操作（file_tool/）：
    - read_file   — 读取文件（文本/图片/Notebook/PDF）
    - write_file  — 创建或覆盖文件
    - edit_file   — 精确字符串替换
    - delete_file — 安全删除文件（默认进回收站）
    - list_files  — 目录浏览
  搜索：
    - glob        — 文件名搜索（通配符匹配）
    - grep        — 内容搜索（正则表达式）
  系统：
    - bash                — Shell 命令执行
    - time_get_current_time — 获取当前时间
  工具：
    - math_calculator     — 安全数学表达式求值
  交互：
    - ask_user            — 向用户发起结构化提问
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pandaren.tool import Tool


def get_builtin_tools() -> "list[Tool]":
    """返回 SDK 内置的通用工具列表。

    可直接传入 AgentBuilder.tools()：

        from pandaren.tools import get_builtin_tools
        builder.tools(get_builtin_tools())

    Returns:
        所有内置 Tool 对象的列表。
    """
    from .file_tool import read_file, write_file, edit_file, delete_file, list_files
    from .glob import glob
    from .grep import grep
    from .bash import bash
    from .time import time_get_current_time
    from .math_calculator import math_calculator
    from .ask_user import ask_user_tool

    return [
        read_file,
        write_file,
        edit_file,
        delete_file,
        list_files,
        glob,
        grep,
        bash,
        time_get_current_time,
        math_calculator,
        ask_user_tool,
    ]


# ── 便捷导出 ──

from .file_tool import (  # noqa: E402
    read_file, write_file, edit_file, delete_file, list_files,
)
from .glob import glob  # noqa: E402
from .grep import grep  # noqa: E402
from .bash import bash  # noqa: E402
from .time import time_get_current_time  # noqa: E402
from .math_calculator import math_calculator  # noqa: E402
from .ask_user import ask_user_tool  # noqa: E402

__all__ = [
    "get_builtin_tools",
    "read_file",
    "write_file",
    "edit_file",
    "delete_file",
    "list_files",
    "glob",
    "grep",
    "bash",
    "time_get_current_time",
    "math_calculator",
    "ask_user_tool",
]
