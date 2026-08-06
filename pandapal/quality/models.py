"""pandapal/quality/models.py — 门控的数据模型与配置

对应设计 §5B-a 属性表 / §Step 6.6 配置项表 / PRD §3.4 数据字典。
全部具名有型，不用裸 dict 跨层传递。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pandaren.tool.definition.tool_result import FeedbackSeverity


@dataclass(frozen=True)
class Diagnostic:
    """一条归一化后的检查器诊断。

    severity 是**我们造的**字段：由各 Checker 从自己的输出归一化而来
    （见 RuffChecker._SEVERITY 的说明），不是检查器原样透传。
    """
    file: str            # 绝对路径
    line: int
    column: int
    code: str            # 如 "F401" / "invalid-syntax"
    message: str
    severity: FeedbackSeverity
    checker: str = ""    # 产出它的检查器名，如 "ruff"（多检查器时溯源用）


class CircuitDecision(Enum):
    """熔断裁决（_apply_circuit 的返回）。

    三段式：continue（正常回灌）→ fuse（恰好达阈值，回灌一次提示）→ silent（已熔断，静默）。
    pass 独立于三段之外，表示本轮干净、计数归零。
    """
    PASS = "pass"          # 本轮无 error → 删 key，退出熔断
    CONTINUE = "continue"  # 有 error 且未达阈值 → 回灌诊断
    FUSE = "fuse"          # 恰好达阈值 → 回灌一次「请如实说明」
    SILENT = "silent"      # 已越过阈值 → 不再回灌


class GateLevel(str, Enum):
    """门控级别。

    WARN（默认，本期唯一交付）：只回灌诊断，不阻断 Agent declare 完成。
    BLOCK（phase-2）：还需框架另加 RunEndVeto 控制面链才能生效，本期**不可用**。
    """
    WARN = "warn"
    BLOCK = "block"


@dataclass(frozen=True)
class GateConfig:
    """门控配置（startup 级、系统配置、用户不可见、无热更、无旁路）。

    **不提供「规则集」类配置项**：规则的唯一真相源是项目 pyproject.toml 的
    [tool.ruff.lint]，门控不得另立 select/ignore —— 否则与 CI 不是同一把尺子
    （见设计 5B-c，本仓 lint.yml 已为此踩过坑）。
    """
    enabled: bool = True
    suffixes: frozenset[str] = frozenset({".py"})
    circuit_threshold: int = 3
    level: GateLevel = GateLevel.WARN
    check_timeout_seconds: float = 5.0

    #: 本期为 **no-op**：ruff 的诊断一律归一化为 error（见 RuffChecker），
    #: 故永不产生 warning 级反馈，此开关不可达。保留是为 phase-2 引入真有等级的
    #: 检查器（mypy 的 note、bandit 的 LOW）。**勿将其默认 false 误读为「关掉了一半功能」**。
    feedback_warnings: bool = False

    #: ruff 子进程的 cwd —— 它靠这个找到 pyproject.toml。设错会让 ruff **静默回落默认档**，
    #: 是本设计最隐蔽的失效模式（门控与 CI 不同尺，且无任何报错）。
    project_root: str = ""

    #: _retry_counts 的容量硬上限（兜底）。主回收是 gate.reclaim_hooks() 在 run 结束时
    #: 按 session 清理；此上限只防该回收未触发的异常路径导致的慢泄漏。
    max_state_entries: int = 512

    #: 单条反馈里最多列出的诊断条数，超出只报数量（防单文件几百条错误撑爆 tool 消息）。
    max_diagnostics_shown: int = 20

    def __post_init__(self) -> None:
        if self.circuit_threshold < 1:
            raise ValueError(f"circuit_threshold 必须 ≥1，收到 {self.circuit_threshold}")
        if self.check_timeout_seconds <= 0:
            raise ValueError(f"check_timeout_seconds 必须 >0，收到 {self.check_timeout_seconds}")
        if self.max_state_entries < 1:
            raise ValueError(f"max_state_entries 必须 ≥1，收到 {self.max_state_entries}")
