/**
 * src/store/chatStore.ts
 *
 * 对话消息 store（v003 会话列表改造：per-session buffers）。
 *
 * 核心改造：
 * - 从单一 messages[] 改为 buffers: Map<sessionId, ChatBuffer>
 * - LRU 淘汰（上限 MAX_BUFFERS）；保护 currentSessionId 不被淘汰
 * - 派生 selector：useCurrentMessages / useIsStreamingIn(sid)
 * - 切换会话不改本 store —— 只切 sessionStore.currentSessionId，视图自动派生
 *
 * BackendProvider 收到任何流事件必须调用 xxx(sessionId, ...) 变体，
 * 让 buffer 写入对应 session 而非全局。
 */
import { create } from "zustand";
import type { ToolStartMsg, QuestionItem, HistoryMessage, ReplyUsage, ToolFeedback } from "../types/api";
import { useSessionStore } from "./sessionStore";
import i18n from "../i18n";

// ── 工具分类 ────────────────────────────────────────────────────────────────

export type ToolCategory =
  | "questionnaire"
  | "task_create"
  | "task_update"
  | "search"
  | "infra"
  | "misc";

export function categorizeTool(toolName: string): ToolCategory {
  switch (toolName) {
    case "ask_user":
      return "questionnaire";
    case "create_agent_task":
      return "task_create";
    case "update_agent_task":
      return "task_update";
    case "web_search":
      return "search";
    case "search_tools":
    case "set_task_dependency":
    case "report_progress":
      return "infra";
    default:
      return "misc";
  }
}

// ── 消息类型 ────────────────────────────────────────────────────────────────

export type MessageRole = "user" | "assistant" | "system";

export type ToolStatus = "running" | "done" | "error" | "skipped";

/** 工具执行结果（结构化，来自 TOOL_END） */
export interface ToolResult {
  preview?: string;        // result_preview
  full?: string | null;    // result_full（展开时用）
  error?: string | null;   // result_error
  mimeType?: string;       // result_mime_type
  sizeBytes?: number;      // result_size_bytes
  truncated?: boolean;     // result_truncated
}

export interface ToolCallState {
  tool_call_id: string;
  tool_name: string;
  category: ToolCategory;
  status: ToolStatus;
  args?: Record<string, unknown>;   // 完整输入（来自 tool_args，不截断）
  result?: ToolResult;
  /** 第三方对这次调用的评价（代码质量门控等），来自 TOOL_END.feedback。
   *  与 result 正交：result 是工具自己说的，feedback 是别人对它的评价。
   *  无 = 没有 provider 对这次调用发言，**不等于**"检查通过"。 */
  feedback?: ToolFeedback | null;
  durationMs?: number | null;       // "Ran for 1.2s"
  startedAt: number;                // 时间线排序 + 计时基准
}

/** TOOL_END 归并载荷（BackendProvider → finishToolCall） */
export interface ToolEndPayload {
  status: ToolStatus;
  result?: ToolResult;
  feedback?: ToolFeedback | null;
  durationMs?: number | null;
}

/** 技能/长任务进度：单个阶段 */
export type SkillActivityStatus = "running" | "completed" | "failed";
export interface SkillProgressStep {
  phase: string;
  status: SkillActivityStatus;
}
/** 一个技能活动的进度块（同 activity 的多次上报归并到此） */
export interface SkillProgressGroup {
  activity: string;
  steps: SkillProgressStep[];
  status: SkillActivityStatus;   // 整体状态：running 直到收到 completed/failed
  startedAt: number;
  durationMs?: number;
}

/** 有序时间线段：思考 / 文本 / 工具 / 技能进度，按真实发生顺序交错 */
export type TimelineItem =
  | { kind: "reasoning"; tokens: string[]; startedAt: number; durationMs?: number }
  | { kind: "text"; content: string }
  | { kind: "tool"; toolCallId: string }
  | { kind: "skill_progress"; group: SkillProgressGroup };

export interface PendingQuestionnaire {
  run_id: string;
  request_id?: string;
  questions: QuestionItem[];
  tool_name?: string;
  replied: boolean;
  /** ★ 该问卷所属会话：作答/回复必须回到此 session，绝不能用「当前正在看的会话」，
   *  否则用户切换会话后作答会串到别的会话（与 plan 审批同源的跨会话污染）。 */
  sessionId: string;
}

