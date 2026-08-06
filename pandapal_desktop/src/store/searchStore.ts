/**
 * src/store/searchStore.ts
 *
 * 命令面板全局搜索的后端结果缓存。
 *
 * 消息流：
 *   前端 searchRequest(query) → Python SEARCH → 回 SEARCH_RESULT
 *   BackendProvider 收到 SEARCH_RESULT → setResult(...)
 *
 * 只保存「最新一次查询」的结果；用 query 字段防止过期响应覆盖新查询
 * （用户快速输入时旧响应后到）。会话标题 + 消息全文由后端返回，
 * 定时任务 / Skills 仍由 CommandPalette 走客户端 store 即时过滤。
 */
import { create } from "zustand";
import type { SearchSessionHit, SearchMessageHit } from "../types/api";

interface SearchState {
  /** 当前正在等待结果的查询词（用于丢弃过期响应） */
  query: string;
  sessions: SearchSessionHit[];
  messages: SearchMessageHit[];
  loading: boolean;

  /** 发起查询前调用：记录查询词 + 置 loading */
  beginQuery: (query: string) => void;
  /** 收到 SEARCH_RESULT：仅当 query 与当前一致才应用 */
  setResult: (query: string, sessions: SearchSessionHit[], messages: SearchMessageHit[]) => void;
  /** 清空（面板关闭 / 查询词清空） */
  clear: () => void;
}

export const useSearchStore = create<SearchState>((set, get) => ({
  query: "",
  sessions: [],
  messages: [],
  loading: false,

  beginQuery: (query) => set({ query, loading: true }),

  setResult: (query, sessions, messages) => {
    // 过期响应保护：只接受与当前查询词一致的结果
    if (query !== get().query) return;
    set({ sessions, messages, loading: false });
  },

  clear: () => set({ query: "", sessions: [], messages: [], loading: false }),
}));
