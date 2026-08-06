"""pandaren/tool/registry/store.py — 纯工具存储。

只做 CRUD + 名称唯一性校验，不含过滤/暴露/执行逻辑。
"""

from __future__ import annotations

import logging

from ..definition.tool import Tool
from ..types import ToolTier
from ..exceptions import ToolRegistrationError
from ..safe_name import to_safe_name
from .validator import validate_required_fields, validate_conflicts

logger = logging.getLogger("pandaren.tool.registry.store")


class ToolStore:
    """纯工具存储。只做 CRUD + 名称唯一性校验。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._namespace_registry: set[str] = set()
        # safe_name → full_name 反向索引（支持 LLM 用安全名称回调）
        self._safe_name_index: dict[str, str] = {}
        self._version: int = 0

    def register(self, tool: Tool, *, skip_if_exists: bool = False) -> None:
        """注册工具。

        流程：
          1. REQUIRED_FIELDS 校验
          2. CONFLICT_CHECKS 矛盾检测（可能自动修正 policy）
          3. 命名空间 + 名称唯一性检查
          4. 写入 _tools 字典

        Args:
            tool: 要注册的工具定义。
            skip_if_exists: 若为 True，同名工具已注册时静默跳过。
        """
        # Step 1: 必填字段校验
        validate_required_fields(tool)

        # Step 2: 矛盾检测 + 自动修正
        tool = validate_conflicts(tool)

        # Step 3: 名称唯一性检查
        full_name = tool.full_name
        if full_name in self._tools:
            if skip_if_exists:
                logger.debug("工具 '%s' 已注册，跳过重复注册", full_name)
                return
            raise ToolRegistrationError(
                f"工具 '{full_name}' 已注册。"
                f"如需注册同名工具，请使用不同的 namespace"
            )

        # Step 4: 命名空间注册
        if tool.namespace:
            self._namespace_registry.add(tool.namespace)

        # Step 5: 写入（最关键步骤：_tools 字典存储所有工具引用）
        self._tools[full_name] = tool

        # Step 6: 维护 safe_name → full_name 反向索引（用于 LLM 回调时的名称解析）
        safe_name = to_safe_name(full_name)
        if safe_name != full_name:
            self._safe_name_index[safe_name] = full_name

        logger.debug(
            "工具已注册: %s [tier=%s, sensitivity=%s, namespace=%s]",
            full_name, tool.tier.name, tool.sensitivity.name, tool.namespace,
        )
        self._version += 1

    @property
    def version(self) -> int:
        return self._version

    def get(self, name: str) -> Tool | None:
        """获取已注册的工具定义（只读）。

        支持两种名称格式：
          - 原始全名（如 "skill.天气预报"）
          - LLM-safe 名称（如 "skill.e4d7f2a1"）
        """
        tool = self._tools.get(name)
        if tool is not None:
            return tool
        # 尝试通过 safe_name 索引查找
        full_name = self._safe_name_index.get(name)
        if full_name is not None:
            return self._tools.get(full_name)
        return None

    def unregister(self, name: str) -> bool:
        """注销工具。支持原始全名和 safe_name 两种格式。

        Returns:
            True 表示成功注销，False 表示工具不存在。
        """
        full_name = self._safe_name_index.pop(name, None)
        if full_name is None:
            # 可能传入的就是原始全名
            if name in self._tools:
                full_name = name
            else:
                return False

        # 也清理 safe_name → full_name 的反向索引
        safe_name = to_safe_name(full_name)
        if safe_name != full_name:
            self._safe_name_index.pop(safe_name, None)

        del self._tools[full_name]

        # 清理命名空间（如果该 ns 下没有其他工具了）
        ns = full_name.split(".", 1)[0] if "." in full_name else ""
        if ns and ns in self._namespace_registry:
            still_used = any(
                t.namespace == ns for t in self._tools.values()
            )
            if not still_used:
                self._namespace_registry.discard(ns)

        logger.debug("工具已注销: %s", full_name)
        self._version += 1
        return True

    def list_all(self) -> list[Tool]:
        """列出所有已注册工具。"""
        return list(self._tools.values())

    def list_names(self) -> list[str]:
        """列出所有已注册工具名。"""
        return list(self._tools.keys())

    def list_by_tier(self, tier: ToolTier) -> list[Tool]:
        """按 tier 过滤工具。"""
        return [t for t in self._tools.values() if t.tier == tier]

    def list_by_tags(self, tags: set[str]) -> list[Tool]:
        """按 tags 过滤（交集匹配）。"""
        return [
            t for t in self._tools.values()
            if set(t.tags) & tags
        ]

    def items(self) -> list[tuple[str, Tool]]:
        """返回 (full_name, tool) 列表。"""
        return list(self._tools.items())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools or name in self._safe_name_index
