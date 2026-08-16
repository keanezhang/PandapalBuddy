/**
 * src/components/LeftSidebar.tsx — v2 重设计版
 *
 * 结构：
 *   ┌─────────────────────────────────┐
 *   │ 🐼 PandaPal            ● 在线   │
 *   ├─────────────────────────────────┤
 *   │ 新对话           ── createSession │
 *   │ 搜索              (预留)     │
 *   │ 任务安排            ── TaskSection  │  ← 替代独立"定时任务"section
 *   │ skills              ── /skills  │  ← 替代独立"技能"section
 *   ├─────────────────────────────────┤
 *   │ 项目                           │
 *   │   📁 ...                       │
 *   ├─────────────────────────────────┤
 *   │ 对话                           │
 *   │   SessionListPanel             │
 *   ├─────────────────────────────────┤
 *   │ ⚙ │ (Z) username     退出     │
 *   └─────────────────────────────────┘
 */
import { useState, useEffect, useRef } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { usePreferenceStore } from "../store/preferenceStore";
import type { AgentMode } from "../types/api";
import { useCommandPaletteStore } from "../store/commandPaletteStore";
import { useSessionStore } from "../store/sessionStore";
import { useBackend } from "../providers/BackendProvider";
import { useConnectionStore } from "../store/connectionStore";
import { useWorkspaceStore } from "../store/workspaceStore";
import { useAuthStore } from "../store/authStore";
import { useFileStore } from "../store/fileStore";
import { SettingsPanel } from "./SettingsPanel";
import { SessionListPanel, SessionGroupSection } from "./SessionListPanel";
import { FileExplorer } from "./FileExplorer";

// ── 工具函数 ────────────────────────────────────────────────────────

function formatTime(isoStr: string): string {
  try {
    const d = new Date(isoStr);
    return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  } catch { return ""; }
}

// ── SidebarHeader ───────────────────────────────────────────────────

function SidebarHeader() {
  const { t } = useTranslation();
  const status = useConnectionStore((s) => s.status);
  const connText: Record<string, string> = {
    waiting: t("leftsidebar.conn.waiting"),
    connecting: t("leftsidebar.conn.connecting"),
    connected: t("leftsidebar.conn.connected"),
    error: t("leftsidebar.conn.error"),
    closed: t("leftsidebar.conn.closed"),
  };
  const online = status === "connected";

  return (
    <div style={{
      display: "flex", alignItems: "center", gap: "var(--space-2)",
      padding: "var(--space-4) var(--space-4) var(--space-3)",
      flexShrink: 0,
    }}>
      <div style={{
        width: 32, height: 32, borderRadius: "var(--radius-md)",
        background: "var(--gradient-avatar)",
        display: "flex", alignItems: "center", justifyContent: "center",
        fontSize: "var(--text-lg)", flexShrink: 0,
      }}>
        🐼
      </div>
      <span style={{
        fontSize: "var(--text-lg)", fontWeight: 600, color: "var(--text-primary)",
        flex: 1, letterSpacing: "-0.01em",
      }}>
        PandaPal
      </span>
      <span className={`sidebar-conn${online ? "" : " offline"}`}>
        {connText[status] ?? status}
      </span>
    </div>
  );
}

// ── ModeSwitcher ─ 办公助手 ↔ 编码 分段切换（原在 TopToolbar，迁至此）──────
//
// 分段 pill 控件：点击左右切换 preferenceStore.mode，驱动 SidebarBody 的
// office/coding 布局。仅本地 UI 状态，无 IPC 副作用。

