/**
 * src/components/SplitDivider.tsx
 *
 * 可拖拽分割线（聊天面板 ↔ 文件查看器）。
 * - 宽度 5px，光标 col-resize
 * - 拖拽范围：聊天 30%～100%（查看器最大 70%）
 * - 拖到聊天侧尽头（ratio ≥ 0.99）自动收起查看器
 * - swapped 模式下左侧面板是查看器，测得的左宽对应查看器宽度，需反相
 */

import React, { useRef } from "react";
import { usePreferenceStore } from "../store/preferenceStore";

interface SplitDividerProps {
  containerRef: React.RefObject<HTMLDivElement | null>;
}

export function SplitDivider({ containerRef }: SplitDividerProps) {
  const setSplitRatio = usePreferenceStore((s) => s.setSplitRatio);
  const dragging = useRef(false);

  const handleMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    dragging.current = true;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";

    const onMove = (ev: MouseEvent) => {
      if (!dragging.current || !containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const totalWidth = rect.width;
      // 分割线左侧区域的宽度占比
      const leftFraction = (ev.clientX - rect.left) / totalWidth;
      // 非交换：左侧是聊天 → chatFraction = leftFraction
      // 交换：左侧是查看器 → chatFraction = 1 - leftFraction（否则拖拽方向相反）
      const swapped = usePreferenceStore.getState().swapped;
      const chatFraction = swapped ? 1 - leftFraction : leftFraction;
      const ratio = Math.min(1.0, Math.max(0.3, chatFraction));
      setSplitRatio(ratio);

      if (ratio >= 0.99) {
        dragging.current = false;
        cleanup();
      }
    };

    const onUp = () => {
      dragging.current = false;
      cleanup();
    };

    const cleanup = () => {
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };

    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  };

  return (
    <div
      onMouseDown={handleMouseDown}
      style={{
        width: 5,
        cursor: "col-resize",
        background: "var(--bg-elevated)",
        flexShrink: 0,
        transition: "background 150ms",
      }}
      onMouseEnter={(e) => {
        (e.currentTarget as HTMLDivElement).style.background = "var(--accent-soft)";
      }}
      onMouseLeave={(e) => {
        (e.currentTarget as HTMLDivElement).style.background = "var(--bg-elevated)";
      }}
    />
  );
}
