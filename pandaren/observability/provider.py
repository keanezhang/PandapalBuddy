"""pandaren/observability/provider.py — ObservabilityProvider 工厂

消费 ObservabilityConfig 显式四态，统一构造 4 个 Facade + HooksAdapter。

职责：
  - 解析四态（False/None/"mem"/实例）→ 具体 Backend 或 Null 后端
  - False / None（未显式配置）→ Null 后端，静默关闭
  - "mem"  → 对应的 InMemory 后端（显式开启 SDK 内置内存后端）
  - 实例   → 使用自定义后端
  - HC4：audit_backend=False（用户显式关闭）→ 降级为 InMemoryAuditBackend + WARN
         audit_backend=None（未配置默认）   → InMemoryAuditBackend，静默无 warning
         audit_backend="mem"               → InMemoryAuditBackend，显式，无 warning
  - 构造 ObservabilityHooksAdapter（Loop 桥接器）
"""

from __future__ import annotations

import logging
from typing import Any

from .config import ObservabilityConfig
from .types import ObservabilityContext
from .audit import AuditLog
from .logger import Logger
from .tracer import Tracer
from .metrics import Metrics
from .backend import (
    InMemoryAuditBackend,
    InMemoryTracerBackend,
    InMemoryMetricsBackend,
    InMemoryLoggerBackend,
)
from .hooks_adapter import ObservabilityHooksAdapter

_logger = logging.getLogger("pandaren.observability.provider")


# ════════════════════════════════════════════════
#  Null 后端（False/None 时使用，所有操作静默忽略）
# ════════════════════════════════════════════════

class _NullLoggerBackend:
    def write_log(self, record: dict[str, Any]) -> None: pass


class _NullTracerBackend:
    def export_span(self, span: Any) -> None: pass


class _NullMetricsBackend:
    def record_counter(self, name: str, value: int, labels: dict[str, str]) -> None: pass
    def record_histogram(self, name: str, value: float, labels: dict[str, str]) -> None: pass
    def record_gauge(self, name: str, value: float, labels: dict[str, str]) -> None: pass


