/**
 * src/components/fileRenderers/PdfRenderer.tsx
 *
 * PDF 渲染器 — 读取文件字节生成 blob URL，通过 <iframe> 交给 webview 内置
 * PDF 阅读器显示（CSP 为 null，blob: 可直接嵌入）。组件卸载时释放 blob URL。
 *
 * 大小闸门（≤50MB）已在 fileStore.loadAndOpenFile 完成；此处仅负责渲染。
 */
import { useState, useEffect } from "react";
import { readFile } from "@tauri-apps/plugin-fs";

interface PdfRendererProps {
  path: string;
}

export function PdfRenderer({ path }: PdfRendererProps) {
  const [url, setUrl] = useState("");
  const [error, setError] = useState(false);

  useEffect(() => {
    let revoked = "";
    let cancelled = false;
    setUrl("");
    setError(false);
    readFile(path)
      .then((bytes) => {
        if (cancelled) return;
        const blob = new Blob([new Uint8Array(bytes)], { type: "application/pdf" });
        const u = URL.createObjectURL(blob);
        revoked = u;
        setUrl(u);
      })
      .catch(() => { if (!cancelled) setError(true); });
    return () => {
      cancelled = true;
      if (revoked) URL.revokeObjectURL(revoked);
    };
  }, [path]);

  if (error) {
    return (
      <div style={{
        flex: 1, display: "flex", alignItems: "center", justifyContent: "center",
        color: "var(--danger)", fontSize: 13,
      }}>
        PDF 加载失败
      </div>
    );
  }

  if (!url) {
    return (
      <div style={{
        flex: 1, display: "flex", alignItems: "center", justifyContent: "center",
        color: "var(--text-muted)", fontSize: 13,
      }}>
        加载中...
      </div>
    );
  }

  return (
    <iframe
      src={url}
      title={path}
      style={{ flex: 1, width: "100%", height: "100%", border: "none", background: "var(--bg-root)" }}
    />
  );
}
