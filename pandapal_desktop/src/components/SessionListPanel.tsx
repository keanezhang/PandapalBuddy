/**
 * src/components/SessionListPanel.tsx
 *
 * 会话列表面板（v003 完整实现）。
 *
 * 结构：
 *   - Header：标题 + 「+」按钮
 *   - Filter：分组下拉（"全部" / "无分组" / 各分组）
 *   - Groups：分组区（内联新建 + 每个分组的更多操作）
 *   - Sessions：会话条目列表（title/preview/badge/删除）
 *   - LoadMore：分页加载更多
 *   - Empty：空状态引导
 *
 * 交互：
 *   - 点击「+」→ createSession()（后端建 → SESSION_SWITCHED 会自动切）
 *   - 点击会话 → switchSession(id)（前端乐观切，后端广播 SESSION_SWITCHED）
 *   - Hover 会话 → 显示删除按钮 → 点击 → 二次确认 → deleteSession(id)
 *   - 星标 → toggleFavoriteSession(id)
 *   - 分组筛选下拉切换 → setGroupFilter + requestSessionList
 *   - Streaming 徽标：useIsStreamingIn(sid)
 *   - HITL 徽标：useHasPending(sid)
 */
import React, { useState, useCallback, useEffect } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { useSessionStore } from "../store/sessionStore";
import { useBackend } from "../providers/BackendProvider";
import type { SessionInfo, SessionGroupInfo } from "../types/api";

const PAGE_SIZE = 10;

/** 分组数量上限（与后端 DEFAULT_MAX_GROUPS 保持一致）。 */
const MAX_GROUPS = 10;

