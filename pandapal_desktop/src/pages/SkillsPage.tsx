/**
 * src/pages/SkillsPage.tsx — v2 嵌入模式
 *
 * 从 ChatLayout (Shell) 拿到 Sidebar。
 * 本组件不再有 100vh 外壳 / banner / 返回按钮，直接渲染技能内容。
 */

import { useEffect, useState, useRef, useCallback } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useSkillStore, useFilteredSkills } from "../store/skillStore";
import { useBackend } from "../providers/BackendProvider";
import type { SkillItem } from "../types/api";
import { ask, open, save } from "@tauri-apps/plugin-dialog";
import { getCurrentWebviewWindow } from "@tauri-apps/api/webviewWindow";
import { Button, Badge, Dropdown } from "../components/ui";

/* ── 图标颜色散列 ──────────────────────────────────────────── */
const ICON_PRESETS = [
  "icon-sky", "icon-amber", "icon-violet", "icon-rose",
  "icon-emerald", "icon-orange", "icon-pink", "icon-blue",
  "icon-fuchsia", "icon-teal", "icon-indigo", "icon-lime",
] as const;

function iconClassFor(name: string): string {
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = (hash * 31 + name.charCodeAt(i)) | 0;
  return ICON_PRESETS[Math.abs(hash) % ICON_PRESETS.length];
}

function truncateDesc(text: string, max = 48): string {
  if (!text) return "";
  if (text.length <= max) return text;
  return text.slice(0, max) + "…";
}

