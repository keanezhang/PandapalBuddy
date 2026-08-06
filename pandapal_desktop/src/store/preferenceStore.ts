/**
 * src/store/preferenceStore.ts
 *
 * 用户 UI 偏好管理。
 * - 持久化到 localStorage（Zustand persist）
 * - Fail-Safe 默认值：新用户开箱即用
 * - viewerVisible 由 splitRatio 推导（ratio < 0.99 → true），不独立存储
 */

import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { AgentMode } from "../types/api";

export type SectionId = "task" | "skill" | "session" | "file" | "quick_apps";

interface PreferenceState {
  /** 深度思考开关 */
  deepThinking: boolean;
  /** Agent 人格模式：coding=编码 / office=办公助手（缺省 office） */
  mode: AgentMode;
  /** 面板交换（聊天 ↔ 查看器位置） */
  swapped: boolean;
  /** 聊天面板占比 [0.5, 1.0]，默认 0.6（查看器展开） */
  splitRatio: number;
  /** 侧边栏折叠 */
  sidebarCollapsed: boolean;
  /** 侧边栏宽度（拖拽调节），默认 260，范围 [180, 400] */
  sidebarWidth: number;
  /** 各 Section 折叠状态，文件 Section 默认折叠 */
  sectionCollapsed: Record<SectionId, boolean>;

  /** 派生：查看器是否可见（splitRatio < 0.99） */
  viewerVisible: () => boolean;

  toggleDeepThinking: () => void;
  setMode: (mode: AgentMode) => void;
  swapPanels: () => void;
  setSplitRatio: (ratio: number) => void;
  toggleViewer: () => void;
  toggleSidebar: () => void;
  setSidebarWidth: (width: number) => void;
  toggleSection: (sectionId: SectionId) => void;
}

export const usePreferenceStore = create<PreferenceState>()(
  persist(
    (set, get) => ({
      deepThinking: false,
      mode: "office",
      swapped: false,
      splitRatio: 0.6,
      sidebarCollapsed: false,
      sidebarWidth: 260,
      sectionCollapsed: { task: false, skill: false, session: false, file: true, quick_apps: false },

      viewerVisible: () => get().splitRatio < 0.99,

      toggleDeepThinking: () => {
        const next = !get().deepThinking;
        set({ deepThinking: next });
        console.debug("[pref] deepThinking:", next);
      },

      setMode: (mode) => {
        set({ mode });
        console.debug("[pref] mode:", mode);
      },

      swapPanels: () => {
        const next = !get().swapped;
        set({ swapped: next });
        console.debug("[layout] swapped:", next);
      },

      setSplitRatio: (ratio) => {
        // 聊天最小 30%（查看器最大 70%），最大 100%（收起查看器）
        const clamped = Math.min(1.0, Math.max(0.3, ratio));
        set({ splitRatio: clamped });
      },

      toggleViewer: () => {
        const current = get().splitRatio;
        // 切换：查看器可见 ↔ 不可见，展开时回退到 0.6
        const next = current >= 0.99 ? 0.6 : 1.0;
        set({ splitRatio: next });
        console.debug("[layout] viewer:", next < 0.99 ? "open" : "closed");
      },

      toggleSidebar: () =>
        set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),

      setSidebarWidth: (width) =>
        set({ sidebarWidth: Math.min(400, Math.max(180, width)) }),

      toggleSection: (sectionId) =>
        set((s) => ({
          sectionCollapsed: {
            ...s.sectionCollapsed,
            [sectionId]: !s.sectionCollapsed[sectionId],
          },
        })),
    }),
    { name: "pandapal-preference" }
  )
);
