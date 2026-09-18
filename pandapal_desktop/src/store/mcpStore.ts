/**
 * src/store/mcpStore.ts
 *
 * MCP 服务器管理 Store。
 *
 * 消息流：
 *   前端发 MCP_LIST   → Python 回 MCP_LIST_RESULT（服务器摘要列表，全量）
 *   前端发 MCP_GET    → Python 回 MCP_GET_RESULT（单服务器详情 + 工具清单）
 *   前端发 MCP_SAVE / MCP_DELETE / MCP_CONNECT / MCP_DISCONNECT
 *     → Python 回 MCP_SAVED / MCP_DELETED / MCP_STATUS_CHANGED（增量）
 *   前端发 MCP_TEST   → Python 回 MCP_TEST_RESULT（连接测试结果）
 *
 * 数据类型对齐后端 wire format：
 *   - 摘要字段来自 pandapal/mcp/manager.py McpManager._summary()
 *   - 工具字段来自 McpManager._tool_dict()
 *   - 详情 = 摘要 + config + tools（get_server_detail()）
 */

import { create } from "zustand";
import type {
  McpServerConfig,
  McpServerDetail,
  McpServerStatus,
  McpServerSummary,
  McpToolItem,
} from "../types/api";

// ── 草稿类型（localStorage 持久化，编辑页防丢）
export interface McpDraft {
  name: string;             // 空字符串 = 新建，有值 = 编辑
  transport: "stdio" | "http";
  enabled: boolean;
  tier: "always" | "deferred";
  command: string;
  argsText: string;         // 空格分隔
  envText: string;          // 每行 KEY=VALUE
  cwd: string;
  url: string;
  headersText: string;      // 每行 KEY=VALUE
  connectTimeout: string;   // 字符串，提交时转 number
  callTimeout: string;
  highRiskText: string;     // 逗号分隔
  safeText: string;         // 逗号分隔
}

// ── 草稿 localStorage key
function draftKey(serverName: string): string {
  return serverName ? `mcp_draft_${serverName}` : "mcp_draft__new";
}

function loadDraft(serverName: string): McpDraft | null {
  try {
    const raw = localStorage.getItem(draftKey(serverName));
    if (!raw) return null;
    return JSON.parse(raw) as McpDraft;
  } catch {
    return null;
  }
}

function saveDraft(draft: McpDraft): void {
  try {
    localStorage.setItem(draftKey(draft.name), JSON.stringify(draft));
  } catch {
    // localStorage 满或不可用时静默失败
  }
}

function removeDraft(serverName: string): void {
  try {
    localStorage.removeItem(draftKey(serverName));
  } catch {
    // 静默
  }
}

/** 新建草稿默认值 */
export function emptyDraft(): McpDraft {
  return {
    name: "",
    transport: "stdio",
    enabled: true,
    tier: "deferred",
    command: "",
    argsText: "",
    envText: "",
    cwd: "",
    url: "",
    headersText: "",
    connectTimeout: "20",
    callTimeout: "60",
    highRiskText: "",
    safeText: "",
  };
}

// ── 状态
interface McpState {
  servers: McpServerSummary[];
  loading: boolean;

  // 按 server name 维护的增量状态（状态推送 / 工具清单推送单独到达）
  statusByName: Record<string, McpServerStatus>;
  toolsByName: Record<string, McpToolItem[]>;

  // 详情面板
  detailServer: McpServerDetail | null;
  detailLoading: boolean;

  // 搜索过滤
  searchQuery: string;

  // ── 列表动作 ─────────────────────────────
  setLoading: (loading: boolean) => void;
  replaceAll: (servers: McpServerSummary[]) => void;
  upsertServer: (server: McpServerSummary) => void;
  removeServer: (name: string) => void;

  // ── 增量动作 ─────────────────────────────
  setStatus: (name: string, status: McpServerStatus) => void;
  setTools: (name: string, tools: McpToolItem[]) => void;

  // ── 详情动作 ─────────────────────────────
  setDetailServer: (detail: McpServerDetail | null) => void;
  setDetailLoading: (loading: boolean) => void;

  // ── 搜索 ────────────────────────────────
  setSearchQuery: (query: string) => void;

  // ── 草稿 ────────────────────────────────
  draft: McpDraft | null;
  setDraft: (draft: McpDraft | null) => void;
  clearDraft: () => void;
  /** 检查指定 server 是否有草稿（不加载到 state，仅查询） */
  hasDraft: (serverName: string) => boolean;

