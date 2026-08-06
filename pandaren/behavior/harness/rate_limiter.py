"""pandaren/behavior/harness/rate_limiter.py — R1 调用频率控制

每个 turn 开始时 reset_turn()，
每次 execute_tool 前 check()，
超出 max_calls_per_turn → 返回拒绝 ToolResult。
"""

from __future__ import annotations

from ...tool.definition.tool_result import ToolResult


class RateLimiter:
    """Turn 级调用频率控制。"""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}  # tool_name → 本轮调用次数

    def check(self, tool_name: str, max_calls: int | None) -> ToolResult | None:
        """检查是否超出调用频率限制。

        返回 None 表示通过，否则返回拒绝 ToolResult。
        无论是否有限制，都要计数（供观测层使用）。
        """
        current = self._counters.get(tool_name, 0)

        if max_calls is not None and current >= max_calls:
            return ToolResult(
                success=False,
                error=f"Tool '{tool_name}' 已达本轮调用上限（{max_calls}次）",
                tool_name=tool_name,
            )

        self._counters[tool_name] = current + 1
        return None

    def reset_turn(self) -> None:
        """Turn 结束时重置所有计数器。"""
        self._counters.clear()

    def get_count(self, tool_name: str) -> int:
        """获取当前 turn 内某工具的调用次数（观测用）。"""
        return self._counters.get(tool_name, 0)
