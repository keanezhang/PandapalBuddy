/**
 * src/pages/dashboard/constants.ts — Dashboard 小系统 · 关键常量（真相源）
 *
 * 对应 PRD docs/prd/dashboard/dashboard-小系统-PRD.md §4.5「禁止魔法数字」。
 * 后端若落地派生/告警（P1/P2）需在 pandapal/dashboard/constants.py 保持同值。
 * 可配项（ALERT_*）此处为出厂默认；P2 告警配置面板修改后覆盖默认值。
 */

/** 性能分位/直方图的最小可信样本量，低于则标「样本不足」。不可配。 */
export const PERF_MIN_SAMPLE_SIZE = 20;

/** 慢调用榜条数。不可配。 */
export const SLOW_CALL_TOP_N = 10;

/** LLM 延迟 P99 告警阈值（毫秒）。P2 可配，此为默认。 */
export const ALERT_P99_LATENCY_MS = 15000;

/** 错误率告警阈值（0~1）。P2 可配，此为默认。 */
export const ALERT_ERROR_RATE = 0.05;

/** 单日费用告警阈值（美元）。P2 可配，此为默认。 */
export const ALERT_DAILY_COST_USD = 5.0;

/** 指标回落后判定「恢复」的持续时长（分钟）。P2 可配。 */
export const ALERT_RECOVER_MINUTES = 5;

/** 首屏聚合响应预算（毫秒，P0 目标）。不可配。 */
export const FIRST_PAINT_BUDGET_MS = 800;

/** 延迟直方图分桶边界（毫秒，左闭右开；最后一桶到 +∞）。 */
export const LATENCY_BUCKETS: [number, number][] = [
  [0, 2000],
  [2000, 4000],
  [4000, 6000],
  [6000, 8000],
  [8000, 10000],
  [10000, 12000],
  [12000, 14000],
  [14000, 16000],
  [16000, Number.POSITIVE_INFINITY],
];

/** 数据可视化调色板（按模型分配，非品牌色）。 */
export const MODEL_PALETTE = [
  "#7C3AED", // violet
  "#06B6D4", // cyan
  "#F59E0B", // amber
  "#EC4899", // pink
  "#22C55E", // green
  "#3B82F6", // blue
  "#8B5CF6",
  "#14B8A6",
];

/** 稳定地把模型名映射到调色板颜色（同名恒定同色）。 */
export function colorForModel(model: string): string {
  let h = 0;
  for (let i = 0; i < model.length; i++) h = (h * 31 + model.charCodeAt(i)) >>> 0;
  return MODEL_PALETTE[h % MODEL_PALETTE.length];
}

/* ── 降级事件（健康视图）──────────────────────────────────────────
 * 枚举值真相源在后端 pandapal/degradation.py（Severity / Category 两个 Literal）。
 * 此处只做「值 → 展示元数据」的映射；未知值一律回落原样文本 + 中性色，
 * 绝不吞掉——后端新增枚举时前端会原样显示，而不是静默漏行。 */

/** 严重度 → 展示元数据。abort=直接中止，ui_bubble=冒泡到 UI，log_only=只留痕。 */
export const SEVERITY_META: Record<string, { labelKey: string; color: string }> = {
  abort: { labelKey: "dashboard.severityAbort", color: "var(--danger)" },
  ui_bubble: { labelKey: "dashboard.severityUiBubble", color: "var(--warning)" },
  log_only: { labelKey: "dashboard.severityLogOnly", color: "var(--text-tertiary)" },
};

/** 字段类别 → 展示 key（i18n）。对应健壮性契约 §0 的四类字段 + 两类补充。 */
export const CATEGORY_LABEL: Record<string, string> = {
  decision: "dashboard.categoryDecision",
  id: "dashboard.categoryId",
  cost: "dashboard.categoryCost",
  exception_swallowed: "dashboard.categoryExceptionSwallowed",
  capability: "dashboard.categoryCapability",
  display: "dashboard.categoryDisplay",
};

/** 治理优先级：前三类（决策/ID/金额）是「本该失败却兜了底」，最该盯。 */
export const HIGH_STAKES_CATEGORIES = new Set(["decision", "id", "cost"]);
