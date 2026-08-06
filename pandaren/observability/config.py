"""pandaren/observability/config.py — Observability 统一配置值对象

显式四态（完全显式语义，不传 = 关闭）：
  False    — 关闭该子系统（AuditLog 除外，HC4 不可关闭）
  None     — 关闭（等同 False，Builder 内部 _UNSET 和 None 都映射到此）
  "mem"    — 使用 SDK 内置 InMemory 后端（显式开启）
  Backend  — 使用应用层传入的自定义后端实例

ObservabilityConfig 是纯值对象，不做任何构造逻辑。
构造由 ObservabilityProvider 统一完成。

用法（通过 Builder）：
    # 全关（零噪音，适合测试/CI）
    AgentBuilder().build()

    # 精细开启指定子系统
    AgentBuilder().observability(
        audit="mem",       # InMemory 审计
        log="mem",         # InMemory 日志
    ).build()

    # 自定义 Audit + 关闭其余
    AgentBuilder().observability(
        audit=MarkdownAuditBackend("./data"),
    ).build()
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import LogLevel, TraceLevel


@dataclass(frozen=True)
class ObservabilityConfig:
    """Observability 统一配置值对象（frozen，构建后不可变）。

    每个 Backend 字段支持显式四态：
      False / None  — 关闭该子系统（Provider 使用 Null 后端，静默）
      "mem"         — 使用 SDK 内置 InMemory 后端
      Backend 实例  — 使用应用层传入的自定义后端

    特殊规则：
      audit_backend 传 False 时，Provider 会降级为 InMemoryAuditBackend 并 warning（HC4 不可关闭）。
      audit_backend 传 None 时，静默使用 InMemoryAuditBackend（Builder._UNSET 映射路径）。
    """

    # ── Logger ──
    log_backend: object = False      # False/None→关闭 / "mem"→InMemory / 实例→自定义
    log_level: LogLevel = LogLevel.INFO

    # ── Tracer ──
    tracer_backend: object = False   # False/None→关闭 / "mem"→InMemory / 实例→自定义
    trace_level: TraceLevel = TraceLevel.SUMMARY

    # ── Metrics ──
    metrics_backend: object = False  # False/None→关闭 / "mem"→InMemory / 实例→自定义

    # ── AuditLog（HC4：不可关闭）──
    audit_backend: object = None     # None→InMemory静默 / False→InMemory+WARN / "mem"→InMemory / 实例→自定义

    # ── 脱敏 ──
    sanitizer: object = None         # None→不脱敏 / 实例→自定义脱敏器
