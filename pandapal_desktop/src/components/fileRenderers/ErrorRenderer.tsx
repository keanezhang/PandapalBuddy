/**
 * src/components/fileRenderers/ErrorRenderer.tsx
 *
 * 文件读取错误渲染器
 */
import React from "react";

interface ErrorRendererProps {
  error: string;
}

export function ErrorRenderer({ error }: ErrorRendererProps) {
  return (
    <div style={{
      flex:1, display:"flex", alignItems:"center", justifyContent:"center",
      flexDirection:"column", gap:8, color:"var(--danger)", padding:24,
    }}>
      <span style={{ fontSize: "var(--text-3xl)" }}>⚠️</span>
      <span style={{ fontSize: "var(--text-base)" }}>无法读取文件</span>
      <span style={{ fontSize: "var(--text-xs)", color:"var(--text-muted)" }}>{error}</span>
    </div>
  );
}
