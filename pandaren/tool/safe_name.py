"""pandaren/tool/safe_name.py — 工具名称安全化工具。

将包含非 ASCII 字符（如中文）的工具名称转换为 ASCII-only 的安全名称，
确保兼容 DeepSeek 等对 tool name 有 ASCII-only 限制的 LLM provider。

规则：
- 纯 ASCII 名称 → 原样返回
- 包含非 ASCII → 使用 MD5 hash 生成确定性安全名称
  例如 "skill.天气预报" → "skill.e4d7f2a1"
"""

from __future__ import annotations

import hashlib


def to_safe_name(full_name: str) -> str:
    """将工具全名转换为 LLM-safe 的 ASCII 名称。

    如果名称已是纯 ASCII，原样返回；否则使用 namespace + hash 生成安全名称。

    Args:
        full_name: 工具全名，格式为 "namespace.name" 或 "name"。

    Returns:
        ASCII-only 的安全名称。相同输入始终返回相同输出（确定性）。

    Examples:
        >>> to_safe_name("search_tools")
        'search_tools'
        >>> to_safe_name("skill.天气预报")
        'skill.e4d7f2a1'
    """
    if full_name.isascii():
        return full_name

    # 分离 namespace 和 name
    parts = full_name.rsplit("_", 1)
    if len(parts) == 2:
        namespace, original = parts
        # 对原始名称取 MD5 前 8 位作为安全后缀
        hash_suffix = hashlib.md5(original.encode("utf-8")).hexdigest()[:8]
        return f"{namespace}_{hash_suffix}"
    else:
        # 无 namespace，直接用 hash
        hash_suffix = hashlib.md5(full_name.encode("utf-8")).hexdigest()[:8]
        return hash_suffix
