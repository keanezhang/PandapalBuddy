/**
 * src/pages/KnowledgeBasePage.tsx
 *
 * 知识库列表页（仿 McpPage.tsx）。每张卡片展示名称 / 状态 / 文档数，
 * 操作：详情（进入编辑页）、建库 / 重新建库、删除。
 * 建库进度由后端 KB_BUILD_PROGRESS 事件驱动（写入 kbStore.buildByName）。
 */

import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useKbStore, kbStatusMeta } from "../store/kbStore";
import { useBackend } from "../providers/BackendProvider";
import type { KBSummary } from "../types/api";
import { ask } from "@tauri-apps/plugin-dialog";
import { Badge } from "../components/ui";

export function KnowledgeBasePage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { requestKbList, buildKb, deleteKb } = useBackend();
  const loading = useKbStore((s) => s.loading);
  const kbs = useKbStore((s) => s.kbs);
  const buildByName = useKbStore((s) => s.buildByName);

  useEffect(() => {
    requestKbList();
  }, [requestKbList]);

  const handleDelete = async (name: string) => {
    if (await ask(`确定删除知识库「${name}」吗？文档与索引将一并清除。`, { title: "删除知识库", kind: "warning" })) {
      deleteKb(name);
    }
  };

  return (
    <div className="page-root">
      <div className="page-header page-header--gold">
        <div style={{ flex: 1 }}>
          <h1 className="page-title">
            <span style={{ width: 34, height: 34, fontSize: "var(--text-lg)", display: "flex", alignItems: "center", justifyContent: "center" }}>📚</span>
            知识库
            {kbs.length > 0 && (
              <span style={{ fontSize: "var(--text-base)", fontWeight: 500, color: "var(--text-tertiary)" }}>· {kbs.length}</span>
            )}
          </h1>
        </div>
        <div className="skills-page-actions">
          <button onClick={() => navigate("/knowledge/new")} className="btn btn-success btn-sm">新建知识库</button>
          {loading && <span className="skills-loading-dot" style={{ marginLeft: 8 }} title="刷新中" />}
        </div>
      </div>

      <div style={{ flex: 1, overflowY: "auto", padding: "28px 36px" }}>
        <div style={{ width: "90%", margin: "0 auto" }}>
          {loading && kbs.length === 0 ? (
            <div className="skills-loading"><span className="skills-loading-dot" /> {t("common.loading")}</div>
          ) : kbs.length === 0 ? (
            <div className="skills-empty">
              <div className="skills-empty-icon">📚</div>
              <div className="skills-empty-title">还没有知识库</div>
              <div className="skills-empty-desc">导入你的私有文档，让 AI 基于它们回答。</div>
              <div className="skills-empty-actions">
                <button onClick={() => navigate("/knowledge/new")} className="btn btn-success">新建知识库</button>
              </div>
            </div>
          ) : (
            <div className="skills-grid">
              {kbs.map((kb) => (
                <KbCard
                  key={kb.name}
                  kb={kb}
                  buildState={buildByName[kb.name]}
                  onOpen={() => navigate(`/knowledge/${encodeURIComponent(kb.name)}/edit`)}
                  onBuild={() => buildKb(kb.name, kb.status === "ready")}
                  onDelete={() => handleDelete(kb.name)}
                />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/* ── 知识库卡片 ─────────────────────────────────────────────── */

function KbCard({
  kb,
  buildState,
  onOpen,
  onBuild,
  onDelete,
}: {
  kb: KBSummary;
  buildState?: { stage: string; percent: number; message: string };
  onOpen: () => void;
  onBuild: () => void;
  onDelete: () => void;
}) {
  const meta = kbStatusMeta(kb.status);
  const building = kb.status === "building";

  return (
    <div className="skill-card mcp-card" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
        <span className="skill-card-icon icon-teal" style={{ width: 40, height: 40, fontSize: "var(--text-xl)", borderRadius: "var(--radius-md)", flexShrink: 0 }}>
          📚
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <span style={{ fontWeight: 600, fontSize: "var(--text-md)", color: "var(--text-primary)", cursor: "pointer" }} onClick={onOpen}>
              {kb.name}
            </span>
            <Badge variant={meta.variant}>{statusText(kb.status)}</Badge>
            {kb.enabled_in_chat ? (
              <span className="skill-card-tag">对话启用</span>
            ) : (
              <span className="skill-card-tag" style={{ color: "var(--text-tertiary)" }}>对话停用</span>
            )}
          </div>
          <div style={{ fontSize: "var(--text-xs)", color: "var(--text-tertiary)", marginTop: 4 }}>
            {kb.document_count} 个文档{!kb.enable_bm25 ? " · 仅向量" : ""}
          </div>
          {kb.description && (
            <div style={{ fontSize: "var(--text-xs)", color: "var(--text-secondary)", marginTop: 4, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {kb.description}
            </div>
          )}
        </div>
      </div>

      {building && buildState && (
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <div style={{ fontSize: "var(--text-xs)", color: "var(--text-secondary)" }}>{buildState.message}</div>
          <div style={{ height: 6, borderRadius: 3, background: "var(--bg-hover)", overflow: "hidden" }}>
            <div style={{ height: "100%", width: `${buildState.percent}%`, background: "var(--accent)", transition: "width var(--duration-fast)" }} />
          </div>
        </div>
      )}

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <button className="btn btn-ghost btn-sm" onClick={onOpen}>详情</button>
        <button className="btn btn-primary btn-sm" onClick={onBuild} disabled={building}>
          {building ? "建库中…" : kb.status === "ready" ? "重新建库" : kb.status === "pending" ? "开始建库" : "建库"}
        </button>
        <button className="btn btn-danger btn-sm" onClick={onDelete}>删除</button>
      </div>
    </div>
  );
}

function statusText(status: string): string {
  switch (status) {
    case "ready":
      return "已就绪";
    case "building":
      return "索引中";
    case "pending":
      return "待索引";
    case "failed":
      return "失败";
    case "empty":
    default:
      return "空库";
  }
}
