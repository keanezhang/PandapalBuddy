"""pandaren/behavior/error_policy.py — 错误处理策略声明

指数退避公式：delay = min(base_delay_s * 2^attempt, max_delay_s)
"""

from __future__ import annotations

import math
import logging

from .exceptions import BehaviorConfigError

logger = logging.getLogger("pandaren.behavior.error_policy")

# ── 默认值常量 ─────────────────────────────────────────────────────────────────
DEFAULT_MAX_RETRIES: int = 3       # LLM 调用失败后最多重试次数
DEFAULT_BASE_DELAY_S: float = 1.0  # 指数退避的初始等待时间（秒）
DEFAULT_MAX_DELAY_S: float = 30.0  # 指数退避的等待时间上限（秒）


class ErrorPolicy:
    """LLM 调用失败重试策略声明。创建后所有字段冻结。"""

    __slots__ = ("_max_retries", "_base_delay_s", "_max_delay_s")

    def __init__(
        self,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_delay_s: float = DEFAULT_BASE_DELAY_S,
        max_delay_s: float = DEFAULT_MAX_DELAY_S,
    ) -> None:
        if not isinstance(max_retries, int) or max_retries < 0:
            raise BehaviorConfigError(
                f"error_policy.max_retries 必须是非负整数，当前值: {max_retries!r}"
            )
        if base_delay_s <= 0:
            raise BehaviorConfigError(
                f"error_policy.base_delay_s 必须 > 0，当前值: {base_delay_s}"
            )
        if max_delay_s < base_delay_s:
            raise BehaviorConfigError(
                f"error_policy.max_delay_s ({max_delay_s}s) 不能小于 "
                f"base_delay_s ({base_delay_s}s)"
            )

        object.__setattr__(self, "_max_retries", max_retries)
        object.__setattr__(self, "_base_delay_s", base_delay_s)
        object.__setattr__(self, "_max_delay_s", max_delay_s)

    def __setattr__(self, name: str, value: object) -> None:
        raise PermissionError(
            f"ErrorPolicy 是不可变对象，禁止修改字段 '{name}'。"
        )

    def __delattr__(self, name: str) -> None:
        raise PermissionError(
            f"ErrorPolicy 是不可变对象，禁止删除字段 '{name}'。"
        )

    @property
    def max_retries(self) -> int:
        return object.__getattribute__(self, "_max_retries")

    @property
    def base_delay_s(self) -> float:
        return object.__getattribute__(self, "_base_delay_s")

    @property
    def max_delay_s(self) -> float:
        return object.__getattribute__(self, "_max_delay_s")

    def calculate_delay(self, attempt: int) -> float:
        """计算第 attempt 次重试的等待时间（秒）。"""
        if attempt < 0:
            raise ValueError(f"attempt 必须 >= 0，当前值: {attempt}")
        delay = object.__getattribute__(self, "_base_delay_s") * math.pow(2, attempt)
        return min(delay, object.__getattribute__(self, "_max_delay_s"))

    def __repr__(self) -> str:
        return (
            f"ErrorPolicy(max_retries={self.max_retries}, "
            f"base_delay_s={self.base_delay_s}s, "
            f"max_delay_s={self.max_delay_s}s)"
        )
