/**
 * src/store/dashboardStore.ts — 看板快照状态。
 *
 * 后端 DASHBOARD_DATA 事件 → BackendProvider 分发 → setSnapshot。
 * DashboardPage 订阅 snapshot；为空（尚未拉取/无数据）时页面回退到 MOCK_SNAPSHOT。
 */

import { create } from "zustand";
import type { DashboardSnapshot } from "../types/dashboard";

interface DashboardState {
  snapshot: DashboardSnapshot | null;
  loading: boolean;
  setSnapshot: (s: DashboardSnapshot) => void;
  setLoading: (v: boolean) => void;
}

export const useDashboardStore = create<DashboardState>((set) => ({
  snapshot: null,
  loading: false,
  setSnapshot: (s) => set({ snapshot: s, loading: false }),
  setLoading: (v) => set({ loading: v }),
}));
