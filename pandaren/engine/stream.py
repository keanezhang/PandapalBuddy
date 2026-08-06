"""pandaren/engine/stream.py — 流式事件定义（StreamEventType + StreamEvent）

零外部依赖——可被任何上层模块 import，不引入循环依赖。

设计原则：
  - StreamEvent 是最小传输单元，不携带可变对象引用
  - StreamEventType 枚举可扩展（新增枚举值不影响现有消费方）
  - 流式路径（run_stream）与非流式路径（run）完全独立，共享零代码
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StreamEventType(str, Enum):
    """流式事件类型枚举（16 种）。

    命名规则：<对象>_<动作>，START/END 成对出现。
    使用 str 混入使得 event.type == "llm_token" 判断可正常工作。
    """

    # ── Run 级别 ──
    RUN_START = "run_start"
    """run_stream() 开始，携带 run_id、task 摘要。"""

    RUN_END = "run_end"
    """run_stream() 正常/异常结束，携带 AgentResult 结构体字段（不含 run_state）。"""

    # ── Step 级别 ──
    STEP_START = "step_start"
    """单步开始，携带 step_n。"""

    STEP_END = "step_end"
    """单步结束，携带 step_n + duration_ms + tokens。"""

    # ── LLM 级别 ──
    LLM_CALL_START = "llm_call_start"
    """LLM 调用开始（含重试），携带 model_name。"""

    LLM_TOKEN = "llm_token"
    """逐 token / chunk 输出。

    data 结构（必须字段）::

        {
            "delta":    str,   # 本次新增文本片段（P0 = 全量内容，P1 = 真增量 chunk）
            "snapshot": str,   # 到目前为止的累计文本（方便无状态消费方直接替换显示）
        }

    P0 阶段：LLM 调用完成后以单个事件发出，delta == snapshot == 完整内容。
    P1 阶段：llm_client.stream() 可用后改为逐 chunk yield，snapshot 逐步增长。
    消费方示例::

        snapshot = ""
        async for event in agent.run_stream(task):
            if event.type == StreamEventType.LLM_TOKEN:
                print(event.data["delta"], end="", flush=True)
                snapshot = event.data["snapshot"]
    """

    LLM_REASONING_TOKEN = "llm_reasoning_token"
    """思考模型的推理内容增量（reasoning_content），思考过程输出专用。

    仅在使用带推理能力的模型（如 qwen3-plus、doubao-thinking 等）时发出。
    data 结构与 LLM_TOKEN 一致::

        {
            "delta":    str,   # 本次新增推理文本片段
            "snapshot": str,   # 累计推理文本
        }

    消费方可选择展示或忽略此事件，不影响正常 LLM_TOKEN 流程。
    消费方示例::

        async for event in agent.run_stream(task):
            if event.type == StreamEventType.LLM_REASONING_TOKEN:
                print(event.data["delta"], end="", flush=True)  # 展示推理过程
    """

    LLM_CALL_END = "llm_call_end"
    """LLM 调用完成，携带 input_tokens + output_tokens。"""

    # ── Tool 级别 ──
    TOOL_CALL_START = "tool_call_start"
    """工具调用开始，携带 tool_name + args（摘要）。"""

    TOOL_CALL_END = "tool_call_end"
    """工具调用结束，携带 tool_name + success + data 摘要。"""

    # ── 安全约束反馈（"看得见"核心体验：约束不是黑盒）──
    PERMISSION_DENIED = "permission_denied"
    """权限拒绝，携带 tool_name + 被拒绝的 permission_required。"""

    HITL_REQUESTED = "hitl_requested"
    """HITL 审批需要，携带 tool_name + sensitivity。
    发出此事件后 run_stream() 立即结束（等同于 PAUSE）。"""

    INTERACTION_REQUESTED = "interaction_requested"
    """交互型工具需要用户回复，携带 tool_args + run_state。
    发出此事件后 run_stream() 立即结束（等同于 PAUSE）。
    Scheduler 应展示问题给用户，收集回复后通过
    agent.run(interaction_response=...) 恢复。"""

    # ── 终止事件 ──
    AGENT_HALTED = "agent_halted"
    """Agent 因错误/超限/熔断等原因终止，携带 terminal_reason + error。"""

    AGENT_CANCELLED = "agent_cancelled"
    """Agent 被外部取消信号终止。"""

    # ── 保留/预留 ──
    HANDOFF = "handoff"
    """多 Agent Handoff（P2 预留），携带 target_agent_id。"""

    # ── Plan Mode ──
    PLAN_APPROVAL_REQUESTED = "plan_approval_requested"
    """Plan Mode 规划完成，等待用户批准。
    data 结构::

        {
            "plan_path":    str,   # 计划文件路径
            "plan_content": str,   # 计划内容（方便调用方直接展示，无需再读文件）
        }
    """


@dataclass(frozen=True)
class StreamEvent:
    """流式输出的最小单元。

    Attributes:
        type:      事件类型（StreamEventType）
        data:      事件附加数据（结构取决于 type，见各 StreamEventType 注释）
        run_id:    本次 run 的唯一 ID（全程不变）
        agent_id:  发出事件的 Agent ID
        step_n:    当前步骤编号（-1 表示 Run 级别事件）
        tool_name: 工具名称（仅 Tool 相关事件填充，其余为 None）
    """

    type: StreamEventType
    data: Any = field(default=None, compare=False)
    run_id: str = ""
    agent_id: str = ""
    step_n: int = -1
    tool_name: str | None = None

    def __str__(self) -> str:
        tool_part = f", tool={self.tool_name!r}" if self.tool_name else ""
        return (
            f"StreamEvent(type={self.type.value!r}, "
            f"run_id={self.run_id!r}, "
            f"step_n={self.step_n}"
            f"{tool_part})"
        )
