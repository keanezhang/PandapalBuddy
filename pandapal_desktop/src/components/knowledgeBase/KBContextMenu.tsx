/**
 * src/components/knowledgeBase/KBContextMenu.tsx
 *
 * 文件树右键菜单（对齐原型 #menu-host / .ctx-menu）。
 * 菜单项随节点类型（文件 / 文件夹）、是否位于根目录动态变化。
 */
import { useEffect, useRef } from "react";
import type { KBDocNode } from "../../types/api";

export type KbMenuAction =
  | "preview"
  | "new-folder"
  | "upload-here"
  | "move-to-root"
  | "rename"
  | "delete";

export interface KBContextMenuProps {
  x: number;
  y: number;
  node: KBDocNode;
  onAction: (action: KbMenuAction) => void;
  onClose: () => void;
}

interface MenuItem {
  action: KbMenuAction;
  label: string;
  icon: string;
  danger?: boolean;
}

const MENU_WIDTH = 176;
const MENU_HEIGHT = 260;

export function KBContextMenu({ x, y, node, onAction, onClose }: KBContextMenuProps) {
  const ref = useRef<HTMLDivElement>(null);

  // 点击外部 / Esc 关闭
  useEffect(() => {
    const onDocDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", onDocDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  const hasParent = node.path.includes("/");
  const items: MenuItem[] = [];
  if (!node.is_dir) {
    items.push({ action: "preview", label: "预览", icon: "👁" });
  }
  items.push({ action: "new-folder", label: "新建文件夹", icon: "🗂" });
  items.push({ action: "upload-here", label: "上传到此处", icon: "⬆" });
  items.push({ action: "rename", label: "重命名", icon: "✎" });
  if (hasParent) items.push({ action: "move-to-root", label: "移到根目录", icon: "⇱" });
  items.push({ action: "delete", label: "删除", icon: "🗑", danger: true });

  const left = Math.min(x, Math.max(8, window.innerWidth - MENU_WIDTH - 8));
  const top = Math.min(y, Math.max(8, window.innerHeight - MENU_HEIGHT - 8));

  return (
    <div
      data-testid="kb-context-menu"
      ref={ref}
      role="menu"
      style={{
        position: "fixed",
        left,
        top,
        minWidth: MENU_WIDTH,
        background: "var(--bg-elevated, #1c1c1c)",
        border: "1px solid var(--border-subtle)",
        borderRadius: "var(--radius-sm, 7px)",
        padding: 5,
        boxShadow: "0 12px 34px rgba(0,0,0,0.6)",
        zIndex: 1000,
      }}
    >
      {items.map((it) => (
        <div
          key={it.action}
          role="menuitem"
          onClick={() => {
            onAction(it.action);
            onClose();
          }}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 9,
            padding: "7px 10px",
            borderRadius: 5,
            fontSize: "var(--text-sm)",
            cursor: "pointer",
            whiteSpace: "nowrap",
            color: it.danger ? "var(--danger)" : "var(--text-primary)",
          }}
          onMouseEnter={(e) => (e.currentTarget.style.background = "var(--bg-hover)")}
          onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
        >
          <span style={{ width: 15, display: "inline-flex", justifyContent: "center" }}>{it.icon}</span>
          <span>{it.label}</span>
        </div>
      ))}
    </div>
  );
}

