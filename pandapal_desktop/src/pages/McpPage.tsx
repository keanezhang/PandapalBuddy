/**
 * src/pages/McpPage.tsx — v2 嵌入模式
 *
 * MCP 服务器管理列表页（仿 SkillsPage.tsx）。
 *
 * 每张卡片展示：名称 / 传输方式 / 状态徽章 / 工具数，
 * 操作：连接 / 断开 / 测试 / 编辑 / 删除。
 * 测试结果由后端 MCP_TEST_RESULT 事件驱动（写入 mcpStore.testResultByName）。
 */

import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useMcpStore, useFilteredMcpServers, mcpStatusMeta } from "../store/mcpStore";
import { useBackend } from "../providers/BackendProvider";
import type { McpServerSummary } from "../types/api";
import { ask } from "@tauri-apps/plugin-dialog";
import { Badge } from "../components/ui";

export function McpPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const {
    requestMcpList,
    connectMcpServer,
    disconnectMcpServer,
    testMcpServer,
    deleteMcpServer,
    setMcpEnabled,
  } = useBackend();
  const loading = useMcpStore((s) => s.loading);
  const searchQuery = useMcpStore((s) => s.searchQuery);
  const setSearchQuery = useMcpStore((s) => s.setSearchQuery);
  const testResultByName = useMcpStore((s) => s.testResultByName);
  const servers = useFilteredMcpServers();

  useEffect(() => {
    requestMcpList();
  }, [requestMcpList]);

  const handleTest = (name: string) => testMcpServer(name);

  const handleDelete = async (name: string) => {
    if (await ask(t("mcp.confirmDelete", { name }), { title: t("mcp.deleteTitle"), kind: "warning" })) {
      deleteMcpServer(name);
    }
  };

  return (
    <div className="page-root">
      {/* 标题行 */}
      <div className="page-header page-header--gold">
        <div style={{ flex: 1 }}>
          <h1 className="page-title">
            <span style={{ width: 34, height: 34, fontSize: "var(--text-lg)", display: "flex", alignItems: "center", justifyContent: "center" }}>🔌</span>
            {t("mcp.title")}
            {servers.length > 0 && (
              <span style={{ fontSize: "var(--text-base)", fontWeight: 500, color: "var(--text-tertiary)" }}>· {servers.length}</span>
            )}
          </h1>
        </div>
        <div className="skills-page-actions">
          <button onClick={() => navigate("/mcp/new")} className="btn btn-success btn-sm">{t("mcp.new")}</button>
          {loading && <span className="skills-loading-dot" style={{ marginLeft: 8 }} title={t("mcp.refreshing")} />}
        </div>
      </div>

      {/* 内容区 */}
      <div style={{ flex: 1, overflowY: "auto", padding: "28px 36px" }}>
        <div style={{ width: "90%", margin: "0 auto" }}>
          <div className="skills-search-wrap" style={{ marginBottom: 32 }}>
            <span className="skills-search-icon">🔍</span>
            <input
              type="text"
              className="skills-search-input"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder={t("mcp.searchPlaceholder")}
              style={{ padding: "12px 36px 12px 42px", fontSize: "var(--text-md)", borderRadius: 10 }}
            />
            {searchQuery && <button className="skills-search-clear" onClick={() => setSearchQuery("")}>✕</button>}
          </div>

          {loading && servers.length === 0 ? (
            <div className="skills-loading"><span className="skills-loading-dot" /> {t("common.loading")}</div>
          ) : servers.length === 0 ? (
            <div className="skills-empty">
              <div className="skills-empty-icon">🔌</div>
              <div className="skills-empty-title">{t("mcp.emptyTitle")}</div>
              <div className="skills-empty-desc">{t("mcp.emptyDesc")}</div>
              <div className="skills-empty-actions">
                <button onClick={() => navigate("/mcp/new")} className="btn btn-success">{t("mcp.newServer")}</button>
              </div>
            </div>
          ) : searchQuery && servers.length === 0 ? (
            <div className="skills-no-match">
              <div className="skills-no-match-icon">🔍</div>
              <div className="skills-no-match-title">{t("mcp.noMatch", { q: searchQuery })}</div>
              <div className="skills-no-match-hint">{t("mcp.tryOther")}</div>
            </div>
          ) : (
            <div className="skills-grid">
              {servers.map((server) => (
                <McpServerCard
                  key={server.name}
                  server={server}
                  testResult={testResultByName[server.name]}
                  onConnect={() => connectMcpServer(server.name)}
                  onDisconnect={() => disconnectMcpServer(server.name)}
                  onTest={() => handleTest(server.name)}
                  onEdit={() => navigate(`/mcp/${encodeURIComponent(server.name)}/edit`)}
                  onDelete={() => handleDelete(server.name)}
                  onToggleEnabled={() => setMcpEnabled(server.name, !server.enabled)}
                />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/* ── 服务器卡片 ─────────────────────────────────────────────── */

function McpServerCard({
  server,
  testResult,
  onConnect,
  onDisconnect,
  onTest,
  onEdit,
  onDelete,
  onToggleEnabled,
}: {
  server: McpServerSummary;
  testResult?: { ok: boolean; msg: string };
  onConnect: () => void;
  onDisconnect: () => void;
  onTest: () => void;
  onEdit: () => void;
  onDelete: () => void;
  onToggleEnabled: () => void;
}) {
  const { t } = useTranslation();
  const meta = mcpStatusMeta(server.status);

  return (
    <div className="skill-card mcp-card" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
        <span className="skill-card-icon icon-teal" style={{ width: 40, height: 40, fontSize: "var(--text-xl)", borderRadius: "var(--radius-md)", flexShrink: 0 }}>
          {server.transport === "stdio" ? "🖥" : "🌐"}
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <span style={{ fontWeight: 600, fontSize: "var(--text-md)", color: "var(--text-primary)" }}>{server.name}</span>
            <Badge variant={meta.variant}>{t(meta.labelKey)}</Badge>
            <span className="skill-card-tag">{server.transport}</span>
            {!server.enabled && (
              <span className="skill-card-tag" style={{ color: "var(--text-tertiary)" }}>
                {t("mcp.disabled")}
              </span>
            )}
          </div>
          <div style={{ fontSize: "var(--text-xs)", color: "var(--text-tertiary)", marginTop: 4 }}>
            {t("mcp.toolCount", { count: server.tool_count })} · {t("mcp.tier", { tier: server.tier })}
          </div>
          {server.error && (
            <div style={{ fontSize: "var(--text-xs)", color: "var(--danger)", marginTop: 4, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {server.error}
            </div>
          )}
        </div>
      </div>

      {testResult && (
        <div style={{
          fontSize: "var(--text-xs)", padding: "6px 10px", borderRadius: "var(--radius-sm)",
          background: testResult.ok ? "color-mix(in srgb, var(--info) 10%, transparent)" : "color-mix(in srgb, var(--danger) 10%, transparent)",
          color: testResult.ok ? "var(--info)" : "var(--danger)",
        }}>
          {testResult.msg}
        </div>
      )}

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        {server.status === "connected" ? (
          <button className="btn btn-ghost btn-sm" onClick={onDisconnect}>{t("mcp.disconnect")}</button>
        ) : (
          <button className="btn btn-primary btn-sm" onClick={onConnect}>{t("mcp.connect")}</button>
        )}
        <button className="btn btn-ghost btn-sm" onClick={onTest}>{t("mcp.test")}</button>
        <button className="btn btn-ghost btn-sm" onClick={onEdit}>{t("mcp.edit")}</button>
        <button className="btn btn-danger btn-sm" onClick={onDelete}>{t("mcp.delete")}</button>
        {/* 启用/禁用开关：保留配置，仅切换工具加载 */}
        <button
          className={server.enabled ? "btn btn-ghost btn-sm" : "btn btn-success btn-sm"}
          onClick={onToggleEnabled}
          title={server.enabled ? t("mcp.disableHint") : t("mcp.enableHint")}
        >
          {server.enabled ? t("mcp.disable") : t("mcp.enable")}
        </button>
      </div>
    </div>
  );
}
