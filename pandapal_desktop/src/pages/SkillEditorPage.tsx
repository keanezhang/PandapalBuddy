/**
 * src/pages/SkillEditorPage.tsx
 *
 * Skill 编辑 / 新建页面。
 *
 * 路由：
 *   /skills/new           → 新建 Skill
 *   /skills/:skillName/edit → 编辑已有 Skill
 *
 * 草稿机制：
 *   - 编辑过程中每次输入变更都会自动持久化到 localStorage
 *   - 页面加载时优先恢复草稿
 *   - 保存成功后清除草稿
 */

import { useEffect, useState, useCallback, useMemo } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useSkillStore } from "../store/skillStore";
import { useBackend } from "../providers/BackendProvider";
import { ask, save } from "@tauri-apps/plugin-dialog";
import { writeTextFile } from "@tauri-apps/plugin-fs";
import type { SkillDetailData } from "../store/skillStore";
import { marked } from "marked";

// ── 编辑器组件（懒加载 Monaco Editor）
import Editor, { loader } from "@monaco-editor/react";

// ── 校验规则
const NAME_PATTERN = /^[a-z0-9_-]+$/;
const MAX_DESCRIPTION_LEN = 250;

type FieldErrors = {
  name?: string;
  description?: string;
  when_to_use?: string;
};

// 日间主题
loader.init().then((monaco) => {
  monaco.editor.defineTheme("pandapal-skill-dark", {
    base: "vs-dark",
    inherit: true,
    rules: [
      { token: "comment", foreground: "6a9955", fontStyle: "italic" },
      { token: "string", foreground: "ce9178" },
      { token: "keyword", foreground: "569cd6" },
      { token: "number", foreground: "b5cea8" },
    ],
    colors: {
      "editor.background": "#1a1a1f",
      "editor.foreground": "#d4d4d4",
      "editor.lineHighlightBackground": "#26262b",
    },
  });
});

