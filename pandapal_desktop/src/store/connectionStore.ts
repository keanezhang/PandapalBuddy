/**
 * src/store/connectionStore.ts
 *
 * 后端连接状态管理（Tauri IPC）。
 */
import { create } from "zustand";

export type ConnectionStatus =
  | "waiting"    // 等待后端就绪（PANDAPAL_READY 信号未到）
  | "connecting" // IPC 正在建立
  | "connected"  // IPC 已连接，可以发消息
  | "error"      // 后端崩溃
  | "closed";    // 后端已关闭

interface ConnectionState {
  status: ConnectionStatus;
  errorMessage: string | null;
  setStatus: (status: ConnectionStatus) => void;
  setError: (msg: string) => void;
}

export const useConnectionStore = create<ConnectionState>((set) => ({
  status: "waiting",
  errorMessage: null,

  setStatus: (status) => set({ status }),
  setError: (msg) => set({ status: "error", errorMessage: msg }),
}));