export interface CompletedMessage {
  kind: "completed";
  id: string;
  role: MessageRole;
  text: string;                  /* 正文拼接：user/system/history 直接渲染；assistant 用于复制/搜索 */
  timeline: TimelineItem[];      /* 有序段：思考/文本/工具（assistant 渲染源） */
  toolCalls: ToolCallState[];    /* 按启动顺序，timeline 的 tool 段按 id 索引到这里 */
  questionnaire?: PendingQuestionnaire;
  timestamp: number;
  replyScope?: string;
  /* 本轮对话消耗汇总，来自 REPLY_END.usage（后端 CostBudgetGuard.summary 精算）。
     气泡末尾展示净费用/tokens 明细/命中率/耗时，前端不重算。undefined = 后端未提供 → 不显示。 */
  usage?: ReplyUsage;
}

export interface StreamingMessage {
  kind: "streaming";
  id: string;
  timeline: TimelineItem[];      /* 有序段：思考/文本/工具，流式归并写入 */
  toolCalls: ToolCallState[];
  questionnaire?: PendingQuestionnaire;
  timestamp: number;
  replyScope?: string;
}

export type ChatMessage = CompletedMessage | StreamingMessage;

/** 单个会话的对话缓冲区。 */
export interface ChatBuffer {
  messages: ChatMessage[];
  streamingReplyId: string | null;
  lastEventAt: number;
  contextStatus: "fresh" | "restored" | "degraded" | null;
  /** ★ 取消中间态（P4，见取消语义-契约.md §7）：用户点了停止、已发 STOP_GENERATION，
   *  但后端尚未回 REPLY_END(halted)。此期间流式仍在（不本地立即收尾，消除前后端错位），
   *  按钮转 loading。收到后端收尾事件 → finishStreaming 清此标志。 */
  stopping: boolean;
}

const MAX_BUFFERS = 5;

// ── Store ──────────────────────────────────────────────────────────────────

interface ChatState {
  buffers: Map<string, ChatBuffer>;

  // 各种消息操作（都要传 sessionId）
  addUserMessage: (sessionId: string, content: string) => void;
  addAssistantMessage: (sessionId: string, content: string) => void;
  addSystemMessage: (sessionId: string, content: string) => void;
  startStreaming: (sessionId: string, replyId: string, replyScope?: string) => void;
  appendToken: (sessionId: string, replyId: string, token: string) => void;
  appendReasoningToken: (sessionId: string, replyId: string, token: string) => void;
  addToolCall: (sessionId: string, replyId: string, msg: ToolStartMsg) => void;
  finishToolCall: (sessionId: string, toolCallId: string, payload: ToolEndPayload) => void;
  applySkillProgress: (
    sessionId: string,
    p: { activity: string; phase: string; status: SkillActivityStatus },
  ) => void;
  finishStreaming: (sessionId: string, replyId: string, output?: string, keepEmpty?: boolean, usage?: ReplyUsage) => void;
  /** 进入取消中间态（P4）：点停止后调用，等后端 REPLY_END(halted) 事件驱动收尾。 */
  markStopping: (sessionId: string) => void;
  addQuestionnaire: (
    sessionId: string,
    replyId: string,
    runId: string,
    questions: QuestionItem[],
    opts?: { request_id?: string; tool_name?: string },
  ) => void;
  resolveQuestionnaire: (sessionId: string, replyId: string, resultText: string) => void;

  // buffer 管理
  setContextStatus: (sessionId: string, status: ChatBuffer["contextStatus"]) => void;
  clearSession: (sessionId: string) => void;
  loadHistory: (sessionId: string, messages: HistoryMessage[]) => void;
}

function makeEmptyBuffer(): ChatBuffer {
  return {
    messages: [],
    streamingReplyId: null,
    lastEventAt: Date.now(),
    contextStatus: null,
    stopping: false,
  };
}

