"""pandapal/quality — 编码质量门控（应用层）

Agent 写完 .py，门控立刻用**与 CI 相同的规则**跑 ruff，诊断随同一条 tool 消息
回到 Agent 下一轮上下文，把"改完代码要检查"从软倡议变成框架强制。

装配（注册两次，各走各的门 —— 见 gate.py 的说明）：

    from pandapal.quality import CodeQualityGate, GateConfig

    gate = CodeQualityGate(GateConfig(project_root=str(repo_root)))
    builder.behavior(tool_feedback_providers=[gate])          # 控制面
    builder.hooks(CompositeAgentHooks([...既有..., gate]))     # 观测面（仅为状态回收）
"""

from .checker import Checker, RuffChecker
from .gate import GATE_SOURCE, CodeQualityGate
from .models import CircuitDecision, Diagnostic, GateConfig, GateLevel

__all__ = [
    "CodeQualityGate",
    "GateConfig",
    "GateLevel",
    "GATE_SOURCE",
    "Checker",
    "RuffChecker",
    "Diagnostic",
    "CircuitDecision",
]
