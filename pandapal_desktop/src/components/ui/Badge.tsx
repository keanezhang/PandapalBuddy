/**
 * src/components/ui/Badge.tsx
 *
 * 全局徽章：薄封装 .badge 系列类（SECTION 9），颜色全部来自 token。
 */

import type { ReactNode } from "react";

export type BadgeVariant =
  | "default"
  | "purple"
  | "green"
  | "yellow"
  | "red"
  | "blue";

interface BadgeProps {
  variant?: BadgeVariant;
  className?: string;
  children: ReactNode;
}

export function Badge({ variant = "default", className = "", children }: BadgeProps) {
  const cls = ["badge", `badge-${variant}`, className].filter(Boolean).join(" ");
  return <span className={cls}>{children}</span>;
}
