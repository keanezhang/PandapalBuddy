/**
 * src/store/workspaceStore.ts
 *
 * 工作区（用户「打开的文件夹」）状态管理。
 *
 * 背景：Agent 文件工具的根目录只能由用户显式选择，不做任何探测。
 * 「登录」与「选工作区并启动 Agent」是两个独立步骤：
 *   - 登录成功后，若无可恢复的工作区 → 展示「打开文件夹」界面（WorkspaceGate）。
 *   - 用户选定 / 恢复上次工作区 → invoke("open_workspace") → Rust 启动 sidecar。
 *
 * 一进程一目录：切换工作区 = Rust kill 旧 sidecar 后用新 --workdir 重启。
 * current 一旦非空即表示「本次会话已打开过工作区」，切换期间不回退到门控界面。
 */
import { create } from "zustand";
import { invoke } from "@tauri-apps/api/core";
import { open as openDialog } from "@tauri-apps/plugin-dialog";

export type WorkspaceStatus =
  | "idle" // 初始，尚未加载 recent
  | "picking" // 未打开工作区，等待用户选择
  | "opening" // 正在启动 / 切换 sidecar
  | "open" // 已打开工作区，Agent 可用
  | "error";

interface RecentWorkspaces {
  last: string | null;
  recent: string[];
}

interface WorkspaceState {
  current: string | null;
  status: WorkspaceStatus;
  recent: string[];
  last: string | null;
  error: string | null;

  /** 登录后初始化：拉取 recent + 当前工作区；若能恢复上次工作区则自动打开。 */
  init: () => Promise<void>;
  /** 打开（或切换到）指定目录。 */
  openWorkspace: (path: string) => Promise<void>;
  /** 弹出系统文件夹选择器并打开所选目录。 */
  pickAndOpen: () => Promise<void>;
  /** 登出时重置为初始状态，使下次登录重新门控。 */
  reset: () => void;
}

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  current: null,
  status: "idle",
  recent: [],
  last: null,
  error: null,

  init: async () => {
    // 进程可能已在服务某个工作区（如前端热重载而 sidecar 未退）
    try {
      const active = await invoke<string>("get_current_workspace");
      if (active) {
        set({ current: active, status: "open" });
        return;
      }
    } catch {
      /* 忽略，走下面的 recent 恢复 */
    }

    let recentData: RecentWorkspaces = { last: null, recent: [] };
    try {
      recentData = await invoke<RecentWorkspaces>("get_recent_workspaces");
    } catch {
      /* store 读取失败：当作首次运行 */
    }
    set({ recent: recentData.recent, last: recentData.last });

    // 自动恢复上次工作区（存在且有效）
    if (recentData.last) {
      await get().openWorkspace(recentData.last);
    } else {
      set({ status: "picking" });
    }
  },

  openWorkspace: async (path: string) => {
    set({ status: "opening", error: null });
    try {
      const resolved = await invoke<string>("open_workspace", { path });
      set({ current: resolved, status: "open", last: resolved });
      // 刷新 recent 列表
      try {
        const r = await invoke<RecentWorkspaces>("get_recent_workspaces");
        set({ recent: r.recent, last: r.last });
      } catch {
        /* 非致命 */
      }
    } catch (e) {
      // 打开失败：若之前从未打开过，回到选择界面；否则保留当前工作区
      const hadWorkspace = get().current !== null;
      set({
        status: hadWorkspace ? "open" : "picking",
        error: typeof e === "string" ? e : String(e),
      });
    }
  },

  pickAndOpen: async () => {
    const dir = await openDialog({ directory: true, multiple: false });
    if (typeof dir === "string" && dir) {
      await get().openWorkspace(dir);
    }
  },

  reset: () => {
    set({ current: null, status: "idle", recent: [], last: null, error: null });
  },
}));
