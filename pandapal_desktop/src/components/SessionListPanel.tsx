/**
 * src/components/SessionListPanel.tsx
 *
 * 会话列表面板（v003 完整实现）。
 *
 * 结构：
 *   - Header：标题 + 「+」按钮
 *   - Filter：分组下拉（"全部" / "无分组" / 各分组）
 *   - Groups：分组区（内联新建 + 每个分组的更多操作）
 *   - Sessions：会话条目列表（无限滚动）
 *   - Empty：空状态引导
 *
 * 交互：
 *   - 点击「+」→ createSession()（后端建 → SESSION_SWITCHED 会自动切）
 *   - 点击会话 → switchSession(id)（前端乐观切，后端广播 SESSION_SWITCHED）
 *   - 右键会话 → 重命名 / 移动到分组 / 删除
 *   - 滚动到底 → 自动加载更多（无限滚动）
 *   - 分组筛选下拉切换 → setGroupFilter + requestSessionList
 *   - Streaming 徽标：useIsStreamingIn(sid)
 *   - HITL 徽标：useHasPending(sid)
 */
import React, { useState, useCallback, useEffect, useRef } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";
import i18n from "../i18n";
import { useSessionStore } from "../store/sessionStore";
import { useBackend } from "../providers/BackendProvider";
import type { SessionInfo, SessionGroupInfo } from "../types/api";

const PAGE_SIZE = 10;

/** 分组数量上限（与后端 DEFAULT_MAX_GROUPS 保持一致）。 */
const MAX_GROUPS = 10;

