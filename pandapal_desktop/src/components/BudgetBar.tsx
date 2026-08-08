/**
 * src/components/BudgetBar.tsx — 预算额度条（按 provider 分账，三态，可折叠）。
 *
 * 多厂家额度：默认折叠，仅显示「当前正在使用的模型」所属 provider 的额度；
 * 点击展开后逐条列出全部已设额度的 provider。每条：已用 / 剩余 + 进度条
 * （绿 normal / 黄 near / 红 exhausted）。
 * 数据源：useBudgetStore（后端 BUDGET_STATUS 推送）+ useModelStore（当前激活模型）。
 * 挂载时 budgetQuery() 主动刷新一次。「设置」按钮打开 BudgetSettingsModal。
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useBackend } from "../providers/BackendProvider";
import { useBudgetStore } from "../store/budgetStore";
import { useCredentialStore } from "../store/credentialStore";
import { useModelStore } from "../store/modelStore";
import type { BudgetView, ProviderMeta } from "../types/api";
import { BudgetSettingsModal } from "./BudgetSettingsModal";

// ⚠️ 这里曾有一张硬编码的 PROVIDER_LABEL 表。它已被删除：
//    provider 显示名的唯一来源是 provider_catalog.toml（PRD G5 / Story 5）。
//    多一张前端副本，就意味着发布者加了新 provider 之后，用户能配却在预算条里
//    看到裸 id——「增删 provider 只改一处」的目标当场失效。
//    catalog 里查不到时兜底显示裸 id（展示类字段，可回落，见 §九）。
const displayNameOf = (providerId: string, catalog: ProviderMeta[]): string =>
  catalog.find((p) => p.id === providerId)?.display_name ?? providerId;

const STATE_COLOR: Record<BudgetView["state"], string> = {
  normal: "var(--success)",
  near: "var(--warning)",
  exhausted: "var(--danger)",
  unset: "var(--text-muted)",
};

const sym = (c: string) => (c === "CNY" ? "¥" : "$");

/** 单条 provider 额度：名称 + 三态标记 + 已用/额度 + 进度条 */
function BudgetRow({
  b,
  current,
  catalog,
}: {
  b: BudgetView;
  current?: boolean;
  catalog: ProviderMeta[];
}) {
  const { t } = useTranslation();
  const color = STATE_COLOR[b.state];
  const pct = Math.min(100, Math.max(0, b.usage_ratio * 100));
  const s = sym(b.currency);
  return (
    <div
      style={{ minWidth: 190, flex: "0 1 240px" }}
      title={
        t("budget.rowTitleSpent", { spent: `${s}${b.spent_native.toFixed(2)}`, limit: `${s}${b.limit_native ?? 0}` }) +
        (b.state === "exhausted" ? t("budget.stateExhaustedSuffix") : b.state === "near" ? t("budget.stateNearSuffix") : "")
      }
    >
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: "var(--text-xs)", marginBottom: 4 }}>
        <span style={{ color: "var(--text-secondary)", fontWeight: 600, display: "flex", alignItems: "center", gap: 5 }}>
          {displayNameOf(b.provider, catalog)}
          {current && (
            <span style={{ fontSize: "var(--text-2xs)", fontWeight: 600, padding: "0 5px", borderRadius: 20, background: "color-mix(in srgb, var(--accent) 14%, transparent)", color: "var(--accent-soft)" }}>
              {t("budget.inUse")}
            </span>
          )}
          {b.state === "exhausted" && <span style={{ color: "var(--danger)" }}>· {t("budget.stateExhausted")}</span>}
          {b.state === "near" && <span style={{ color: "var(--warning)" }}>· {t("budget.stateNear")}</span>}
        </span>
        <span className="mono" style={{ color: "var(--text-tertiary)" }}>
          {s}{b.spent_native.toFixed(2)} / {s}{b.limit_native ?? 0}
        </span>
      </div>
      <div style={{ height: 6, borderRadius: 4, background: "var(--bg-track)", overflow: "hidden" }}>
        <div style={{ height: "100%", width: `${pct}%`, background: color, borderRadius: 4, transition: "width 0.3s" }} />
      </div>
    </div>
  );
}

