/**
 * src/components/CommandPalette.tsx — ⌘K 命令面板 / 全局搜索
 *
 * 交互形态：Spotlight 风格居中浮层（顶部输入框 + 下方分组结果）。
 *
 * 打开方式：
 *   - 主导航「🔍 搜索」按钮 → openPalette()
 *   - 全局快捷键 ⌘K / Ctrl+K（监听器常驻本组件）
 *
 * 检索范围与数据来源：
 *   - 会话标题 / 消息全文  → 后端 SEARCH（searchStore，覆盖全部历史，非仅已加载）
 *   - 定时任务 / Skills    → 客户端 store 即时过滤（已全量加载）
 *   - 知识库(RAG)          → 界面预留分组，标注「即将上线」，暂不检索
 *
 * 键盘：↑/↓ 切换选中，Enter 执行，Esc 关闭。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useCommandPaletteStore } from "../store/commandPaletteStore";
import { useSessionStore } from "../store/sessionStore";
import { useTaskSchedulerStore } from "../store/taskSchedulerStore";
import { useSkillStore } from "../store/skillStore";
import { useSearchStore } from "../store/searchStore";
import { useBackend } from "../providers/BackendProvider";

// ── 结果条目统一模型 ──────────────────────────────────────────────
type ResultKind = "session" | "message" | "task" | "skill";

interface ResultItem {
  kind: ResultKind;
  id: string;
  icon: string;
  title: string;
  subtitle?: string;
  run: () => void;
}

interface ResultGroup {
  kind: ResultKind;
  label: string;
  items: ResultItem[];
}

const MAX_CLIENT_PER_GROUP = 6;
const SEARCH_DEBOUNCE_MS = 180;

const ROLE_ICON: Record<string, string> = {
  user: "🧑", assistant: "🤖", system: "⚙", tool: "🔧",
};

function match(haystack: string, q: string): boolean {
  return haystack.toLowerCase().includes(q);
}

export function CommandPalette() {
  const open = useCommandPaletteStore((s) => s.open);
  const closePalette = useCommandPaletteStore((s) => s.closePalette);
  const toggle = useCommandPaletteStore((s) => s.toggle);

  const navigate = useNavigate();
  const { switchSession, searchRequest, requestScheduledTasks, requestSkillList } = useBackend();

  // 客户端数据源（任务 / 技能全量加载，会话仅作空查询时的「最近」建议）
  const sessions = useSessionStore((s) => s.sessions);
  const tasks = useTaskSchedulerStore((s) => s.tasks);
  const selectTask = useTaskSchedulerStore((s) => s.selectTask);
  const skills = useSkillStore((s) => s.skills);
  const selectSkill = useSkillStore((s) => s.selectSkill);

  // 后端搜索结果（会话标题 + 消息全文）
  const backendSessions = useSearchStore((s) => s.sessions);
  const backendMessages = useSearchStore((s) => s.messages);
  const searchLoading = useSearchStore((s) => s.loading);
  const clearSearch = useSearchStore((s) => s.clear);

  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  // ── 全局快捷键 ⌘K / Ctrl+K（常驻监听）──────────────────────────
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        toggle();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggle]);

  // ── 打开时：重置 + 拉取最新任务/技能 + 聚焦；关闭时清空后端结果 ──
  useEffect(() => {
    if (!open) {
      clearSearch();
      return;
    }
    setQuery("");
    setActiveIndex(0);
    requestScheduledTasks();
    requestSkillList();
    const t = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => window.clearTimeout(t);
  }, [open, requestScheduledTasks, requestSkillList, clearSearch]);

  // ── 查询词变化 → 防抖发起后端搜索（会话标题 + 消息全文）──────────
  useEffect(() => {
    if (!open) return;
    const q = query.trim();
    if (!q) {
      clearSearch();
      return;
    }
    const t = window.setTimeout(() => searchRequest(q), SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(t);
  }, [query, open, searchRequest, clearSearch]);

  // ── 构建分组结果 ──────────────────────────────────────────────
  const groups: ResultGroup[] = useMemo(() => {
    const q = query.trim().toLowerCase();
    const out: ResultGroup[] = [];

    // 1. 会话：空查询→客户端最近；非空→后端标题命中
    const sessionItems: ResultItem[] = q === ""
      ? sessions
          .filter((s) => !s.is_empty)
          .slice(0, MAX_CLIENT_PER_GROUP)
          .map((s) => ({
            kind: "session" as const,
            id: s.session_id,
            icon: s.is_favorite ? "⭐" : "💬",
            title: s.title || "新会话",
            subtitle: s.group_name ?? undefined,
            run: () => { switchSession(s.session_id); closePalette(); },
          }))
      : backendSessions.map((s) => ({
          kind: "session" as const,
          id: s.session_id,
          icon: s.is_favorite ? "⭐" : "💬",
          title: s.title || "新会话",
          subtitle: s.preview || undefined,
          run: () => { switchSession(s.session_id); closePalette(); },
        }));
    if (sessionItems.length) out.push({ kind: "session", label: "会话", items: sessionItems });

    // 2. 消息全文（仅非空查询，来自后端）
    if (q !== "" && backendMessages.length) {
      const messageItems: ResultItem[] = backendMessages.map((m, i) => ({
        kind: "message" as const,
        id: `${m.session_id}-${i}`,
        icon: ROLE_ICON[m.role] ?? "💬",
        title: m.snippet || "(空)",
        subtitle: m.title,
        run: () => { switchSession(m.session_id); closePalette(); },
      }));
      out.push({ kind: "message", label: "消息", items: messageItems });
    }

    // 3. 定时任务（客户端过滤）
    const taskItems: ResultItem[] = tasks
      .filter((t) => t.name && t.name.trim() && (q === "" || match(t.name, q)))
      .slice(0, MAX_CLIENT_PER_GROUP)
      .map((t) => ({
        kind: "task" as const,
        id: t.task_id,
        icon: "📋",
        title: t.name,
        subtitle: t.cron_expression || undefined,
        run: () => { selectTask(t.task_id); closePalette(); },
      }));
    if (taskItems.length) out.push({ kind: "task", label: "任务安排", items: taskItems });

    // 4. Skills（客户端过滤）
    const skillItems: ResultItem[] = skills
      .filter((s) => q === "" || match(s.name, q) || match(s.description ?? "", q))
      .slice(0, MAX_CLIENT_PER_GROUP)
      .map((s) => ({
        kind: "skill" as const,
        id: s.name,
        icon: "🧩",
        title: s.name,
        subtitle: s.description || undefined,
        run: () => {
          selectSkill(s.name);
          navigate(`/skills/${encodeURIComponent(s.name)}`);
          closePalette();
        },
      }));
    if (skillItems.length) out.push({ kind: "skill", label: "Skills", items: skillItems });

    return out;
  }, [
    query, sessions, backendSessions, backendMessages, tasks, skills,
    switchSession, selectTask, selectSkill, navigate, closePalette,
  ]);

  // 扁平化用于键盘导航
  const flat: ResultItem[] = useMemo(() => groups.flatMap((g) => g.items), [groups]);

  useEffect(() => {
    setActiveIndex((i) => (flat.length === 0 ? 0 : Math.min(i, flat.length - 1)));
  }, [flat.length]);

  if (!open) return null;

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      closePalette();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex((i) => (flat.length ? (i + 1) % flat.length : 0));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex((i) => (flat.length ? (i - 1 + flat.length) % flat.length : 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      flat[activeIndex]?.run();
    }
  };

  const hasQuery = query.trim() !== "";
  let runningIndex = -1;

  return (
    <div className="cmdk-overlay" onMouseDown={closePalette}>
      <div className="cmdk-panel" onMouseDown={(e) => e.stopPropagation()} onKeyDown={onKeyDown}>
        {/* 输入框 */}
        <div className="cmdk-input-row">
          <span className="cmdk-input-icon">🔍</span>
          <input
            ref={inputRef}
            className="cmdk-input"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索会话、消息、任务、Skills…"
            spellCheck={false}
            autoComplete="off"
          />
          {hasQuery && searchLoading && <span className="cmdk-loading">搜索中…</span>}
        </div>

        {/* 结果区 */}
        <div className="cmdk-results">
          {flat.length === 0 && (
            <div className="cmdk-empty">
              {hasQuery
                ? (searchLoading ? "搜索中…" : `没有匹配「${query.trim()}」的结果`)
                : "开始输入以搜索"}
            </div>
          )}

          {groups.map((g) => (
            <div key={g.kind} className="cmdk-group">
              <div className="cmdk-group-label">{g.label}</div>
              {g.items.map((item) => {
                runningIndex += 1;
                const idx = runningIndex;
                const active = idx === activeIndex;
                return (
                  <div
                    key={`${item.kind}-${item.id}`}
                    className={`cmdk-item${active ? " active" : ""}`}
                    onMouseEnter={() => setActiveIndex(idx)}
                    onClick={() => item.run()}
                  >
                    <span className="cmdk-item-icon">{item.icon}</span>
                    <span className="cmdk-item-title">{item.title}</span>
                    {item.subtitle && <span className="cmdk-item-sub">{item.subtitle}</span>}
                  </div>
                );
              })}
            </div>
          ))}

          {/* 知识库（RAG）预留分组 —— 暂不检索 */}
          {/* <div className="cmdk-group">
            <div className="cmdk-group-label">知识库</div>
            <div className="cmdk-item disabled">
              <span className="cmdk-item-icon">📚</span>
              <span className="cmdk-item-title">知识库检索</span>
              <span className="cmdk-item-sub">即将上线（RAG）</span>
            </div>
          </div> */}
        </div>

        {/* 底部提示 */}
        <div className="cmdk-footer">
          <span><kbd className="cmdk-kbd">↑</kbd><kbd className="cmdk-kbd">↓</kbd> 切换</span>
          <span><kbd className="cmdk-kbd">↵</kbd> 打开</span>
          <span><kbd className="cmdk-kbd">⌘K</kbd> 唤起</span>
          <span><kbd className="cmdk-kbd">Esc</kbd></span>
        </div>
      </div>
    </div>
  );
}
