/**
 * src/components/fileRenderers/ErrorRenderer.tsx
 *
 * 文件读取错误渲染器
 */
import React from "react";
import { useTranslation } from "react-i18next";

interface ErrorRendererProps {
  error: string;
}

export function ErrorRenderer({ error }: ErrorRendererProps) {
  const { t } = useTranslation();
  return (
    <div style={{
      flex:1, display:"flex", alignItems:"center", justifyContent:"center",
      flexDirection:"column", gap:8, color:"var(--danger)", padding:24,
    }}>
      <span style={{ fontSize: "var(--text-3xl)" }}>⚠️</span>
      <span style={{ fontSize: "var(--text-base)" }}>{t("fileRenderers.readFailed")}</span>
      <span style={{ fontSize: "var(--text-xs)", color:"var(--text-muted)" }}>{error}</span>
    </div>
  );
}
