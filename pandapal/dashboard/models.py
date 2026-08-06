"""pandapal/dashboard/models.py — Dashboard 快照的强类型模型。

frozen dataclass，字段与前端 pandapal_desktop/src/store/dashboardMock.ts 的
DashboardSnapshot 形状严格对齐（IPC 序列化后前端可直接消费）。to_dict() 产出
纯 JSON 可序列化 dict（供 IPC 出站编码）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass(frozen=True)
class ToolStat:
    """单个工具在某范围内的调用统计（来自 traces 的 tool_call 行）。"""
    name: str
    count: int
    duration_ms: float  # 平均耗时


@dataclass(frozen=True)
class Histogram:
    count: int
    avg: float
    min: float
    max: float
    sum: float


@dataclass(frozen=True)
class ToolCall:
    """assistant 轮请求的一次工具调用（来自 raw_log message_json.tool_calls）。"""
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ToolSpan:
    """assistant 轮实际执行的一次工具调用（来自 traces tool_call 行，带真实耗时）。
    按 (run_id, step) 与 assistant 轮 join（P1：waterfall 用真值、不再取均值近似）。"""
    name: str
    duration_ms: float
    status: str = "ok"  # 工具 span 状态 ok | error（供「工具成功率·按模型」统计，D6）


@dataclass(frozen=True)
class TurnLLM:
    """assistant 轮关联的那次 llm_call（按 (run_id, step) 与 traces join）。

    费用字段全部来自应用层唯一计费函数 `cost_of_call`（正向三项式，见 llm_pricing）——
    源头一次算清，前端只做 sum。SDK trace 不再带任何金额（无 cost_usd）。
    """
    model: str
    provider: str  # 平台名（dashscope/volcengine/openai/deepseek）；按 provider 分账/分组，空=未知
    input_tokens: int
    output_tokens: int
    duration_ms: float
    cached_tokens: int | None
    cache_hit_ratio: float | None
    step: int
    tool_calls_count: int
    # 费用（应用层 cost_of_call 精算）：
    #   net_cost_usd  = 实际净费用（命中按缓存价 + 未命中按全价 + 输出价）——主口径
    #   input_cost_usd + output_cost_usd = net_cost_usd（净费用的输入/输出拆分）
    #   cache_saved_usd = 相对全价省下的钱（= full − net）
    input_cost_usd: float = 0.0
    output_cost_usd: float = 0.0
    net_cost_usd: float = 0.0
    cache_saved_usd: float = 0.0


@dataclass(frozen=True)
class Turn:
    turn: int
    role: str  # user | assistant | tool
    timestamp: str
    content: str
    run_id: str = ""  # 归属 run（run_id[:8]）；前端按此分组/标注，避免 runs[] 重复项错位
    reasoning: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_spans: list[ToolSpan] = field(default_factory=list)  # 实际执行的工具 span（含真实耗时）
    llm: TurnLLM | None = None


@dataclass(frozen=True)
class RunInfo:
    id: str
    status: str  # ok | error | ...（以 audit run_finished 为权威）
    duration_ms: float
    finish_reason: str = ""  # audit run_finished 的 detail（失败/终止原因）；无则标注未正常结束


@dataclass(frozen=True)
class SessionData:
    id: str
    session_id: str          # 短 id
    title: str
    preview: str
    created_at: str
    last_active: str
    message_count: int
    group_name: str | None
    model: str               # 会话级 model（各 llm_call.model 去重；多值→"混合"）
    llm_calls: int
    step_count: int
    input_tokens: int
    output_tokens: int
    cost: float              # 会话净费用 = Σ 轮次 net_cost_usd（应用层价格表精算，唯一口径）
    llm_durations: list[float]
    tools: list[ToolStat]
    runs: list[RunInfo]
    turns: list[Turn]
    system_prompt: str = ""  # 生效系统提示词（从 logs.md 首个 llm 调用的 messages[system] 纯读）


@dataclass(frozen=True)
class GlobalMetrics:
    """全局快照（metrics.md）。前端筛选时以会话聚合为准，此处提供权威累计做对照。"""
    agent_id: str
    last_updated: str
    llm_call_total: int = 0
    llm_input_tokens_total: int = 0
    llm_output_tokens_total: int = 0
    error_total: int = 0
    run_total_started: int = 0
    run_total_success: int = 0
    run_total_failed: int = 0
    step_total: int = 0
    # 注：token_cost_total_usd 已移除——SDK 不再计价/记录花费 gauge。
    # 累计费用改由前端从各会话 net_cost_usd 求和（health.netCost），不依赖全局 gauge。
    active_runs: float = 0.0


@dataclass(frozen=True)
class DegradationStat:
    """一类降级事件的累计计数（来自 metrics counter `degradation` 的 label 分组）。

    分组键 = (event_code, category, severity, source)，四者都是**低基数稳定枚举**
    （见 pandapal.degradation.DegradationEvent / Category / Severity），故可直接
    作为明细行而不会撑爆行数。绝不含 session_id/run_id——高基数，取证走
    `pandapal.degradation` logger，不进看板。

    为什么它不是 GlobalMetrics 上的一个标量：GlobalMetrics 的语义是「单 agent 的
    累计计数标量」，塞一个 degradation_total 进去只能答「降级了 N 次」，答不出
    「是哪个 event_code 在涨」——而后者才是这条通道存在的理由（§5.1 趋势下钻）。
    """
    event_code: str
    category: str   # decision | id | cost | exception_swallowed | capability | display
    severity: str   # log_only | ui_bubble | abort
    source: str     # 触发点标识，如 "hitl_manager.resume"
    count: int


@dataclass(frozen=True)
class DashboardSnapshot:
    global_: GlobalMetrics
    sessions: list[SessionData]
    # 降级事件明细（非会话级——多数降级根本没有 session_id，故与 sessions 平级独立成段，
    # 而不是挂在某个会话下）。空列表 = 本次扫描窗口内无降级，是正常态。
    degradations: list[DegradationStat] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON 可序列化 dict（供 IPC 出站）。键 global_ → global 以对齐前端。"""
        return {
            "global": asdict(self.global_),
            "sessions": [asdict(s) for s in self.sessions],
            "degradations": [asdict(d) for d in self.degradations],
        }
