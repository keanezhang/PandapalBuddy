/**
 * src/pages/TasksPage.tsx — v2 嵌入模式
 *
 * 信息优先级：做什么 > 什么时候 > 是否正常。
 * 下次触发时间用 accent 色强调，cron 退居次要位置。
 */

import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTaskSchedulerStore } from "../store/taskSchedulerStore";
import { useBackend } from "../providers/BackendProvider";
import type { ScheduledTaskItem } from "../types/api";

/* ── 星期映射 ────────────────────────────────────────────────── */
const DOW_NAMES = ["日", "一", "二", "三", "四", "五", "六"];

/* ── 帮助函数 ────────────────────────────────────────────────── */
function formatDateTime(isoStr: string): string {
  try {
    const d = new Date(isoStr);
    const pad = (n: number) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  } catch { return isoStr || "—"; }
}

function formatDate(isoStr: string): string {
  try { const d = new Date(isoStr); return `${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`; }
  catch { return ""; }
}

/**
 * 将 cron 表达式转为人类可读的"下次触发时间"。
 * 返回 { text: string, isSoon: boolean }
 * isSoon = 24 小时内触发（用于加亮颜色）
 */
function nextTriggerText(cron: string): { text: string; isSoon: boolean } {
  if (!cron) return { text: "—", isSoon: false };
  const parts = cron.trim().split(/\s+/);
  if (parts.length < 5) return { text: cron, isSoon: false };

  const [min, hour, dom, mon, dow] = parts;
  const hh = String(parseInt(hour)).padStart(2, "0");
  const mm = String(parseInt(min)).padStart(2, "0");
  const now = new Date();

  // 每天：M H * * *
  if (hour !== "*" && min !== "*" && dom === "*" && mon === "*" && dow === "*") {
    const triggerTime = new Date(now);
    triggerTime.setHours(parseInt(hour), parseInt(min), 0, 0);
    if (triggerTime <= now) triggerTime.setDate(triggerTime.getDate() + 1);
    const diffMs = triggerTime.getTime() - now.getTime();
    const diffHours = diffMs / 3600000;
    const isToday = triggerTime.toDateString() === now.toDateString();
    if (isToday) return { text: `今天 ${hh}:${mm}`, isSoon: true };
    if (diffHours <= 24) return { text: `明天 ${hh}:${mm}`, isSoon: true };
    return { text: `每天 ${hh}:${mm}`, isSoon: false };
  }

  // 每周：M H * * DOW
  if (hour !== "*" && min !== "*" && dom === "*" && mon === "*" && dow !== "*") {
    const targetDow = parseInt(dow);
    const currentDow = now.getDay();
    let daysUntil = targetDow - currentDow;
    if (daysUntil <= 0) daysUntil += 7;
    const nextDate = new Date(now);
    nextDate.setDate(nextDate.getDate() + daysUntil);
    nextDate.setHours(parseInt(hour), parseInt(min), 0, 0);
    if (nextDate <= now) nextDate.setDate(nextDate.getDate() + 7);
    const diffMs = nextDate.getTime() - now.getTime();
    return { text: `周${DOW_NAMES[targetDow]} ${hh}:${mm}`, isSoon: diffMs <= 86400000 };
  }

  // 每月：M H D * *  (D = day of month)
  if (hour !== "*" && min !== "*" && dom !== "*" && mon === "*" && dow === "*") {
    const d = parseInt(dom);
    return { text: `每月 ${d} 日 ${hh}:${mm}`, isSoon: false };
  }

  // 每年 / 一次性：M H D M *
  if (hour !== "*" && min !== "*" && dom !== "*" && mon !== "*" && dow === "*") {
    const d = parseInt(dom);
    const m = parseInt(mon);
    return { text: `${m}月${d}日 ${hh}:${mm}`, isSoon: false };
  }

  // 兜底
  return { text: cron, isSoon: false };
}

const TRIGGER_ICONS: Record<string, string> = { recurring: "⏱", oneshot: "📌", event: "📡", manual: "👆" };
const TRIGGER_LABELS: Record<string, string> = { recurring: "重复任务", oneshot: "单次任务", event: "事件触发", manual: "手动执行" };
const TRIGGER_BADGE: Record<string, { bg: string; color: string }> = {
  recurring: { bg: "rgba(124,58,237,0.12)", color: "var(--accent-soft)" },
  oneshot: { bg: "rgba(234,179,8,0.12)", color: "var(--accent-2)" },
  event: { bg: "rgba(59,130,246,0.12)", color: "#60A5FA" },
  manual: { bg: "rgba(255,255,255,0.06)", color: "var(--text-tertiary)" },
};

