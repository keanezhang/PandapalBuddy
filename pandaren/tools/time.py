"""pandaren/tools/time.py — 时间获取内置工具"""

import datetime

from pandaren.tool.types import ToolTier, SensitivityLevel
from pandaren.tool import ToolContext
from pandaren.tool.definition.tool_policy import ToolPolicy
from pandaren.tool.decorator import tool


@tool.function(
    tier=ToolTier.ALWAYS,
    name="time_get_current_time",
    description="获取当前系统时间",
    when_to_use="当用户询问当前时间或日期时调用",
    policy=ToolPolicy(
        sensitivity=SensitivityLevel.LOW,
        is_reversible=True,
        audit_required=False,
        is_idempotent=True,
    ),
)
def time_get_current_time(ctx: ToolContext) -> str:
    """获取当前系统时间。

    Args:
        ctx: 工具上下文。

    Returns:
        格式化的当前日期时间字符串，包含年月日、时分秒及星期。
    """
    now = datetime.datetime.now()
    return f"当前时间：{now.strftime('%Y年%m月%d日 %H:%M:%S')}（星期{'一二三四五六日'[now.weekday()]}）"
