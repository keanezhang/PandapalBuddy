/**
 * src/store/sessionConcurrencyStore.ts
 *
 * SessionAgentPool 三态并发反馈的前端 store。
 * 来源事件：SESSION_CONCURRENCY（Python → 前端）
 *
 * 用途：
 *   - 会话列表在排队中的 session 上显示徽章（"排队中，前面还有 N 个"）
 *   - 当前会话展示 pool 忙碌状态（可选）
 *
 * 数据模型：
 *   - concurrency[session_id] → 该 session 最近一次的 SESSION_CONCURRENCY payload
 *   - status="released" 时**移除**该 session 的记录（不留旧状态）
 *   - poolStats：聚合快照（running_count / max_concurrent）由最新一次事件覆写
 */

import { create } from "zustand";

export interface SessionConcurrencyInfo {
  status: "queued" | "started" | "released";
  queue_position: number;
  queue_length: number;
  running_count: number;
  max_concurrent: number;
  /** 最近一次事件时间戳（毫秒） */
  timestamp: number;
}

export interface PoolStats {
  running_count: number;
  max_concurrent: number;
  updated_at: number;
}

interface State {
  concurrency: Record<string, SessionConcurrencyInfo>;
  poolStats: PoolStats;

  update: (
    sessionId: string,
    payload: Omit<SessionConcurrencyInfo, "timestamp">,
  ) => void;
  clearSession: (sessionId: string) => void;
  reset: () => void;
}

export const useSessionConcurrencyStore = create<State>((set) => ({
  concurrency: {},
  poolStats: { running_count: 0, max_concurrent: 0, updated_at: 0 },

  update: (sessionId, payload) => {
    const now = Date.now();
    set((s) => {
      // "released" 状态代表 slot 已归还——记录清除即可，前端徽章消失
      if (payload.status === "released") {
        const { [sessionId]: _removed, ...rest } = s.concurrency;
        return {
          concurrency: rest,
          poolStats: {
            running_count: payload.running_count,
            max_concurrent: payload.max_concurrent,
            updated_at: now,
          },
        };
      }
      return {
        concurrency: {
          ...s.concurrency,
          [sessionId]: { ...payload, timestamp: now },
        },
        poolStats: {
          running_count: payload.running_count,
          max_concurrent: payload.max_concurrent,
          updated_at: now,
        },
      };
    });
  },

  clearSession: (sessionId) =>
    set((s) => {
      const { [sessionId]: _r, ...rest } = s.concurrency;
      return { concurrency: rest };
    }),

  reset: () =>
    set({
      concurrency: {},
      poolStats: { running_count: 0, max_concurrent: 0, updated_at: 0 },
    }),
}));
