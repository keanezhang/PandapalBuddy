/**
 * src/components/ui/toastStore.ts
 *
 * 全局 Toast 通知 store（zustand）。
 * 任意模块（含非 React 的 provider/store）均可通过 toast.success()/error() 推送；
 * 视图由 <ToastHost />（App 根挂载）统一渲染，样式类为 .toast-*（SECTION 20）。
 *
 * 从 skillStore 迁移而来：原 toast 仅 SkillsPage 本地渲染，现为全局能力。
 */

import { create } from "zustand";

export type ToastType = "success" | "error" | "info";

export interface ToastItem {
  id: number;
  type: ToastType;
  message: string;
  /** 高亮片段（如技能名/文件路径），等宽字体强调显示 */
  highlight?: string;
}

interface ToastState {
  toasts: ToastItem[];
  push: (t: Omit<ToastItem, "id">) => void;
  dismiss: (id: number) => void;
}

const AUTO_DISMISS_MS = 4000;
let nextId = 1;

export const useToastStore = create<ToastState>((set) => ({
  toasts: [],
  push: (t) => {
    const id = nextId++;
    set((s) => ({ toasts: [...s.toasts, { ...t, id }] }));
    setTimeout(() => {
      set((s) => ({ toasts: s.toasts.filter((x) => x.id !== id) }));
    }, AUTO_DISMISS_MS);
  },
  dismiss: (id) => set((s) => ({ toasts: s.toasts.filter((x) => x.id !== id) })),
}));

/** 非 React 环境可用的便捷 API */
export const toast = {
  success: (message: string, highlight?: string) =>
    useToastStore.getState().push({ type: "success", message, highlight }),
  error: (message: string) =>
    useToastStore.getState().push({ type: "error", message }),
  info: (message: string, highlight?: string) =>
    useToastStore.getState().push({ type: "info", message, highlight }),
};
