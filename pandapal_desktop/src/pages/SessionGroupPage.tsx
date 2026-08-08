/**
 * src/pages/SessionGroupPage.tsx — 会话分组详情页（v2 嵌入模式）
 *
 * 路由：/groups/:groupId
 * 从 ChatLayout (Shell) 拿到 Sidebar，本页只渲染右侧内容区（Outlet）。
 *
 * 布局：
 *   - Banner：← 返回列表 + 🗂 分组图标 + 分组名 + 会话数徽标
 *   - 内容区：居中列（宽度 = 右侧界面宽度 * 90%），逐条会话卡片
 *       icon（⭐/💬） + 标题 + 内容前 100 字（preview）
 *   - 点击卡片 → switchSession + 跳回聊天页
 *   - 空 / 加载 / 加载更多
 */
import { useEffect, useCallback } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useBackend } from "../providers/BackendProvider";
import { useSessionStore } from "../store/sessionStore";
import { useGroupViewStore } from "../store/groupViewStore";
import type { SessionInfo } from "../types/api";

const PAGE_SIZE = 20;

/** 图标渐变色散列（与 SkillsPage 一致的视觉语言） */
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

function truncate(text: string, max = 100): string {
  if (!text) return "";
  return text.length <= max ? text : text.slice(0, max) + "…";
}

export function SessionGroupPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { groupId } = useParams<{ groupId: string }>();

  const { requestGroupSessions, switchSession } = useBackend();
  const groups = useSessionStore((s) => s.groups);
  const currentSessionId = useSessionStore((s) => s.currentSessionId);

  const sessions = useGroupViewStore((s) => s.sessions);
  const loading = useGroupViewStore((s) => s.loading);
  const hasMore = useGroupViewStore((s) => s.hasMore);
  const page = useGroupViewStore((s) => s.page);
  const viewGroupId = useGroupViewStore((s) => s.groupId);

  const group = groups.find((g) => g.id === groupId);
  const groupName = group?.name ?? t("sessionGroup.fallbackTitle");

  useEffect(() => {
    if (groupId) requestGroupSessions(groupId, 1, PAGE_SIZE);
  }, [groupId, requestGroupSessions]);

  // 分组被删除（从 groups 消失）→ 该详情页失去归属，退回聊天页。
  // 用 groups.length>0 作守卫，避免启动瞬间 groups 尚未到达时误跳。
  useEffect(() => {
    if (groupId && groups.length > 0 && !groups.some((g) => g.id === groupId)) {
      navigate("/");
    }
  }, [groupId, groups, navigate]);

  const onLoadMore = useCallback(() => {
    if (groupId) requestGroupSessions(groupId, page + 1, PAGE_SIZE);
  }, [groupId, page, requestGroupSessions]);

  const onOpenSession = useCallback(
    (s: SessionInfo) => {
      switchSession(s.session_id);
      navigate("/");
    },
    [switchSession, navigate],
  );

  // 首屏加载（尚无数据且 store 仍指向别的分组）
  const initialLoading = loading && (viewGroupId !== groupId || sessions.length === 0);

  return (
    <div className="page-root">
      {/* Banner */}
      <div className="page-header">
        <button onClick={() => navigate("/")} className="btn btn-ghost btn-sm">← {t("sessionGroup.back")}</button>
        <span className="skill-card-icon icon-violet" style={{ width: 34, height: 34, fontSize: "var(--text-lg)" }}>🗂</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="page-title" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {groupName}
          </div>
          <div style={{ fontSize: "var(--text-xs)", color: "var(--text-tertiary)", marginTop: 2 }}>
            {t("sessionGroup.subtitle")}
          </div>
        </div>
        <span className="badge badge-purple">{t("sessionGroup.sessionCount", { count: sessions.length })}</span>
      </div>

      {/* 内容区 */}
      <div style={{ flex: 1, overflowY: "auto", padding: "24px 0 32px" }}>
        {/* 居中容器：自适应宽度（最大 1400px），卡片按容器宽度自动换行 */}
        <div style={{ width: "100%", maxWidth: 1400, margin: "0 auto", padding: "0 28px" }}>
          {initialLoading ? (
            <div className="skills-loading" style={{ padding: "48px 0" }}>
              <span className="skills-loading-dot" /> {t("sessionGroup.loading")}
            </div>
          ) : sessions.length === 0 ? (
            <div className="skills-empty" style={{ padding: "56px 0" }}>
              <div className="skills-empty-icon">📭</div>
              <div className="skills-empty-title">{t("sessionGroup.emptyTitle")}</div>
              <div className="skills-empty-desc">{t("sessionGroup.emptyDesc")}</div>
            </div>
          ) : (
            <>
              {/* 自适应网格：每列最小 300px，随宽度自动决定一行显示几个 */}
              <div style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))",
                gap: 12,
                alignItems: "start",
              }}>
                {sessions.map((s) => (
                  <div
                    key={s.session_id}
                    className="skill-card"
                    onClick={() => onOpenSession(s)}
                    style={s.session_id === currentSessionId ? { background: "var(--bg-elevated)" } : undefined}
                  >
                    <span className={`skill-card-icon ${iconClassFor(s.title || s.session_id)}`}>
                      {s.is_favorite ? "⭐" : "💬"}
                    </span>
                    <div className="skill-card-body">
                      <div className="skill-card-name">{s.title || t("sessionGroup.newSession")}</div>
                      <div
                        className="skill-card-desc"
                        style={{ whiteSpace: "normal", display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical" }}
                      >
                        {truncate(s.preview) || t("sessionGroup.noContent")}
                      </div>
                    </div>
                    <span className="skill-card-check">›</span>
                  </div>
                ))}
              </div>
              {hasMore && (
                <div style={{ display: "flex", justifyContent: "center", marginTop: 16 }}>
                  <button
                    onClick={onLoadMore}
                    disabled={loading}
                    className="btn btn-ghost btn-sm"
                    style={{ cursor: loading ? "wait" : "pointer" }}
                  >
                    {loading ? t("common.loading") : t("sessionGroup.loadMore")}
                  </button>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
