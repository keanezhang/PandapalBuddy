/**
 * src/components/ui/Dropdown.tsx
 *
 * 全局下拉菜单：触发器 + 菜单项，内置开关状态与外部点击关闭。
 * 菜单样式复用 .dropdown-menu / .dropdown-item（SECTION 11），
 * 定位为组件内置（absolute 贴触发器），调用方无需再内联覆盖。
 */

import { useEffect, useRef, useState, type ReactNode } from "react";

export interface DropdownItem {
  label: string;
  onClick: () => void;
  danger?: boolean;
}

interface DropdownProps {
  /** 触发器（通常是一个 Button） */
  trigger: ReactNode;
  items: DropdownItem[];
  /** 菜单对齐触发器的哪一侧，默认右对齐 */
  align?: "left" | "right";
}

export function Dropdown({ trigger, items, align = "right" }: DropdownProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as HTMLElement)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  return (
    <div ref={rootRef} style={{ position: "relative", flexShrink: 0 }}>
      <div onClick={() => setOpen((v) => !v)}>{trigger}</div>
      {open && (
        <div
          className="dropdown-menu"
          style={{ position: "absolute", top: "100%", [align]: 0 }}
        >
          {items.map((item) => (
            <button
              key={item.label}
              className="dropdown-item"
              style={item.danger ? { color: "var(--danger)" } : undefined}
              onClick={() => {
                setOpen(false);
                item.onClick();
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
