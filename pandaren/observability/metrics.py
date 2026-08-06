"""pandaren/observability/metrics.py — 指标采集 Facade

Metrics 子系统：预定义 Agent 运行的关键指标。
非 HC4 子系统——故障时优雅降级，不传播异常到 Loop。

指标类型映射：
  Counter   → record_counter
  Histogram → record_histogram
  Gauge     → record_gauge

设计文档对齐：
  通用 API：record_duration / increment_counter / set_gauge / record_tokens
  Token 指标为 Counter 类型（累计总量），非 Histogram。
  标准 Metrics 清单见设计文档 Step 6。
"""

from __future__ import annotations

import logging

from .protocols import MetricsBackend

# 观测 Fail-Safe 边界：后端异常不得炸断主链路（非 HC4），但 §九「降级必留痕」——
# 不再纯 pass，统一降级为 debug 留痕（默认 INFO 级不输出、无生产刷屏与性能开销，
# 需要排查时开 DEBUG 即可见后端失败的 traceback）。
logger = logging.getLogger(__name__)


class Metrics:
    """指标采集系统。

    对外 API：
      - 通用 API：record_duration / increment_counter / set_gauge / record_tokens
      - 命名便捷 API：inc_run_total / observe_step_duration_ms / ...
    """

    def __init__(self, *, backend: MetricsBackend | None = None, agent_id: str = "") -> None:
        self._backend: MetricsBackend | None = backend
        self._agent_id: str = agent_id

    def _labels(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        labels = {"agent_id": self._agent_id} if self._agent_id else {}
        if extra:
            labels.update(extra)
        return labels

    # ════════════════════════════════════════════
    # 通用 API（设计文档行为需求）
    # ════════════════════════════════════════════

    def record_duration(self, name: str, value_ms: float, labels: dict[str, str] | None = None) -> None:
        """记录耗时（直方图）。"""
        if self._backend is None:
            return
        try: self._backend.record_histogram(name, value_ms, self._labels(labels))
        except Exception: logger.debug("metrics backend call failed", exc_info=True)

    def increment_counter(self, name: str, labels: dict[str, str] | None = None) -> None:
        """计数器递增。"""
        if self._backend is None:
            return
        try: self._backend.record_counter(name, 1, self._labels(labels))
        except Exception: logger.debug("metrics backend call failed", exc_info=True)

    def set_gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """设置仪表值。"""
        if self._backend is None:
            return
        try: self._backend.record_gauge(name, value, self._labels(labels))
        except Exception: logger.debug("metrics backend call failed", exc_info=True)

    def flush(self) -> None:
        """把内存中的指标态强制落盘（生命周期边界调用，如 run 结束）。

        metrics 后端通常按批（如每 N 次写）刷盘以省 IO；本方法用于在
        run/session 边界或进程退出时补一次强制刷盘，避免最后不足一批的写入
        滞留内存 → 裸文件停在旧快照 / 硬杀丢尾。非 HC4，故障静默降级。
        """
        if self._backend is None:
            return
        try: self._backend.flush()
        except Exception: logger.debug("metrics backend call failed", exc_info=True)

    def record_tokens(self, input_tokens: int, output_tokens: int, model_name: str = "", agent_id: str = "", provider: str = "") -> None:
        """记录 token 消耗。Token 使用 Counter 类型（累计总量）。

        provider（平台名）：非空时作为 label，支持按 provider 的全局累计 token 统计。
        """
        if self._backend is None:
            return
        try:
            agent = agent_id or self._agent_id
            labels = {"model": model_name, "agent_id": agent} if model_name else {"agent_id": agent}
            if provider:
                labels["provider"] = provider
            self._backend.record_counter("llm_input_tokens_total", input_tokens, self._labels(labels))
            self._backend.record_counter("llm_output_tokens_total", output_tokens, self._labels(labels))
        except Exception: logger.debug("metrics backend call failed", exc_info=True)

    # 注：花费 gauge（token_cost_total_usd）已移除——SDK 不计价、不报告费用。
    # 费用统计归应用层（看板从 token 用量 + 价格表自算，见 pandapal.config.llm_pricing）。

    # ════════════════════════════════════════════
    # 命名便捷 API（标准 Metrics 清单）
    # ════════════════════════════════════════════

    # ── 计数器（Counter） ──

    def inc_run_total(self, status: str = "started") -> None:
        """run_total（success/failed/cancelled 分标签）。"""
        if self._backend is None: return
        try: self._backend.record_counter("run_total", 1, self._labels({"status": status}))
        except Exception: logger.debug("metrics backend call failed", exc_info=True)

    def inc_step_total(self) -> None:
        """step_total（按 agent_id）。"""
        if self._backend is None: return
        try: self._backend.record_counter("step_total", 1, self._labels())
        except Exception: logger.debug("metrics backend call failed", exc_info=True)

    def inc_llm_call_total(self, model: str = "", status: str = "success", provider: str = "") -> None:
        """llm_call_total（按 model_name、status；provider 非空时附加 provider label）。"""
        if self._backend is None: return
        labels = {"model": model, "status": status}
        if provider:
            labels["provider"] = provider
        try: self._backend.record_counter("llm_call_total", 1, self._labels(labels))
        except Exception: logger.debug("metrics backend call failed", exc_info=True)

    def inc_tool_execute_total(self, tool_name: str, status: str = "success") -> None:
        """tool_execute_total（按 tool_name、status）。"""
        if self._backend is None: return
        try: self._backend.record_counter("tool_execute_total", 1, self._labels({"tool_name": tool_name, "status": status}))
        except Exception: logger.debug("metrics backend call failed", exc_info=True)

    def inc_error_total(self, error_type: str = "unknown") -> None:
        """error_total（按 error_type、agent_id）。"""
        if self._backend is None: return
        try: self._backend.record_counter("error_total", 1, self._labels({"error_type": error_type}))
        except Exception: logger.debug("metrics backend call failed", exc_info=True)

    def inc_permission_check_total(self, result: str = "allow") -> None:
        """permission_check_total（allow/deny 分标签）。"""
        if self._backend is None: return
        try: self._backend.record_counter("permission_check_total", 1, self._labels({"result": result}))
        except Exception: logger.debug("metrics backend call failed", exc_info=True)

    def inc_hitl_approval_total(self, result: str = "pass") -> None:
        """hitl_approval_total（pass/need_approval 分标签）。"""
        if self._backend is None: return
        try: self._backend.record_counter("hitl_approval_total", 1, self._labels({"result": result}))
        except Exception: logger.debug("metrics backend call failed", exc_info=True)

    # ── 直方图（Histogram） ──

    def observe_run_duration_ms(self, duration_ms: float) -> None:
        """run_duration_ms — Run 总耗时。"""
        if self._backend is None: return
        try: self._backend.record_histogram("run_duration_ms", duration_ms, self._labels())
        except Exception: logger.debug("metrics backend call failed", exc_info=True)

    def observe_step_duration_ms(self, duration_ms: float) -> None:
        """step_duration_ms — 单步耗时。"""
        if self._backend is None: return
        try: self._backend.record_histogram("step_duration_ms", duration_ms, self._labels())
        except Exception: logger.debug("metrics backend call failed", exc_info=True)

    def observe_llm_call_duration_ms(self, duration_ms: float, model: str = "", provider: str = "") -> None:
        """llm_call_duration_ms（按 model_name；provider 非空时附加 provider label）。"""
        if self._backend is None: return
        labels = {"model": model}
        if provider:
            labels["provider"] = provider
        try: self._backend.record_histogram("llm_call_duration_ms", duration_ms, self._labels(labels))
        except Exception: logger.debug("metrics backend call failed", exc_info=True)

    def observe_tool_execute_duration_ms(self, duration_ms: float, tool_name: str = "") -> None:
        """tool_execute_duration_ms（按 tool_name）。"""
        if self._backend is None: return
        try: self._backend.record_histogram("tool_execute_duration_ms", duration_ms, self._labels({"tool_name": tool_name}))
        except Exception: logger.debug("metrics backend call failed", exc_info=True)

    # ── 仪表盘（Gauge） ──

    def set_active_runs(self, count: int) -> None:
        """active_runs — 当前活跃 Run 数（绝对值设置，仅限单 Agent 场景）。"""
        if self._backend is None: return
        try: self._backend.record_gauge("active_runs", float(count), self._labels())
        except Exception: logger.debug("metrics backend call failed", exc_info=True)

    def inc_active_runs(self, delta: int = 1) -> None:
        """active_runs — 活跃 Run 数增减（支持多 Agent 并发安全）。"""
        if self._backend is None: return
        try: self._backend.record_counter("active_runs_delta", delta, self._labels())
        except Exception: logger.debug("metrics backend call failed", exc_info=True)

    # ── 其他 ──

    def set_agent_id(self, agent_id: str) -> None:
        self._agent_id = agent_id

    def __repr__(self) -> str:
        return f"Metrics(agent_id='{self._agent_id}')"
