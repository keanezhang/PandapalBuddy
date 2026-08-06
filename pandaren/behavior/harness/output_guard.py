"""pandaren/behavior/harness/output_guard.py — R2 输出大小控制

超出 max_output_bytes → 截断数据并附加说明（不丢弃）。
截断后设置 truncated=True，并触发 on_tool_output_truncated hook。

hooks 由 HarnessExecutor.set_hooks() 注入（统一 AgentHooks 协议），注入后不可替换。
"""

from __future__ import annotations

import json
import logging

from ...tool.definition.tool_result import ToolResult
from ...hook import AgentHooks

logger = logging.getLogger("pandaren.behavior.harness.output_guard")


class OutputGuard:
    """输出大小控制。"""

    def __init__(self) -> None:
        self._hooks: AgentHooks | None = None

    def set_hooks(self, hooks: AgentHooks) -> None:
        """注入 hooks（由 HarnessExecutor.set_hooks 统一调用）。"""
        self._hooks = hooks

    def check(self, result: ToolResult, max_bytes: int) -> ToolResult:
        """检查并截断超限输出。截断时同步触发 on_tool_output_truncated hook。"""
        if not result.data:
            return result

        # 序列化计算大小
        try:
            serialized = json.dumps(result.data, ensure_ascii=False)
        except (TypeError, ValueError):
            serialized = str(result.data)

        data_bytes = len(serialized.encode("utf-8"))

        if data_bytes <= max_bytes:
            return result

        # 字符级截断，避免切断多字节字符（如中文）
        # 使用二分查找高效定位截断点
        lo, hi = 0, len(serialized)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(serialized[:mid].encode("utf-8")) <= max_bytes:
                lo = mid
            else:
                hi = mid - 1
        truncated_chars = serialized[:lo]
        truncation_notice = (
            f"\n[输出已截断，原始大小 {data_bytes} 字节，"
            f"上限 {max_bytes} 字节，请缩小查询范围]"
        )

        result.data = truncated_chars + truncation_notice
        result.truncated = True

        # 截断事件同步触发 hook
        if self._hooks:
            self._hooks.on_tool_output_truncated(
                tool_name=result.tool_name,
                original_size=data_bytes,
                max_size=max_bytes,
            )
        else:
            logger.warning(
                "工具 '%s' 输出被截断: %d 字节 → %d 字节（hooks 未注入）",
                result.tool_name, data_bytes, max_bytes,
            )

        return result
