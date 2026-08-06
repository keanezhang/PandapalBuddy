"""TaskScheduler 层数据模型。

TriggerRule 序列化为 JSON 存入 TaskDefinition.trigger_rule_json，
TaskScheduler 负责序列化/反序列化；Storage 层不解析 trigger_rule 内部结构。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TriggerType(str, Enum):
    """任务触发类型。"""

    RECURRING = "recurring"  # 周期性触发
    ONESHOT = "oneshot"      # 一次性触发，触发后自动注销
    EVENT = "event"
    MANUAL = "manual"


@dataclass(frozen=True)
class TriggerRule:
    """任务触发规则（frozen dataclass，序列化为 JSON 持久化）。

    字段：
        trigger_type:     触发类型（RECURRING / ONESHOT / EVENT / MANUAL）
        cron_expression:  cron 表达式（trigger_type=RECURRING 或 ONESHOT 时必填）
        event_name:       事件名称（trigger_type=EVENT 时必填）
    """

    trigger_type: TriggerType
    cron_expression: str | None = None  # TriggerType.RECURRING / ONESHOT 时使用
    event_name: str | None = None       # TriggerType.EVENT 时使用
