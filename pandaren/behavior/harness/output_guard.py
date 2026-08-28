"""pandaren/behavior/harness/output_guard.py — R2 输出大小控制

超出 max_output_bytes → 截断数据并附加说明（不丢弃）。
截断后设置 truncated=True，并触发 on_tool_output_truncated hook。

hooks 由 HarnessExecutor.set_hooks() 注入（统一 AgentHooks 协议），注入后不可替换。
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace as dc_replace

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
        # inv-OG-1：截断点按「数据前缀 + 截断提示」经消费方 json 序列化后的总长收敛到
        # max_bytes 内——二分直接以最终序列化长度为判据（json.dumps 加两端引号并转义
        # 内部字符，只按原始编码长度截会多出序列化开销，见 OG-01 known-gap 转正）。
        truncation_notice = (
            f"\n[输出已截断，原始大小 {data_bytes} 字节，"
            f"上限 {max_bytes} 字节，请缩小查询范围]"
        )

        def _fits(data_prefix: str) -> bool:
            return (
                len(json.dumps(data_prefix + truncation_notice, ensure_ascii=False).encode("utf-8"))
                <= max_bytes
            )

        if not _fits(""):
            # 极端：提示本身经序列化已超上限 → 仅保留提示（字节级截断，不切断多字节字符）
            truncated_chars = ""
            truncation_notice = (
                truncation_notice.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
            )
        else:
            lo, hi = 0, len(serialized)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if _fits(serialized[:mid]):
                    lo = mid
                else:
                    hi = mid - 1
            truncated_chars = serialized[:lo]

        # 截断事件同步触发 hook（用原对象字段，dc_replace 后引用一致）
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

        # inv-EX-5 / inv-OG-2：截断产出**新对象**，绝不就地改写原对象。
        # 原对象可能已被 R4 幂等缓存引用，就地 mutation 会让后续命中返回被污染的结果
        # （截断提示叠加/truncated 标志永久焊死），缓存与观测全部失真。
        return dc_replace(
            result,
            data=truncated_chars + truncation_notice,
            truncated=True,
        )