export function BudgetBar() {
  const { t } = useTranslation();
  const { budgetQuery } = useBackend();
  const budgets = useBudgetStore((s) => s.budgets);
  // 「当前使用中」= 用户实际选中的模型所属 provider。
  // 旧写法取 availableModels[0]，那是清单首项而非当前选择——多模型下必然标错。
  const currentProvider = useModelStore(
    (s) => s.availableModels.find((m) => m.id === s.currentModelId)?.provider,
  );
  const providerCatalog = useCredentialStore((s) => s.providerCatalog);
  const loadCatalog = useCredentialStore((s) => s.loadCatalog);
  const [open, setOpen] = useState(false);       // 设置弹窗
  const [expanded, setExpanded] = useState(false); // 额度列表：默认折叠

  useEffect(() => {
    budgetQuery();
  }, [budgetQuery]);

  // 显示名唯一来源 = catalog；预算条可能先于配置页挂载，这里自行确保拉过一次
  useEffect(() => {
    loadCatalog();
  }, [loadCatalog]);

  // 当前正在使用的模型所属 provider 的额度；找不到则退回第一条
  const currentBudget =
    budgets.find((b) => b.provider === currentProvider) ?? budgets[0];
  const multiple = budgets.length > 1;

  return (
    <div
      style={{
        display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap",
        padding: "10px 14px", borderRadius: 12, marginBottom: 14,
        background: "var(--bg-elevated, rgba(127,127,127,0.06))",
        border: "1px solid var(--border-subtle)",
      }}
    >
      <span style={{ fontSize: "var(--text-sm)", fontWeight: 600, color: "var(--text-secondary)", display: "flex", alignItems: "center", gap: 6, flexShrink: 0 }}>
        💳 {t("budget.title")}<span style={{ fontWeight: 400, color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>{t("budget.titleHint")}</span>
      </span>

      {budgets.length === 0 ? (
        <span style={{ flex: 1, fontSize: "var(--text-sm)", color: "var(--text-muted)" }}>
          {t("budget.unsetHint")}
        </span>
      ) : (
        <div style={{ flex: 1, display: "flex", gap: 18, flexWrap: "wrap", alignItems: "center" }}>
          {/* 折叠：仅当前使用中模型的 provider；展开：全部 */}
          {(expanded ? budgets : currentBudget ? [currentBudget] : []).map((b) => (
            <BudgetRow
              key={b.provider}
              b={b}
              current={!expanded && b.provider === currentProvider}
              catalog={providerCatalog}
            />
          ))}
          {multiple && (
            <button
              onClick={() => setExpanded((v) => !v)}
              style={{
                fontSize: "var(--text-xs)", padding: "3px 10px", borderRadius: 8, cursor: "pointer",
                border: "1px solid var(--border-subtle)", background: "transparent",
                color: "var(--text-tertiary)", flexShrink: 0, whiteSpace: "nowrap",
              }}
              title={expanded ? t("budget.collapseTitle") : t("budget.expandTitle")}
            >
              {expanded ? t("budget.collapse") : t("budget.expandAll", { count: budgets.length })}
            </button>
          )}
        </div>
      )}

      <button
        onClick={() => setOpen(true)}
        style={{
          fontSize: "var(--text-sm)", padding: "5px 12px", borderRadius: 8, cursor: "pointer",
          border: "1px solid var(--border-subtle)", background: "transparent",
          color: "var(--accent-soft)", flexShrink: 0,
        }}
      >
        ⚙ {t("budget.settings")}
      </button>

      {open && <BudgetSettingsModal onClose={() => setOpen(false)} budgets={budgets} />}
    </div>
  );
}
