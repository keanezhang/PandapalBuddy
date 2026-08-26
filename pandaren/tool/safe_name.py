"""pandaren/tool/safe_name.py — 工具名的 LLM-safe 转换。

OpenAI-compatible API 要求函数名/参数名为 ASCII，否则可能 400。
本模块保证暴露给 LLM 的 tool schema 名称恒为 ASCII。

规则：
- name 纯 ASCII → 原样返回（拼接 namespace 前缀）
- name 含非 ASCII → 使用 MD5 hash 生成确定性安全后缀
  例如 to_safe_name_parts("skill", "天气预报") → "skill_a68661fb"

为什么用 parts 而非字符串猜测：
Tool.full_name = f"{namespace}_{name}"（见 definition/tool.py），name 本身可能含下划线
（如 namespace="ns", name="my_tool_天气"）。若从 full_name 字符串 rsplit("_", 1) 猜测，
会把 "my_tool" 误当作 namespace 前缀，导致 schema 名与 store 索引不一致。
所有持有 Tool 对象的调用点必须用 to_safe_name_parts(namespace, name) 精确计算，
保证 LLM 回传的 schema 名能经 ToolStore._safe_name_index 反查到真实工具。
"""

from __future__ import annotations

import hashlib


def to_safe_name_parts(namespace: str | None, name: str) -> str:
    """将 (namespace, name) 转换为 LLM-safe 的 ASCII 名称。

    Args:
        namespace: 工具命名空间，可为 None（无命名空间工具）。
        name: 工具名（不含命名空间部分）。

    Returns:
        ASCII-only 的安全名称。相同输入始终返回相同输出（确定性）。
        namespace 若含非 ASCII，同样 hash 化，保证输出恒为 ASCII。

    Examples:
        >>> to_safe_name_parts(None, "search_tools")
        'search_tools'
        >>> to_safe_name_parts("skill", "天气预报")
        'skill_a68661fb'
    """
    ns_part = ""
    if namespace:
        ns_part = (
            namespace
            if namespace.isascii()
            else hashlib.md5(namespace.encode("utf-8")).hexdigest()[:8]
        )

    if name.isascii():
        result = f"{ns_part}_{name}" if ns_part else name
    else:
        hash_suffix = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
        result = f"{ns_part}_{hash_suffix}" if ns_part else hash_suffix

    return result
