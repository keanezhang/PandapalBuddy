/**
 * src/components/BudgetSettingsModal.tsx — 预算额度设置弹窗（按 provider 分账）。
 *
 * 为某个 LLM 厂家（provider）设/改额度：选 provider + 币种（默认 CNY）+ 输入额度。
 * 保存 → BackendProvider.setBudget → SET_BUDGET IPC → 后端 BudgetLedger.set_budget。
 * 额度以用户币种记于 limit_native；内部消费按静态汇率归一为 spent_usd（PRD R9）。
 *
 * provider 清单与显示名一律取自 provider_catalog.toml（经 credentialStore），
 * 本文件不得再出现任何 provider 常量表或硬编码 provider id（PRD G5 / Story 5）。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useBackend } from "../providers/BackendProvider";
import { useCredentialStore } from "../store/credentialStore";
import type { BudgetView } from "../types/api";

interface Props {
  onClose: () => void;
  budgets: BudgetView[]; // 现有额度（用于回填某 provider 已设的币种/额度）
}

// ⚠️ 这里曾有一张硬编码的 PROVIDERS 表 + useState("openai") 种子。两者都已删除：
//    provider 清单与显示名的唯一来源是 provider_catalog.toml（PRD G5 / Story 5）。
//    硬编码副本会让「新增 provider 后用户能配却设不了预算」（Story 5 反例原文）。
// 币种以 CNY 为主：单价填写与预算额度都以 CNY 记，USD 仅为内部归一口径（PRD R9）。
const CURRENCIES = ["CNY", "USD"];

export function BudgetSettingsModal({ onClose, budgets }: Props) {
  const { setBudget } = useBackend();
  const panelRef = useRef<HTMLDivElement>(null);

  const providerCatalog = useCredentialStore((s) => s.providerCatalog);
  const loadCatalog = useCredentialStore((s) => s.loadCatalog);

  // 初值为空串，catalog 到位后再落到**目录首项**——不硬编码任何 provider id
  const [provider, setProvider] = useState("");
  const [currency, setCurrency] = useState("CNY");
  const [limit, setLimit] = useState("");

  useEffect(() => {
    loadCatalog();
  }, [loadCatalog]);

  useEffect(() => {
    if (provider || providerCatalog.length === 0) return;
    setProvider(providerCatalog[0].id);
  }, [providerCatalog, provider]);

  // 切换 provider 时回填其已有额度（若已设过）
  const byProvider = useMemo(() => {
    const m = new Map<string, BudgetView>();
    for (const b of budgets) m.set(b.provider, b);
    return m;
  }, [budgets]);
  useEffect(() => {
    const existing = byProvider.get(provider);
    if (existing && existing.limit_native != null) {
      setCurrency(existing.currency);
      setLimit(String(existing.limit_native));
    } else {
      setLimit("");
    }
  }, [provider, byProvider]);

  // 点外部关闭 + Esc 关闭（与 SettingsPanel 一致）
  useEffect(() => {
    const h = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) onClose();
    };
    const t = setTimeout(() => document.addEventListener("mousedown", h), 0);
    return () => { clearTimeout(t); document.removeEventListener("mousedown", h); };
  }, [onClose]);
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", h);
    return () => document.removeEventListener("keydown", h);
  }, [onClose]);

  const amount = parseFloat(limit);
  // provider 是 ID 类字段：为空时不允许提交，绝不 `or 默认`（§九）
  const valid = Number.isFinite(amount) && amount >= 0 && provider.length > 0;
  const symbol = currency === "CNY" ? "¥" : "$";

  const save = () => {
    if (!valid) return;
    setBudget(provider, currency, amount);
    onClose();
  };

  const labelStyle: React.CSSProperties = {
    fontSize: 10, fontWeight: 600, color: "var(--text-muted)",
    textTransform: "uppercase", letterSpacing: "0.05em",
    margin: "0 0 6px",
  };
  const inputStyle: React.CSSProperties = {
    width: "100%", padding: "8px 10px", borderRadius: 8,
    border: "1px solid var(--border-subtle)", background: "var(--bg-root)",
    color: "var(--text-primary)", fontSize: 13, boxSizing: "border-box",
  };

  return (
    <div className="modal-overlay">
      <div ref={panelRef} className="modal" style={{ width: 420 }}>
        <div className="modal-header">
          <span className="modal-title">💳 设置预算额度</span>
          <button className="modal-close" onClick={onClose}>✕</button>
        </div>
        <div className="modal-body" style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <div style={{ fontSize: "var(--text-sm)", color: "var(--text-tertiary)", lineHeight: 1.5 }}>
            为每个 LLM 厂家分别设置总额度（各家充值独立）。累计净费用达额度即
            <b>只停该厂家</b>，其余照常；可随时上调。
          </div>

          <div>
            <div style={labelStyle}>厂家 Provider</div>
            <select
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              style={inputStyle}
              disabled={providerCatalog.length === 0}
            >
              {providerCatalog.map((p) => (
                <option key={p.id} value={p.id}>{p.display_name}</option>
              ))}
            </select>
            {providerCatalog.length === 0 && (
              <div style={{ fontSize: "var(--text-xs)", color: "var(--danger)", marginTop: 6 }}>
                系统 provider 目录未加载，暂时无法设置预算
              </div>
            )}
          </div>

          <div style={{ display: "flex", gap: 12 }}>
            <div style={{ width: 110 }}>
              <div style={labelStyle}>币种</div>
              <select value={currency} onChange={(e) => setCurrency(e.target.value)} style={inputStyle}>
                {CURRENCIES.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </div>
            <div style={{ flex: 1 }}>
              <div style={labelStyle}>总额度（{symbol}）</div>
              <input
                type="number" min={0} step="0.01" value={limit}
                onChange={(e) => setLimit(e.target.value)}
                placeholder={`如 ${symbol}50`} style={inputStyle}
                onKeyDown={(e) => { if (e.key === "Enter" && valid) save(); }}
              />
            </div>
          </div>

          {byProvider.get(provider)?.limit_native != null && (
            <div style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
              当前已用 {symbol}{(byProvider.get(provider)!.spent_native).toFixed(2)}
              {" · "}设 0 或低于已用将立即进入「耗尽」拦截。
            </div>
          )}
        </div>
        <div className="modal-footer" style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button className="btn" onClick={onClose}>取消</button>
          <button
            className="btn btn-primary" onClick={save} disabled={!valid}
            style={{ opacity: valid ? 1 : 0.5, cursor: valid ? "pointer" : "not-allowed" }}
          >
            保存
          </button>
        </div>
      </div>
    </div>
  );
}