function makeCompleted(
  id: string,
  role: MessageRole,
  text: string,
  toolCalls: ToolCallState[],
  questionnaire?: PendingQuestionnaire,
): CompletedMessage {
  return {
    kind: "completed",
    id,
    role,
    text,
    timeline: text ? [{ kind: "text", content: text }] : [],
    toolCalls: toolCalls.map((tc) => ({ ...tc, status: "done" as ToolStatus })),
    questionnaire,
    timestamp: Date.now(),
  };
}

/** 从 buffers 中拿到或建一个新 buffer，同时按 LRU 淘汰过量 buffer。
 *  被保护的 session：currentSessionId + 传入的 sessionId 本身。
 */
function upsertBuffer(
  buffers: Map<string, ChatBuffer>,
  sessionId: string,
  currentSessionId: string | null,
): { buffer: ChatBuffer; next: Map<string, ChatBuffer> } {
  const next = new Map(buffers);
  let buffer = next.get(sessionId);
  if (!buffer) {
    buffer = makeEmptyBuffer();
    next.set(sessionId, buffer);
    // LRU 淘汰：超过 MAX_BUFFERS 时移除 lastEventAt 最旧的（保护 current + sessionId 本身）
    if (next.size > MAX_BUFFERS) {
      let oldestSid: string | null = null;
      let oldestAt = Infinity;
      for (const [sid, b] of next) {
        if (sid === currentSessionId || sid === sessionId) continue;
        if (b.lastEventAt < oldestAt) {
          oldestAt = b.lastEventAt;
          oldestSid = sid;
        }
      }
      if (oldestSid) {
        next.delete(oldestSid);
      }
    }
  }
  // 刷新 lastEventAt
  buffer = { ...buffer, lastEventAt: Date.now() };
  next.set(sessionId, buffer);
  return { buffer, next };
}

