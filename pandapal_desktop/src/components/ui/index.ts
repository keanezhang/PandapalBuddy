/**
 * src/components/ui/index.ts — 基础 UI 组件统一出口
 */

export { Button, type ButtonVariant, type ButtonSize } from "./Button";
export { Modal } from "./Modal";
export { Badge, type BadgeVariant } from "./Badge";
export { Dropdown, type DropdownItem } from "./Dropdown";
export { ToastHost } from "./Toast";
export { toast, useToastStore, type ToastType } from "./toastStore";
export { GateScreen, GateLoading } from "./GateScreen";
