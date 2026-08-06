/**
 * src/components/fileRenderers/ImageRenderer.tsx
 *
 * 图片渲染器 — 读取文件字节生成 blob URL 显示（不依赖 asset 协议），
 * 支持缩放（按钮 + Ctrl/⌘ 滚轮），组件卸载时释放 blob URL。
 */
import { useState, useEffect, useRef, useCallback } from "react";
import { readFile } from "@tauri-apps/plugin-fs";

interface ImageRendererProps {
  /** 文件路径 */
  path: string;
}

const MIME: Record<string, string> = {
  png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif",
  webp: "image/webp", svg: "image/svg+xml", bmp: "image/bmp", ico: "image/x-icon",
};

const MIN_ZOOM = 0.1;
const MAX_ZOOM = 8;

function extOf(path: string): string {
  return path.split(".").pop()?.toLowerCase() || "";
}

export function ImageRenderer({ path }: ImageRendererProps) {
  const [url, setUrl] = useState("");
  const [error, setError] = useState(false);
  const [zoom, setZoom] = useState(1);

  useEffect(() => {
    let revoked = "";
    let cancelled = false;
    setUrl("");
    setError(false);
    setZoom(1);
    readFile(path)
      .then((bytes) => {
        if (cancelled) return;
        const type = MIME[extOf(path)] || "application/octet-stream";
        // 复制到独立 ArrayBuffer，避免 Uint8Array 视图偏移问题
        const blob = new Blob([new Uint8Array(bytes)], { type });
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

  const clamp = (z: number) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z));
  const zoomIn = () => setZoom((z) => clamp(z * 1.25));
  const zoomOut = () => setZoom((z) => clamp(z / 1.25));
  const reset = () => setZoom(1);

  const onWheel = useCallback((e: React.WheelEvent) => {
    if (!(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    setZoom((z) => clamp(z * (e.deltaY < 0 ? 1.1 : 1 / 1.1)));
  }, []);

  const btn: React.CSSProperties = {
    padding: "2px 10px", fontSize: 12, cursor: "pointer", minWidth: 32,
    border: "1px solid var(--border-subtle)", borderRadius: 4,
    background: "transparent", color: "var(--text-secondary)",
  };

  return (
    <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}>
      {/* 缩放工具栏 */}
      <div style={{
        display: "flex", alignItems: "center", gap: 6, padding: "4px 8px",
        background: "var(--bg-panel)", borderBottom: "1px solid var(--border-subtle)",
        flexShrink: 0,
      }}>
        <button onClick={zoomOut} style={btn} title="缩小">−</button>
        <span style={{ fontSize: 11, color: "var(--text-tertiary)", minWidth: 44, textAlign: "center" }}>
          {Math.round(zoom * 100)}%
        </span>
        <button onClick={zoomIn} style={btn} title="放大">+</button>
        <button onClick={reset} style={{ ...btn, minWidth: 0 }} title="重置">1:1</button>
        <span style={{ fontSize: 10, color: "var(--text-muted)", opacity: 0.6, marginLeft: 4 }}>
          Ctrl/⌘ + 滚轮缩放
        </span>
      </div>
      {/* 图片区 */}
      <div
        onWheel={onWheel}
        style={{
          flex: 1, display: "flex", alignItems: "center", justifyContent: "center",
          background: "repeating-conic-gradient(var(--bg-panel) 0% 25%, var(--bg-elevated) 0% 50%) 0 0/20px 20px",
          padding: 24, overflow: "auto",
        }}
      >
        {error ? (
          <span style={{ color: "var(--danger)", fontSize: 13 }}>图片加载失败</span>
        ) : url ? (
          <img
            src={url}
            alt={path}
            style={{
              transform: `scale(${zoom})`, transformOrigin: "center",
              maxWidth: "100%", maxHeight: "100%", objectFit: "contain",
              transition: "transform 0.05s linear",
            }}
            onError={() => setError(true)}
          />
        ) : (
          <span style={{ color: "var(--text-muted)", fontSize: 13 }}>加载中...</span>
        )}
      </div>
    </div>
  );
}