export const useChatStore = create<ChatState>((set) => ({
  buffers: new Map<string, ChatBuffer>(),

  addUserMessage: (sessionId, content) =>
    set((state) => {
      const currentSid = useSessionStore.getState().currentSessionId;
      const { buffer, next } = upsertBuffer(state.buffers, sessionId, currentSid);
      const msg = makeCompleted(`user-${Date.now()}`, "user", content, []);
      next.set(sessionId, { ...buffer, messages: [...buffer.messages, msg] });
      return { buffers: next };
    }),

  addAssistantMessage: (sessionId, content) =>
    set((state) => {
      const currentSid = useSessionStore.getState().currentSessionId;
      const { buffer, next } = upsertBuffer(state.buffers, sessionId, currentSid);
      const msg = makeCompleted(`assistant-${Date.now()}`, "assistant", content, []);
      next.set(sessionId, { ...buffer, messages: [...buffer.messages, msg] });
      return { buffers: next };
    }),

  addSystemMessage: (sessionId, content) =>
    set((state) => {
      const currentSid = useSessionStore.getState().currentSessionId;
      const { buffer, next } = upsertBuffer(state.buffers, sessionId, currentSid);
      const msg = makeCompleted(`system-${Date.now()}`, "system", content, []);
      next.set(sessionId, { ...buffer, messages: [...buffer.messages, msg] });
      return { buffers: next };
    }),

  startStreaming: (sessionId, replyId, replyScope) =>
    set((state) => {
      const currentSid = useSessionStore.getState().currentSessionId;
      const { buffer, next } = upsertBuffer(state.buffers, sessionId, currentSid);

      // 如果已有同 id 的 streaming 消息，不重复创建
      const existingStreaming = buffer.messages.find(
        (m) => m.kind === "streaming" && m.id === replyId,
      );
      if (existingStreaming) {
        next.set(sessionId, { ...buffer, streamingReplyId: replyId });
        return { buffers: next };
      }
      const newMsg: StreamingMessage = {
        kind: "streaming",
        id: replyId,
        timeline: [],
        toolCalls: [],
        timestamp: Date.now(),
        replyScope,
      };
      next.set(sessionId, {
        ...buffer,
        streamingReplyId: replyId,
        messages: [...buffer.messages, newMsg],
      });
      return { buffers: next };
    }),

  appendToken: (sessionId, replyId, token) =>
    set((state) => {
      const buffer = state.buffers.get(sessionId);
      if (!buffer) return state;
      const now = Date.now();
      const messages = buffer.messages.map((m) =>
        m.kind === "streaming" && m.id === replyId
          ? { ...m, timeline: appendTextToTimeline(m.timeline, token, now) }
          : m,
      );
      const next = new Map(state.buffers);
      next.set(sessionId, { ...buffer, messages, lastEventAt: now });
      return { buffers: next };
    }),

  appendReasoningToken: (sessionId, replyId, token) =>
    set((state) => {
      const buffer = state.buffers.get(sessionId);
      if (!buffer) return state;
      const now = Date.now();
      const messages = buffer.messages.map((m) =>
        m.kind === "streaming" && m.id === replyId
          ? { ...m, timeline: appendReasoningToTimeline(m.timeline, token, now) }
          : m,
      );
      const next = new Map(state.buffers);
      next.set(sessionId, { ...buffer, messages, lastEventAt: now });
      return { buffers: next };
    }),

  addToolCall: (sessionId, replyId, msg) =>
    set((state) => {
      const buffer = state.buffers.get(sessionId);
      if (!buffer) return state;
      const now = Date.now();
      const messages = buffer.messages.map((m) => {
        if (m.kind !== "streaming" || m.id !== replyId) return m;
        if (m.toolCalls.some((tc) => tc.tool_call_id === msg.tool_call_id)) return m;
        const toolCall: ToolCallState = {
          tool_call_id: msg.tool_call_id,
          tool_name: msg.tool_name,
          category: categorizeTool(msg.tool_name),
          status: "running",
          args: msg.tool_args,
          startedAt: now,
        };
        return {
          ...m,
          toolCalls: [...m.toolCalls, toolCall],
          timeline: [
            ...closeOpenReasoning(m.timeline, now),
            { kind: "tool" as const, toolCallId: msg.tool_call_id },
          ],
        };
      });
      const next = new Map(state.buffers);
      next.set(sessionId, { ...buffer, messages, lastEventAt: now });
      return { buffers: next };
    }),

  finishToolCall: (sessionId, toolCallId, payload) =>
    set((state) => {
      const buffer = state.buffers.get(sessionId);
      if (!buffer) return state;
      const messages = buffer.messages.map((m) => {
        if (m.kind !== "streaming") return m;
        if (!m.toolCalls.some((tc) => tc.tool_call_id === toolCallId)) return m;
        return {
          ...m,
          toolCalls: m.toolCalls.map((tc) =>
            tc.tool_call_id === toolCallId
              ? { ...tc, status: payload.status, result: payload.result, feedback: payload.feedback, durationMs: payload.durationMs }
              : tc,
          ),
        };
      });
      const next = new Map(state.buffers);
      next.set(sessionId, { ...buffer, messages, lastEventAt: Date.now() });
      return { buffers: next };
    }),

  applySkillProgress: (sessionId, p) =>
    set((state) => {
      const buffer = state.buffers.get(sessionId);
      if (!buffer) return state;
      const now = Date.now();
      // 附到当前流式消息（与 TOOL_START 一致：取最后一个 streaming）
      let targetId: string | null = null;
      for (let i = buffer.messages.length - 1; i >= 0; i--) {
        if (buffer.messages[i].kind === "streaming") {
          targetId = buffer.messages[i].id;
          break;
        }
      }
      if (!targetId) return state;
      const messages = buffer.messages.map((m) =>
        m.kind === "streaming" && m.id === targetId
          ? { ...m, timeline: applyProgressToTimeline(m.timeline, p, now) }
          : m,
      );
      const next = new Map(state.buffers);
      next.set(sessionId, { ...buffer, messages, lastEventAt: now });
      return { buffers: next };
    }),

  finishStreaming: (sessionId, replyId, output, keepEmpty = false, usage) =>
    set((state) => {
      const buffer = state.buffers.get(sessionId);
      if (!buffer) return state;
      const now = Date.now();
      const messages = buffer.messages.reduce<ChatMessage[]>((acc, m) => {
        if (m.kind !== "streaming" || m.id !== replyId) {
          acc.push(m);
          return acc;
        }
        // 封掉仍打开的思考段（结算耗时）
        let timeline = closeOpenReasoning(m.timeline, now);
        const derivedText = timelineText(timeline);
        const text = derivedText || output || (keepEmpty ? i18n.t("chat.youAborted") : "");
        const hasVisible = m.toolCalls.some(
          (tc) => tc.category !== "infra" && tc.category !== "task_update",
        );
        const hasSkillProgress = timeline.some((i) => i.kind === "skill_progress");
        if (!text && !hasVisible && !hasSkillProgress && !keepEmpty) return acc;
        // 最终 text 来自 output/halt 兜底但 timeline 无文本段 → 补一段
        if (text && !derivedText) {
          timeline = [...timeline, { kind: "text", content: text }];
        }
        const completed: CompletedMessage = {
          kind: "completed",
          id: m.id,
          role: "assistant",
          text,
          timeline,
          // 收尾：仍在 running 的工具置为 done，避免残留转圈
          toolCalls: m.toolCalls.map((tc) =>
            tc.status === "running" ? { ...tc, status: "done" as ToolStatus } : tc,
          ),
          questionnaire: m.questionnaire,
          timestamp: m.timestamp,
          replyScope: m.replyScope,
          usage,
        };
        acc.push(completed);
        return acc;
      }, []);
      const next = new Map(state.buffers);
      next.set(sessionId, {
        ...buffer,
        messages,
        streamingReplyId:
          buffer.streamingReplyId === replyId ? null : buffer.streamingReplyId,
        lastEventAt: Date.now(),
        // 收尾即退出取消中间态（事件驱动收口，见 P4）。
        stopping: false,
      });
      return { buffers: next };
    }),

  markStopping: (sessionId) =>
    set((state) => {
      const buffer = state.buffers.get(sessionId);
      // 仅在确有流式在跑时进入中间态；无流式则忽略（幂等、防误置）。
      if (!buffer || !buffer.messages.some((m) => m.kind === "streaming")) return state;
      const next = new Map(state.buffers);
      next.set(sessionId, { ...buffer, stopping: true });
      return { buffers: next };
    }),

  addQuestionnaire: (sessionId, replyId, runId, questions, opts) =>
    set((state) => {
      const buffer = state.buffers.get(sessionId);
      if (!buffer) return state;
      const messages = buffer.messages.map((m) => {
        if (m.id !== replyId) return m;
        return {
          ...m,
          questionnaire: {
            run_id: runId,
            request_id: opts?.request_id,
            questions,
            tool_name: opts?.tool_name,
            replied: false,
            sessionId,
          } satisfies PendingQuestionnaire,
        };
      });
      const next = new Map(state.buffers);
      next.set(sessionId, { ...buffer, messages, lastEventAt: Date.now() });
      return { buffers: next };
    }),

  // 作答完成：标记 replied，并把「用户的选择」补成一条已完成的 ask_user 工具卡。
  //
  // ★ 运行时 ask_user 全程不产生 TOOL_START/TOOL_END 事件：首次调用在执行工具前就
  //   暂停（只发 INTERACTION_REQUESTED），恢复时静默执行（run_core.py 明确不 yield
  //   tool_call 事件）。因此前端唯一的 ask_user 信号是 INTERACTION_REQUEST（驱动
  //   InteractionInline 出题）。作答后 InteractionInline 不回显答案，而是期望时间线里
  //   出现 AskUserRenderer 工具卡——但该卡依赖一个从不存在的 ToolCallState。
  //   这里就地补一个「已完成」的合成工具调用（结果文本与后端持久化、历史还原完全一致：
  //   "用户选择了：..."），让运行时与历史还原渲染同一张卡。
  resolveQuestionnaire: (sessionId, replyId, resultText) =>
    set((state) => {
      const buffer = state.buffers.get(sessionId);
      if (!buffer) return state;
      const messages = buffer.messages.map((m) => {
        if (m.id !== replyId) return m;
        if (!("questionnaire" in m) || !m.questionnaire) return m;
        const toolName = m.questionnaire.tool_name || "ask_user";
        const synthId = `interaction-${m.questionnaire.run_id || replyId}`;
        // 幂等：重复作答只翻 replied，不重复补卡
        const already = m.toolCalls.some((tc) => tc.tool_call_id === synthId);
        const toolCalls = already
          ? m.toolCalls
          : [
              ...m.toolCalls,
              {
                tool_call_id: synthId,
                tool_name: toolName,
                category: categorizeTool(toolName),
                status: "done" as ToolStatus,
                result: { full: resultText },
                startedAt: Date.now(),
              },
            ];
        const timeline = already
          ? m.timeline
          : [...m.timeline, { kind: "tool" as const, toolCallId: synthId }];
        return {
          ...m,
          toolCalls,
          timeline,
          questionnaire: { ...m.questionnaire, replied: true },
        };
      });
      const next = new Map(state.buffers);
      next.set(sessionId, { ...buffer, messages });
      return { buffers: next };
    }),

  setContextStatus: (sessionId, status) =>
    set((state) => {
      const currentSid = useSessionStore.getState().currentSessionId;
      const { buffer, next } = upsertBuffer(state.buffers, sessionId, currentSid);
      next.set(sessionId, { ...buffer, contextStatus: status });
      return { buffers: next };
    }),

  clearSession: (sessionId) =>
    set((state) => {
      if (!state.buffers.has(sessionId)) return state;
      const next = new Map(state.buffers);
      next.delete(sessionId);
      return { buffers: next };
    }),

  loadHistory: (sessionId, msgs) =>
    set((state) => {
      const currentSid = useSessionStore.getState().currentSessionId;
      const { buffer, next } = upsertBuffer(state.buffers, sessionId, currentSid);
      const messages: ChatMessage[] = msgs.map((m, idx) =>
        historyToCompleted(m, `history-${sessionId}-${idx}`, Date.now() - (msgs.length - idx) * 1000),
      );
      next.set(sessionId, {
        ...buffer,
        messages,
        contextStatus: "restored",
      });
      return { buffers: next };
    }),
}));

