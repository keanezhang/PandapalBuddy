/**
 * src/store/authStore.ts
 *
 * 认证状态管理（Zustand）— 串行启动架构。
 *
 * 职责边界：
 *   authStore   → 只管"是否持有有效凭据"（authenticated / unauthenticated / loading）
 *   BackendProvider → 管理 IPC 连接状态（connecting / connected / closed）
 *
 * 启动流程（冷启动 / 自动登录）：
 * 1. app 启动 → initialize()
 * 2. invoke("auth_verify_token")
 *    - store 中无凭据 → status = "unauthenticated" → 跳登录页
 *    - store 中有凭据 → Rust 携带 --user-id / --token 启动 sidecar
 *                     → status = "authenticated" → AuthGuard 放行
 * 3. BackendProvider 挂载后自行等待 backend-ready 事件完成 IPC 连接
 *
 * 启动流程（新登录 / 注册）：
 * 1. 前端 fetch relay HTTP → 得到 token + user_id + username
 * 2. invoke("auth_notify_ready", {token, userId, username})
 *    → Rust 保存到 store + 携带凭据启动 sidecar
 * 3. status = "authenticated" → AuthGuard 放行
 * 4. BackendProvider 挂载后自行等待 backend-ready
 */

import { create } from "zustand";
import { invoke } from "@tauri-apps/api/core";
import { useWorkspaceStore } from "./workspaceStore";
import i18n from "../i18n";

// ── 配置 ─────────────────────────────────────────────────────────────────────

/** Relay 服务器 HTTP base URL（认证接口） */
// 地址统一由 VITE_RELAY_AUTH_URL 提供（完整 URL，见 .env.example）：
// 开发走 Vite dev proxy（相对路径 /auth，绕 CORS）；生产直连 env 地址（缺失即显式失败）
const RELAY_AUTH_BASE_URL = import.meta.env.DEV
  ? "/auth"
  : import.meta.env.VITE_RELAY_AUTH_URL;

// ── 类型定义 ──────────────────────────────────────────────────────────────────

interface AuthCommandResult {
  success: boolean;
  user_id: string;
  username: string;
  /** 认证模式："local"（本地账号）/ "cloud"（云端账号） */
  mode: string;
}

/** 本地账号状态（前端判断显示「创建」还是「登录」表单） */
interface LocalAccountStatus {
  registered: boolean;
  username: string;
}

/** Relay HTTP 登录/注册响应 */
interface RelayAuthResponse {
  token: string;
  user_id: string;
  username: string;
  expires_at: string;
}

/** Relay HTTP 错误响应 */
interface RelayAuthError {
  detail: {
    error: string;
    code: string;
  };
}

export type AuthStatus = "loading" | "authenticated" | "unauthenticated";
export type AuthMode = "local" | "cloud" | null;

interface AuthState {
  status: AuthStatus;
  username: string | null;
  token: string | null;
  userId: string | null;
  /** 认证模式："local"（本地账号）/ "cloud"（云端账号）/ null（未登录） */
  authMode: AuthMode;
  error: string | null;

  /** 初始化：尝试从 store 恢复凭据并启动 sidecar */
  initialize: () => Promise<void>;

  /** 查询本地账号是否已注册（无账号→创建表单；有账号→登录表单） */
  fetchLocalStatus: () => Promise<LocalAccountStatus>;

  /** 本地注册（创建本地账号并自动登录，bcrypt 哈希存本地） */
  registerLocal: (username: string, password: string) => Promise<boolean>;

  /** 本地登录（bcrypt 本地校验，不连服务器） */
  loginLocal: (username: string, password: string) => Promise<boolean>;

  /** 登录（直连 relay HTTP） */
  login: (username: string, password: string) => Promise<boolean>;

  /** 注册（直连 relay HTTP） */
  register: (username: string, password: string) => Promise<boolean>;

  /** 登出 */
  logout: () => Promise<void>;

  /** 清除错误信息 */
  clearError: () => void;
}

// ── Store ─────────────────────────────────────────────────────────────────────

