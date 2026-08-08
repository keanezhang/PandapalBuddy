/**
 * src/components/ui/Button.tsx
 *
 * 全局按钮：薄封装 .btn 系列类（global-v2.css SECTION 8）。
 * variant 对应修饰类，颜色全部来自设计 token，主题自动适配。
 */

import type { ButtonHTMLAttributes } from "react";

export type ButtonVariant =
  | "primary"
  | "ghost"
  | "danger"
  | "danger-solid"
  | "success"
  | "accent";
export type ButtonSize = "xs" | "sm" | "md" | "lg";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
}

export function Button({
  variant = "primary",
  size = "md",
  className = "",
  ...rest
}: ButtonProps) {
  const cls = [
    "btn",
    `btn-${variant}`,
    size !== "md" ? `btn-${size}` : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");
  return <button className={cls} {...rest} />;
}