export function SessionListPanel() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const sessions = useSessionStore((s) => s.sessions);
  const groups = useSessionStore((s) => s.groups);
  const currentSessionId = useSessionStore((s) => s.currentSessionId);
  const currentGroupFilter = useSessionStore((s) => s.currentGroupFilter);
  const hasMore = useSessionStore((s) => s.hasMore);
  const loading = useSessionStore((s) => s.loading);
  const page = useSessionStore((s) => s.page);
  const setGroupFilter = useSessionStore((s) => s.setGroupFilter);

  const {
    requestSessionList,
    createSession,
    switchSession,
    deleteSession,
    renameSession,
    groupMutate,
  } = useBackend();

  // ── 右键菜单状态（统一管理，避免多实例冲突）──
  const [ctxMenu, setCtxMenu] = useState<{
    sessionId: string;
    title: string;
    currentGroupId: string | null;
    x: number;
    y: number;
  } | null>(null);

  // ── 会话重命名弹窗 ──
  const [renameTarget, setRenameTarget] = useState<{ sessionId: string; title: string } | null>(null);

  useEffect(() => {
    if (!ctxMenu) return;
    const close = () => setCtxMenu(null);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("click", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("click", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [ctxMenu]);

  const onFilterChange = useCallback(
    (filter: "all" | null | string) => {
      setGroupFilter(filter);
      requestSessionList(filter === "all" ? "all" : filter, 1, PAGE_SIZE);
    },
    [setGroupFilter, requestSessionList],
  );

  const scrollRef = useRef<HTMLDivElement>(null);

  const onLoadMore = useCallback(() => {
    if (loading || !hasMore) return;
    requestSessionList(
      currentGroupFilter === "all" ? "all" : currentGroupFilter,
      page + 1,
      PAGE_SIZE,
    );
  }, [loading, hasMore, currentGroupFilter, page, requestSessionList]);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 80) {
      onLoadMore();
    }
  }, [onLoadMore]);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", position: "relative" }}>
      <div ref={scrollRef} onScroll={onScroll} style={{ flex: 1, overflowY: "auto" }}>
        {sessions.length === 0 ? (
          <SessionListEmpty />
        ) : (
          sessions.map((s) => (
            <SessionItem
              key={s.session_id}
              session={s}
              selected={s.session_id === currentSessionId}
              onClick={() => { switchSession(s.session_id); navigate("/"); }}
              onContextMenu={(x, y) =>
                setCtxMenu({
                  sessionId: s.session_id,
                  title: s.title,
                  currentGroupId: s.group_id ?? null,
                  x,
                  y,
                })
              }
            />
          ))
        )}
        {loading && sessions.length > 0 && (
          <div style={{ padding: "9px 16px", textAlign: "center", color: "var(--text-muted)", fontSize: "var(--text-base)" }}>
            {t("sessions.loading")}
          </div>
        )}
        {!loading && hasMore && sessions.length > 0 && (
          <LoadMoreButton loading={loading} onClick={onLoadMore} />
        )}
      </div>

      {/* ── 右键菜单（全局单例）── */}
      {ctxMenu && (
        <div
          style={{
            position: "fixed",
            left: ctxMenu.x,
            top: ctxMenu.y,
            minWidth: 170,
            padding: 4,
            background: "var(--bg-elevated)",
            border: "1px solid var(--border-strong)",
            borderRadius: "var(--radius-md)",
            boxShadow: "var(--shadow-lg)",
            zIndex: 200,
            animation: "fade-in 0.12s var(--ease-out)",
          }}
          onClick={(e) => e.stopPropagation()}
        >
          <div
            className="dropdown-item"
            onClick={() => {
              setRenameTarget({ sessionId: ctxMenu.sessionId, title: ctxMenu.title });
              setCtxMenu(null);
            }}
          >
            ✏️ {t("sessions.rename")}
          </div>
          <div className="dropdown-divider" />
          <div style={{
            padding: "4px 10px",
            fontSize: "var(--text-2xs)",
            fontWeight: 600,
            color: "var(--text-muted)",
            textTransform: "uppercase",
            letterSpacing: "0.05em",
          }}>
            {t("sessions.moveToGroup")}
          </div>
          <div className="dropdown-divider" />
          {groups.map((g) => (
            <div
              key={g.id}
              className={`dropdown-item${g.id === ctxMenu.currentGroupId ? " selected" : ""}`}
              onClick={() => {
                groupMutate({ op: "assign", session_id: ctxMenu.sessionId, group_id: g.id });
                setCtxMenu(null);
              }}
            >
              {g.id === ctxMenu.currentGroupId ? "✓ " : ""}📁 {g.name}
            </div>
          ))}
          {ctxMenu.currentGroupId && (
            <div
              className="dropdown-item"
              style={{ color: "var(--text-tertiary)" }}
              onClick={() => {
                groupMutate({ op: "assign", session_id: ctxMenu.sessionId, group_id: null });
                setCtxMenu(null);
              }}
            >
              {t("sessions.removeFromGroup")}
            </div>
          )}
          {groups.length === 0 && (
            <div style={{
              padding: "6px 10px",
              fontSize: "var(--text-sm)",
              color: "var(--text-muted)",
            }}>
              {t("sessions.noGroupsHint")}
            </div>
          )}
          <div className="dropdown-divider" />
          <div
            className="dropdown-item"
            style={{ color: "var(--danger)" }}
            onClick={() => {
              if (window.confirm(t("sessions.deleteSessionConfirm", { title: ctxMenu.title || ctxMenu.sessionId }))) {
                deleteSession(ctxMenu.sessionId);
              }
              setCtxMenu(null);
            }}
          >
            🗑 {t("sessions.deleteSession")}
          </div>
        </div>
      )}

      {/* ── 会话重命名弹窗 ── */}
      {renameTarget && (
        <NameModal
          title={t("sessions.renameSession")}
          confirmLabel={t("sessions.save")}
          initialValue={renameTarget.title}
          maxLength={40}
          placeholder={t("sessions.sessionNamePlaceholder")}
          onClose={() => setRenameTarget(null)}
          onConfirm={(name) => {
            if (name !== renameTarget.title) {
              renameSession(renameTarget.sessionId, name);
            }
            setRenameTarget(null);
          }}
        />
      )}
    </div>
  );
}

