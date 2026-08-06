/**
 * src/store/groupViewStore.ts
 *
 * 「会话分组详情页」专用 Store。
 *
 * 与侧边栏的 sessionStore 完全解耦：分组详情页（SessionGroupPage）
 * 拥有自己的会话列表 / 分页 / 加载态，避免打开某个分组时污染侧边栏
 * 的对话列表。BackendProvider 通过一个「待响应分组标记」把
 * SESSION_LIST 响应精准路由到这里，而不影响 sessionStore。
 */
import { create } from "zustand";
import type { SessionInfo } from "../types/api";

interface GroupViewState {
  /** 当前正在展示的分组 id（用于判断增量事件是否命中） */
  groupId: string | null;
  sessions: SessionInfo[];
  page: number;
  hasMore: boolean;
  loading: boolean;

  /** 首屏（page===1）替换 */
  setSessions: (groupId: string, sessions: SessionInfo[], hasMore: boolean, page: number) => void;
  /** 加载更多（page>1）追加去重 */
  appendSessions: (sessions: SessionInfo[], hasMore: boolean) => void;
  /** 会话被删除 → 从列表移除 */
  dropSession: (sessionId: string) => void;
  /** 会话被更新：命中当前分组则 upsert，否则（被移出分组）移除 */
  upsertSession: (info: SessionInfo) => void;
  setLoading: (loading: boolean) => void;
  /** 切换到新分组前重置 */
  reset: (groupId: string | null) => void;
}

export const useGroupViewStore = create<GroupViewState>((set) => ({
  groupId: null,
  sessions: [],
  page: 1,
  hasMore: false,
  loading: false,

  setSessions: (groupId, sessions, hasMore, page) =>
    set({ groupId, sessions, hasMore, page, loading: false }),

  appendSessions: (newSessions, hasMore) =>
    set((s) => ({
      sessions: mergeSessions(s.sessions, newSessions),
      hasMore,
      page: s.page + 1,
      loading: false,
    })),

  dropSession: (sessionId) =>
    set((s) => ({ sessions: s.sessions.filter((x) => x.session_id !== sessionId) })),

  upsertSession: (info) =>
    set((s) => {
      // 只处理当前分组视图；分组不匹配的事件忽略
      if (s.groupId == null) return s;
      const belongsHere = info.group_id === s.groupId && !info.is_empty;
      const idx = s.sessions.findIndex((x) => x.session_id === info.session_id);
      if (!belongsHere) {
        // 被移出该分组（或变空）→ 从列表移除
        return idx >= 0
          ? { sessions: s.sessions.filter((x) => x.session_id !== info.session_id) }
          : s;
      }
      if (idx >= 0) {
        const next = [...s.sessions];
        next[idx] = info;
        return { sessions: next };
      }
      return { sessions: [info, ...s.sessions] };
    }),

  setLoading: (loading) => set({ loading }),

  reset: (groupId) => set({ groupId, sessions: [], page: 1, hasMore: false, loading: true }),
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