function ModeSwitcher() {
  const { t } = useTranslation();
  const mode = usePreferenceStore((s) => s.mode);
  const setMode = usePreferenceStore((s) => s.setMode);

  const segments: { value: AgentMode; icon: string; label: string; mono?: boolean }[] = [
    { value: "office", icon: "⌨️", label: t("leftsidebar.mode.office") },
    { value: "coding", icon: "</>", label: t("leftsidebar.mode.coding"), mono: true },
  ];

  return (
    <div style={{ padding: "0 var(--space-4) var(--space-3)" }}>
      <div style={{
        display: "flex", gap: 3, padding: 3,
        background: "var(--bg-panel)",
        border: "1px solid var(--border-default)",
        borderRadius: "var(--radius-md)",
      }}>
        {segments.map((seg) => {
          const active = mode === seg.value;
          return (
            <button
              key={seg.value}
              type="button"
              onClick={() => setMode(seg.value)}
              style={{
                flex: 1, display: "flex", alignItems: "center", justifyContent: "center",
                gap: 6, padding: "6px 8px", borderRadius: "var(--radius-sm)",
                border: active ? "1px solid var(--border-default)" : "1px solid transparent",
                background: active ? "var(--bg-elevated)" : "transparent",
                color: active ? "var(--text-primary)" : "var(--text-tertiary)",
                boxShadow: active ? "0 1px 2px rgba(0,0,0,0.25)" : "none",
                fontSize: "var(--text-sm)", fontWeight: active ? 600 : 500, cursor: "pointer",
                fontFamily: "inherit", whiteSpace: "nowrap", overflow: "hidden",
                transition: "background var(--duration-fast), color var(--duration-fast)",
              }}
              onMouseEnter={(e) => { if (!active) { e.currentTarget.style.background = "var(--bg-hover)"; e.currentTarget.style.color = "var(--text-primary)"; } }}
              onMouseLeave={(e) => { if (!active) { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = "var(--text-tertiary)"; } }}
            >
              <span style={{
                fontSize: seg.mono ? 11 : 13, flexShrink: 0,
                fontFamily: seg.mono ? "'SF Mono','Cascadia Code',monospace" : "inherit",
              }}>
                {seg.icon}
              </span>
              <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{seg.label}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ── SectionHeader ────────────────────────────────────────────────────

function SectionHeader({ theme, label, children, onAdd, addLabel = "+", addTitle }: {
  theme: "gold" | "purple"; label: string;
  children?: React.ReactNode; onAdd?: () => void;
  addLabel?: string; addTitle?: string;
}) {
  const { t } = useTranslation();
  return (
    <>
      <div className={`sidebar-section-header${theme === "purple" ? " purple" : ""}`}>
        {label}
        {onAdd && (
          <button
            type="button"
            className="section-add"
            title={addTitle ?? t("leftsidebar.new")}
            onClick={(e) => { e.stopPropagation(); onAdd(); }}
          >
            {addLabel}
          </button>
        )}
      </div>
      {children}
    </>
  );
}

// ── ProjectSection ─ 当前工作目录（替代顶部工具栏中的工作区指示）──────

function workspaceName(path: string | null): string {
  if (!path) return "";
  const parts = path.split(/[/\\]/).filter(Boolean);
  return parts.length > 0 ? parts[parts.length - 1] : path;
}

function ProjectSection() {
  const { t } = useTranslation();
  const current = useWorkspaceStore((s) => s.current);
  const pickAndOpen = useWorkspaceStore((s) => s.pickAndOpen);
  const name = current ? workspaceName(current) : t("leftsidebar.workspaceNotOpened");

  return (
    <div
      className="sidebar-chat-item"
      onClick={() => void pickAndOpen()}
      title={current
        ? t("leftsidebar.workspaceTitle", { path: current })
        : t("leftsidebar.workspaceOpenTitle")}
    >
      <span className="chat-icon">📂</span>
      <span className="chat-title">{name}</span>
    </div>
  );
}

// ── SessionGroupsWrapper ─ 会话分组区域 ──────────────────────────

function SessionGroupsWrapper() {
  const { t } = useTranslation();
  const groups = useSessionStore((s) => s.groups);
  const { groupMutate } = useBackend();

  return (
    <div>
      <SectionHeader theme="purple" label={t("leftsidebar.sectionGroups")} />
      <SessionGroupSection
        groups={groups}
        onCreate={(name) => groupMutate({ op: "create", name })}
        onRename={(gid, newName) => groupMutate({ op: "rename", group_id: gid, new_name: newName })}
        onDelete={(gid, deleteSessions) => groupMutate({ op: "delete", group_id: gid, delete_sessions: deleteSessions })}
      />
    </div>
  );
}

// ── SidebarDock ─────────────────────────────────────────────────────

function SidebarDock() {
  const { t } = useTranslation();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const username = useAuthStore((s) => s.username);
  const logout = useAuthStore((s) => s.logout);
  const avatarLetter = (username ?? "?")[0].toUpperCase();

  return (
    <>
      <div style={{
        display: "flex", alignItems: "center", gap: "var(--space-3)",
        padding: "var(--space-3) var(--space-4)",
        borderTop: "1px solid var(--border-subtle)",
        flexShrink: 0,
      }}>
        <button
          type="button" onClick={() => setSettingsOpen(true)} title={t("common.settings")}
          style={{
            width: 30, height: 30, borderRadius: "var(--radius-md)",
            border: "1px solid var(--border-default)", background: "transparent",
            color: "var(--text-tertiary)", fontSize: "var(--text-lg)", cursor: "pointer",
            display: "flex", alignItems: "center", justifyContent: "center",
            transition: "background var(--duration-fast), color var(--duration-fast)",
          }}
          onMouseEnter={(e) => { e.currentTarget.style.background = "var(--bg-hover)"; e.currentTarget.style.color = "var(--text-primary)"; }}
          onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = "var(--text-tertiary)"; }}
        >
          ⚙
        </button>

        <div style={{
          width: 30, height: 30, borderRadius: "var(--radius-full)",
          background: "var(--gradient-avatar)",
          display: "flex", alignItems: "center", justifyContent: "center",
          fontSize: "var(--text-sm)", fontWeight: 700, color: "var(--text-on-accent)", flexShrink: 0,
        }}>
          {avatarLetter}
        </div>

        <span style={{
          fontSize: "var(--text-md)", fontWeight: 500, color: "var(--text-primary)",
          flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
        }}>
          {username ?? "—"}
        </span>

        <button
          onClick={() => logout()}
          style={{
            fontSize: "var(--text-xs)", padding: "4px 12px", borderRadius: "var(--radius-full)",
            border: "1px solid var(--border-default)", background: "transparent",
            color: "var(--text-tertiary)", cursor: "pointer", whiteSpace: "nowrap",
            transition: "border-color var(--duration-fast), color var(--duration-fast)",
            fontFamily: "inherit",
          }}
          onMouseEnter={(e) => { e.currentTarget.style.borderColor = "var(--accent-2)"; e.currentTarget.style.color = "var(--accent-2)"; }}
          onMouseLeave={(e) => { e.currentTarget.style.borderColor = "var(--border-default)"; e.currentTarget.style.color = "var(--text-tertiary)"; }}
        >
          {t("common.logout")}
        </button>
      </div>
      {settingsOpen && <SettingsPanel onClose={() => setSettingsOpen(false)} />}
    </>
  );
}


// ── SidebarBody ─ 模式驱动的中部区域 ─────────────────────────────────
//
// 「一套外壳 + 模式驱动分区」：Header / 拖拽 / Dock 等外壳在两种模式下共享，
// 仅中部随 preferenceStore.mode 切换内容。模式开关复用 TopToolbar 已有的
// 「办公助手 / 编码」切换,不再单独造一个,避免两处状态争抢。
//
//   office（办公）：工作目录 + 会话分组 + 对话列表(主滚动区)
//   coding（编码）：工作目录 + 文件树(主滚动区) + 对话列表(次要,固定矮区)
//
// 各分区都是独立组件,加模式 = 改这一处组合,不动外壳。

function SidebarBody({ mode }: { mode: AgentMode }) {
  const { t } = useTranslation();
  const loadFileTree = useFileStore((s) => s.loadFileTree);
  const workspace = useWorkspaceStore((s) => s.current);

  if (mode === "coding") {
    return (
      <>
        <SectionHeader theme="purple" label={t("leftsidebar.sectionWorkspace")}>
          <ProjectSection />
        </SectionHeader>

        <SectionHeader
          theme="purple"
          label={t("leftsidebar.sectionFiles")}
          onAdd={workspace ? () => void loadFileTree(workspace) : undefined}
          addLabel="⟳"
          addTitle={t("leftsidebar.refreshFileTree")}
        />
        <div style={{ flex: 1, overflowY: "auto", minHeight: 0 }}>
          <FileExplorer />
        </div>

        {/* 编码模式下对话列表退居次要：固定矮区,不抢文件树的空间 */}
        <SectionHeader theme="purple" label={t("leftsidebar.sectionSessions")} />
        <div style={{
          maxHeight: 180, overflowY: "auto", flexShrink: 0,
          borderTop: "1px solid var(--border-subtle)",
        }}>
          <SessionListPanel />
        </div>
      </>
    );
  }

  // office（默认）
  return (
    <>
      <SectionHeader theme="purple" label={t("leftsidebar.sectionWorkspace")}>
        <ProjectSection />
      </SectionHeader>

      <SessionGroupsWrapper />

      <SectionHeader theme="purple" label={t("leftsidebar.sectionSessions")} />
      <div style={{ flex: 1, overflowY: "auto", minHeight: 0 }}>
        <SessionListPanel />
      </div>
    </>
  );
}

// ── LeftSidebar ──────────────────────────────────────────────────────

export function LeftSidebar() {
  const { t } = useTranslation();
  const sidebarCollapsed = usePreferenceStore((s) => s.sidebarCollapsed);
  const sidebarWidth = usePreferenceStore((s) => s.sidebarWidth);
  const setSidebarWidth = usePreferenceStore((s) => s.setSidebarWidth);
  const mode = usePreferenceStore((s) => s.mode);
  const { createSession } = useBackend();
  const width = sidebarCollapsed ? 48 : sidebarWidth;

  // ── 侧边栏宽度拖拽 ──────────────────────────────────────────────
  const [dragging, setDragging] = useState(false);
  const dragStartX = useRef(0);
  const dragStartW = useRef(0);

  useEffect(() => {
    if (!dragging) return;
    const onMove = (e: MouseEvent) => {
      const delta = e.clientX - dragStartX.current;
      setSidebarWidth(dragStartW.current + delta);
    };
    const onUp = () => setDragging(false);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [dragging, setSidebarWidth]);

  if (sidebarCollapsed) {
    return (
      <div style={{
        width: 48, minWidth: 48, maxWidth: 48,
        background: "var(--bg-elevated)",
        borderRight: "1px solid var(--border-subtle)",
        display: "flex", flexDirection: "column",
        alignItems: "center", padding: "var(--space-3) 0",
        flexShrink: 0, height: "100%",
      }}>
        <div style={{
          width: 32, height: 32, borderRadius: "var(--radius-md)",
          background: "var(--gradient-avatar)",
          display: "flex", alignItems: "center", justifyContent: "center", fontSize: "var(--text-lg)",
        }}>🐼</div>
        <div style={{ flex: 1 }} />
        <button
          onClick={usePreferenceStore.getState().toggleSidebar}
          style={{
            width: 28, height: 28, borderRadius: "var(--radius-sm)",
            border: "1px solid var(--border-default)", background: "transparent",
            color: "var(--text-tertiary)", fontSize: "var(--text-md)", cursor: "pointer",
            display: "flex", alignItems: "center", justifyContent: "center",
          }}
          title={t("leftsidebar.expand")}
        >▶</button>
      </div>
    );
  }

  return (
    <div style={{
      width, minWidth: 180, maxWidth: 400,
      background: "var(--shadow-md)" /* ui-lint-ok: 弹层阴影保留实色 */,
      borderRight: "1px solid var(--border-subtle)",
      display: "flex", flexDirection: "column",
      height: "100%", overflow: "hidden",
      userSelect: "none",
      position: "relative",
      flexShrink: 0,
    }}>
      {/* 右侧拖拽手柄 */}
      <div
        onMouseDown={(e) => {
          e.preventDefault();
          setDragging(true);
          dragStartX.current = e.clientX;
          dragStartW.current = sidebarWidth;
        }}
        style={{
          position: "absolute", right: -3, top: 0, bottom: 0,
          width: 6, cursor: "col-resize", zIndex: 20,
          background: dragging ? "var(--accent)" : "transparent",
          transition: "background var(--duration-fast)",
        }}
        onMouseEnter={(e) => { (e.currentTarget as HTMLDivElement).style.background = "var(--accent)"; }}
        onMouseLeave={(e) => { if (!dragging) (e.currentTarget as HTMLDivElement).style.background = "transparent"; }}
      />

      <SidebarHeader />

      {/* —— 模式切换（办公助手 ↔ 编码）—— */}
      <ModeSwitcher />

      {/* —— 4 主导航 —— */}
      <MainNav />

      {/* —— 模式驱动的中部区域（办公 ↔ 编码）—— */}
      <SidebarBody mode={mode} />

      <SidebarDock />
    </div>
  );
}

// ── MainNav ─ 4 主导航 ─────────────────────────────────────────────
//
// 焦点语义修正（避免与下方对话列表的「选中会话」争抢高亮）：
//   - 「新对话」「搜索」是【瞬时动作】——点了就执行，不常驻 active 高亮，
//     只有 :active 按下反馈。
//   - 「任务安排」「Skills」是【有持续状态的入口】——分别反映
//     showScheduled / 当前是否在 /skills 路由，才显示 active。
// 这样任一时刻侧边栏「紫色选中」只有一处（对话列表的当前会话是唯一真相）。

type NavId = "chat" | "search" | "scheduled" | "plugins" | "dashboard";

function MainNav() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const { createSession } = useBackend();
  const openPalette = useCommandPaletteStore((s) => s.openPalette);

  const onSkillsRoute = location.pathname.startsWith("/skills");
  const onTasksRoute = location.pathname.startsWith("/tasks");
  const onDashboardRoute = location.pathname.startsWith("/dashboard");

  const items: { id: NavId; icon: string; label: string; active: boolean; run: () => void }[] = [
    { id: "chat",      icon: "🍀", label: t("leftsidebar.nav.chat"),       active: false,             run: () => { createSession(); navigate("/"); } },
    { id: "search",    icon: "🔍", label: t("leftsidebar.nav.search"),     active: false,             run: () => openPalette() },
    { id: "dashboard", icon: "🚀", label: "dashboard",                     active: onDashboardRoute,   run: () => navigate("/dashboard") },
    { id: "scheduled", icon: "📋", label: t("leftsidebar.nav.tasks"),      active: onTasksRoute,       run: () => navigate("/tasks") },
    { id: "plugins",   icon: "📙", label: "Skills",                        active: onSkillsRoute,      run: () => navigate("/skills") },
  ];

  return (
    <nav style={{
      padding: "var(--space-2) var(--space-3)",
      display: "flex", flexDirection: "column", gap: 3,
    }}>
      {items.map((item) => (
        <button
          key={item.id}
          type="button"
          className={`sidebar-nav-item${item.active ? " active" : ""}`}
          onClick={item.run}
        >
          <span className="sidebar-nav-icon">{item.icon}</span>
          <span>{item.label}</span>
        </button>
      ))}
    </nav>
  );
}