export function SessionListPanel() {
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
    toggleFavoriteSession,
    groupMutate,
  } = useBackend();

  // ── 右键菜单状态（统一管理，避免多实例冲突）──
  const [ctxMenu, setCtxMenu] = useState<{
    sessionId: string;
    currentGroupId: string | null;
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

  const onFilterChange = useCallback(
    (filter: "all" | null | string) => {
      setGroupFilter(filter);
      requestSessionList(filter === "all" ? "all" : filter, 1, PAGE_SIZE);
    },
    [setGroupFilter, requestSessionList],
  );

  const onLoadMore = useCallback(() => {
    requestSessionList(
      currentGroupFilter === "all" ? "all" : currentGroupFilter,
      page + 1,
      PAGE_SIZE,
    );
  }, [currentGroupFilter, page, requestSessionList]);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", position: "relative" }}>
      <div style={{ flex: 1, overflowY: "auto" }}>
        {sessions.length === 0 ? (
          <SessionListEmpty />
        ) : (
          sessions.map((s) => (
            <SessionItem
              key={s.session_id}
              session={s}
              selected={s.session_id === currentSessionId}
              onClick={() => { switchSession(s.session_id); navigate("/"); }}
              onDelete={() => {
                if (window.confirm(`确定删除会话「${s.title || s.session_id}」？\n删除后数据不可恢复。`)) {
                  deleteSession(s.session_id);
                }
              }}
              onToggleFavorite={() => toggleFavoriteSession(s.session_id)}
              onContextMenu={(x, y) =>
                setCtxMenu({
                  sessionId: s.session_id,
                  currentGroupId: s.group_id ?? null,
                  x,
                  y,
                })
              }
            />
          ))
        )}
        {hasMore && sessions.length > 0 && (
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
          <div style={{
            padding: "4px 10px",
            fontSize: "var(--text-2xs)",
            fontWeight: 600,
            color: "var(--text-muted)",
            textTransform: "uppercase",
            letterSpacing: "0.05em",
          }}>
            移动到分组
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
              移出分组
            </div>
          )}
          {groups.length === 0 && (
            <div style={{
              padding: "6px 10px",
              fontSize: "var(--text-sm)",
              color: "var(--text-muted)",
            }}>
              暂无分组，请先在上方新建
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Header ────────────────────────────────────────────────

function SessionListHeader({ onCreate }: { onCreate: () => void }) {
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
        会话
      </span>
      <button
        title="新建会话"
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
        <option value="__all__">全部</option>
        <option value="__none__">无分组</option>
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
        <span className="chat-title">新建分组</span>
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
          最多创建 {MAX_GROUPS} 个分组，不支持更多了。请先删除部分分组。
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
            ✏️ 重命名
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
            🗑 删除
          </div>
        </div>
      )}

      {/* ── 新建 / 重命名弹窗 ── */}
      {modal && (
        <GroupNameModal
          title={modal.mode === "create" ? "新建分组" : "重命名分组"}
          confirmLabel={modal.mode === "create" ? "创建" : "保存"}
          initialValue={modal.mode === "rename" ? modal.current : ""}
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
  return (
    <div
      className={`sidebar-chat-item${active ? " active" : ""}`}
      onClick={onClick}
      onContextMenu={(e) => {
        e.preventDefault();
        e.stopPropagation();
        onContextMenu(e.clientX, e.clientY);
      }}
      title="左键打开分组 · 右键更多操作"
    >
      <span className="chat-icon">🗂</span>
      <span className="chat-title">{group.name}</span>
    </div>
  );
}

// ── 分组名弹窗（新建 / 重命名共用）────────────────────────

function GroupNameModal({
  title,
  confirmLabel,
  initialValue,
  onConfirm,
  onClose,
}: {
  title: string;
  confirmLabel: string;
  initialValue: string;
  onConfirm: (name: string) => void;
  onClose: () => void;
}) {
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
            maxLength={20}
            placeholder="请输入分组名称"
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
            取消
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
  // false = 仅删分组保留会话；true = 连同会话一并删除
  const [deleteSessions, setDeleteSessions] = useState(false);

  const options: { value: boolean; title: string; desc: string }[] = [
    { value: false, title: "仅删除分组", desc: "组内会话保留，变为「无分组」状态" },
    { value: true, title: "连同会话一起删除", desc: "组内所有会话将被一并删除，且不可恢复" },
  ];

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" style={{ width: 420 }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <span className="modal-title">删除分组「{name}」</span>
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
            取消
          </button>
          <button
            type="button"
            className={`btn btn-sm ${deleteSessions ? "btn-danger-solid" : "btn-danger"}`}
            onClick={() => onConfirm(deleteSessions)}
          >
            {deleteSessions ? "永久删除" : "删除分组"}
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
  onDelete,
  onToggleFavorite,
  onContextMenu,
}: {
  session: SessionInfo;
  selected: boolean;
  onClick: () => void;
  onDelete: () => void;
  onToggleFavorite: () => void;
  onContextMenu: (x: number, y: number) => void;
}) {
  const [hover, setHover] = useState(false);

  return (
    <div
      onClick={onClick}
      onContextMenu={(e) => {
        e.preventDefault();
        e.stopPropagation();
        onContextMenu(e.clientX, e.clientY);
      }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      className={`sidebar-chat-item${selected ? " active" : ""}`}
      style={{ position: "relative" }}
    >
      <span className="chat-icon">
        {session.is_favorite ? "⭐" : "🍀"}
      </span>
      <span
        className="chat-title"
        style={{ fontWeight: selected ? 600 : 400 }}
      >
        {session.title || "新会话"}
      </span>
      {session.group_name && (
        <span className="session-group-tag">{session.group_name}</span>
      )}
      {hover && (
        <div style={{ display: "flex", gap: 4, alignItems: "center" }} onClick={(e) => e.stopPropagation()}>
          <button
            title={session.is_favorite ? "取消收藏" : "收藏"}
            onClick={onToggleFavorite}
            style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", padding: 0, fontSize: "var(--text-sm)", fontFamily: "inherit" }}
          >
            {session.is_favorite ? "☆" : "⭐"}
          </button>
          <button
            title="删除"
            onClick={onDelete}
            style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", padding: 0, fontSize: "var(--text-sm)", fontFamily: "inherit" }}
          >
            ✕
          </button>
        </div>
      )}
    </div>
  );
}

// ── Empty / LoadMore ──────────────────────────────────────

function SessionListEmpty() {
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
      <div>点击 + 开始新对话</div>
    </div>
  );
}

function LoadMoreButton({
  onClick,
  loading,
}: {
  onClick: () => void;
  loading: boolean;
}) {
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
      {loading ? "加载中..." : "▼ 加载更多"}
    </button>
  );
}

// ── 工具 ─────────────────────────────────────────────────

function formatTime(iso: string): string {
  if (!iso) return "";
  try {
    const dt = new Date(iso);
    const now = Date.now();
    const diff = now - dt.getTime();
    if (diff < 60_000) return "刚刚";
    if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
    if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
    if (diff < 604_800_000) return `${Math.floor(diff / 86_400_000)} 天前`;
    return dt.toLocaleDateString();
  } catch {
    return "";
  }
}