// ── 派生 selectors ─────────────────────────────────────────────────────────

/** 当前视图的消息列表（视图切换时自动派生新数组） */
export function useCurrentMessages(): ChatMessage[] {
  const sid = useSessionStore((s) => s.currentSessionId);
  return useChatStore((s) => {
    if (!sid) return [];
    return s.buffers.get(sid)?.messages ?? [];
  });
}

/** 当前视图是否有 streaming（用于 InputBar 禁用） */
export function useIsStreaming(): boolean {
  const sid = useSessionStore((s) => s.currentSessionId);
  return useChatStore((s) => {
    if (!sid) return false;
    const buffer = s.buffers.get(sid);
    if (!buffer) return false;
    return buffer.messages.some((m) => m.kind === "streaming");
  });
}

/** 当前视图是否处于取消中间态（P4：点了停止、等后端收尾）。用于停止按钮转 loading。 */
export function useIsStopping(): boolean {
  const sid = useSessionStore((s) => s.currentSessionId);
  return useChatStore((s) => {
    if (!sid) return false;
    return s.buffers.get(sid)?.stopping ?? false;
  });
}

/** SessionItem 徽标用：某 session 是否有 streaming（后台生成中） */
export function useIsStreamingIn(sessionId: string): boolean {
  return useChatStore((s) => {
    const buffer = s.buffers.get(sessionId);
    if (!buffer) return false;
    return buffer.messages.some((m) => m.kind === "streaming");
  });
}

