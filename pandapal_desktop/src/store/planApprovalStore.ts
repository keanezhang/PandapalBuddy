/**
 * src/store/planApprovalStore.ts
 *
 * Plan Mode 计划审批弹窗按 session 归档 store（与 hitlStore 同源同款）。
 *
 * 背景：Plan 审批原先走 BackendProvider 的全局单态 pendingPlanApproval，
 * 无 session 门控——B 会话的计划审批会在你正在看的 A 窗口弹出（跨 session 串窗）。
 * 数据不污染（决策带 run_id + 计划自带 session_id 路由正确），但 UI 会错弹。
 * 此 store 复用 hitlStore 的分桶 + 派生 selector 门控，彻底消除错弹。
 *
 * 设计：
 * - pendingBySessionId: Map<sessionId, PlanApprovalRequestMsg>
 * - 切走 session 时弹窗自动消失（selector 从 currentSessionId 派生）
 * - 切回 session 时若该 session 有 pending 计划则重新出现
 * - 收到审批决策 / 删除会话时清理对应条目
 */
import { create } from "zustand";
import { useSessionStore } from "./sessionStore";
import type { PlanApprovalRequestMsg } from "../types/api";

interface PlanApprovalState {
  pendingBySessionId: Map<string, PlanApprovalRequestMsg>;

  addPlan: (sessionId: string, plan: PlanApprovalRequestMsg) => void;
  removeBySession: (sessionId: string) => void;
  dropSession: (sessionId: string) => void;
  clear: () => void;
}

export const usePlanApprovalStore = create<PlanApprovalState>((set) => ({
  pendingBySessionId: new Map(),

  addPlan: (sessionId, plan) =>
    set((state) => {
      const next = new Map(state.pendingBySessionId);
      next.set(sessionId, plan);
      return { pendingBySessionId: next };
    }),

  removeBySession: (sessionId) =>
    set((state) => {
      if (!state.pendingBySessionId.has(sessionId)) return state;
      const next = new Map(state.pendingBySessionId);
      next.delete(sessionId);
      return { pendingBySessionId: next };
    }),

  dropSession: (sessionId) =>
    set((state) => {
      if (!state.pendingBySessionId.has(sessionId)) return state;
      const next = new Map(state.pendingBySessionId);
      next.delete(sessionId);
      return { pendingBySessionId: next };
    }),

  clear: () => set({ pendingBySessionId: new Map() }),
}));

// ── 派生 selectors ──────────────────────────────────────────

/** 当前视图 session 的待审批计划（null = 无弹窗） */
export function useCurrentPlan(): PlanApprovalRequestMsg | null {
  const sid = useSessionStore((s) => s.currentSessionId);
  const map = usePlanApprovalStore((s) => s.pendingBySessionId);
  if (!sid) return null;
  return map.get(sid) ?? null;
}

/** SessionItem 徽标用：某 session 是否有 pending 计划审批 */
export function useHasPendingPlan(sessionId: string): boolean {
  return usePlanApprovalStore((s) => s.pendingBySessionId.has(sessionId));
}