// ── Header ────────────────────────────────────────────────

function SessionListHeader({ onCreate }: { onCreate: () => void }) {
  const { t } = useTranslation();
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        padding: "8px 14px",
        borderBottom: "1px solid var(--border-subtle)",
      }}
    >
      <span style={{ fontSize: "var(--text-sm)", fontWeight: 600, color: "var(--text-primary)" }}>
        {t("sessions.title")}
      </span>
      <button
        title={t("sessions.new")}
        onClick={onCreate}
        style={{
          width: 24,
          height: 24,
          borderRadius: 6,
          border: "1px solid var(--border-default)",
          background: "var(--bg-root)",
          color: "var(--text-primary)",
          cursor: "pointer",
          fontSize: "var(--text-md)",
          lineHeight: 1,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        +
      </button>
    </div>
  );
}

// ── Filter ─────────────────────────────────────────────────

function SessionListFilter({
  groups,
  value,
  onChange,
}: {
  groups: SessionGroupInfo[];
  value: "all" | null | string;
  onChange: (v: "all" | null | string) => void;
}) {
  const { t } = useTranslation();
  const selectValue: string = value === "all" ? "__all__" : value === null ? "__none__" : value;
  return (
    <div style={{ padding: "6px 14px", borderBottom: "1px solid var(--border-subtle)" }}>
      <select
        value={selectValue}
        onChange={(e) => {
          const v = e.target.value;
          if (v === "__all__") onChange("all");
          else if (v === "__none__") onChange(null);
          else onChange(v);
        }}
        style={{
          width: "100%",
          padding: "4px 8px",
          borderRadius: "var(--radius-sm)",
          border: "1px solid var(--border-default)",
          background: "var(--bg-root)",
          color: "var(--text-primary)",
          fontSize: "var(--text-xs)",
          outline: "none",
          boxSizing: "border-box",
        }}
      >
        <option value="__all__">{t("sessions.filterAll")}</option>
        <option value="__none__">{t("sessions.filterNone")}</option>
        {groups.map((g) => (
          <option key={g.id} value={g.id}>
            📁 {g.name}
          </option>
        ))}
      </select>
    </div>
  );
}

// ── Group Section ─────────────────────────────────────────

/** 会话分组管理组件（提取供 LeftSidebar 独立使用）
 *
 *  交互：
 *   - 新建分组 → 弹窗输入名称（取消 / 确定）
 *   - 左键点击分组 → 打开分组详情页 /groups/:id（右侧内容区）
 *   - 右键分组 → 上下文菜单（重命名 / 删除）
 *   - 重命名 → 复用弹窗（预填当前名）
 *   - 删除 → 二次确认（组内会话保留）
 */