export function TasksPage() {
  const navigate = useNavigate();
  const { tasks, loading, removeTask } = useTaskSchedulerStore();
  const { requestScheduledTasks, deleteScheduledTask, createSession } = useBackend();

  // 当前展开详情的任务（默认全部折叠 → null）；同一时刻仅展开一个
  const [expandedId, setExpandedId] = useState<string | null>(null);
  // 删除二次确认态（记录当前待确认的 task_id，避免误删）
  const [confirmingId, setConfirmingId] = useState<string | null>(null);

  useEffect(() => { requestScheduledTasks(); }, [requestScheduledTasks]);

  const validTasks = tasks.filter((t) => t.name?.trim().length > 0);

  const toggleExpand = (taskId: string) => {
    setConfirmingId(null);
    setExpandedId((prev) => (prev === taskId ? null : taskId));
  };

  const handleDelete = (taskId: string) => {
    setConfirmingId(null);
    if (expandedId === taskId) setExpandedId(null);
    // 乐观移除：立即从列表消失。后端 unregister_task_definition 会删除持久化
    // 文件（含 .md）并回推全量列表做最终对账；若失败则回推恢复该条目。
    removeTask(taskId);
    // 确定性删除：直连后端 task_scheduler，绕过 LLM（旧路径靠 Agent 解析名称→id
    // 而无对应工具，导致后端文件残留的根因已在此彻底规避）。
    deleteScheduledTask(taskId);
  };

  const handleCreateTask = () => {
    createSession();
    navigate("/");
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", flex: 1, background: "var(--bg-root)", overflow: "hidden" }}>
      {/* 标题行 */}
      <div style={{
        padding: "12px 24px 10px",
        background: "linear-gradient(135deg, rgba(234,179,8,0.06) 0%, rgba(249,115,22,0.03) 50%, rgba(234,179,8,0.02) 100%)",
        borderBottom: "1px solid var(--border-subtle)",
        display: "flex", alignItems: "center", gap: 14,
        flexShrink: 0,
      }}>
        <div style={{ flex: 1 }}>
          <h1 style={{ fontSize: 22, fontWeight: 600, color: "var(--text-primary)", display: "flex", alignItems: "center", gap: 10, margin: 0, letterSpacing: "-0.01em" }}>
            <span style={{ width: 34, height: 34, fontSize: 17, display: "flex", alignItems: "center", justifyContent: "center" }}>📋</span>
            任务安排
            {validTasks.length > 0 && (
              <span style={{ fontSize: 13, fontWeight: 500, color: "var(--text-tertiary)" }}>· {validTasks.length}</span>
            )}
          </h1>
        </div>
        {validTasks.length > 0 && (
          <button onClick={handleCreateTask} className="btn btn-sm" style={{
            background: "rgba(234,179,8,0.10)", color: "var(--accent-2)",
            border: "1px solid rgba(234,179,8,0.20)",
          }}>＋ 创建任务</button>
        )}
        {loading && <span className="skills-loading-dot" title="刷新中..." />}
      </div>

      {/* 内容区：自适应卡片网格 */}
      <div style={{ flex: 1, overflowY: "auto" }}>
        {loading && validTasks.length === 0 ? (
          <div className="skills-loading" style={{ padding: 40 }}><span className="skills-loading-dot" /> 加载中…</div>
        ) : validTasks.length === 0 ? (
          /* ── 空状态 ── */
          <div style={{
            display: "flex", flexDirection: "column", alignItems: "center",
            padding: "64px 28px", textAlign: "center",
          }}>
            <div style={{
              width: 64, height: 64, borderRadius: "var(--radius-lg)",
              background: "linear-gradient(135deg, rgba(234,179,8,0.15), rgba(249,115,22,0.10))",
              display: "flex", alignItems: "center", justifyContent: "center",
              fontSize: 32, marginBottom: 20,
            }}>⏰</div>
            <div style={{ fontSize: 16, fontWeight: 600, color: "var(--text-primary)", marginBottom: 6 }}>还没有定时任务</div>
            <div style={{ fontSize: 13, color: "var(--text-tertiary)", lineHeight: 1.7, marginBottom: 20, maxWidth: 260 }}>
              在聊天中对 Agent 说出你想定时做的事情，<br />AI 会自动为你创建定时任务
            </div>
            <div style={{
              background: "var(--bg-elevated)", borderRadius: "var(--radius-md)",
              border: "1px solid var(--border-subtle)",
              padding: "var(--space-3) var(--space-4)", marginBottom: 24,
              width: "100%", maxWidth: 280, textAlign: "left",
            }}>
              <div style={{ fontSize: 10, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 10 }}>试试这样说</div>
              {["每天早上 8 点提醒我背单词", "每周五下午 5 点生成周报", "每天 18 点检查代码并推送"].map((tip) => (
                <div key={tip} style={{ fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.7, padding: "4px 0", display: "flex", alignItems: "center", gap: 6 }}>
                  <span style={{ color: "var(--text-muted)", fontSize: 11 }}>💬</span>{tip}
                </div>
              ))}
            </div>
            <button onClick={handleCreateTask} className="btn btn-primary" style={{ fontSize: 13, fontWeight: 600, padding: "10px 28px", borderRadius: "var(--radius-md)" }}>
              💬 去创建任务
            </button>
          </div>
        ) : (
          /* 自适应网格：每列最小 340px，随宽度自动决定一行显示几个 */
          <div style={{
            maxWidth: 1400, margin: "0 auto", padding: "24px 28px",
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))",
            gap: 16,
            alignItems: "start",
          }}>
            {validTasks.map((task) => (
              <TaskCard
                key={task.task_id}
                task={task}
                expanded={expandedId === task.task_id}
                confirming={confirmingId === task.task_id}
                onToggle={() => toggleExpand(task.task_id)}
                onRequestDelete={() => setConfirmingId(task.task_id)}
                onCancelDelete={() => setConfirmingId(null)}
                onConfirmDelete={() => handleDelete(task.task_id)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/* ── 任务卡片（详情默认折叠，点击头部展开） ─────────────────────── */

function TaskCard({
  task, expanded, confirming, onToggle, onRequestDelete, onCancelDelete, onConfirmDelete,
}: {
  task: ScheduledTaskItem;
  expanded: boolean;
  confirming: boolean;
  onToggle: () => void;
  onRequestDelete: () => void;
  onCancelDelete: () => void;
  onConfirmDelete: () => void;
}) {
  const nt = task.cron_expression ? nextTriggerText(task.cron_expression) : { text: "", isSoon: false };
  const badge = TRIGGER_BADGE[task.trigger_type] ?? TRIGGER_BADGE.manual;

  return (
    <div style={{
      background: "var(--bg-elevated)",
      border: `1px solid ${expanded ? "rgba(124,58,237,0.25)" : "var(--border-subtle)"}`,
      borderRadius: "var(--radius-md)",
      overflow: "hidden",
      transition: "border-color var(--duration-fast)",
    }}>
      {/* ── 卡片头部（点击展开/折叠） ── */}
      <div onClick={onToggle} style={{
        padding: "var(--space-4)", cursor: "pointer",
        display: "flex", flexDirection: "column", gap: 12,
      }}>
        <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
          <span className="skill-card-icon icon-amber" style={{
            width: 40, height: 40, fontSize: 20, borderRadius: "var(--radius-md)", flexShrink: 0,
          }}>
            {TRIGGER_ICONS[task.trigger_type] || "⏰"}
          </span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <h2 style={{
                fontSize: 15, fontWeight: 600, color: "var(--text-primary)", margin: 0,
                letterSpacing: "-0.01em", overflow: "hidden", textOverflow: "ellipsis",
              }}>
                {task.name}
              </h2>
              <span className="badge" style={{ background: badge.bg, color: badge.color, flexShrink: 0 }}>
                {TRIGGER_LABELS[task.trigger_type] || task.trigger_type}
              </span>
            </div>
          </div>
          <StatusBadge task={task} />
          <span style={{
            color: "var(--text-muted)", fontSize: 18, lineHeight: 1,
            transform: expanded ? "rotate(90deg)" : "none",
            transition: "transform var(--duration-fast)", flexShrink: 0, alignSelf: "center",
          }}>›</span>
        </div>

        {/* 下次触发时间 */}
        {nt.text && (
          <div style={{
            display: "inline-flex", alignItems: "center", gap: 8, alignSelf: "flex-start",
            padding: "5px 12px",
            background: nt.isSoon ? "rgba(124,58,237,0.08)" : "rgba(255,255,255,0.03)",
            borderRadius: "var(--radius-sm)",
            border: `1px solid ${nt.isSoon ? "rgba(124,58,237,0.20)" : "var(--border-subtle)"}`,
          }}>
            <span style={{ fontSize: 12, color: "var(--text-tertiary)" }}>提醒 @</span>
            <span style={{ fontSize: 14, fontWeight: 700, color: nt.isSoon ? "var(--accent)" : "var(--text-primary)" }}>
              {nt.text}
            </span>
          </div>
        )}
      </div>

      {/* ── 展开详情 ── */}
      {expanded && (
        <div style={{ padding: "0 var(--space-4) var(--space-4)", borderTop: "1px solid var(--border-subtle)" }}>
          {/* 任务指令 */}
          <div style={{
            background: "#121212",
            border: "1px solid var(--border-subtle)",
            borderRadius: "var(--radius-md)", overflow: "hidden",
            marginTop: 16, marginBottom: 16,
          }}>
            <div style={{
              padding: "var(--space-2) var(--space-4)",
              background: "rgba(255,255,255,0.02)",
              borderBottom: "1px solid var(--border-subtle)",
              fontSize: 11, fontWeight: 600, color: "var(--text-muted)",
              textTransform: "uppercase", letterSpacing: "0.04em",
            }}>任务指令</div>
            <div style={{
              fontSize: 13, color: "var(--text-secondary)", lineHeight: 1.75,
              padding: "var(--space-4)",
              fontFamily: "var(--font-mono)", whiteSpace: "pre-wrap",
              maxHeight: 300, overflowY: "auto",
            }}>
              {task.task_prompt || "（无指令）"}
            </div>
          </div>

          {/* 元信息 */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 16 }}>
            <InfoTile label="敏感度" value={task.sensitivity} />
            <InfoTile label="创建时间" value={formatDate(task.created_at)} />
            <InfoTile label="最近执行" value={<StatusText task={task} />} />
            <InfoTile label="会话" value={task.session_id ? task.session_id.slice(0, 12) + "…" : "—"} />
          </div>

          {/* 删除：二次确认，防误删 */}
          {!confirming ? (
            <button className="btn btn-danger"
              onClick={onRequestDelete}
              style={{ width: "100%", justifyContent: "center", padding: "10px 0" }}
            >
              🗑 删除任务
            </button>
          ) : (
            <div style={{ display: "flex", gap: 10 }}>
              <button className="btn btn-ghost"
                onClick={onCancelDelete}
                style={{ flex: 1, justifyContent: "center", padding: "10px 0" }}
              >
                取消
              </button>
              <button className="btn btn-danger-solid"
                onClick={onConfirmDelete}
                style={{ flex: 2, justifyContent: "center", padding: "10px 0" }}
              >
                确认删除
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ── 状态组件 ────────────────────────────────────────────────── */

function StatusBadge({ task }: { task: ScheduledTaskItem }) {
  const ls = task.last_status;
  if (ls === "running") return <span className="badge badge-blue" style={{ fontSize: 10 }}>⏳ 运行中</span>;
  if (ls === "completed") return <span className="badge badge-green" style={{ fontSize: 10 }}>✓ 已完成</span>;
  if (ls === "failed") return <span className="badge badge-red" style={{ fontSize: 10 }}>⚠ 失败</span>;
  // 待触发 — 紫色高亮（最常见状态，醒目标记）
  return <span className="badge" style={{ fontSize: 10, background: "rgba(124,58,237,0.15)", color: "var(--accent-soft)", border: "1px solid rgba(124,58,237,0.25)" }}>⏱ 待触发</span>;
}

function StatusText({ task }: { task: ScheduledTaskItem }) {
  const ls = task.last_status, lr = task.last_run_at;
  if (ls === "running") return <span style={{ color: "var(--accent-soft)", fontWeight: 500 }}>执行中…</span>;
  if (ls === "completed" && lr) return <span style={{ color: "var(--success)", fontWeight: 500 }}>{formatDateTime(lr)}</span>;
  if (ls === "failed" && lr) return <span style={{ color: "var(--danger)", fontWeight: 500 }}>{formatDateTime(lr)}</span>;
  // 待触发 — 紫色（最常见状态）
  return <span style={{ color: "var(--accent)", fontWeight: 600 }}>⏱ 待触发</span>;
}

function InfoTile({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div style={{
      background: "var(--bg-elevated)",
      border: "1px solid var(--border-subtle)",
      borderRadius: "var(--radius-sm)",
      padding: "var(--space-3) var(--space-4)",
    }}>
      <div style={{ fontSize: 10, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 4 }}>
        {label}
      </div>
      <div style={{ fontSize: 13, color: "var(--text-primary)", fontWeight: 500 }}>
        {value}
      </div>
    </div>
  );
}
