"""pandaren/tool/definition/context.py — 工具执行上下文（只读快照）"""

from __future__ import annotations

import types as builtin_types
from dataclasses import dataclass, field

from ...identity.models import TrustLevel
from ...memory.protocols import WorkingMemoryAccessor


@dataclass(frozen=True)
class ToolContext:
    """工具执行上下文（只读快照，B2 原则）。

    创建者：agent_loop
    消费者：executor
    生命周期：一次 execute_tool() 调用

    安全约束（B2 原则）：
      ✅ 工具可以读 ctx 里的信息
      ❌ 工具不能修改 ctx 的任何字段（frozen=True，物理上报错）
      ❌ 工具不能通过 ctx 修改 Agent 的权限、信任等级、配置
    """
    run_id: str
    step_n: int
    agent_id: str
    session_id: str  # 必填，每轮工具调用都隶属一个会话
    permissions: frozenset[str] = field(default_factory=frozenset)
    trust_level: TrustLevel = TrustLevel.SUB_AGENT
    namespace: str | None = None
    metadata: builtin_types.MappingProxyType = field(
        default_factory=lambda: builtin_types.MappingProxyType({})
    )
    working_memory: WorkingMemoryAccessor | None = None