export function SessionGroupSection({
  groups,
  onCreate,
  onRename,
  onDelete,
}: {
  groups: SessionGroupInfo[];
  onCreate: (name: string) => void;
  onRename: (id: string, newName: string) => void;
  /** deleteSessions=true 时连同组内会话一并删除；false 仅删分组保留会话 */
  onDelete: (id: string, deleteSessions: boolean) => void;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();

  // 弹窗：新建 / 重命名
  const [modal, setModal] = useState<
    | { mode: "create" }
    | { mode: "rename"; id: string; current: string }
    | null
  >(null);

  // 删除确认弹窗（让用户选择是否连带会话）
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string } | null>(null);

  // 达到分组上限时的提示（点击「新建分组」触发，短暂显示）
  const [limitHint, setLimitHint] = useState(false);
  useEffect(() => {
    if (!limitHint) return;
    const t = setTimeout(() => setLimitHint(false), 4000);
    return () => clearTimeout(t);
  }, [limitHint]);

  // 右键菜单（全局单例）
  const [ctxMenu, setCtxMenu] = useState<{
    id: string;
    name: string;
    x: number;
    y: number;
  } | null>(null);

  useEffect(() => {
    if (!ctxMenu) return;
    const close = () => setCtxMenu(null);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("click", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("click", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [ctxMenu]);

  const activeGroupId = location.pathname.startsWith("/groups/")
    ? decodeURIComponent(location.pathname.slice("/groups/".length))
    : null;

  return (
    <div style={{ fontSize: "var(--text-sm)", padding: "var(--space-1) 0", position: "relative" }}>
      {groups.map((g) => (
        <GroupItem
          key={g.id}
          group={g}
          active={g.id === activeGroupId}
          onClick={() => navigate(`/groups/${encodeURIComponent(g.id)}`)}
          onContextMenu={(x, y) => setCtxMenu({ id: g.id, name: g.name, x, y })}
        />
      ))}

      <div
        onClick={() => {
          if (groups.length >= MAX_GROUPS) {
            setLimitHint(true);
          } else {
            setModal({ mode: "create" });
          }
        }}
        className="sidebar-chat-item muted"
      >
        <span className="chat-icon">+</span>
        <span className="chat-title">{t("sessions.newGroup")}</span>
      </div>

      {limitHint && (
        <div
          style={{
            margin: "2px var(--space-4) 0",
            padding: "6px 10px",
            fontSize: "var(--text-xs)",
            lineHeight: 1.5,
            color: "var(--danger)",
            background: "color-mix(in srgb, var(--danger) 8%, transparent)",
            border: "1px solid rgba(239,68,68,0.25)",
            borderRadius: "var(--radius-sm)",
          }}
        >
          {t("sessions.maxGroups", { count: MAX_GROUPS })}
        </div>
      )}

      {/* ── 右键菜单 ── */}
      {ctxMenu && (
        <div
          style={{
            position: "fixed",
            left: ctxMenu.x,
            top: ctxMenu.y,
            minWidth: 150,
            padding: 4,
            background: "var(--bg-elevated)",
            border: "1px solid var(--border-strong)",
            borderRadius: "var(--radius-md)",
            boxShadow: "var(--shadow-lg)",
            zIndex: 200,
            animation: "fade-in 0.12s var(--ease-out)",
          }}
          onClick={(e) => e.stopPropagation()}
        >
          <div
            className="dropdown-item"
            onClick={() => {
              setModal({ mode: "rename", id: ctxMenu.id, current: ctxMenu.name });
              setCtxMenu(null);
            }}
          >
            ✏️ {t("sessions.rename")}
          </div>
          <div className="dropdown-divider" />
          <div
            className="dropdown-item"
            style={{ color: "var(--danger)" }}
            onClick={() => {
              setDeleteTarget({ id: ctxMenu.id, name: ctxMenu.name });
              setCtxMenu(null);
            }}
          >
            🗑 {t("sessions.delete")}
          </div>
        </div>
      )}

      {/* ── 新建 / 重命名弹窗 ── */}
      {modal && (
        <NameModal
          title={modal.mode === "create" ? t("sessions.createGroup") : t("sessions.renameGroup")}
          confirmLabel={modal.mode === "create" ? t("sessions.create") : t("sessions.save")}
          initialValue={modal.mode === "rename" ? modal.current : ""}
          maxLength={20}
          placeholder={t("sessions.groupNamePlaceholder")}
          onClose={() => setModal(null)}
          onConfirm={(name) => {
            if (modal.mode === "create") onCreate(name);
            else if (name !== modal.current) onRename(modal.id, name);
            setModal(null);
          }}
        />
      )}

      {/* ── 删除分组确认弹窗（选择是否连带会话）── */}
      {deleteTarget && (
        <GroupDeleteModal
          name={deleteTarget.name}
          onClose={() => setDeleteTarget(null)}
          onConfirm={(deleteSessions) => {
            onDelete(deleteTarget.id, deleteSessions);
            setDeleteTarget(null);
          }}
        />
      )}
    </div>
  );
}

function GroupItem({
  group,
  active,
  onClick,
  onContextMenu,
}: {
  group: SessionGroupInfo;
  active: boolean;
  onClick: () => void;
  onContextMenu: (x: number, y: number) => void;
}) {
  const { t } = useTranslation();
  return (
    <div
      className={`sidebar-chat-item${active ? " active" : ""}`}
      onClick={onClick}
      onContextMenu={(e) => {
        e.preventDefault();
        e.stopPropagation();
        onContextMenu(e.clientX, e.clientY);
      }}
      title={t("sessions.groupItemHint")}
    >
      <span className="chat-icon">🗂</span>
      <span className="chat-title">{group.name}</span>
    </div>
  );
}

// ── 名称弹窗（新建 / 重命名共用，分组与会话通用）──────────

function NameModal({
  title,
  confirmLabel,
  initialValue,
  maxLength = 20,
  placeholder,
  onConfirm,
  onClose,
}: {
  title: string;
  confirmLabel: string;
  initialValue: string;
  maxLength?: number;
  placeholder: string;
  onConfirm: (name: string) => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(initialValue);
  const trimmed = name.trim();
  const submit = () => {
    if (trimmed) onConfirm(trimmed);
  };
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal"
        style={{ width: 380 }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <span className="modal-title">{title}</span>
          <button type="button" className="modal-close" onClick={onClose}>✕</button>
        </div>
        <div className="modal-body">
          <input
            autoFocus
            value={name}
            maxLength={maxLength}
            placeholder={placeholder}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") submit();
              else if (e.key === "Escape") onClose();
            }}
            style={{
              width: "100%",
              padding: "9px 12px",
              fontSize: "var(--text-md)",
              border: "1px solid var(--border-default)",
              background: "var(--bg-elevated)",
              color: "var(--text-primary)",
              borderRadius: "var(--radius-sm)",
              outline: "none",
              fontFamily: "inherit",
              boxSizing: "border-box",
            }}
          />
        </div>
        <div className="modal-footer">
          <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
            {t("sessions.cancel")}
          </button>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={!trimmed}
            style={{ opacity: trimmed ? 1 : 0.5, cursor: trimmed ? "pointer" : "not-allowed" }}
            onClick={submit}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── 删除分组弹窗（选择处理方式）────────────────────────────

function GroupDeleteModal({
  name,
  onConfirm,
  onClose,
}: {
  name: string;
  onConfirm: (deleteSessions: boolean) => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  // false = 仅删分组保留会话；true = 连同会话一并删除
  const [deleteSessions, setDeleteSessions] = useState(false);

  const options: { value: boolean; title: string; desc: string }[] = [
    { value: false, title: t("sessions.deleteGroupOnly"), desc: t("sessions.deleteGroupOnlyDesc") },
    { value: true, title: t("sessions.deleteGroupWithSessions"), desc: t("sessions.deleteGroupWithSessionsDesc") },
  ];

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" style={{ width: 420 }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <span className="modal-title">{t("sessions.deleteGroupTitle", { name })}</span>
          <button type="button" className="modal-close" onClick={onClose}>✕</button>
        </div>
        <div className="modal-body" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {options.map((opt) => {
            const active = deleteSessions === opt.value;
            const danger = opt.value;
            return (
              <div
                key={String(opt.value)}
                onClick={() => setDeleteSessions(opt.value)}
                style={{
                  display: "flex",
                  gap: 10,
                  padding: "10px 12px",
                  borderRadius: "var(--radius-sm)",
                  cursor: "pointer",
                  border: `1px solid ${active ? (danger ? "color-mix(in srgb, var(--danger) 50%, transparent)" : "var(--accent)") : "var(--border-default)"}`,
                  background: active ? (danger ? "color-mix(in srgb, var(--danger) 8%, transparent)" : "var(--bg-selected)") : "transparent",
                  transition: "all var(--duration-fast)",
                }}
              >
                <span
                  style={{
                    width: 16, height: 16, borderRadius: "var(--radius-full)",
                    border: `2px solid ${active ? (danger ? "var(--danger)" : "var(--accent)") : "var(--border-strong)"}`,
                    display: "flex", alignItems: "center", justifyContent: "center",
                    flexShrink: 0, marginTop: 2,
                  }}
                >
                  {active && (
                    <span style={{
                      width: 8, height: 8, borderRadius: "var(--radius-full)",
                      background: danger ? "var(--danger)" : "var(--accent)",
                    }} />
                  )}
                </span>
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: "var(--text-base)", fontWeight: 600, color: danger ? "var(--danger)" : "var(--text-primary)" }}>
                    {opt.title}
                  </div>
                  <div style={{ fontSize: "var(--text-sm)", color: "var(--text-tertiary)", marginTop: 2, lineHeight: 1.5 }}>
                    {opt.desc}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
        <div className="modal-footer">
          <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
            {t("sessions.cancel")}
          </button>
          <button
            type="button"
            className={`btn btn-sm ${deleteSessions ? "btn-danger-solid" : "btn-danger"}`}
            onClick={() => onConfirm(deleteSessions)}
          >
            {deleteSessions ? t("sessions.permanentDelete") : t("sessions.deleteGroup")}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Session Item ──────────────────────────────────────────

function SessionItem({
  session,
  selected,
  onClick,
  onContextMenu,
}: {
  session: SessionInfo;
  selected: boolean;
  onClick: () => void;
  onContextMenu: (x: number, y: number) => void;
}) {
  const { t } = useTranslation();

  return (
    <div
      onClick={onClick}
      onContextMenu={(e) => {
        e.preventDefault();
        e.stopPropagation();
        onContextMenu(e.clientX, e.clientY);
      }}
      className={`sidebar-chat-item${selected ? " active" : ""}`}
      style={{ position: "relative" }}
    >
      <span className="chat-icon">🍀</span>
      <span
        className="chat-title"
        style={{ fontWeight: selected ? 600 : 400 }}
      >
        {session.title || t("sessions.newSessionFallback")}
      </span>
      {session.group_name && (
        <span className="session-group-tag">{session.group_name}</span>
      )}
    </div>
  );
}

// ── Empty ─────────────────────────────────────────────────

function SessionListEmpty() {
  const { t } = useTranslation();
  return (
    <div
      style={{
        padding: "28px 16px",
        textAlign: "center",
        fontSize: "var(--text-base)",
        color: "var(--text-muted)",
      }}
    >
      <div style={{ fontSize: "var(--text-3xl)", marginBottom: 10 }}>💬</div>
      <div>{t("sessions.emptyHint")}</div>
    </div>
  );
}

// ── 工具 ─────────────────────────────────────────────────

function LoadMoreButton({
  onClick,
  loading,
}: {
  onClick: () => void;
  loading: boolean;
}) {
  const { t } = useTranslation();
  return (
    <button
      onClick={onClick}
      disabled={loading}
      style={{
        width: "100%",
        padding: "9px 16px",
        border: "none",
        background: "transparent",
        color: "var(--text-secondary)",
        cursor: loading ? "wait" : "pointer",
        fontSize: "var(--text-base)",
      }}
    >
      {loading ? t("sessions.loading") : t("sessions.loadMore")}
    </button>
  );
}

function formatTime(iso: string): string {
  if (!iso) return "";
  try {
    const dt = new Date(iso);
    const now = Date.now();
    const diff = now - dt.getTime();
    if (diff < 60_000) return i18n.t("sessions.justNow");
    if (diff < 3_600_000) return i18n.t("sessions.minutesAgo", { count: Math.floor(diff / 60_000) });
    if (diff < 86_400_000) return i18n.t("sessions.hoursAgo", { count: Math.floor(diff / 3_600_000) });
    if (diff < 604_800_000) return i18n.t("sessions.daysAgo", { count: Math.floor(diff / 86_400_000) });
    return dt.toLocaleDateString();
  } catch {
    return "";
  }
}