export const useAuthStore = create<AuthState>((set) => ({
  status: "loading",
  username: null,
  token: null,
  userId: null,
  authMode: null,
  error: null,

  initialize: async () => {
    // 尝试从 store 恢复凭据；若有凭据，Rust 会携带参数启动 sidecar
    try {
      const cmdResult = await invoke<AuthCommandResult>("auth_verify_token");
      if (!cmdResult.success) {
        // 没有存储的凭据，跳登录页
        set({ status: "unauthenticated" });
        return;
      }

      // 凭据有效，sidecar 已启动（或已在运行）
      // BackendProvider 挂载后将自行等待 backend-ready
      set({
        status: "authenticated",
        userId: cmdResult.user_id || null,
        username: cmdResult.username || null,
        authMode: cmdResult.mode === "local" ? "local" : "cloud",
        error: null,
      });
    } catch {
      set({ status: "unauthenticated", error: i18n.t("auth.errAutoLoginFailed") });
    }
  },

  fetchLocalStatus: async () => {
    try {
      return await invoke<LocalAccountStatus>("auth_local_status");
    } catch {
      return { registered: false, username: "" };
    }
  },

  registerLocal: async (username: string, password: string) => {
    set({ error: null });
    try {
      // 已有云端会话时提示将切换到本地模式（本地会话身份会覆盖 cloud 的 auth_mode）
      const cmd = await invoke<AuthCommandResult>("auth_local_register", {
        username,
        password,
      });
      set({
        status: "authenticated",
        token: null,
        userId: cmd.user_id,
        username: cmd.username,
        authMode: "local",
        error: null,
      });
      return true;
    } catch (e) {
      set({ error: e instanceof Error ? e.message : String(e) });
      return false;
    }
  },

  loginLocal: async (username: string, password: string) => {
    set({ error: null });
    try {
      const cmd = await invoke<AuthCommandResult>("auth_local_login", {
        username,
        password,
      });
      set({
        status: "authenticated",
        token: null,
        userId: cmd.user_id,
        username: cmd.username,
        authMode: "local",
        error: null,
      });
      return true;
    } catch (e) {
      set({ error: e instanceof Error ? e.message : String(e) });
      return false;
    }
  },

  login: async (username: string, password: string) => {
    set({ error: null });
    if (!RELAY_AUTH_BASE_URL) {
      set({ error: i18n.t("auth.errNoServer") });
      return false;
    }
    try {
      const response = await fetch(`${RELAY_AUTH_BASE_URL}/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });

      if (!response.ok) {
        const err: RelayAuthError = await response.json();
        const message = err.detail?.error || i18n.t("auth.errLoginFailed", { status: response.status });
        set({ error: message });
        return false;
      }

      const data: RelayAuthResponse = await response.json();

      // 通知 Rust 保存凭据并携带参数启动 sidecar
      await invoke("auth_notify_ready", {
        token: data.token,
        userId: data.user_id,
        username: data.username,
      });

      // sidecar 已启动；set authenticated，BackendProvider 挂载后完成 IPC 连接
      set({
        status: "authenticated",
        token: data.token,
        userId: data.user_id,
        username: data.username,
        authMode: "cloud",
        error: null,
      });
      return true;
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      set({ error: msg.includes("fetch") ? i18n.t("auth.errNetwork") : msg });
      return false;
    }
  },

  register: async (username: string, password: string) => {
    set({ error: null });
    if (!RELAY_AUTH_BASE_URL) {
      set({ error: i18n.t("auth.errNoServer") });
      return false;
    }
    try {
      const response = await fetch(`${RELAY_AUTH_BASE_URL}/register`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });

      if (!response.ok) {
        const err: RelayAuthError = await response.json();
        const message = err.detail?.error || i18n.t("auth.errRegisterFailed", { status: response.status });
        set({ error: message });
        return false;
      }

      const data: RelayAuthResponse = await response.json();

      // 通知 Rust 保存凭据并携带参数启动 sidecar（注册即登录）
      await invoke("auth_notify_ready", {
        token: data.token,
        userId: data.user_id,
        username: data.username,
      });

      set({
        status: "authenticated",
        token: data.token,
        userId: data.user_id,
        username: data.username,
        authMode: "cloud",
        error: null,
      });
      return true;
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      set({ error: msg.includes("fetch") ? i18n.t("auth.errNetwork") : msg });
      return false;
    }
  },

  logout: async () => {
    try {
      await invoke("auth_logout");
    } catch {
      // ignore
    }
    // 重置工作区门控状态，使下次登录重新走「打开文件夹」流程
    useWorkspaceStore.getState().reset();
    set({
      status: "unauthenticated",
      token: null,
      userId: null,
      username: null,
      authMode: null,
    });
  },

  clearError: () => set({ error: null }),
}));