// ── 时间线归并工具 ──────────────────────────────────────────────────────────

/** 若末段是仍打开的思考段，则结算其耗时（封段）。 */
function closeOpenReasoning(timeline: TimelineItem[], now: number): TimelineItem[] {
  const last = timeline[timeline.length - 1];
  if (last && last.kind === "reasoning" && last.durationMs == null) {
    return [...timeline.slice(0, -1), { ...last, durationMs: now - last.startedAt }];
  }
  return timeline;
}

/** 追加正文 token：并入末尾文本段，或封掉思考段后开新文本段。 */
function appendTextToTimeline(timeline: TimelineItem[], token: string, now: number): TimelineItem[] {
  const last = timeline[timeline.length - 1];
  if (last && last.kind === "text") {
    return [...timeline.slice(0, -1), { ...last, content: last.content + token }];
  }
  return [...closeOpenReasoning(timeline, now), { kind: "text", content: token }];
}

/** 追加思考 token：并入末尾（仍打开的）思考段，或另开新思考段。 */
function appendReasoningToTimeline(timeline: TimelineItem[], token: string, now: number): TimelineItem[] {
  const last = timeline[timeline.length - 1];
  if (last && last.kind === "reasoning" && last.durationMs == null) {
    return [...timeline.slice(0, -1), { ...last, tokens: [...last.tokens, token] }];
  }
  return [...timeline, { kind: "reasoning", tokens: [token], startedAt: now }];
}