  // ── 连接测试结果（name → 结果；测试发起时置 pending，收到 MCP_TEST_RESULT 覆盖）──
  testResultByName: Record<string, { ok: boolean; msg: string }>;
  setTestResult: (name: string, result: { ok: boolean; msg: string }) => void;

  reset: () => void;
}

export const useMcpStore = create<McpState>((set, get) => ({
  servers: [],
  loading: false,
  statusByName: {},
  toolsByName: {},
  detailServer: null,
  detailLoading: false,
  searchQuery: "",

  setLoading: (loading) => set({ loading }),

  replaceAll: (servers) => {
    // 全量替换：同时用摘要里的 status 重建 statusByName（保持与列表一致）
    const statusByName: Record<string, McpServerStatus> = {};
    for (const s of servers) statusByName[s.name] = s.status;
    set({ servers, statusByName, loading: false });
  },

  upsertServer: (server) => {
    const servers = [...get().servers];
    const idx = servers.findIndex((s) => s.name === server.name);
    if (idx >= 0) {
      servers[idx] = server;
    } else {
      servers.push(server);
    }
    set((state) => ({
      servers,
      statusByName: { ...state.statusByName, [server.name]: server.status },
    }));
  },

  removeServer: (name) => {
    set((state) => {
      const statusByName = { ...state.statusByName };
      delete statusByName[name];
      const toolsByName = { ...state.toolsByName };
      delete toolsByName[name];
      return {
        servers: state.servers.filter((s) => s.name !== name),
        statusByName,
        toolsByName,
      };
    });
  },

  setStatus: (name, status) => {
    set((state) => ({
      statusByName: { ...state.statusByName, [name]: status },
      servers: state.servers.map((s) =>
        s.name === name ? { ...s, status } : s,
      ),
    }));
  },

  setTools: (name, tools) => {
    set((state) => ({
      toolsByName: { ...state.toolsByName, [name]: tools },
      // 同步摘要里的 tool_count（若该 server 已在列表）
      servers: state.servers.map((s) =>
        s.name === name ? { ...s, tool_count: tools.length } : s,
      ),
    }));
  },

  setDetailServer: (detail) => set({ detailServer: detail, detailLoading: false }),
  setDetailLoading: (loading) => set({ detailLoading: loading }),

  setSearchQuery: (query) => set({ searchQuery: query }),

  draft: null,
  setDraft: (draft) => {
    if (draft) {
      saveDraft(draft);
    }
    set({ draft });
  },
  clearDraft: () => {
    const current = get().draft;
    if (current) {
      removeDraft(current.name);
    }
    set({ draft: null });
  },
  hasDraft: (serverName: string) => loadDraft(serverName) !== null,

  testResultByName: {},
  setTestResult: (name, result) =>
    set((state) => ({
      testResultByName: { ...state.testResultByName, [name]: result },
    })),

  reset: () =>
    set({
      servers: [],
      loading: false,
      statusByName: {},
      toolsByName: {},
      detailServer: null,
      detailLoading: false,
      searchQuery: "",
      draft: null,
      testResultByName: {},
    }),
}));

// ── 派生选择器 ──────────────────────────────────────────────────────────

/** 按 query 过滤服务器列表（纯函数：空查询直通，匹配 name/transport，大小写不敏感） */
export function filterMcpServers(
  servers: McpServerSummary[],
  query: string,
): McpServerSummary[] {
  const q = query.toLowerCase().trim();
  if (!q) return servers;
  return servers.filter(
    (s) =>
      s.name.toLowerCase().includes(q) ||
      s.transport.toLowerCase().includes(q),
  );
}

/** 按 searchQuery 过滤的服务器列表（React hook） */
export function useFilteredMcpServers(): McpServerSummary[] {
  const servers = useMcpStore((s) => s.servers);
  const query = useMcpStore((s) => s.searchQuery);
  return filterMcpServers(servers, query);
}

// ── 校验 / 状态映射（纯函数，零 i18n 依赖，供编辑页/列表页复用）──────────

const NAME_PATTERN = /^[^\s]+$/;

/** 校验错误码（稳定字符串，组件据此映射 i18n 文案） */
export interface McpDraftErrors {
  name?: string;
  command?: string;
  url?: string;
}

