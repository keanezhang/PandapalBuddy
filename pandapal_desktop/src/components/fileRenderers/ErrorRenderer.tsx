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
      <span style={{ fontSize:24 }}>⚠️</span>
      <span style={{ fontSize:13 }}>无法读取文件</span>
      <span style={{ fontSize:11, color:"var(--text-muted)" }}>{error}</span>
    </div>
  );
}
