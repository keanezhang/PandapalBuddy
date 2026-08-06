"""Config 层专用异常。

遵循 BL5（语义异常）原则 + SDK5（可操作错误信息）原则。
每个异常类名直接表达失败场景，ConfigValidationError 必须包含 suggestion 字段。
"""

from __future__ import annotations

from typing import Any

# 敏感字段名
_SENSITIVE_FIELD_NAMES = frozenset({"llm_api_key", "relay_auth_token"})


class ConfigValidationError(Exception):
    """字段级校验错误，用户通过修改配置可修复。

    SDK5 要求：suggestion 必填，直接告诉用户怎么修。
    """

    def __init__(
        self,
        field_name: str,
        value: Any,
        reason: str,
        suggestion: str,
    ) -> None:
        self.field_name = field_name
        self.value = value
        self.reason = reason
        self.suggestion = suggestion
        super().__init__(
            f"Config validation error [{self.field_name}]: {self.reason}"
        )

    def __str__(self) -> str:
        return f"[{self.field_name}] {self.reason} (suggestion: {self.suggestion})"

    def __repr__(self) -> str:
        """Fix #7: 覆写 __repr__，对敏感字段的 value 做 mask，防止日志泄露。"""
        display_value = self.value
        if self.field_name in _SENSITIVE_FIELD_NAMES:
            display_value = "***"
        return (
            f"ConfigValidationError(field_name={self.field_name!r}, "
            f"value={display_value!r}, reason={self.reason!r}, "
            f"suggestion={self.suggestion!r})"
        )


class ConfigFileError(Exception):
    """YAML 解析错误或文件不存在。"""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"Config file error ({path}): {reason}")


class ConfigStorageError(Exception):
    """Storage 层故障（SQLite 不可用等）。"""

    def __init__(self, cause: Exception) -> None:
        self.cause = cause
        super().__init__(f"Config storage error: {cause}")


class ConfigLoadError(Exception):
    """load_config() 校验失败异常（包含所有字段级错误）。"""

    def __init__(self, errors: list[ConfigValidationError]) -> None:
        self.errors = errors
        msg_parts = [str(e) for e in errors]
        super().__init__(f"Config load failed: {'; '.join(msg_parts)}")


