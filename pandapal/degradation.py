"""pandapal/degradation.py — 统一降级可观测通道（健壮性原则 §5 落地）。

所有「在这兜了底 / 静默降级 / 门禁 fail-fast」的事件都汇到这一个入口，做**双写**：
  - log（取证）：`logging.getLogger("pandapal.degradation")`，`event_code` 作稳定主键；
  - counter（趋势）：复用四大观测支柱里现有的 **Metrics Facade**，`counter("degradation", labels=…)`。

设计要点（见 docs/audit/健壮性与降级工程原则.md §5）：
  1. `event_code` 是稳定主键（不是自由文本）——才能 group by 做趋势（「过去 7 天 model_id 降级涨没涨」）。
     它**必须进 counter 的 labels**：只写进 log 而不进 labels，趋势就退化成一个无法下钻的总数。
  2. **不造第五根观测柱**：counter 打到启动时注入的现有 `Metrics` facade，未注入则为 no-op（log 仍写）。
  3. **degradation ≠ audit**：audit 管「安全相关发生了什么」（HC4 不可绕过）；本通道管「在哪兜了底」。
  4. **去重防刷屏**：高频路径传 `dedup_key`，每 key 每小时最多一条。
  5. 本身是观测边界：**绝不向业务抛异常**。

走 `Metrics` facade 而非裸 `MetricsBackend` 的理由（两条都是「别自己再实现一遍」）：
  - facade 已是 SDK 的观测 Fail-Safe 边界（后端异常 → debug 留痕、不外抛），本模块无需自备 try/except；
  - facade 统一补 `agent_id` label，与其余所有 counter 同构——否则这批行按 agent_id join 会整体漏掉。

分层：本模块属 pandapal 应用层，仅从 pandaren 引入观测 facade（只读用法），无应用层反向依赖。
"""

from __future__ import annotations

import logging
import time
from typing import Literal

from pandaren.observability.metrics import Metrics

# 字段分类（§0/§5.2）：决策 / ID·身份 / 金额·计费 / 异常吞没 / 能力降级 / 展示辅助。
Category = Literal["decision", "id", "cost", "exception_swallowed", "capability", "display"]
# 严重度（§2.3 三档）：只留痕 / 冒泡到 UI / 直接中止。
Severity = Literal["log_only", "ui_bubble", "abort"]

# 稳定 event_code 主键集合（B5：契约字符串收编，别在 call 点散写自由文本）。
class DegradationEvent:
    # ── id · abort（撞额度事故类：模型身份丢失，拒绝恢复）──
    MODEL_ID_MISSING_IN_RESUME = "model_id_missing_in_resume"
    # ── decision · abort（门禁缺失，拒绝放行）──
    HITL_DECISION_MISSING = "hitl_decision_missing"
    PLAN_ACTION_MISSING = "plan_action_missing"
    # ── cost（金额类：静默超支/预算失效的根因，最该盯）──
    MODEL_UNPRICED = "model_unpriced"                           # 价格表未命中→费用计 0→预算永不触发
    BUDGET_FX_MISSING = "budget_fx_missing"                     # 汇率缺失→预算上限被关（不限额）
    # ── id · log_only ──
    JWT_USER_ID_PARSE_FAILED = "jwt_user_id_parse_failed"
    # ── capability · log_only（source 区分具体后端/落盘）──
    BACKEND_UNAVAILABLE = "backend_unavailable"
    BUDGET_PERSIST_FAILED = "budget_persist_failed"
    RUN_STATE_NOT_FOUND = "run_state_not_found"                 # 运行状态丢失，无法恢复
    TOKENIZER_FALLBACK = "tokenizer_fallback"                   # tiktoken 不可用→token 估算退回 chars/4.0（压缩判据量纲回旧）
    # ── display / exception_swallowed · log_only（低价值但纳入统一视角）──
    RESULT_SUMMARY_EXTRACT_FAILED = "result_summary_extract_failed"
    TRIGGER_RULE_JSON_CORRUPT = "trigger_rule_json_corrupt"
    STDIN_RECONFIGURE_FAILED = "stdin_reconfigure_failed"


# counter 名：跨模块契约字符串（看板聚合按此名筛行），故公开常量、禁止两处各写字面量（B5）。
DEGRADATION_COUNTER_NAME = "degradation"

_DEDUP_WINDOW_SECONDS = 3600.0  # 高频路径按 dedup_key 每小时最多一条（§5.3 去重防刷屏）。

# 降级事件专用 logger（取证）；与业务 logger 分流，便于单独采集/告警。
_logger = logging.getLogger("pandapal.degradation")

# 启动装配时注入的 Metrics facade（复用现有四柱之一，不造第五柱）。
_metrics: Metrics | None = None
_dedup_last_emit: dict[str, float] = {}


def set_metrics(metrics: Metrics | None) -> None:
    """启动装配时注入 Metrics facade；未注入则 counter 为 no-op（log 仍写）。"""
    global _metrics
    _metrics = metrics


def report_degradation(
    event_code: str,
    *,
    category: Category,
    source: str,
    severity: Severity = "log_only",
    expected: object = None,
    fallback: object = None,
    session_id: str | None = None,
    run_id: str | None = None,
    dedup_key: str | None = None,
    exc_info: bool = False,
) -> None:
    """记录一次降级/兜底：写 `pandapal.degradation` logger + Metrics counter(+1)。

    Args:
        event_code: 稳定主键（用 `DegradationEvent` 常量），用于聚合统计。
        category:   字段分类，决定 counter 的 category label 与治理优先级。
        source:     触发点标识（如 "hitl_manager.resume"），counter 的 source label。
        severity:   log_only / ui_bubble / abort（§2.3 三档）。
        expected/fallback: 本该是什么 / 实际回落了什么（取证用，无则 None）。
        session_id/run_id: 关联上下文（取证用）。
        dedup_key:  非空时按 key 每 _DEDUP_WINDOW_SECONDS 秒去重（高频路径防刷屏）。
        exc_info:   在 except 内调用时置 True，log 附带异常栈（异常吞没类用）。

    本函数**绝不抛**（观测边界）：counter 后端未注入或打点失败时，仅写 log。
    """
    # 去重（防刷屏）：只影响是否再次落痕，不影响业务。
    if dedup_key is not None:
        now = time.monotonic()
        last = _dedup_last_emit.get(dedup_key)
        if last is not None and (now - last) < _DEDUP_WINDOW_SECONDS:
            return
        _dedup_last_emit[dedup_key] = now

    # 1) log（取证）—— event_code 作 message 主键 + 结构化 extra。
    #    stdlib logging 设计上不向调用方抛（handler 异常内部 handleError），无需 try 包裹。
    _logger.warning(
        event_code,
        exc_info=exc_info,
        extra={
            "event_code": event_code,
            "category": category,
            "severity": severity,
            "source": source,
            "expected": expected,
            "fallback": fallback,
            "session_id": session_id,
            "run_id": run_id,
        },
    )

    # 2) counter（趋势）—— labels 只放**低基数**维度：event_code/category/source/severity
    #    四者都是稳定枚举（event_code 见 DegradationEvent），可安全作时间序列维度；
    #    绝不放 session_id/run_id 这类高基数（会撑爆时间序列），它们只进上面的 log 取证。
    #    facade 自身即 Fail-Safe 边界（后端异常内部消化），故此处无需 try/except。
    metrics = _metrics
    if metrics is not None:
        metrics.increment_counter(
            DEGRADATION_COUNTER_NAME,
            {
                "event_code": event_code,
                "category": category,
                "source": source,
                "severity": severity,
            },
        )
