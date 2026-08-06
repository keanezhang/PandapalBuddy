"""pandaren/tool/builtin/protocol.py — 内置工具工厂协议。"""

from __future__ import annotations

from typing import Protocol

from ..definition.tool import Tool


class BuiltinToolFactory(Protocol):
    """内置工具工厂协议（无状态）。

    与旧 Provider 的区别：
      1. 不持有 Registry 引用（消除循环依赖）
      2. executor 的运行时依赖通过 ToolContext.metadata 传递
      3. 条件判断由调用方负责，Factory 只管构建
    """

    def create_tools(self) -> list[Tool]: ...
