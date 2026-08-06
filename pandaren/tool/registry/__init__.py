"""pandaren/tool/registry — 注册中心子包。

同时作为兼容层，对外重导出 ToolRegistry Facade。
旧代码 `from pandaren.tool.registry import ToolRegistry` 仍可正常工作。

注意：ToolRegistry 的重导出通过 __getattr__ 延迟加载，
以避免与 facade.py 之间的循环导入。
"""

from .store import ToolStore
from .discovery import DiscoveryManager
from .validator import validate_required_fields, validate_conflicts

__all__ = [
    "ToolStore",
    "DiscoveryManager",
    "validate_required_fields",
    "validate_conflicts",
    "ToolRegistry",
    "create_tool_registry",
]

# 延迟导入避免循环依赖：facade.py → registry.store → registry/__init__ → facade.py
_LAZY_IMPORTS = {"ToolRegistry", "create_tool_registry"}


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        from ..facade import ToolRegistry, create_tool_registry  # noqa: F811
        globals()["ToolRegistry"] = ToolRegistry
        globals()["create_tool_registry"] = create_tool_registry
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