/** 校验草稿是否可提交；返回错误码字典，空对象 = 合法 */
export function validateMcpDraft(draft: McpDraft): McpDraftErrors {
  const errors: McpDraftErrors = {};
  const name = draft.name.trim();
  if (!name) {
    errors.name = "name_required";
  } else if (!NAME_PATTERN.test(name)) {
    errors.name = "name_pattern";
  }

  if (draft.transport === "stdio" && !draft.command.trim()) {
    errors.command = "command_required";
  }

  if (draft.transport === "http") {
    const url = draft.url.trim();
    if (!url) {
      errors.url = "url_required";
    } else if (!/^https?:\/\//i.test(url)) {
      errors.url = "url_scheme";
    }
  }

  return errors;
}

/** 状态 → 徽章映射（返回 variant + labelKey，组件据此 t(labelKey)） */
export function mcpStatusMeta(
  status: McpServerStatus,
): { variant: "green" | "blue" | "yellow" | "red" | "default"; labelKey: string } {
  switch (status) {
    case "connected":
      return { variant: "green", labelKey: "mcp.status.connected" };
    case "connecting":
      return { variant: "blue", labelKey: "mcp.status.connecting" };
    case "error":
      return { variant: "red", labelKey: "mcp.status.error" };
    case "disconnected":
    default:
      return { variant: "default", labelKey: "mcp.status.disconnected" };
  }
}

/** 把表单字段解析为提交用 McpServerConfig（丢弃空的可选字段） */
export function draftToConfig(draft: McpDraft): McpServerConfig {
  const config: McpServerConfig = {
    name: draft.name.trim(),
    transport: draft.transport,
    enabled: draft.enabled,
    tier: draft.tier,
  };

  if (draft.transport === "stdio") {
    const command = draft.command.trim();
    if (command) config.command = command;
    const args = draft.argsText
      .split(/\s+/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (args.length > 0) config.args = args;
    const cwd = draft.cwd.trim();
    if (cwd) config.cwd = cwd;
  } else {
    const url = draft.url.trim();
    if (url) config.url = url;
  }

  const env = parseKvText(draft.envText);
  if (Object.keys(env).length > 0) config.env = env;
  const headers = parseKvText(draft.headersText);
  if (Object.keys(headers).length > 0) config.headers = headers;

  const connectTimeout = Number(draft.connectTimeout);
  if (Number.isFinite(connectTimeout) && connectTimeout > 0) {
    config.connect_timeout = connectTimeout;
  }
  const callTimeout = Number(draft.callTimeout);
  if (Number.isFinite(callTimeout) && callTimeout > 0) {
    config.call_timeout = callTimeout;
  }

  const highRisk = draft.highRiskText
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  if (highRisk.length > 0) config.high_risk_tools = highRisk;
  const safe = draft.safeText
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  if (safe.length > 0) config.safe_tools = safe;

  return config;
}

/** 把详情 config 反填为草稿（编辑页回显） */
export function configToDraft(name: string, config: McpServerConfig): McpDraft {
  return {
    name,
    transport: config.transport,
    enabled: config.enabled,
    tier: config.tier,
    command: config.command ?? "",
    argsText: (config.args ?? []).join(" "),
    envText: kvToText(config.env),
    cwd: config.cwd ?? "",
    url: config.url ?? "",
    headersText: kvToText(config.headers),
    connectTimeout: String(config.connect_timeout ?? 20),
    callTimeout: String(config.call_timeout ?? 60),
    highRiskText: (config.high_risk_tools ?? []).join(", "),
    safeText: (config.safe_tools ?? []).join(", "),
  };
}

// ── 工具函数 ────────────────────────────────────────────────────────────

/** 解析每行 KEY=VALUE 文本为 dict（非法行静默跳过） */
function parseKvText(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  if (!text) return out;
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const eq = trimmed.indexOf("=");
    if (eq <= 0) continue;
    const key = trimmed.slice(0, eq).trim();
    const value = trimmed.slice(eq + 1).trim();
    if (key) out[key] = value;
  }
  return out;
}

/** 把 dict 渲染为每行 KEY=VALUE 文本 */
function kvToText(map: Record<string, string> | undefined): string {
  if (!map) return "";
  return Object.entries(map)
    .map(([k, v]) => `${k}=${v}`)
    .join("\n");
}