/**
 * 应用一条技能进度事件到 timeline。
 * 同 activity 且仍 running 的进度块归并；否则新建一个块（保持发生位置）。
 * - running + 新 phase：上一阶段自动落 completed，追加新阶段
 * - completed / failed：整个活动收尾，末阶段与整体一起落终态
 */
function applyProgressToTimeline(
  timeline: TimelineItem[],
  p: { activity: string; phase: string; status: SkillActivityStatus },
  now: number,
): TimelineItem[] {
  let idx = -1;
  for (let i = timeline.length - 1; i >= 0; i--) {
    const it = timeline[i];
    if (it.kind === "skill_progress" && it.group.activity === p.activity && it.group.status === "running") {
      idx = i;
      break;
    }
  }

  if (idx === -1) {
    const base = closeOpenReasoning(timeline, now);
    const group: SkillProgressGroup = {
      activity: p.activity,
      steps: p.phase ? [{ phase: p.phase, status: p.status }] : [],
      status: p.status,
      startedAt: now,
      durationMs: p.status === "running" ? undefined : 0,
    };
    return [...base, { kind: "skill_progress", group }];
  }

  const item = timeline[idx] as Extract<TimelineItem, { kind: "skill_progress" }>;
  const g = item.group;
  const steps = g.steps.slice();
  const last = steps[steps.length - 1];

  let group: SkillProgressGroup;
  if (p.status === "running") {
    if (!last || last.phase !== p.phase) {
      if (last && last.status === "running") steps[steps.length - 1] = { ...last, status: "completed" };
      if (p.phase) steps.push({ phase: p.phase, status: "running" });
    }
    group = { ...g, steps, status: "running" };
  } else {
    if (last && last.status === "running") steps[steps.length - 1] = { ...last, status: p.status };
    group = { ...g, steps, status: p.status, durationMs: now - g.startedAt };
  }
  return [...timeline.slice(0, idx), { kind: "skill_progress", group }, ...timeline.slice(idx + 1)];
}

/** 拼接 timeline 中所有文本段（用于 completed.text 兜底/复制/搜索）。 */
function timelineText(timeline: TimelineItem[]): string {
  return timeline
    .filter((i): i is Extract<TimelineItem, { kind: "text" }> => i.kind === "text")
    .map((i) => i.content)
    .join("");
}

/** 历史投影消息 → CompletedMessage（与实时流式共用 Timeline 渲染）。 */
function historyToCompleted(m: HistoryMessage, id: string, timestamp: number): CompletedMessage {
  const role = (m.role as MessageRole) || "assistant";
  const text = m.content || "";

  // user/system 或无富结构 → 纯文本兜底
  if (role !== "assistant" || !m.timeline || m.timeline.length === 0) {
    return {
      kind: "completed",
      id,
      role,
      text,
      timeline: text ? [{ kind: "text", content: text }] : [],
      toolCalls: [],
      timestamp,
    };
  }

  const toolCalls: ToolCallState[] = (m.tool_calls ?? []).map((tc) => ({
    tool_call_id: tc.tool_call_id,
    tool_name: tc.tool_name,
    category: categorizeTool(tc.tool_name),
    status: tc.status,
    args: tc.args,
    result: tc.result,
    startedAt: timestamp,
  }));

  const timeline: TimelineItem[] = m.timeline.map((item) => {
    if (item.kind === "reasoning") return { kind: "reasoning", tokens: [item.text], startedAt: timestamp };
    if (item.kind === "text") return { kind: "text", content: item.content };
    return { kind: "tool", toolCallId: item.tool_call_id };
  });

  return { kind: "completed", id, role, text, timeline, toolCalls, timestamp };
}