class ObservabilityProvider:
    """Observability 工厂：消费 Config，产出 4 个 Facade + HooksAdapter。

    用法：
        config = ObservabilityConfig(audit_backend=MarkdownAuditBackend("./data"))
        provider = ObservabilityProvider(config, agent_id="my_agent")

        loop = AgentLoop(
            audit_log=provider.audit_log,
            hooks=provider.hooks_adapter,
            ...
        )
    """

    def __init__(
        self,
        config: ObservabilityConfig | None = None,
        *,
        agent_id: str = "",
    ) -> None:
        cfg = config or ObservabilityConfig()

        self.logger = self._build_logger(cfg, agent_id)
        self.tracer = self._build_tracer(cfg, agent_id)
        self.metrics = self._build_metrics(cfg, agent_id)
        self.audit_log = self._build_audit(cfg)
        # SDK 不计价：llm_call span 只记 token/命中等事实，金额由应用层自算（见 builder 注释）。
        self.hooks_adapter = ObservabilityHooksAdapter(
            logger=self.logger,
            tracer=self.tracer,
            metrics=self.metrics,
        )

    # ── 四态解析：None/False→Null / "mem"→InMemory / 实例→自定义 ──

    @staticmethod
    def _build_logger(cfg: ObservabilityConfig, agent_id: str) -> Logger:
        """解析 log_backend 四态：
          False / None → Null（关闭，静默）
          "mem"        → InMemoryLoggerBackend
          实例          → 自定义
        """
        if cfg.log_backend is False or cfg.log_backend is None:
            return Logger(
                backend=_NullLoggerBackend(),
                min_level=cfg.log_level,
                agent_id=agent_id,
            )
        if cfg.log_backend == "mem":
            return Logger(
                backend=InMemoryLoggerBackend(),
                min_level=cfg.log_level,
                agent_id=agent_id,
            )
        # 自定义 LoggerBackend 实例
        return Logger(backend=cfg.log_backend, min_level=cfg.log_level, agent_id=agent_id)  # type: ignore[arg-type]

    @staticmethod
    def _build_tracer(cfg: ObservabilityConfig, agent_id: str) -> Tracer:
        """解析 tracer_backend 四态：
          False / None → Noop（关闭，静默）
          "mem"        → InMemoryTracerBackend
          实例          → 自定义
        """
        if cfg.tracer_backend is False or cfg.tracer_backend is None:
            return Tracer(
                backend=None,  # None → Noop
                trace_level=cfg.trace_level,
                agent_id=agent_id,
                sanitizer=cfg.sanitizer,
            )
        if cfg.tracer_backend == "mem":
            return Tracer(
                backend=InMemoryTracerBackend(),
                trace_level=cfg.trace_level,
                agent_id=agent_id,
                sanitizer=cfg.sanitizer,
            )
        # 自定义 TracerBackend 实例
        return Tracer(
            backend=cfg.tracer_backend,  # type: ignore[arg-type]
            trace_level=cfg.trace_level,
            agent_id=agent_id,
            sanitizer=cfg.sanitizer,
        )

    @staticmethod
    def _build_metrics(cfg: ObservabilityConfig, agent_id: str) -> Metrics:
        """解析 metrics_backend 四态：
          False / None → Noop（关闭，静默）
          "mem"        → InMemoryMetricsBackend
          实例          → 自定义
        """
        if cfg.metrics_backend is False or cfg.metrics_backend is None:
            return Metrics(backend=None, agent_id=agent_id)
        if cfg.metrics_backend == "mem":
            return Metrics(backend=InMemoryMetricsBackend(), agent_id=agent_id)
        # 自定义 MetricsBackend 实例
        return Metrics(backend=cfg.metrics_backend, agent_id=agent_id)  # type: ignore[arg-type]

    @staticmethod
    def _build_audit(cfg: ObservabilityConfig) -> AuditLog:
        """HC4：AuditLog 不可关闭，必须有默认实现。

          None / "mem" → InMemoryAuditBackend（静默，正常默认路径）
          False        → 用户显式要求关闭 → HC4 不允许，强制降级为 InMemoryAuditBackend + WARN
          实例          → 使用自定义后端
        """
        if cfg.audit_backend is False:
            # 用户显式传 False → HC4 不允许，强制降级 + 警告
            _logger.warning(
                "AuditLog 不可关闭（HC4），已降级为 InMemoryAuditBackend。"
                "InMemoryAuditBackend 重启后审计数据丢失，"
                "生产环境必须配置持久化后端！"
            )
            return AuditLog(backend=InMemoryAuditBackend())
        if cfg.audit_backend is None or cfg.audit_backend == "mem":
            # None：Builder._UNSET 映射而来，静默使用 InMemory
            # "mem"：用户显式要求 InMemory，同样无需 warning
            return AuditLog(backend=InMemoryAuditBackend())
        # 自定义 AuditBackend 实例
        return AuditLog(backend=cfg.audit_backend)  # type: ignore[arg-type]

    # ── 工厂方法 ──

    @staticmethod
    def build_observability_context(
        agent_id: str,
        run_id: str,
        *,
        parent_span_id: str | None = None,
        trace_id: str | None = None,
    ) -> ObservabilityContext:
        """构建 ObservabilityContext（场景 7/10：跨 Agent 追踪 + SDK 初始化）。

        从 Identity 层信息 + run_id 构建观测上下文。
        trace_id 跨 Agent 全局唯一；单 Agent 场景 = run_id。
        """
        return ObservabilityContext(
            run_id=run_id,
            agent_id=agent_id,
            trace_id=trace_id or run_id,  # 单 Agent 场景 trace_id = run_id
            parent_span_id=parent_span_id,
        )
