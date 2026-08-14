/**
 * src/components/SettingsPanel.tsx — v2 重设计版
 *
 * 设置面板弹窗。支持 Tab 切换：壁纸 / 模型服务 / 语言。
 * 纯 v2 Token。
 */
import { useRef, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { WallpaperPicker } from "./WallpaperPicker";
import { ModelServiceSettings } from "./ModelServiceSettings";
import { LanguageSettings } from "./LanguageSettings";

type SettingsTab = "wallpaper" | "model" | "language";

interface Props { onClose: () => void }

export function SettingsPanel({ onClose }: Props) {
  const { t } = useTranslation();
  const panelRef = useRef<HTMLDivElement>(null);
  const [activeTab, setActiveTab] = useState<SettingsTab>("wallpaper");

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      const target = e.target as Node | null;
      // 防御：target 已在事件传播期间被 React 同步卸载（如 combobox 下拉项选中后
      // 移除）时，document.contains(target) 为 false，此点击语义已失效，
      // 不应视为「点外部」——否则会误关面板。
      if (!target || !document.contains(target)) return;
      if (panelRef.current && !panelRef.current.contains(target)) onClose();
    };
    const timer = setTimeout(() => document.addEventListener("mousedown", handler), 0);
    return () => { clearTimeout(timer); document.removeEventListener("mousedown", handler); };
  }, [onClose]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose]);

  const tabs: { id: SettingsTab; label: string; icon: string }[] = [
    { id: "wallpaper", label: t("settings.tab.wallpaper"), icon: "🎨" },
    { id: "model", label: t("settings.tab.model"), icon: "🔑" },
    { id: "language", label: t("settings.tab.language"), icon: "🌐" },
  ];

  return (
    <div className="modal-overlay">
      <div ref={panelRef} className="modal" style={{ width: 520, maxHeight: "85vh" }}>
        <div className="modal-header">
          <div style={{ display: "flex", alignItems: "center", gap: "var(--space-4)" }}>
            <span className="modal-title">{t("settings.title")}</span>
            {/* Tab 切换 */}
            <div style={{ display: "flex", gap: 2, marginLeft: "var(--space-4)" }}>
              {tabs.map((tab) => (
                <button
                  key={tab.id}
                  type="button"
                  onClick={() => setActiveTab(tab.id)}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 4,
                    padding: "4px 12px",
                    borderRadius: "var(--radius-sm)",
                    border: "none",
                    background: activeTab === tab.id
                      ? "var(--bg-selected)"
                      : "transparent",
                    color: activeTab === tab.id
                      ? "var(--accent)"
                      : "var(--text-tertiary)",
                    fontSize: "var(--text-xs)",
                    fontWeight: activeTab === tab.id ? 600 : 400,
                    fontFamily: "inherit",
                    cursor: "pointer",
                    transition: "all var(--duration-fast)",
                  }}
                  onMouseEnter={(e) => {
                    if (activeTab !== tab.id) {
                      e.currentTarget.style.background = "var(--bg-hover)";
                      e.currentTarget.style.color = "var(--text-primary)";
                    }
                  }}
                  onMouseLeave={(e) => {
                    if (activeTab !== tab.id) {
                      e.currentTarget.style.background = "transparent";
                      e.currentTarget.style.color = "var(--text-tertiary)";
                    }
                  }}
                >
                  <span style={{ fontSize: "var(--text-base)" }}>{tab.icon}</span>
                  {tab.label}
                </button>
              ))}
            </div>
          </div>
          <button className="modal-close" onClick={onClose}>✕</button>
        </div>
        <div className="modal-body">
          {activeTab === "wallpaper" && (
            <>
              <div style={{
                fontSize: "var(--text-2xs)", fontWeight: 600, color: "var(--text-muted)",
                textTransform: "uppercase", letterSpacing: "0.05em",
                marginBottom: "var(--space-2)",
              }}>
                {t("settings.tab.wallpaper")}
              </div>
              <WallpaperPicker />
            </>
          )}
          {activeTab === "model" && (
            <ModelServiceSettings onClose={onClose} />
          )}
          {activeTab === "language" && (
            <LanguageSettings />
          )}
        </div>
      </div>
    </div>
  );
}
