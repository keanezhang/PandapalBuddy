"""pandaren/observability/sanitizer.py — 数据脱敏

E4 Fail-Safe：脱敏失败时返回 "[SANITIZE_ERROR]"，不暴露原始敏感数据。

用法示例：
    sanitizer = DefaultSanitizer(
        global_patterns=[
            (r"sk-[a-zA-Z0-9]{48}", "[API_KEY]"),                          # OpenAI API key
            (r"[0-9]{4}[-]?[0-9]{4}[-]?[0-9]{4}[-]?[0-9]{4}", "[CARD_NUMBER]"),  # 信用卡号
            (r"Bearer[\\s]+[A-Za-z0-9._~+/]+=*", "[BEARER_TOKEN]"),        # Bearer token
        ],
        field_patterns={
            "llm_input": [(r".", "*")],   # LLM input 全部掩码
        },
    )
    clean = sanitizer.sanitize("sk-abc...xyz", field_name="api_key")
"""

from __future__ import annotations

import re


class DefaultSanitizer:
    """默认脱敏器：基于正则的字段级脱敏。

    支持两种脱敏模式：
      1. 全局模式（global_patterns）：对所有字段应用相同的正则替换规则
      2. 字段级模式（field_patterns）：不同字段名使用不同的脱敏规则

    字段级规则优先于全局规则。

    E4 Fail-Safe：
      - 任何异常均静默捕获，返回 "[SANITIZE_ERROR]"
      - 不会将原始敏感数据透传给调用方
    """

    def __init__(
        self,
        global_patterns: list[tuple[str, str]] | None = None,
        field_patterns: dict[str, list[tuple[str, str]]] | None = None,
    ) -> None:
        """初始化脱敏器。

        Args:
            global_patterns: 全局正则规则列表，每项为 (pattern, replacement)。
                             pattern 是正则表达式字符串，replacement 是替换文本。
            field_patterns:  字段级规则字典，key 为字段名，value 为该字段的规则列表。
                             字段名匹配时，只应用字段级规则，跳过全局规则。
        """
        self._global_patterns: list[tuple[re.Pattern[str], str]] = [
            (re.compile(p), r) for p, r in (global_patterns or [])
        ]
        self._field_patterns: dict[str, list[tuple[re.Pattern[str], str]]] = {
            field: [(re.compile(p), r) for p, r in patterns]
            for field, patterns in (field_patterns or {}).items()
        }

    def sanitize(self, data: str, field_name: str = "") -> str:
        """对数据执行脱敏处理。

        优先级：字段级规则 > 全局规则。
        字段级规则命中时，跳过全局规则（避免双重替换）。

        Args:
            data:       原始数据字符串
            field_name: 字段名（可选），用于匹配字段级规则

        Returns:
            脱敏后的字符串。
            若发生任何异常，返回 "[SANITIZE_ERROR]"（E4 Fail-Safe）。
        """
        try:
            result = data
            # 字段级规则优先
            if field_name and field_name in self._field_patterns:
                for pattern, replacement in self._field_patterns[field_name]:
                    result = pattern.sub(replacement, result)
                return result
            # 全局规则
            for pattern, replacement in self._global_patterns:
                result = pattern.sub(replacement, result)
            return result
        except Exception:
            return "[SANITIZE_ERROR]"

    def __repr__(self) -> str:
        return (
            f"DefaultSanitizer("
            f"global_patterns={len(self._global_patterns)}, "
            f"field_patterns={list(self._field_patterns.keys())})"
        )
