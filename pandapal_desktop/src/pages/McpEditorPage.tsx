/**
 * src/pages/McpEditorPage.tsx
 *
 * MCP 服务器配置编辑 / 新建页面。
 *
 * 路由：
 *   /mcp/new              → 新建服务器
 *   /mcp/:name/edit       → 编辑已有服务器
 *
 * 表单字段（对齐 McpServerConfig wire format）：
 *   - 通用：name / transport / enabled / tier
 *   - stdio：command / args / env / cwd
 *   - http：url / headers
 *   - 高级：connect_timeout / call_timeout / high_risk_tools / safe_tools
 */

import { useEffect, useState, useCallback } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  useMcpStore,
  emptyDraft,
  draftToConfig,
  configToDraft,
  validateMcpDraft,
  type McpDraft,
  type McpDraftErrors,
} from "../store/mcpStore";
import { useBackend } from "../providers/BackendProvider";

type FieldErrors = McpDraftErrors;

export function McpEditorPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { name: paramName } = useParams<{ name?: string }>();
  const serverName = paramName ? decodeURIComponent(paramName) : undefined;
  const isNew = !serverName || serverName === "new";

  const { saveMcpServer, requestMcpDetail } = useBackend();
  const detailServer = useMcpStore((s) => s.detailServer);
  const detailLoading = useMcpStore((s) => s.detailLoading);
  const draft = useMcpStore((s) => s.draft);
  const setDraft = useMcpStore((s) => s.setDraft);
  const clearDraft = useMcpStore((s) => s.clearDraft);

  // 本地表单态（从 draft 同步）
  const [form, setForm] = useState<McpDraft>(emptyDraft());
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});

  // ── 初始化：新建空白；编辑恢复草稿或拉详情 ────────────────────
  useEffect(() => {
    if (isNew) {
      localStorage.removeItem("mcp_draft__new");
      clearDraft();
      setForm(emptyDraft());
      return;
    }
    // 编辑：优先草稿，否则拉详情
    const stored = localStorage.getItem(`mcp_draft_${serverName}`);
    if (stored) {
      try {
        const d = JSON.parse(stored) as McpDraft;
        setForm(d);
        setDraft(d);
        return;
      } catch { /* 忽略 */ }
    }
    requestMcpDetail(serverName!);
  }, [isNew, serverName]);

  // ── 详情到达时填充表单（编辑模式） ───────────────────────────
  useEffect(() => {
    if (isNew) return;
    if (!detailServer || detailServer.name !== serverName) return;
    const stored = localStorage.getItem(`mcp_draft_${serverName}`);
    if (stored) {
      try {
        const d = JSON.parse(stored) as McpDraft;
        setForm(d);
        setDraft(d);
        return;
      } catch { /* 忽略 */ }
    }
    const d = configToDraft(detailServer.name, detailServer.config);
    setForm(d);
    setDraft(d);
  }, [isNew, serverName, detailServer]);

  // ── 自动保存草稿（防抖） ─────────────────────────────────────
  const syncDraft = useCallback(() => {
    setDraft({ ...form, name: isNew ? "" : (serverName ?? "") });
  }, [form, isNew, serverName, setDraft]);

  useEffect(() => {
    const timer = setTimeout(syncDraft, 500);
    return () => clearTimeout(timer);
  }, [syncDraft]);

  // ── 字段更新 ────────────────────────────────────────────────
  const patch = (partial: Partial<McpDraft>) => {
    setForm((prev) => ({ ...prev, ...partial }));
    setFieldErrors((prev) => ({ ...prev, name: undefined, command: undefined, url: undefined }));
  };

  // ── 校验（纯逻辑委托 validateMcpDraft，本层只做错误码 → i18n 文案映射）──
  const errorText: Record<string, string> = {
    name_required: t("mcp.errNameRequired"),
    name_pattern: t("mcp.errNamePattern"),
    command_required: t("mcp.errCommandRequired"),
    url_required: t("mcp.errUrlRequired"),
    url_scheme: t("mcp.errUrlScheme"),
  };

  function validate(): boolean {
    const codes = validateMcpDraft(form);
    const errors: FieldErrors = {};
    if (codes.name) errors.name = errorText[codes.name] ?? codes.name;
    if (codes.command) errors.command = errorText[codes.command] ?? codes.command;
    if (codes.url) errors.url = errorText[codes.url] ?? codes.url;
    setFieldErrors(errors);
    return Object.keys(errors).length === 0;
  }

  // ── 保存 ────────────────────────────────────────────────────
  const handleSave = () => {
    if (!validate()) {
      setSaveMsg(null);
      return;
    }
    setSaving(true);
    setSaveMsg(null);
    try {
      saveMcpServer(draftToConfig(form));
      localStorage.removeItem("mcp_draft__new");
      if (serverName) localStorage.removeItem(`mcp_draft_${serverName}`);
      clearDraft();
      setSaveMsg(t("mcp.saveSuccess"));
      setTimeout(() => navigate("/mcp"), 800);
    } catch {
      setSaveMsg(t("mcp.saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  // ── 离开编辑器（草稿确认） ─────────────────────────────────
  const handleBack = () => {
    // 编辑态保留草稿，直接返回列表（草稿已随输入自动持久化）
    navigate("/mcp");
  };

  // ── 编辑模式加载中 ──────────────────────────────────────────
  if (!isNew && detailLoading && !detailServer) {
    return (
      <div style={{ height: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "var(--bg-root)" }}>
        <span style={{ color: "var(--text-muted)", fontSize: "var(--text-md)" }}>{t("mcp.loadingDetail")}</span>
      </div>
    );
  }

  if (!isNew && !detailLoading && !detailServer) {
    return (
      <div style={{ height: "100vh", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", background: "var(--bg-root)" }}>
        <div style={{ fontSize: "var(--icon-empty-lg)", marginBottom: 12 }}>🔌</div>
        <span style={{ fontSize: "var(--text-md)", color: "var(--text-muted)", marginBottom: 16 }}>{t("mcp.notFoundEdit", { name: serverName })}</span>
        <button onClick={() => navigate("/mcp")} className="btn btn-ghost">{t("mcp.backToList")}</button>
      </div>
    );
  }

  return (
    <div style={{ height: "100vh", display: "flex", flexDirection: "column", background: "var(--bg-root)" }}>
      {/* 顶部工具栏 */}
      <div style={{
        padding: "12px 20px", background: "var(--bg-panel)", borderBottom: "1px solid var(--border-default)",
        display: "flex", alignItems: "center", gap: 12,
      }}>
        <button onClick={handleBack} className="btn btn-ghost btn-sm">{t("mcp.back")}</button>
        <span style={{ fontWeight: 700, fontSize: "var(--text-lg)", color: "var(--text-primary)" }}>
          {isNew ? t("mcp.newTitle") : t("mcp.editTitle", { name: serverName })}
        </span>
        {draft && (
          <span style={{
            fontSize: "var(--text-2xs)", fontWeight: 600, color: "var(--warning)",
            background: "color-mix(in srgb, var(--warning) 12%, transparent)", borderRadius: 4, padding: "2px 8px",
          }}>{t("mcp.draftBadge")}</span>
        )}
        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          <button onClick={handleSave} disabled={saving} className="btn btn-success btn-sm">
            {saving ? t("mcp.saving") : t("mcp.save")}
          </button>
        </div>
      </div>

      {/* 提示消息 */}
      {saveMsg && (
        <div style={{
          padding: "8px 20px", fontSize: "var(--text-sm)",
          color: saveMsg.startsWith("✅") ? "var(--success)" : "var(--danger)",
          background: saveMsg.startsWith("✅") ? "color-mix(in srgb, var(--success) 8%, transparent)" : "color-mix(in srgb, var(--danger) 8%, transparent)",
          borderBottom: "1px solid var(--border-default)",
        }}>
          {saveMsg}
        </div>
      )}

      {/* 表单区 */}
      <div style={{ flex: 1, overflowY: "auto", padding: "24px 36px" }}>
        <div style={{ maxWidth: 720, margin: "0 auto", display: "flex", flexDirection: "column", gap: 16 }}>
          {/* 名称 */}
          <Field label={t("mcp.fieldName")} required error={fieldErrors.name}>
            <input type="text" value={form.name} disabled={!isNew}
              onChange={(e) => patch({ name: e.target.value })}
              placeholder={t("mcp.placeholderName")}
              style={inputStyle(!isNew, !!fieldErrors.name)} />
          </Field>

          {/* 传输方式 */}
          <Field label={t("mcp.fieldTransport")} required>
            <div style={{ display: "flex", gap: 8 }}>
              {(["stdio", "http"] as const).map((tr) => (
                <button key={tr} type="button"
                  onClick={() => patch({ transport: tr })}
                  className={form.transport === tr ? "btn btn-primary btn-sm" : "btn btn-ghost btn-sm"}>
                  {tr === "stdio" ? t("mcp.transportStdio") : t("mcp.transportHttp")}
                </button>
              ))}
            </div>
          </Field>

          {/* 启用 */}
          <Field label={t("mcp.fieldEnabled")}>
            <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: "var(--text-base)", color: "var(--text-secondary)", cursor: "pointer" }}>
              <input type="checkbox" checked={form.enabled} onChange={(e) => patch({ enabled: e.target.checked })} style={{ accentColor: "var(--success)" }} />
              {t("mcp.enabledHint")}
            </label>
          </Field>

          {/* 暴露层级 */}
          <Field label={t("mcp.fieldTier")}>
            <div style={{ display: "flex", gap: 8 }}>
              {(["deferred", "always"] as const).map((tier) => (
                <button key={tier} type="button"
                  onClick={() => patch({ tier })}
                  className={form.tier === tier ? "btn btn-primary btn-sm" : "btn btn-ghost btn-sm"}>
                  {tier === "deferred" ? t("mcp.tierDeferred") : t("mcp.tierAlways")}
                </button>
              ))}
            </div>
          </Field>

          {/* stdio 专属 */}
          {form.transport === "stdio" && (
            <>
              <Field label={t("mcp.fieldCommand")} required error={fieldErrors.command}>
                <input type="text" value={form.command}
                  onChange={(e) => patch({ command: e.target.value })}
                  placeholder={t("mcp.placeholderCommand")}
                  style={inputStyle(false, !!fieldErrors.command)} />
              </Field>
              <Field label={t("mcp.fieldArgs")}>
                <input type="text" value={form.argsText}
                  onChange={(e) => patch({ argsText: e.target.value })}
                  placeholder={t("mcp.placeholderArgs")}
                  style={inputStyle(false)} />
              </Field>
              <Field label={t("mcp.fieldEnv")}>
                <textarea value={form.envText}
                  onChange={(e) => patch({ envText: e.target.value })}
                  placeholder={t("mcp.placeholderEnv")}
                  rows={3} style={{ ...inputStyle(false), resize: "vertical" }} />
              </Field>
              <Field label={t("mcp.fieldCwd")}>
                <input type="text" value={form.cwd}
                  onChange={(e) => patch({ cwd: e.target.value })}
                  placeholder={t("mcp.placeholderCwd")}
                  style={inputStyle(false)} />
              </Field>
            </>
          )}

          {/* http 专属 */}
          {form.transport === "http" && (
            <>
              <Field label={t("mcp.fieldUrl")} required error={fieldErrors.url}>
                <input type="text" value={form.url}
                  onChange={(e) => patch({ url: e.target.value })}
                  placeholder={t("mcp.placeholderUrl")}
                  style={inputStyle(false, !!fieldErrors.url)} />
              </Field>
              <Field label={t("mcp.fieldHeaders")}>
                <textarea value={form.headersText}
                  onChange={(e) => patch({ headersText: e.target.value })}
                  placeholder={t("mcp.placeholderHeaders")}
                  rows={3} style={{ ...inputStyle(false), resize: "vertical" }} />
              </Field>
            </>
          )}

          {/* 高级 */}
          <div style={{ marginTop: 8, padding: "14px", background: "var(--bg-card-subtle)", borderRadius: 8, border: "1px solid var(--border-default)", display: "flex", flexDirection: "column", gap: 14 }}>
            <div style={{ fontSize: "var(--text-xs)", fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.04em" }}>{t("mcp.advanced")}</div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
              <Field label={t("mcp.fieldConnectTimeout")}>
                <input type="number" value={form.connectTimeout}
                  onChange={(e) => patch({ connectTimeout: e.target.value })}
                  style={inputStyle(false)} />
              </Field>
              <Field label={t("mcp.fieldCallTimeout")}>
                <input type="number" value={form.callTimeout}
                  onChange={(e) => patch({ callTimeout: e.target.value })}
                  style={inputStyle(false)} />
              </Field>
            </div>
            <Field label={t("mcp.fieldHighRisk")}>
              <input type="text" value={form.highRiskText}
                onChange={(e) => patch({ highRiskText: e.target.value })}
                placeholder={t("mcp.placeholderHighRisk")}
                style={inputStyle(false)} />
            </Field>
            <Field label={t("mcp.fieldSafe")}>
              <input type="text" value={form.safeText}
                onChange={(e) => patch({ safeText: e.target.value })}
                placeholder={t("mcp.placeholderSafe")}
                style={inputStyle(false)} />
            </Field>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ── 表单字段组件 ────────────────────────────────────────────── */

function Field({
  label, required, error, children,
}: {
  label: string;
  required?: boolean;
  error?: string;
  children: React.ReactNode;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <label style={{ fontSize: "var(--text-sm)", fontWeight: 600, color: "var(--text-secondary)", display: "flex", alignItems: "center", gap: 4 }}>
        {label}
        {required && <span style={{ color: "var(--danger)" }}>*</span>}
      </label>
      {children}
      {error && <span style={{ fontSize: "var(--text-xs)", color: "var(--danger)", lineHeight: 1.4 }}>{error}</span>}
    </div>
  );
}

function inputStyle(disabled: boolean, hasError?: boolean): React.CSSProperties {
  return {
    padding: "8px 12px",
    fontSize: 13,
    background: disabled ? "rgba(255,255,255,0.02)" : "var(--bg-elevated)",
    border: hasError ? "1px solid var(--danger)" : "1px solid var(--border-default)",
    borderRadius: 8,
    color: "var(--text-primary)",
    outline: "none",
    width: "100%",
    boxSizing: "border-box",
    opacity: disabled ? 0.6 : 1,
    cursor: disabled ? "not-allowed" : "text",
  };
}
