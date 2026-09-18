/**
 * mcpStore 单元测试（回链 design/mcp-frontend.design.md）。
 *
 * 覆盖：
 *   - draftToConfig 边界（非法 timeout 丢弃 / transport 二选一 / args 切分 / KV 解析）
 *   - configToDraft round-trip
 *   - validateMcpDraft 校验（name/command/url）
 *   - mcpStatusMeta 状态映射
 *   - useFilteredMcpServers 搜索过滤
 *   - state 动作一致性
 *   - 草稿持久化（localStorage round-trip）
 */

import { beforeEach, describe, expect, it } from "vitest";
import {
  useMcpStore,
  emptyDraft,
  draftToConfig,
  configToDraft,
  validateMcpDraft,
  mcpStatusMeta,
  filterMcpServers,
  type McpDraft,
} from "../mcpStore";
import type { McpServerConfig, McpServerSummary } from "../../types/api";

function summary(overrides: Partial<McpServerSummary> = {}): McpServerSummary {
  return {
    name: "fs",
    transport: "stdio",
    enabled: true,
    tier: "deferred",
    status: "disconnected",
    tool_count: 0,
    ...overrides,
  };
}

function stdioDraft(overrides: Partial<McpDraft> = {}): McpDraft {
  return { ...emptyDraft(), name: "fs", command: "npx", ...overrides };
}

function resetStore(): void {
  useMcpStore.getState().reset();
}

describe("draftToConfig 边界", () => {
  it.each(["abc", "0", "-5", ""])("MCP-01: 非法 connectTimeout=%j 被丢弃", (v) => {
    const cfg = draftToConfig(stdioDraft({ connectTimeout: v }));
    expect(cfg.connect_timeout).toBeUndefined();
  });

  it("MCP-02: stdio 草稿忽略 url（http 专属）", () => {
    const cfg = draftToConfig(stdioDraft({ url: "https://x" }));
    expect(cfg.url).toBeUndefined();
    expect(cfg.command).toBe("npx");
  });

  it("MCP-02b: http 草稿忽略 command/args/cwd（stdio 专属）", () => {
    const cfg = draftToConfig({
      ...emptyDraft(),
      name: "r",
      transport: "http",
      url: "https://x",
      command: "npx",
      argsText: "x",
      cwd: "/tmp",
    });
    expect(cfg.command).toBeUndefined();
    expect(cfg.args).toBeUndefined();
    expect(cfg.cwd).toBeUndefined();
    expect(cfg.url).toBe("https://x");
  });

  it("MCP-03: args 连续空格被正确切分", () => {
    const cfg = draftToConfig(stdioDraft({ argsText: "  -y   server-filesystem  /tmp  " }));
    expect(cfg.args).toEqual(["-y", "server-filesystem", "/tmp"]);
  });

  it("MCP-04: parseKvText 非法行跳过", () => {
    const cfg = draftToConfig(stdioDraft({ envText: "A=1\n\nnoequals\n=orphan\nB = 2 " }));
    expect(cfg.env).toEqual({ A: "1", B: "2" });
  });
});

describe("configToDraft round-trip", () => {
  it("MCP-05: round-trip 等价", () => {
    const config: McpServerConfig = {
      name: "fs",
      transport: "stdio",
      enabled: false,
      tier: "always",
      command: "python",
      args: ["s.py"],
      env: { A: "1" },
      connect_timeout: 25,
      call_timeout: 65,
      high_risk_tools: ["rm"],
      safe_tools: ["ls"],
    };
    const round = draftToConfig(configToDraft("fs", config));
    expect(round).toEqual(config);
  });
});

