"""pandaren/engine/models.py — 核心数据模型：AgentResult, RunState, StepRecord"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .types import TerminalReason


@dataclass
class StepRecord:
    """单步执行记录（trace 用）。"""
    step_n: int = 0
    duration_ms: float = 0.0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    tool_calls: list[str] = field(default_factory=list)
    error: str | None = None
    permission_denied: bool = False
    hitl_requested: bool = False


@dataclass
class RunState:
    """HITL PAUSE 时的状态快照（可序列化）。

    所有字段必须保持 JSON 可序列化（str / int / dict / list / None），
    禁止存入自定义对象，以保证可通过 dataclasses.asdict() + json.dumps()
    持久化到数据库，并在跨进程 / 跨请求场景下用 RunState(**dict) 恢复后 resume。

    session_id 是隔离必填字段——resume 时必须与 pause 时一致，
    防止跨会话越权恢复。
    """
    run_id: str
    agent_id: str
    step_n: int
    session_id: str
    messages: list[dict] = field(default_factory=list)
    pending_tool_call: dict | None = None
    working: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    """Loop 执行的最终结果。永远不抛异常到外部（O3 原则）。"""
    success: bool
    output: Any = None
    error: str | None = None
    terminal_reason: TerminalReason | None = TerminalReason.COMPLETED

    run_id: str = ""
    total_steps: int = 0
    total_duration_ms: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    # 注：费用不再由 SDK 计算/报告（价格与预算全归应用层，见 pandapal.config.llm_pricing）。
    # 需要费用的消费方从 token 用量 + 应用层价格表自算（如看板 cost_breakdown）。

    steps: tuple[StepRecord, ...] = ()
    run_state: RunState | None = None

    started_at: datetime | None = None
    finished_at: datetime | None = None

    # Plan Mode：仅 terminal_reason=PLAN_COMPLETE 时有值
    plan_path: str | None = None

    @property
    def paused(self) -> bool:
        return not self.success and self.run_state is not None
