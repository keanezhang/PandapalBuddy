"""pandaren/engine/step_counter.py — 步数计数器（HC5-COUNTER）

只增不减，不提供 reset / set_max。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("pandaren.engine.step_counter")


class StepCounter:
    """步数计数器。只能递增，不能重置，不能修改上限。"""

    __slots__ = ("_count", "_max")

    def __init__(self, max_steps: int) -> None:
        if max_steps <= 0:
            raise ValueError(f"max_steps 必须 > 0，当前值: {max_steps}")
        object.__setattr__(self, "_count", 0)
        object.__setattr__(self, "_max", max_steps)

    def __setattr__(self, name: str, value: object) -> None:
        raise PermissionError(
            f"StepCounter 是不可变对象，禁止直接修改字段 '{name}'。"
            f"只能通过 increment() 递增计数。"
        )

    def __delattr__(self, name: str) -> None:
        raise PermissionError(
            f"StepCounter 字段 '{name}' 不可删除。"
        )

    def increment(self) -> bool:
        """递增计数。返回 True 表示未到上限可继续，False 表示已到上限。"""
        new_count = object.__getattribute__(self, "_count") + 1
        object.__setattr__(self, "_count", new_count)
        return new_count <= object.__getattribute__(self, "_max")

    @property
    def count(self) -> int:
        return object.__getattribute__(self, "_count")

    @property
    def max_steps(self) -> int:
        return object.__getattribute__(self, "_max")

    @property
    def remaining(self) -> int:
        return max(0, self.max_steps - self.count)

    def __repr__(self) -> str:
        return f"StepCounter(count={self.count}/{self.max_steps})"