export function SkillEditorPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { skillName } = useParams<{ skillName?: string }>();
  const isNew = !skillName || skillName === "new";

  const { saveSkill, requestSkillDetail } = useBackend();
  const detailSkill = useSkillStore((s) => s.detailSkill);
  const detailLoading = useSkillStore((s) => s.detailLoading);
  const draft = useSkillStore((s) => s.draft);
  const setDraft = useSkillStore((s) => s.setDraft);
  const clearDraft = useSkillStore((s) => s.clearDraft);

  // ── 本地表单状态（从 draft 同步）
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [whenToUse, setWhenToUse] = useState("");
  const [content, setContent] = useState("");
  const [tagsText, setTagsText] = useState(""); // 逗号分隔
  const [skillType, setSkillType] = useState<"KNOWLEDGE" | "ACTION">("KNOWLEDGE");
  const [allowAutoTrigger, setAllowAutoTrigger] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});

  // ── 初始化：新建永远空白，编辑则恢复草稿/加载详情 ────────────────────
  useEffect(() => {
    if (isNew) {
      // 新建 → 永远从空白开始，清掉可能残留的草稿
      localStorage.removeItem("skill_draft__new");
      clearDraft();
      return;
    }

    // 编辑已有 Skill → 优先恢复草稿，否则拉取详情
    if (draft) {
      loadFromDraft(draft);
      return;
    }
    const stored = localStorage.getItem(`skill_draft_${skillName}`);
    if (stored) {
      try {
        const d = JSON.parse(stored);
        loadFromDraft(d);
        setDraft(d);
        return;
      } catch { /* 忽略 */ }
    }
    requestSkillDetail(skillName!);
  }, [isNew, skillName]);

  // ── detailSkill 到达时填充表单（编辑模式） ───────────────────────────
  useEffect(() => {
    if (isNew) return;
    if (!detailSkill) return;

    // 优先草稿
    const stored = localStorage.getItem(`skill_draft_${detailSkill.name}`);
    if (stored) {
      try {
        const d = JSON.parse(stored);
        loadFromDraft(d);
        setDraft(d);
        return;
      } catch { /* 忽略 */ }
    }

    // 无草稿 → 用 detail 填充
    loadFromDetail(detailSkill);
    // 同时创建草稿
    const newDraft = {
      name: detailSkill.name,
      description: detailSkill.description || "",
      when_to_use: detailSkill.when_to_use || "",
      content: detailSkill.content || "",
      tags: detailSkill.tags || [],
    };
    setDraft(newDraft);
  }, [isNew, detailSkill]);

  // ── 从草稿/详情加载表单 ──────────────────────────────────────────────
  function loadFromDraft(d: { name: string; description: string; when_to_use: string; content: string; tags: string[] }) {
    setName(d.name);
    setDescription(d.description);
    setWhenToUse(d.when_to_use);
    setContent(d.content);
    setTagsText(d.tags?.join(", ") || "");
  }

  function loadFromDetail(d: SkillDetailData) {
    setName(d.name);
    setDescription(d.description || "");
    setWhenToUse(d.when_to_use || "");
    setContent(d.content || "");
    setTagsText(d.tags?.join(", ") || "");
    setSkillType(d.type || "KNOWLEDGE");
    setAllowAutoTrigger(d.allow_auto_trigger ?? true);
  }

  // ── 自动保存草稿 ─────────────────────────────────────────────────────
  const syncDraft = useCallback(() => {
    const d = {
      name: isNew ? "" : (detailSkill?.name || skillName || ""),
      description,
      when_to_use: whenToUse,
      content,
      tags: tagsText.split(",").map((tag) => tag.trim()).filter(Boolean),
    };
    setDraft(d);
  }, [name, description, whenToUse, content, tagsText, isNew, detailSkill, skillName, setDraft]);

  // 每次表单变化 → 自动同步草稿
  useEffect(() => {
    const timer = setTimeout(syncDraft, 500); // 500ms 防抖
    return () => clearTimeout(timer);
  }, [syncDraft]);

  // ── 保存（含前端校验） ─────────────────────────────────────────────
  const handleSave = async () => {
    const errors: FieldErrors = {};

    if (!name.trim()) {
      errors.name = t("skills.errNameRequired");
    } else if (!NAME_PATTERN.test(name.trim())) {
      errors.name = t("skills.errNamePattern");
    }

    if (description.length > MAX_DESCRIPTION_LEN) {
      errors.description = t("skills.errDescTooLong", { n: MAX_DESCRIPTION_LEN });
    }

    if (!whenToUse.trim()) {
      errors.when_to_use = t("skills.errWhenToUse");
    }

    setFieldErrors(errors);
    if (Object.keys(errors).length > 0) {
      setSaveMsg(null);
      return;
    }

    setSaving(true);
    setSaveMsg(null);
    setFieldErrors({});
    try {
      const tags = tagsText.split(",").map((tag) => tag.trim()).filter(Boolean);
      saveSkill(name.trim(), description.trim(), whenToUse.trim(), content, tags);
      // 清除草稿（新建 key + 编辑 key 都要清）
      localStorage.removeItem("skill_draft__new");
      if (detailSkill?.name) {
        localStorage.removeItem(`skill_draft_${detailSkill.name}`);
      }
      clearDraft();
      setSaveMsg(t("skills.saveSuccess"));
      setTimeout(() => navigate("/skills"), 800);
    } catch {
      setSaveMsg(t("skills.saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  const handleExport = async () => {
    if (!name.trim()) {
      setSaveMsg(t("skills.errExportName"));
      return;
    }
    const filePath = await save({
      defaultPath: `${name.trim()}.md`,
      filters: [{ name: "Markdown", extensions: ["md"] }],
    });
    if (!filePath) return;
    try {
      await writeTextFile(filePath, content);
      setSaveMsg(t("skills.exportSuccess", { path: filePath }));
    } catch (e) {
      console.error("[editor] export failed:", e);
      setSaveMsg(t("skills.exportFailed", { err: String(e) }));
    }
  };

  // ── 编辑模式加载中 ───────────────────────────────────────────────────
  if (!isNew && detailLoading && !detailSkill) {
    return (
      <div style={{ height: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "var(--bg-root)" }}>
        <span style={{ color: "var(--text-muted)", fontSize: "var(--text-md)" }}>{t("skills.loadingDetail")}</span>
      </div>
    );
  }

  if (!isNew && !detailLoading && !detailSkill) {
    return (
      <div style={{ height: "100vh", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", background: "var(--bg-root)" }}>
        <div style={{ fontSize: "var(--icon-empty-lg)", marginBottom: 12 }}>🔍</div>
        <span style={{ fontSize: "var(--text-md)", color: "var(--text-muted)", marginBottom: 16 }}>{t("skills.notFoundEdit", { name: skillName })}</span>
        <button
          onClick={() => navigate("/skills")}
          style={{
            background: "var(--bg-elevated)", border: "1px solid var(--border-default)", borderRadius: 8,
            padding: "8px 20px", cursor: "pointer", fontSize: "var(--text-base)", color: "var(--text-secondary)",
          }}
        >{t("skills.backToList")}</button>
      </div>
    );
  }

  // ── 渲染 ──────────────────────────────────────────────────────────────
  return (
    <div style={{ height: "100vh", display: "flex", flexDirection: "column", background: "var(--bg-root)" }}>
      {/* 顶部工具栏 */}
      <div style={{
        padding: "12px 20px",
        background: "var(--bg-panel)",
        borderBottom: "1px solid var(--border-default)",
        display: "flex",
        alignItems: "center",
        gap: 12,
      }}>
        <button
          onClick={async () => {
            const saveDraft = await ask(t("skills.askKeepDraft"), {
              title: t("skills.leaveTitle"),
              kind: "warning",
            });
            if (saveDraft) {
              // 立即同步最新草稿到 localStorage
              syncDraft();
            } else {
              // 清除所有草稿
              localStorage.removeItem("skill_draft__new");
              if (detailSkill?.name) {
                localStorage.removeItem(`skill_draft_${detailSkill.name}`);
              }
              if (skillName && skillName !== "new") {
                localStorage.removeItem(`skill_draft_${skillName}`);
              }
              clearDraft();
            }
            navigate("/skills");
          }}
          style={{
            background: "none", border: "1px solid var(--border-default)", borderRadius: 8,
            padding: "7px 14px", cursor: "pointer", fontSize: "var(--text-base)", fontWeight: 500,
            color: "var(--text-secondary)",
          }}
        >{t("skills.back")}</button>

        <span style={{ fontWeight: 700, fontSize: "var(--text-lg)", color: "var(--text-primary)" }}>
          {isNew ? t("skills.newTitle") : t("skills.editTitle", { name: skillName })}
        </span>

        {/* Draft 标识 */}
        {draft && (
          <span style={{
            fontSize: "var(--text-2xs)", fontWeight: 600, color: "var(--warning)",
            background: "color-mix(in srgb, var(--warning) 12%, transparent)", borderRadius: 4,
            padding: "2px 8px",
          }}>{t("skills.draftBadge")}</span>
        )}

        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          <button onClick={handleExport} style={{
            background: "color-mix(in srgb, var(--accent) 10%, transparent)", color: "var(--accent-soft)",
            border: "1px solid color-mix(in srgb, var(--accent) 20%, transparent)", borderRadius: 8,
            padding: "7px 16px", cursor: "pointer", fontSize: "var(--text-sm)", fontWeight: 500,
          }}>{t("skills.export")}</button>

          <button
            onClick={handleSave}
            disabled={saving}
            style={{
              background: saving ? "color-mix(in srgb, var(--success) 20%, transparent)" : "color-mix(in srgb, var(--success) 15%, transparent)",
              color: "var(--success)",
              border: "1px solid color-mix(in srgb, var(--success) 30%, transparent)",
              borderRadius: 8, padding: "7px 20px", cursor: saving ? "not-allowed" : "pointer",
              fontSize: "var(--text-base)", fontWeight: 600,
            }}
          >{saving ? t("skills.saving") : t("skills.save")}</button>
        </div>
      </div>

      {/* 提示消息 */}
      {saveMsg && (
        <div style={{
          padding: "8px 20px",
          fontSize: "var(--text-sm)",
          color: saveMsg.startsWith("✅") ? "var(--success)" : "var(--danger)",
          background: saveMsg.startsWith("✅") ? "color-mix(in srgb, var(--success) 8%, transparent)" : "color-mix(in srgb, var(--danger) 8%, transparent)",
          borderBottom: "1px solid var(--border-default)",
        }}>
          {saveMsg}
        </div>
      )}

      {/* 表单区（左） + 预览区（右） */}
      <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
        {/* 左：表单 */}
        <div style={{
          width: 360, flexShrink: 0,
          borderRight: "1px solid var(--border-default)",
          overflowY: "auto",
          padding: "20px",
          display: "flex", flexDirection: "column", gap: 16,
        }}>
          {/* 名称 */}
          <Field label={t("skills.fieldName")} required error={fieldErrors.name}>
            <input
              type="text"
              value={name}
              onChange={(e) => { setName(e.target.value); if (fieldErrors.name) setFieldErrors((prev) => ({ ...prev, name: undefined })); }}
              placeholder={t("skills.placeholderName")}
              disabled={!isNew}
              style={inputStyle(!isNew, !!fieldErrors.name)}
            />
          </Field>

          {/* 类型 */}
          <Field label={t("skills.fieldType")}>
            <select
              value={skillType}
              onChange={(e) => setSkillType(e.target.value as "KNOWLEDGE" | "ACTION")}
              style={inputStyle(false)}
            >
              <option value="KNOWLEDGE">{t("skills.typeKnowledge")}</option>
              <option value="ACTION">{t("skills.typeAction")}</option>
            </select>
          </Field>

          {/* 自动触发 */}
          <Field label={t("skills.fieldAutoTrigger")}>
            <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: "var(--text-base)", color: "var(--text-secondary)", cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={allowAutoTrigger}
                onChange={(e) => setAllowAutoTrigger(e.target.checked)}
                style={{ accentColor: "var(--success)" }}
              />
              {t("skills.allowAutoTrigger")}
            </label>
          </Field>

          {/* 描述 */}
          <Field label={t("skills.fieldDesc")} error={fieldErrors.description}>
            <textarea
              value={description}
              onChange={(e) => { setDescription(e.target.value); if (fieldErrors.description) setFieldErrors((prev) => ({ ...prev, description: undefined })); }}
              placeholder={t("skills.placeholderDesc")}
              rows={3}
              style={{ ...inputStyle(false, !!fieldErrors.description), resize: "vertical", minHeight: 60 }}
            />
            <div style={{ fontSize: "var(--text-2xs)", color: description.length > MAX_DESCRIPTION_LEN ? "var(--danger)" : "var(--text-muted)", textAlign: "right", marginTop: 2 }}>
              {description.length}/{MAX_DESCRIPTION_LEN}
            </div>
          </Field>

          {/* 触发时机 */}
          <Field label={t("skills.fieldWhenToUse")} required error={fieldErrors.when_to_use}>
            <textarea
              value={whenToUse}
              onChange={(e) => { setWhenToUse(e.target.value); if (fieldErrors.when_to_use) setFieldErrors((prev) => ({ ...prev, when_to_use: undefined })); }}
              placeholder={t("skills.placeholderWhenToUse")}
              rows={3}
              style={{ ...inputStyle(false, !!fieldErrors.when_to_use), resize: "vertical", minHeight: 60 }}
            />
          </Field>

          {/* 标签 */}
          <Field label={t("skills.fieldTags")}>
            <input
              type="text"
              value={tagsText}
              onChange={(e) => setTagsText(e.target.value)}
              placeholder={t("skills.placeholderTags")}
              style={inputStyle(false)}
            />
          </Field>

          {/* 元信息（编辑模式） */}
          {!isNew && detailSkill && (
            <div style={{ marginTop: 8, padding: "12px", background: "var(--bg-card-subtle)", borderRadius: 8, border: "1px solid var(--border-default)" }}>
              <div style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", marginBottom: 4 }}>{t("skills.metaInfo")}</div>
              <div style={{ fontSize: "var(--text-sm)", color: "var(--text-secondary)", lineHeight: 1.8 }}>
                <div>{t("skills.source", { src: detailSkill.source === "system" ? t("skills.sourceSystem") : t("skills.sourceUser") })}</div>
                <div>{t("skills.metaSize", { size: (detailSkill.size / 1024).toFixed(1) })}</div>
                <div>{t("skills.metaModified", { time: detailSkill.modified_at || t("skills.unknown") })}</div>
              </div>
            </div>
          )}
        </div>

        {/* 右：编辑器 + 预览 */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
          {/* 编辑器区域 */}
          <div style={{ flex: "0 0 55%", display: "flex", flexDirection: "column", minHeight: 0 }}>
            <div style={{
              padding: "8px 16px",
              fontSize: "var(--text-xs)",
              color: "var(--text-muted)",
              background: "var(--bg-panel)",
              borderBottom: "1px solid var(--border-default)",
              display: "flex",
              alignItems: "center",
              gap: 8,
            }}>
              <span>{t("skills.editorTitle")}</span>
              <span style={{ marginLeft: "auto" }}>
                {t("skills.charCount", { n: content.length.toLocaleString() })}
              </span>
            </div>
            <Editor
              height="100%"
              defaultLanguage="markdown"
              value={content}
              onChange={(v) => setContent(v || "")}
              theme="pandapal-skill-dark"
              options={{
                fontSize: 13,
                fontFamily: "'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace",
                lineNumbers: "on",
                minimap: { enabled: false },
                wordWrap: "on",
                scrollBeyondLastLine: false,
                padding: { top: 12 },
              }}
            />
          </div>

          {/* 分割线 */}
          <div style={{
            height: 4,
            background: "var(--border-default)",
            cursor: "row-resize",
            flexShrink: 0,
          }} />

          {/* 实时预览 */}
          <div style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0 }}>
            <div style={{
              padding: "8px 16px",
              fontSize: "var(--text-xs)",
              color: "var(--text-muted)",
              background: "var(--bg-panel)",
              borderBottom: "1px solid var(--border-default)",
              display: "flex",
              alignItems: "center",
              gap: 8,
              flexShrink: 0,
            }}>
              <span>{t("skills.livePreview")}</span>
            </div>
            <MarkdownPreview content={content} />
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Markdown 预览组件 ────────────────────────────────────────────────────

function MarkdownPreview({ content }: { content: string }) {
  const { t } = useTranslation();
  const html = useMemo(() => {
    if (!content.trim()) return "";
    try {
      return marked.parse(content, { async: false }) as string;
    } catch {
      return `<p style='color:var(--danger)'>${t("skills.markdownError")}</p>`;
    }
  }, [content, t]);

  return (
    <div
      style={{
        flex: 1,
        overflowY: "auto",
        padding: "16px 20px",
      }}
    >
      {!content.trim() ? (
        <p style={{ fontSize: "var(--text-base)", color: "var(--text-muted)", fontStyle: "italic" }}>{t("skills.previewPlaceholder")}</p>
      ) : (
        <div
          className="markdown-preview"
          dangerouslySetInnerHTML={{ __html: html }}
          style={{
            fontSize: "var(--text-md)",
            color: "var(--text-primary)",
            lineHeight: 1.75,
          }}
        />
      )}
    </div>
  );
}

// ── 表单字段组件 ────────────────────────────────────────────────────────

function Field({
  label,
  required,
  error,
  children,
}: {
  label: string;
  required?: boolean;
  error?: string;
  children: React.ReactNode;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <label style={{
        fontSize: "var(--text-sm)",
        fontWeight: 600,
        color: "var(--text-secondary)",
        display: "flex",
        alignItems: "center",
        gap: 4,
      }}>
        {label}
        {required && <span style={{ color: "var(--danger)" }}>*</span>}
      </label>
      {children}
      {error && (
        <span style={{ fontSize: "var(--text-xs)", color: "var(--danger)", lineHeight: 1.4 }}>{error}</span>
      )}
    </div>
  );
}

// ── 输入框统一样式 ──────────────────────────────────────────────────────

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
