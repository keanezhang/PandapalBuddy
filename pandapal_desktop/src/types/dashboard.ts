/**
 * src/types/dashboard.ts — Dashboard 快照类型（IPC 契约 · 单一真相源）。
 *
 * 与后端 pandapal/dashboard/models.py 的 DashboardSnapshot.to_dict() 严格对齐：
 * 后端 DASHBOARD_DATA 事件的 payload 直接就是 { global, sessions }。
 * store / DashboardPage / dashboardMock 均复用这些类型。
 */

export interface ToolStat {
  name: string;
  count: number;
  duration_ms: number; // 平均耗时
}

/** assistant 轮关联的那次 llm_call（后端按 (run_id, step) 与 traces join）。 */
export interface TurnLLM {
  model: string;
  provider: string; // 平台名（dashscope/volcengine/openai/deepseek）；按 provider 分账/分组，空=未知
  input_tokens: number;
  output_tokens: number;
  duration_ms: number;
  cached_tokens: number | null;
  cache_hit_ratio: number | null;
  step: number;
  tool_calls_count: number;
  // 费用真相源 = 应用层价格表，后端 cost_of_call 一处算清（正向三项式）；前端只取值+sum（见 derive.ts）。
  // SDK trace 不再带金额（无 cost_usd）。
  input_cost_usd?: number; // 净费用的输入侧（命中价+全价两段）
  output_cost_usd?: number; // 净费用的输出侧
  net_cost_usd?: number; // 实际净费用 = input+output（命中按缓存价 + 未命中按全价 + 输出价）
  cache_saved_usd?: number; // 命中缓存相对全价省下的 USD（= full − net）；旧快照可缺省
}

export interface ToolCall {
  name: string;
  args: Record<string, unknown>;
}

/** assistant 轮实际执行的工具 span（来自 traces，含真实耗时；按 (run_id, step) join）。 */
export interface ToolSpan {
  name: string;
  duration_ms: number;
  status?: string; // 工具 span 状态 ok | error（工具成功率·按模型；旧快照可缺省，视为无状态）
}

export interface Turn {
  turn: number;
  role: "user" | "assistant" | "tool";
  timestamp: string;
  content: string;
  run_id?: string; // 归属 run（run_id[:8]），前端按此分组/标注
  reasoning?: string | null;
  tool_calls?: ToolCall[];
  tool_spans?: ToolSpan[];
  llm?: TurnLLM | null;
}

export interface RunInfo {
  id: string;
  status: string; // ok | error | …（以 audit run_finished 为权威）
  duration_ms: number;
  finish_reason?: string; // run 结束/失败原因（audit run_finished detail）
}

export interface SessionData {
  id: string;
  session_id: string;
  title: string;
  preview: string;
  created_at: string;
  last_active: string;
  message_count: number;
  group_name: string | null;
  model: string;
  llm_calls: number;
  step_count: number;
  input_tokens: number;
  output_tokens: number;
  cost: number; // 会话净费用 = Σ 轮次 net_cost_usd（app 价格表，唯一口径；dailySeries 据此）
  llm_durations: number[];
  tools: ToolStat[];
  runs: RunInfo[];
  turns: Turn[];
  system_prompt?: string; // 生效系统提示词（logs.md 纯读；缺失时前端降级为占位）
  tools_schema?: Array<Record<string, unknown>>; // 生效工具 schema（首个 llm 调用 extra_json 纯读；缺失时前端降级为占位）
}

export interface GlobalMetrics {
  agent_id: string;
  last_updated: string;
  llm_call_total?: number;
  llm_input_tokens_total?: number;
  llm_output_tokens_total?: number;
  error_total?: number;
  run_total_started?: number;
  run_total_success?: number;
  run_total_failed?: number;
  step_total?: number;
  // token_cost_total_usd 已移除：费用不再由 SDK 全局 gauge 提供，前端从会话 net_cost_usd 求和
  active_runs?: number;
}

/** 一类降级事件的累计计数。真相源：pandapal/dashboard/models.py DegradationStat。
 *
 *  来自后端统一降级通道（pandapal/degradation.py）的 counter `degradation`，
 *  按 (event_code, category, severity, source) 四个低基数 label 分组。
 *  **累计口径**：counter 无时间维度，故这些数字是「自有数据以来的总计」，
 *  不受顶栏日期筛选影响——UI 必须显式标注，否则会被误读成筛选后的结果。 */
export interface DegradationStat {
  event_code: string; // 稳定主键，见 degradation.DegradationEvent
  category: string; // decision | id | cost | exception_swallowed | capability | display
  severity: string; // log_only | ui_bubble | abort
  source: string; // 触发点标识，如 "hitl_manager.resume"
  count: number;
}

export interface DashboardSnapshot {
  global: GlobalMetrics;
  sessions: SessionData[];
  /** 降级事件明细。非会话级（多数降级没有 session_id），故与 sessions 平级。
   *  旧后端快照无此字段 → 可选，前端按空数组降级（展示辅助类，可回落）。 */
  degradations?: DegradationStat[];
}
