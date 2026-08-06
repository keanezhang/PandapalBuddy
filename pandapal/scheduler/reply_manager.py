"""pandapal.scheduler.reply_manager — reply_id 统一管理。

★ 5.2.C 核心设计：
reply_id 标识"一次完整的回复周期"，从 REPLY_START 到 REPLY_END 的所有事件
共享同一个 reply_id。

命名规范：
- 普通对话：reply_id == run_id（保持现有约定）
- HITL 恢复：reply_id == resume_reply_id（透传原始值，不变）
- 非流式系统消息：reply_id == "ns:{scope}:{uuid_hex[:8]}"（带 ns: 前缀）
- 错误降级：reply_id == error_reply_id（特殊前缀）

★ 收益：
- 传错 reply_id 类型时编译期报错（强类型 ReplyId）
- Scheduler 不再有散落的 str(uuid.uuid4())
- 透传规则明确（resume 时必须 == 原始 reply_id）
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import Enum


class ReplyScope(str, Enum):
    """reply_id 的语义范围。"""
    NORMAL      = "normal"            # reply_id == run_id
    HITL_RESUME = "hitl_resume"        # 透传 resume_reply_id
    SYSTEM      = "system"             # ns:system:xxxxxx
    TASK        = "task"               # ns:task:xxxxxx
    ERROR       = "error"              # ns:error:xxxxxx


# reply_id 格式校验正则
# - r- 前缀：run_id 形式（r-7f8a, r-7f8a1234abcd 等）
# - ns: 前缀：ns:scope:hex 形式
_REPLY_ID_PATTERN = re.compile(
    r"^(r-[a-zA-Z0-9_-]{4,64})"
    r"|(ns:[a-z_]+:[a-f0-9]{4,32})$"
)


@dataclass(frozen=True)
class ReplyId:
    """强类型 reply_id（取代裸 str）。

    ★ 收益：传错 reply_id 类型时编译期报错。
    """
    value: str
    scope: ReplyScope

    def __str__(self) -> str:
        return self.value


class ReplyIdManager:
    """统一 reply_id 管理（取代 Scheduler 内零散的 UUID 生成）。

    用法：
        mgr = ReplyIdManager()
        rid = mgr.new_run_reply(run_id="r-7f8a")          # ReplyId(value="r-7f8a")
        rid = mgr.resume_reply(original="r-7f8a")         # ReplyId(value="r-7f8a") 透传
        rid = mgr.system_reply(scope=ReplyScope.TASK)     # ReplyId(value="ns:task:abcd1234")

        # 在 NormalizedEvent 中使用
        event = NormalizedEvent(event_type=EventType.LLM_TOKEN, reply_id=rid.value, ...)
    """

    def new_run_reply(self, run_id: str) -> ReplyId:
        """新对话开始：reply_id == run_id（约定）。"""
        if not run_id:
            raise ValueError("run_id cannot be empty")
        return ReplyId(value=run_id, scope=ReplyScope.NORMAL)

    def resume_reply(self, original_reply_id: str) -> ReplyId:
        """HITL/Interaction 恢复：透传原始 reply_id。

        ★ 重要：Option C 约定的核心 —— Agent 恢复时，前端能继续看到原 reply。
        """
        if not original_reply_id:
            raise ValueError("original_reply_id cannot be empty")
        if not _REPLY_ID_PATTERN.match(original_reply_id):
            raise ValueError(
                f"invalid reply_id format: {original_reply_id!r}. "
                f"Expected 'r-*' or 'ns:scope:hex'"
            )
        return ReplyId(value=original_reply_id, scope=ReplyScope.HITL_RESUME)

    def system_reply(self, scope: ReplyScope) -> ReplyId:
        """非流式系统消息：ns:{scope}:{hex} 形式。

        前缀 ns: 让前端能识别"这不是 Agent 真实回复，是系统通知"。
        """
        if scope not in (ReplyScope.SYSTEM, ReplyScope.TASK, ReplyScope.ERROR):
            raise ValueError(
                f"system_reply requires SYSTEM/TASK/ERROR, got {scope}"
            )
        value = f"ns:{scope.value}:{uuid.uuid4().hex[:8]}"
        return ReplyId(value=value, scope=scope)

    def parse(self, reply_id: str) -> ReplyId:
        """从字符串解析（用于从 InboundMessage 中恢复 ReplyId）。"""
        if reply_id.startswith("ns:"):
            parts = reply_id.split(":", 2)
            if len(parts) == 3:
                return ReplyId(value=reply_id, scope=ReplyScope(parts[1]))
        return ReplyId(value=reply_id, scope=ReplyScope.NORMAL)