export function SkillsPage() {
  const navigate = useNavigate();
  const { skillName } = useParams<{ skillName?: string }>();
  const decodedName = skillName ? decodeURIComponent(skillName) : undefined;

  const { requestSkillList, requestSkillDetail, deleteSkill, importSkill, exportSkill } = useBackend();
  const skills = useSkillStore((s) => s.skills);
  const loading = useSkillStore((s) => s.loading);
  const detailSkill = useSkillStore((s) => s.detailSkill);
  const detailLoading = useSkillStore((s) => s.detailLoading);
  const searchQuery = useSkillStore((s) => s.searchQuery);
  const setSearchQuery = useSkillStore((s) => s.setSearchQuery);
  const hasDraft = useSkillStore((s) => s.hasDraft);
  const { system: systemSkills, user: userSkills } = useFilteredSkills();

  /* ── 拖拽导入 ────────────────────────────────────────────── */
  const [isDragOver, setIsDragOver] = useState(false);
  const importSkillRef = useRef(importSkill);
  importSkillRef.current = importSkill;

  useEffect(() => {
    let unlisten: (() => void) | undefined;
    getCurrentWebviewWindow().onDragDropEvent((event) => {
      const { type } = event.payload;
      if (type === "over") {
        setIsDragOver(true);
      } else {
        setIsDragOver(false);
        if (type === "drop") {
          const paths = event.payload.paths;
          if (paths && paths.length > 0) handleDrop(paths);
        }
      }
    }).then((fn) => { unlisten = fn; });
    return () => { unlisten?.(); };
  }, []);

  const handleDrop = useCallback(async (paths: string[]) => {
    for (const p of paths) {
      const lower = p.toLowerCase();
      if (lower.endsWith(".zip")) importSkillRef.current("", "zip", false, p);
      else importSkillRef.current("", "folder", false, p);
    }
  }, []);

  useEffect(() => { requestSkillList(); }, [requestSkillList]);
  useEffect(() => {
    if (decodedName) requestSkillDetail(decodedName);
  }, [decodedName, requestSkillDetail]);

  const handleImportFolder = async () => {
    const folderPath = await open({ directory: true, title: "选择技能文件夹" });
    if (folderPath) importSkill("", "folder", false, folderPath as string);
  };
  const handleImportZip = async () => {
    const filePath = await open({
      title: "选择技能 ZIP 文件",
      filters: [{ name: "ZIP 文件", extensions: ["zip"] }],
    });
    if (filePath) importSkill("", "zip", false, filePath as string);
  };

  /* ── 辅助组件 ────────────────────────────────────────────── */
  const renderDragOverlay = () => (
    <div style={{ position: "fixed", inset: 0, zIndex: 9999, background: "color-mix(in srgb, var(--accent) 12%, transparent)", border: "3px dashed var(--accent)", display: "flex", alignItems: "center", justifyContent: "center", pointerEvents: "none" }}>
      <div style={{ background: "var(--bg-elevated)", borderRadius: 16, padding: "32px 48px", textAlign: "center", boxShadow: "var(--shadow-lg)" }}>
        <div style={{ fontSize: "var(--icon-empty-lg)", marginBottom: 12 }}>📥</div>
        <div style={{ fontSize: "var(--text-xl)", fontWeight: 700, color: "var(--text-primary)" }}>松开导入技能</div>
        <div style={{ fontSize: "var(--text-base)", color: "var(--text-tertiary)", marginTop: 8 }}>支持文件夹 / ZIP 文件</div>
      </div>
    </div>
  );

  const renderSkillCard = (skill: SkillItem) => {
    const draftExists = hasDraft(skill.name);
    return (
      <div key={skill.name} className="skill-card" onClick={() => navigate(`/skills/${encodeURIComponent(skill.name)}`)}>
        <span className={`skill-card-icon ${iconClassFor(skill.name)}`}>
          {skill.type === "ACTION" ? "⚡" : "📋"}
        </span>
        <div className="skill-card-body">
          <div className="skill-card-name" style={{ display: "flex", alignItems: "center", gap: 6 }}>
            {skill.name}
            {draftExists && <span className="badge badge-yellow" style={{ fontSize: "var(--text-2xs)", padding: "1px 5px" }}>Draft</span>}
          </div>
          <div className="skill-card-desc">{truncateDesc(skill.description, 120)}</div>
          {skill.tags && skill.tags.length > 0 && (
            <div className="skill-card-tags">
              {skill.tags.slice(0, 3).map((tag) => <span key={tag} className="skill-card-tag">{tag}</span>)}
              {skill.tags.length > 3 && <span className="skill-card-tag">+{skill.tags.length - 3}</span>}
            </div>
          )}
        </div>
        <span className="skill-card-check">✓</span>
      </div>
    );
  };

  /* ════════════════════════════════════════════════════════════
     详情页渲染（嵌入模式：无 100vh / 无 banner / 无返回按钮）
     ════════════════════════════════════════════════════════════ */
  if (decodedName) {
    const detail = detailSkill;
    const isUser = detail?.source === "user";

    if (detailLoading || (!detail && loading)) {
      return (
        <div style={{ display: "flex", flexDirection: "column", flex: 1, background: "var(--bg-root)" }}>
          <div style={{ padding: "14px 24px", background: "var(--bg-elevated)", borderBottom: "1px solid var(--border-subtle)", display: "flex", alignItems: "center", gap: 12 }}>
            <button onClick={() => navigate("/skills")} className="btn btn-ghost btn-sm">← 返回</button>
            <span style={{ fontWeight: 600, fontSize: "var(--text-lg)", color: "var(--text-primary)" }}>{decodedName}</span>
          </div>
          <div className="skills-loading" style={{ flex: 1 }}>
            <span className="skills-loading-dot" /> 加载中…
          </div>
        </div>
      );
    }
    if (!detail) {
      return (
        <div style={{ display: "flex", flexDirection: "column", flex: 1, background: "var(--bg-root)" }}>
          <div style={{ padding: "14px 24px", background: "var(--bg-elevated)", borderBottom: "1px solid var(--border-subtle)", display: "flex", alignItems: "center", gap: 12 }}>
            <button onClick={() => navigate("/skills")} className="btn btn-ghost btn-sm">← 返回</button>
            <span style={{ fontWeight: 600, fontSize: "var(--text-lg)", color: "var(--text-primary)" }}>{decodedName}</span>
          </div>
          <div className="skills-empty" style={{ flex: 1 }}>
            <div className="skills-empty-icon">🔍</div>
            <div className="skills-empty-title">未找到技能 &quot;{decodedName}&quot;</div>
          </div>
        </div>
      );
    }

    return (
      <div className="page-root">
        <div className={isUser ? "page-header page-header--success" : "page-header"}>
          <button onClick={() => navigate("/skills")} className="btn btn-ghost btn-sm">← 返回列表</button>
          <span className={`skill-card-icon ${iconClassFor(detail.name)}`} style={{ width: 36, height: 36, fontSize: "var(--text-lg)" }}>{isUser ? "🎨" : "📦"}</span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontWeight: 700, fontSize: "var(--text-lg)", color: "var(--text-primary)" }}>{detail.name}</div>
            <div style={{ fontSize: "var(--text-xs)", color: "var(--text-tertiary)", marginTop: 2 }}>{truncateDesc(detail.description, 72) || "暂无描述"}</div>
          </div>
          <Badge variant={isUser ? "green" : "purple"}>{isUser ? "🎨 我的技能" : "📦 系统技能"}</Badge>
          <Dropdown
            trigger={<Button variant="ghost" size="sm">📤 导出 ▾</Button>}
            items={[
              { label: "📦 导出为 ZIP", onClick: async () => { const fp = await save({ defaultPath: `${detail.name}.zip`, filters: [{ name: "ZIP 完整包", extensions: ["zip"] }] }); if (fp) exportSkill(detail.name, "zip", fp); } },
              { label: "📁 导出为文件夹", onClick: async () => { const dp = await open({ directory: true, title: "选择导出目标文件夹" }); if (dp) exportSkill(detail.name, "folder", (dp as string) + "/" + detail.name); } },
            ]}
          />
          {isUser && (
            <div style={{ display: "flex", gap: 8, flexShrink: 0 }}>
              <button onClick={() => navigate(`/skills/${encodeURIComponent(detail.name)}/edit`)} className="btn btn-success btn-sm">编辑</button>
              <button onClick={async () => { if (await ask(`确认删除技能 "${detail.name}"？`, { title: "删除技能", kind: "warning" })) { deleteSkill(detail.name); navigate("/skills"); } }} className="btn btn-danger btn-sm">删除</button>
            </div>
          )}
        </div>
        <div style={{ flex: 1, overflowY: "auto", padding: "24px 36px", maxWidth: 820, margin: "0 auto", width: "100%" }}>
          <div className="skill-detail-card accent-left">
            <h3 style={{ fontSize: "var(--text-md)", fontWeight: 600, color: "var(--text-primary)", margin: "0 0 12px" }}>📝 技能描述</h3>
            <p style={{ fontSize: "var(--text-md)", color: "var(--text-secondary)", lineHeight: 1.7, margin: "0 0 16px" }}>{detail.description || "暂无描述"}</p>
            {detail.tags && detail.tags.length > 0 && <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 14 }}>{detail.tags.map((tag) => <span key={tag} className="skill-card-tag">{tag}</span>)}</div>}
            <div style={{ display: "flex", gap: 20, fontSize: "var(--text-sm)", color: "var(--text-tertiary)" }}>
              <span>📄 {(detail.size / 1024).toFixed(1)} KB</span><span>🕐 {detail.modified_at || "未知"}</span><span>🔖 {detail.type === "ACTION" ? "动作" : "知识"}技能</span>
            </div>
          </div>
          {detail.when_to_use && (
            <div className="skill-detail-card warn-left">
              <h3 style={{ fontSize: "var(--text-md)", fontWeight: 600, color: "var(--text-primary)", margin: "0 0 10px" }}>🎯 何时调用</h3>
              <p style={{ fontSize: "var(--text-base)", color: "var(--text-secondary)", lineHeight: 1.85, margin: 0 }}>{detail.when_to_use}</p>
            </div>
          )}
          <div className="skill-detail-card">
            <h3 style={{ fontSize: "var(--text-md)", fontWeight: 600, color: "var(--text-primary)", margin: "0 0 12px" }}>📋 技能内容</h3>
            <pre style={{ fontSize: "var(--text-sm)", color: "var(--text-secondary)", lineHeight: 1.7, margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-word", background: "var(--color-code-bg)", borderRadius: 8, padding: 18, maxHeight: 500, overflowY: "auto", border: "1px solid var(--border-subtle)" }}>{detail.content || "(暂无内容)"}</pre>
          </div>
        </div>
        {isDragOver && renderDragOverlay()}
      </div>
    );
  }

  /* ════════════════════════════════════════════════════════════
     列表页渲染（嵌入模式）
     ════════════════════════════════════════════════════════════ */
  return (
    <div className="page-root">

      {/* 紧凑 Banner（无返回按钮） */}
      <div className="page-header">
        <div style={{ flex: 1 }}>
          <h1 className="page-title" style={{ marginBottom: 4 }}>
            <span style={{ width: 34, height: 34, fontSize: "var(--text-lg)", display: "flex", alignItems: "center", justifyContent: "center" }}>📙</span>
            技能
          </h1>

        </div>
        <div className="skills-page-actions">
          <Dropdown
            trigger={<Button variant="accent" size="sm">📥 导入 ▾</Button>}
            items={[
              { label: "📁 导入文件夹", onClick: handleImportFolder },
              { label: "📦 导入 ZIP", onClick: handleImportZip },
            ]}
          />
          <button onClick={() => navigate("/skills/new")} className="btn btn-success btn-sm">＋ 新建</button>
          {loading && <span className="skills-loading-dot" style={{ marginLeft: 8 }} title="刷新中..." />}
        </div>
      </div>

      {/* 内容区 */}
      <div style={{ flex: 1, overflowY: "auto", padding: "28px 36px" }}>
        <div style={{ width: "90%", margin: "0 auto" }}>
          <div className="skills-search-wrap" style={{ marginBottom: 32 }}>
            <span className="skills-search-icon">🔍</span>
            <input type="text" className="skills-search-input" value={searchQuery} onChange={(e) => setSearchQuery(e.target.value)} placeholder="搜索技能名称、描述或标签..." style={{ padding: "12px 36px 12px 42px", fontSize: "var(--text-md)", borderRadius: 10 }} />
            {searchQuery && <button className="skills-search-clear" onClick={() => setSearchQuery("")}>✕</button>}
          </div>

          {loading && skills.length === 0 ? (
            <div className="skills-loading"><span className="skills-loading-dot" /> 加载技能列表中…</div>
          ) : skills.length === 0 ? (
            <div className="skills-empty">
              <div className="skills-empty-icon">📭</div>
              <div className="skills-empty-title">暂无可用技能</div>
              <div className="skills-empty-desc">你可以新建一个自定义技能，或从外部导入 SKILL.md 文件</div>
              <div className="skills-empty-actions">
                <button onClick={() => navigate("/skills/new")} className="btn btn-success">＋ 新建技能</button>
                <Dropdown
                  trigger={<Button variant="accent">📥 导入 ▾</Button>}
                  items={[
                    { label: "📁 导入文件夹", onClick: handleImportFolder },
                    { label: "📦 导入 ZIP", onClick: handleImportZip },
                  ]}
                />
              </div>
            </div>
          ) : searchQuery && systemSkills.length === 0 && userSkills.length === 0 ? (
            <div className="skills-no-match">
              <div className="skills-no-match-icon">🔍</div>
              <div className="skills-no-match-title">没有匹配 &quot;{searchQuery}&quot; 的技能</div>
              <div className="skills-no-match-hint">请尝试其他关键词</div>
            </div>
          ) : (
            <>
              {systemSkills.length > 0 && (
                <section className="skills-section">
                  <div className="skills-section-label">System Installed</div>
                  <div className="skills-section-divider" />
                  <div className="skills-grid">{systemSkills.map(renderSkillCard)}</div>
                </section>
              )}
              {userSkills.length > 0 && (
                <section className="skills-section">
                  <div className="skills-section-label">My Skills</div>
                  <div className="skills-section-divider" />
                  <div className="skills-grid">{userSkills.map(renderSkillCard)}</div>
                </section>
              )}
            </>
          )}
        </div>
      </div>
      {isDragOver && renderDragOverlay()}
    </div>
  );
}
