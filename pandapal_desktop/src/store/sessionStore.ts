/**
 * src/store/sessionStore.ts
 *
 * 会话列表 Store（v003 完整实现）。
 *
 * - 分页加载（后端主导排序：created_at DESC 创建时间倒序）
 * - 分组过滤（"all" | null(=无分组) | 具体 group_id）
 * - 单一真相源：currentSessionId 由此 store 持有，chatStore/hitlStore 派生
 * - IPC 事件更新走 setSessions / upsertSession / dropSession 等 mutators
 */
import { create } from "zustand";
import type { SessionInfo, SessionGroupInfo, SessionRoutingResult } from "../types/api";

export type GroupFilter = "all" | null | string;

interface SessionState {
  sessions: SessionInfo[];
  groups: SessionGroupInfo[];
  currentSessionId: string | null;
  currentGroupFilter: GroupFilter;
  page: number;
  hasMore: boolean;
  loading: boolean;

  // 列表更新
  setSessions: (sessions: SessionInfo[], hasMore: boolean, page: number) => void;
  appendSessions: (sessions: SessionInfo[], hasMore: boolean) => void;
  upsertSession: (info: SessionInfo) => void;
  dropSession: (sessionId: string) => void;

  // 分组
  setGroups: (groups: SessionGroupInfo[]) => void;

  // 视图状态
  switchSession: (sessionId: string | null) => void;
  setGroupFilter: (filter: GroupFilter) => void;
  setLoading: (loading: boolean) => void;
  setPage: (page: number) => void;

  // 路由决策后前端应用（收到 SESSION_DELETED 时调）
  applyRouting: (routing: SessionRoutingResult) => void;
}

export const useSessionStore = create<SessionState>((set, get) => ({
  sessions: [],
  groups: [],
  currentSessionId: null,
  currentGroupFilter: "all",
  page: 1,
  hasMore: true,
  loading: false,

  setSessions: (sessions, hasMore, page) =>
    set({ sessions, hasMore, page, loading: false }),

  appendSessions: (newSessions, hasMore) =>
    set((s) => ({
      sessions: mergeSessions(s.sessions, newSessions),
      hasMore,
      page: s.page + 1,
      loading: false,
    })),

  /** 增量更新单个会话（收到 SESSION_UPDATED）。
   *  规则：
   *   - is_empty=1 的不进入列表（保持不可见）
   *   - 已存在则替换；不存在则插入到最前（后续排序前端不做，等下次刷新）
   */
  upsertSession: (info) =>
    set((s) => {
      if (info.is_empty) return s; // 空会话不进入可见列表
      const idx = s.sessions.findIndex((x) => x.session_id === info.session_id);
      if (idx >= 0) {
        const next = [...s.sessions];
        next[idx] = info;
        return { sessions: next };
      }
      return { sessions: [info, ...s.sessions] };
    }),

  dropSession: (sessionId) =>
    set((s) => ({
      sessions: s.sessions.filter((x) => x.session_id !== sessionId),
    })),

  setGroups: (groups) => set({ groups }),

  switchSession: (sessionId) => set({ currentSessionId: sessionId }),

  setGroupFilter: (filter) => set({ currentGroupFilter: filter }),

  setLoading: (loading) => set({ loading }),

  setPage: (page) => set({ page }),

  applyRouting: (routing) => {
    const state = get();
    if (routing.action === "no_change") return;
    if (routing.action === "switch" && routing.target_session_id) {
      state.switchSession(routing.target_session_id);
      return;
    }
    if (routing.action === "empty_state") {
      state.switchSession(null);
    }
  },
}));

/** 合并去重（按 session_id）。 */
function mergeSessions(prev: SessionInfo[], next: SessionInfo[]): SessionInfo[] {
  const seen = new Set(prev.map((s) => s.session_id));
  const merged = [...prev];
  for (const s of next) {
    if (seen.has(s.session_id)) continue;
    merged.push(s);
    seen.add(s.session_id);
  }
  return merged;
}
