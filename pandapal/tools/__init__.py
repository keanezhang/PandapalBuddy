"""pandapal.tools — 工具自动发现与加载。

约定：每个 *_tools.py 模块导出一个 get_xxx_tools() -> list[Tool] 函数。
调用 get_all_tools() 会扫描本目录所有符合约定的模块并汇总返回。
"""

from __future__ import annotations

import importlib
import pkgutil
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pandaren.tool import Tool

logger = logging.getLogger(__name__)


def get_all_tools() -> "list[Tool]":
    """自动发现并加载 pandapal/tools/ 下所有工具模块。

    扫描规则：
    - 模块名以 '_tools' 结尾的 .py 文件（如 file_tools, calendar_tools）
    - 在模块中查找 'get_xxx_tools' 函数（名称匹配 'get_*_tools' 模式）
    - 调用该函数收集返回的 Tool 列表

    Returns:
        所有工具模块返回的 Tool 对象的合并列表。
    """
    all_tools: list[Tool] = []

    package_path = __path__  # type: ignore[name-defined]
    package_name = __name__

    for module_info in pkgutil.iter_modules(package_path):
        # 只加载以 _tools 结尾的模块
        if not module_info.name.endswith("_tools"):
            continue

        try:
            module = importlib.import_module(f"{package_name}.{module_info.name}")
        except Exception as e:
            logger.warning("Failed to import tool module '%s': %s", module_info.name, e)
            continue

        # 查找 get_*_tools 函数
        for attr_name in dir(module):
            if attr_name.startswith("get_") and attr_name.endswith("_tools") and callable(getattr(module, attr_name)):
                try:
                    tools = getattr(module, attr_name)()
                    if isinstance(tools, list):
                        all_tools.extend(tools)
                        logger.debug(
                            "Loaded %d tools from %s.%s()",
                            len(tools), module_info.name, attr_name,
                        )
                except Exception as e:
                    logger.warning(
                        "Failed to call %s.%s(): %s",
                        module_info.name, attr_name, e,
                    )

    logger.info("get_all_tools(): loaded %d tools from %d modules", len(all_tools), len([
        m for m in pkgutil.iter_modules(package_path) if m.name.endswith("_tools")
    ]))
    return all_tools