describe("validateMcpDraft 校验", () => {
  it("MCP-06: name 空 → name_required", () => {
    const errors = validateMcpDraft(stdioDraft({ name: "  " }));
    expect(errors.name).toBe("name_required");
  });

  it("MCP-07: name 含空白 → name_pattern", () => {
    const errors = validateMcpDraft(stdioDraft({ name: "my server" }));
    expect(errors.name).toBe("name_pattern");
  });

  it("MCP-08: stdio 缺 command → command_required", () => {
    const errors = validateMcpDraft(stdioDraft({ command: "  " }));
    expect(errors.command).toBe("command_required");
  });

  it("MCP-09: http 缺 url → url_required", () => {
    const errors = validateMcpDraft({ ...emptyDraft(), name: "r", transport: "http", url: "" });
    expect(errors.url).toBe("url_required");
  });

  it("MCP-10: http url 非 http(s) → url_scheme", () => {
    const errors = validateMcpDraft({ ...emptyDraft(), name: "r", transport: "http", url: "ftp://x" });
    expect(errors.url).toBe("url_scheme");
  });

  it("MCP-11: 合法 stdio → 无错误", () => {
    const errors = validateMcpDraft(stdioDraft());
    expect(errors).toEqual({});
  });

  it("MCP-11b: 合法 http → 无错误", () => {
    const errors = validateMcpDraft({ ...emptyDraft(), name: "r", transport: "http", url: "https://ok" });
    expect(errors).toEqual({});
  });
});

describe("mcpStatusMeta 状态映射", () => {
  it("MCP-12: 四态映射", () => {
    expect(mcpStatusMeta("connected")).toEqual({ variant: "green", labelKey: "mcp.status.connected" });
    expect(mcpStatusMeta("connecting")).toEqual({ variant: "blue", labelKey: "mcp.status.connecting" });
    expect(mcpStatusMeta("error")).toEqual({ variant: "red", labelKey: "mcp.status.error" });
    expect(mcpStatusMeta("disconnected")).toEqual({ variant: "default", labelKey: "mcp.status.disconnected" });
  });

  it("MCP-12b: 未知状态兜底 disconnected", () => {
    expect(mcpStatusMeta("weird" as never)).toEqual({ variant: "default", labelKey: "mcp.status.disconnected" });
  });
});

describe("filterMcpServers 搜索过滤", () => {
  it("MCP-13: 空查询直通", () => {
    const servers = [summary({ name: "a" }), summary({ name: "b" })];
    expect(filterMcpServers(servers, "")).toHaveLength(2);
  });

  it("MCP-13b: name 匹配 + 大小写不敏感", () => {
    const servers = [summary({ name: "filesystem" }), summary({ name: "remote" })];
    const result = filterMcpServers(servers, "File");
    expect(result.map((s) => s.name)).toEqual(["filesystem"]);
  });

  it("MCP-13c: transport 匹配", () => {
    const servers = [
      summary({ name: "a", transport: "stdio" }),
      summary({ name: "b", transport: "http" }),
    ];
    const result = filterMcpServers(servers, "http");
    expect(result.map((s) => s.name)).toEqual(["b"]);
  });
});

describe("state 动作一致性", () => {
  beforeEach(() => resetStore());

  it("MCP-14: setStatus 同步摘要 status", () => {
    useMcpStore.getState().replaceAll([summary({ name: "a", status: "disconnected" })]);
    useMcpStore.getState().setStatus("a", "connected");
    const s = useMcpStore.getState();
    expect(s.statusByName["a"]).toBe("connected");
    expect(s.servers[0].status).toBe("connected");
  });

  it("MCP-14b: setTools 同步摘要 tool_count", () => {
    useMcpStore.getState().replaceAll([summary({ name: "a", tool_count: 0 })]);
    useMcpStore.getState().setTools("a", [
      { name: "mcp_a__x", description: "d", when_to_use: "w", sensitivity: "low" },
      { name: "mcp_a__y", description: "d2", when_to_use: "w2", sensitivity: "critical" },
    ]);
    const s = useMcpStore.getState();
    expect(s.toolsByName["a"]).toHaveLength(2);
    expect(s.servers[0].tool_count).toBe(2);
  });
});

describe("草稿持久化", () => {
  beforeEach(() => {
    localStorage.clear();
    resetStore();
  });

  it("setDraft 写 localStorage，clearDraft 删除", () => {
    const draft = stdioDraft();
    useMcpStore.getState().setDraft(draft);
    expect(useMcpStore.getState().hasDraft("fs")).toBe(true);
    useMcpStore.getState().clearDraft();
    expect(useMcpStore.getState().hasDraft("fs")).toBe(false);
  });
});
