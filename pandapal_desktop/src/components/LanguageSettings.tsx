/**
 * src/components/LanguageSettings.tsx — v2 重设计版
 *
 * 界面语言设置（i18n）。语言切换即时生效并持久化（preferenceStore.locale）。
 * 纯 v2 Token。
 */
import { useTranslation } from "react-i18next";
import { applyLocale } from "../i18n";
import { usePreferenceStore } from "../store/preferenceStore";
import type { AppLocale } from "../store/preferenceStore";

const LOCALE_OPTIONS: { id: AppLocale; labelKey: string }[] = [
  { id: "zh-CN", labelKey: "settings.language.zh" },
  { id: "en-US", labelKey: "settings.language.en" },
];

export function LanguageSettings() {
  const { t } = useTranslation();
  const locale = usePreferenceStore((s) => s.locale);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)" }}>
      <div style={{
        fontSize: "var(--text-2xs)", fontWeight: 600, color: "var(--text-muted)",
        textTransform: "uppercase", letterSpacing: "0.05em",
      }}>
        {t("settings.language.label")}
      </div>
      <div style={{ display: "flex", gap: "var(--space-2)" }}>
        {LOCALE_OPTIONS.map((opt) => {
          const active = locale === opt.id;
          return (
            <button
              key={opt.id}
              type="button"
              onClick={() => applyLocale(opt.id)}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                padding: "6px 16px",
                borderRadius: "var(--radius-sm)",
                border: active ? "1px solid var(--accent)" : "1px solid var(--border)",
                background: active ? "var(--bg-selected)" : "var(--bg-elevated)",
                color: active ? "var(--accent)" : "var(--text-primary)",
                fontSize: "var(--text-sm)",
                fontWeight: active ? 600 : 400,
                fontFamily: "inherit",
                cursor: "pointer",
                transition: "all var(--duration-fast)",
              }}
            >
              {t(opt.labelKey)}
            </button>
          );
        })}
      </div>
      <div style={{ fontSize: "var(--text-xs)", color: "var(--text-tertiary)" }}>
        {t("settings.language.description")}
      </div>
    </div>
  );
}
