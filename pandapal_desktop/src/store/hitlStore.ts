/**
 * src/store/hitlStore.ts
 *
 * HITL 弹窗按 session 归档 store（v003 会话列表引入）。
 *
 * 设计：
 * - pendingBySessionId: Map<sessionId, HitlPrompt>
 * - 切走 session 时弹窗自动消失（selector 从 currentSessionId 派生）
 * - 切回 session 时若该 session 有 pending 弹窗则重新出现
 * - 收到 APPROVAL_RESULT 或删除会话时清理对应条目
 *
 * P4 派生状态：useCurrentPrompt 派生自 currentSessionId + pendingBySessionId，
 * 不单独存 currentPrompt 字段。
 */
import { create } from "zustand";
import { useSessionStore } from "./sessionStore";

export interface HitlPrompt {
  approvalId: string;
  sessionId: string;
  toolName: string;
  toolArgsSummary: Record<string, unknown>;
  runId: string;
  createdAt: number;
}

interface HitlState {
  pendingBySessionId: Map<string, HitlPrompt>;

  addPrompt: (prompt: HitlPrompt) => void;
  removePromptByApproval: (approvalId: string) => void;
  dropSession: (sessionId: string) => void;
  clear: () => void;
}

export const useHitlStore = create<HitlState>((set) => ({
  pendingBySessionId: new Map(),

  addPrompt: (prompt) =>
    set((state) => {
      const next = new Map(state.pendingBySessionId);
      next.set(prompt.sessionId, prompt);
      return { pendingBySessionId: next };
    }),

  removePromptByApproval: (approvalId) =>
    set((state) => {
      const next = new Map(state.pendingBySessionId);
      for (const [sid, p] of next) {
        if (p.approvalId === approvalId) {
          next.delete(sid);
          break;
        }
      }
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

/** 当前视图 session 的待审批弹窗（null = 无弹窗） */
export function useCurrentPrompt(): HitlPrompt | null {
  const sid = useSessionStore((s) => s.currentSessionId);
  const map = useHitlStore((s) => s.pendingBySessionId);
  if (!sid) return null;
  return map.get(sid) ?? null;
}

/** SessionItem 徽标用：某 session 是否有 pending */
export function useHasPending(sessionId: string): boolean {
  return useHitlStore((s) => s.pendingBySessionId.has(sessionId));
}
