/**
 * src/components/knowledgeBase/KBDropZone.tsx
 *
 * 外部文件拖拽上传区（F14 / R1）。
 *
 * Tauri webview 会拦截操作系统文件拖拽（HTML5 dataTransfer 不可用），故经
 * `getCurrentWebviewWindow().onDragDropEvent` 取绝对路径列表；落点目录由
 * 屏幕坐标命中树中带 `data-drop-dir` 的元素解析（folder=自身 path，file=父目录）。
 */
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { getCurrentWebviewWindow } from "@tauri-apps/api/webviewWindow";

export interface KBDropZoneProps {
  children: ReactNode;
  /** 松开时回调：本地绝对路径列表 + 目标目录（"" = 根） */
  onFilesDropped: (paths: string[], targetDir: string) => void;
  /** 悬停目标变化（供文件树高亮）；null = 无目标 */
  onDropTargetChange?: (targetDir: string | null) => void;
}

/**
 * 由屏幕（逻辑）坐标解析落点目录（命中最近的 `[data-drop-dir]` 元素）。
 * 导出便于单测。
 */
export function resolveDropDirAt(x: number, y: number): string | null {
  const el = document.elementFromPoint(x, y) as HTMLElement | null;
  if (!el) return null;
  const holder = el.closest?.("[data-drop-dir]") as HTMLElement | null;
  if (!holder) return null;
  return holder.getAttribute("data-drop-dir") ?? null;
}

export function KBDropZone({ children, onFilesDropped, onDropTargetChange }: KBDropZoneProps) {
  const [active, setActive] = useState(false);
  const dropRef = useRef(onFilesDropped);
  dropRef.current = onFilesDropped;
  const targetRef = useRef(onDropTargetChange);
  targetRef.current = onDropTargetChange;

  useEffect(() => {
    let unlisten: (() => void) | undefined;
    let disposed = false;

    getCurrentWebviewWindow()
      .onDragDropEvent((event) => {
        const p = event.payload;
        if (p.type === "leave") {
          setActive(false);
          targetRef.current?.(null);
          return;
        }
        const dpr = window.devicePixelRatio || 1;
        const dir = resolveDropDirAt(p.position.x / dpr, p.position.y / dpr);
        if (p.type === "drop") {
          setActive(false);
          targetRef.current?.(null);
          if (p.paths.length > 0) dropRef.current(p.paths, dir ?? "");
        } else {
          setActive(true);
          targetRef.current?.(dir);
        }
      })
      .then((fn) => {
        if (disposed) fn();
        else unlisten = fn;
      })
      .catch(() => {
        /* 非 Tauri 环境（如 Web 预览）下静默降级 */
      });

    return () => {
      disposed = true;
      unlisten?.();
    };
  }, []);

  return (
    <div style={{ position: "relative", display: "flex", flexDirection: "column", flex: 1, minHeight: 0, height: "100%" }}>
      {children}
      {active && (
        <div
          data-testid="kb-drop-overlay"
          style={{
            position: "absolute",
            inset: 0,
            zIndex: 10,
            pointerEvents: "none",
            border: "2px dashed var(--accent)",
            borderRadius: "var(--radius-md)",
            background: "color-mix(in srgb, var(--accent) 10%, transparent)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <div
            style={{
              background: "var(--bg-elevated)",
              borderRadius: "var(--radius-md)",
              padding: "12px 20px",
              fontSize: "var(--text-sm)",
              color: "var(--text-primary)",
              boxShadow: "var(--shadow-lg, 0 8px 26px rgba(0,0,0,0.5))",
            }}
          >
            📥 松开以导入到目标文件夹
          </div>
        </div>
      )}
    </div>
  );
}

