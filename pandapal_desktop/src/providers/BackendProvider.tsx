/**
 * src/providers/BackendProvider.tsx
 *
 * 职责：
 * - 监听 Tauri "backend-ready" 事件
 * - 通过 invoke 向 Python sidecar 发消息
 * - 监听 "backend-event" 事件接收 Python 推送
 * - 解析后分发到对应 Store
 *
 * 通信路径（无 WebSocket）：
 *   前端 → invoke("send_message") → Rust → Python stdin
 *   Python stdout (IPC:{json}) → Rust → emit("backend-event") → 前端
 *
 * 5.2 干净版：
 *   - 不做 compat 兜底：所有字段直接走 Python 5.2 wire format
 *   - AGENT_TASK_EVENT 用 store.notifyTaskChange（不存 stub data）
 *   - TASK_NOTIFICATION 直接传扁平字段
 *   - INTERACTION_REQUEST 走扁平 question + options
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
} from "react";
import { listen } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";
import { readTextFile } from "@tauri-apps/plugin-fs";

import { useConnectionStore } from "../store/connectionStore";
import { useChatStore, categorizeTool } from "../store/chatStore";
import { useTaskSchedulerStore } from "../store/taskSchedulerStore";
import { useAgentTaskStore } from "../store/agentTaskStore";
import { useSkillStore } from "../store/skillStore";
import { toast } from "../components/ui";
import { useSearchStore } from "../store/searchStore";
import { useSessionConcurrencyStore } from "../store/sessionConcurrencyStore";
import { useFileStore } from "../store/fileStore";
import { pickOriginalCandidate } from "./editFileOriginalPicker";
import { reconstructOriginalFromResult } from "./editDiffReconstructor";
import { useModelStore } from "../store/modelStore";
import { useSessionStore } from "../store/sessionStore";
import { usePetStore } from "../store/petStore";
import { useDashboardStore } from "../store/dashboardStore";
import { useBudgetStore } from "../store/budgetStore";
import { useCredentialStore } from "../store/credentialStore";
import { useGroupViewStore } from "../store/groupViewStore";
import { useHitlStore } from "../store/hitlStore";
import { usePlanApprovalStore } from "../store/planApprovalStore";
import type {
  OutboundApiMessage,
  TaskNotificationMsg,
  PlanApprovalRequestMsg,
  ScheduledTaskListMsg,
  DashboardDataMsg,
  BudgetStatusMsg,
  ModelListMsg,
  AgentHaltedMsg,
  ScheduledTaskChangedMsg,
  AgentTaskEventMsg,
  SkillProgressEventMsg,
  SearchResultMsg,
  SkillListResultMsg,
  SkillGetResultMsg,
  SkillSavedMsg,
  SkillDeletedMsg,
  SkillActivatedMsg,
  SkillClearedMsg,
  SkillImportedMsg,
  SkillExportedMsg,
  SessionConcurrencyMsg,
  SessionListMsg,
  SessionSwitchedMsg,
  SessionUpdatedMsg,
  SessionDeletedMsg,
  SessionGroupListMsg,
  SessionHistoryListMsg,
  AgentMode,
  CredentialsListMsg,
  CredentialsSavedMsg,
  CredentialsVerifiedMsg,
  CredentialsStatusMsg,
  AuthTokenRefreshedMsg,
} from "../types/api";
import { useAuthStore } from "../store/authStore";
import type { LLMProvider, VerifyResult } from "../store/credentialStore";

// edit_file 原始内容缓存：AI 编辑文件前，缓存原始内容用于 Accept/Reject 对比
const editFileOriginals = new Map<string, string>();

// P4 取消中间态超时保护窗口：点停止后若 10s 内后端仍无 REPLY_END(halted)，本地强制收尾兜底。
const STOP_GUARD_MS = 10_000;

// ── AGENT_HALTED 图标映射 ─────────────────────────────────────────────────
// halt_kind → 图标，供前端按停机类别差异化渲染。
const HALT_KIND_ICON: Record<string, string> = {
  budget_exhausted:      "🛑",
  guard_halt:            "🛡️",
  max_steps:             "🚶",
  llm_error:             "⚠️",
  timeout:               "⏰",
  circuit_breaker:       "🔌",
  loop_detected:         "🔄",
  context_overflow:      "📊",
  tool_halt:             "🔧",
  tools_exhausted:        "🔧",
  permission_exhausted:  "🚫",
  hitl_rejected:         "🚫",
  audit_failure:         "🛡️",
  cancelled:             "✋",
};

// ── Context ────────────────────────────────────────────────────────────────

export interface SendMessageOptions {
  deepThinking?: boolean;
  modelId?: string;
  activeAppId?: string | null;
  mode?: AgentMode;
}

interface BackendContextValue {
  sendMessage: (content: string, options?: SendMessageOptions) => void;
  stopGeneration: () => void;
  sendHitlDecision: (runId: string, decision: "approved" | "rejected", approvalId: string, sessionId: string) => void;
  sendInteractionResponse: (runId: string, response: string, sessionId: string) => void;
  sendPlanApprovalDecision: (runId: string, planAction: "approve" | "refine" | "abandon", sessionId: string, userId: string | null, userText?: string, editedPlanContent?: string | null) => void;
  requestScheduledTasks: () => void;
  requestDashboard: () => void;
  deleteScheduledTask: (taskId: string) => void;
  searchRequest: (query: string) => void;
  requestSkillList: () => void;
  requestSkillDetail: (skillName: string) => void;
  saveSkill: (skillName: string, description: string, whenToUse: string, content: string, tags?: string[]) => void;
  deleteSkill: (skillName: string) => void;
  importSkill: (content: string, format: "zip" | "folder", overwrite?: boolean, sourcePath?: string) => void;
  exportSkill: (skillName: string, format: "zip" | "folder", targetPath?: string) => void;
  pendingTaskNotification: TaskNotificationMsg | null;
  clearTaskNotification: () => void;
  // ── 会话列表（v003）──────────────────────
  requestSessionList: (groupId: string | null, page: number, limit: number) => void;
  /** 分组详情页专用：拉取指定分组的会话，结果路由到 groupViewStore（不污染侧边栏） */
  requestGroupSessions: (groupId: string, page: number, limit: number) => void;
  createSession: () => void;
  switchSession: (targetSessionId: string) => void;
  deleteSession: (sessionId: string) => void;
  toggleFavoriteSession: (sessionId: string) => void;
  groupMutate: (payload: Record<string, unknown>) => void;
  requestSessionHistory: (sessionId: string, limit?: number) => void;
  // ── 预算额度（按 provider 分账）──────────────
  /** 设/改某 provider 额度（内部记账 USD，用户可设币种，默认 USD） */
  setBudget: (provider: string, currency: string, limitNative: number) => void;
  /** 查询全部 provider 额度态（额度条刷新） */
  budgetQuery: () => void;
}

const BackendContext = createContext<BackendContextValue | null>(null);

export function useBackend(): BackendContextValue {
  const ctx = useContext(BackendContext);
  if (!ctx) throw new Error("useBackend must be used inside <BackendProvider>");
  return ctx;
}

// ── Provider ───────────────────────────────────────────────────────────────

export function BackendProvider({ children }: { children: React.ReactNode }) {
  const [pendingTaskNotification, setPendingTaskNotification] = React.useState<TaskNotificationMsg | null>(null);
  const readyRef = useRef(false);
  const pendingCallbacksRef = useRef<Array<() => void>>([]);
  // 空状态下发送的消息：暂存于此，待 SESSION_SWITCHED 到达后自动补发
  const pendingSendRef = useRef<{ content: string; options?: SendMessageOptions } | null>(null);
  // P4 取消中间态超时保护：sessionId → setTimeout handle。收到后端 REPLY_END(halted)
  // 正常收尾即清；若 STOP_GUARD_MS 内后端仍无收尾，则本地强制收尾兜底（记 warning）。
  const stopGuardRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  const { setStatus, setError } = useConnectionStore();

  // 实际发送逻辑（sendMessage 与「空状态补发」共用）
  const doSend = useCallback(
    (sid: string, content: string, options?: SendMessageOptions) => {
      useChatStore.getState().addUserMessage(sid, content);
      invoke("send_message", {
        msgId: crypto.randomUUID(),
        content,
        deepThinking: options?.deepThinking ?? false,
        modelId: options?.modelId ?? "",
        activeAppId: options?.activeAppId ?? null,
        sessionId: sid,
        mode: options?.mode ?? null,
      }).catch((e) => console.error("[ipc] send_message failed:", e));
    },
    [],
  );

  // ── 消息分发 ────────────────────────────────────────────────────────────

  const handleIncoming = useCallback((msg: OutboundApiMessage) => {
    const chat = useChatStore.getState();

    /** 从 msg 拿 session_id；无则 fallback 到当前视图（保证不丢事件）。 */
    const sidOf = (m: OutboundApiMessage): string => {
      const raw = (m as { session_id?: string }).session_id;
      if (raw) return raw;
      return useSessionStore.getState().currentSessionId ?? "";
    };

    // debug: 追踪消息接收（可在生产环境注释掉）
    if (msg.type !== "TOKEN" && msg.type !== "REASONING_TOKEN") {
      console.debug("[ipc] recv", msg.type, {
        reply_id: (msg as any).reply_id,
        msg_id: (msg as any).msg_id,
        run_id: (msg as any).run_id,
        tool_name: (msg as any).tool_name,
        session_id: (msg as any).session_id,
      });
    }

    switch (msg.type) {
      case "REPLY_START": {
        // 每轮新对话清除旧激活状态（兜底，防止 SKILL_CLEARED IPC 丢失）
        useSkillStore.getState().clearActivatedSkill();
        const replyId = msg.reply_id ?? msg.msg_id;
        if (!replyId) {
          console.warn("[ipc] REPLY_START: missing reply_id / msg_id", msg);
          break;
        }
        const sid = sidOf(msg);
        if (!sid) {
          console.warn("[ipc] REPLY_START: no session_id, skipped");
          break;
        }
        chat.startStreaming(sid, replyId, msg.reply_scope);
        break;
      }

      case "TOKEN": {
        const replyId = msg.reply_id ?? msg.msg_id;
        if (!replyId) break;
        const sid = sidOf(msg);
        if (!sid) break;
        chat.appendToken(sid, replyId, msg.token);
        break;
      }

      case "REASONING_TOKEN": {
        const replyId = msg.reply_id ?? msg.msg_id;
        if (!replyId) break;
        const sid = sidOf(msg);
        if (!sid) break;
        chat.appendReasoningToken(sid, replyId, msg.token);
        break;
      }

      case "TOOL_START": {
        if (!msg.tool_name || !msg.tool_call_id) {
          console.warn("[ipc] TOOL_START: missing tool_name or tool_call_id", msg);
          break;
        }
        const sid = sidOf(msg);
        if (!sid) break;
        const buffer = useChatStore.getState().buffers.get(sid);
        const streaming = buffer
          ? [...buffer.messages].reverse().find((m) => m.kind === "streaming")
          : null;
        if (streaming) {
          chat.addToolCall(sid, streaming.id, msg);
        } else {
          const cat = categorizeTool(msg.tool_name);
          if (cat !== "infra" && cat !== "task_update") {
            console.warn("[ipc] TOOL_START: no streaming message for", msg.tool_name);
          }
        }
        // edit_file：解析文件路径后立即在查看器中打开，缓存原文用于后续 diff
        if (msg.tool_name === "edit_file" && msg.tool_args?.file_path) {
          const fp = String(msg.tool_args.file_path);
          void useFileStore.getState().loadAndOpenFile(fp); // fire-and-forget，不阻塞主流程

          const cached = useFileStore.getState().fileContents[fp];
          console.debug("[ipc] edit_file START", { fp, cached: cached != null, cachedLen: cached?.length ?? 0 });
          if (cached != null && !cached.startsWith("__ERROR__:")) {
            editFileOriginals.set(fp, cached);
          } else {
            // 缓存未命中 → 主动读盘兜底，避免 original 为空导致全文件标记为变更
            readTextFile(fp)
              .then((diskContent) => {
                console.debug("[ipc] edit_file START: read disk fallback", { fp, diskLen: diskContent.length });
                editFileOriginals.set(fp, diskContent);
              })
              .catch((e) => {
                console.warn("[ipc] edit_file START: cannot read original from disk", { fp, err: String(e) });
              });
          }
        }
        break;
      }

      case "TOOL_END": {
        const isError = msg.is_error === true;
        const sid = sidOf(msg);
        if (sid) {
          chat.finishToolCall(sid, msg.tool_call_id, {
            status: isError ? "error" : "done",
            result: {
              preview: msg.result_preview,
              full: msg.result_full ?? null,
              error: msg.result_error ?? null,
              mimeType: msg.result_mime_type,
              sizeBytes: msg.result_size_bytes,
              truncated: msg.result_truncated,
            },
            // 原样透传：渲染层按 severity/source 决定怎么显示，这里不解释、不判断。
            feedback: msg.feedback ?? null,
            durationMs: msg.duration_ms ?? null,
          });
        }

        // write_file：工具执行完文件已落盘 → 在查看器中打开
        if (!isError && msg.tool_name === "write_file" && msg.tool_args?.file_path) {
          const fp = String(msg.tool_args.file_path);
          void useFileStore.getState().loadAndOpenFile(fp); // fire-and-forget
        }

        // edit_file：读修改后的文件 → 触发 Accept/Reject Diff
        if (!isError && msg.tool_name === "edit_file" && msg.tool_args?.file_path) {
          const fp = String(msg.tool_args.file_path);
          readTextFile(fp)
            .then((suggested) => {
              // 连续多次 edit_file 同一文件时，必须沿用当前 suggestion 的 original 基线
              // （= 第一次编辑前的真实内容）；否则每次 TOOL_START 重新捕获的都是
              // 「上一次编辑后」的中间态，早期修改会被吞进 original 导致 diff 消失。
              const existingSuggestion = useFileStore.getState().suggestions[fp];
              // original 候选（按优先级）：
              //   1) existingSuggestion.original —— 连续编辑同一文件时的基线（最可靠，
              //      显示「初版 → 终版」累计 diff，不吞早期修改）
              //   2) reconstructOriginalFromResult —— 根治：用 TOOL_END 事件自带的
              //      unified diff + 磁盘 suggested 反推原文（零竞态：diff 由后端
              //      算好随事件下发，不依赖前端读盘时机）
              //   3) editFileOriginals.get(fp) / fileContents[fp] —— 读盘兜底
              //      （TOOL_START 读盘与后端写盘存在必然性竞态：后端 emit 后同步
              //      写盘 ≈1-3ms，事件到前端再读盘 ≈5-20ms，对未打开过的文件几乎
              //      总是晚于写盘，只能救「文件已打开/缓存命中」场景）
              const openedContent = useFileStore.getState().fileContents[fp];
              const sugOrig = existingSuggestion?.original;
              const reconstructed = reconstructOriginalFromResult(suggested, msg.result_full);
              const orig =
                (sugOrig && sugOrig !== suggested ? sugOrig : null) ??
                reconstructed ??
                pickOriginalCandidate(
                  [editFileOriginals.get(fp), openedContent],
                  suggested,
                );
              console.debug("[ipc] edit_file END", {
                fp,
                sugLen: suggested.length,
                hasSuggestion: existingSuggestion != null,
                hasDiff: msg.result_full != null,
                origFrom:
                  orig === sugOrig ? "suggestion" :
                  orig === reconstructed ? "diff-reconstruct" :
                  orig === editFileOriginals.get(fp) ? "start-fallback" :
                  orig === openedContent ? "opened-content" :
                  orig != null ? "fallback-first" : "none",
              });
              if (orig == null) {
                // 所有候选都拿不到原文 → 放弃 diff，避免空 original 导致全文件绿色误报
                console.warn("[ipc] edit_file END: no original available, skip diff", { fp });
                editFileOriginals.delete(fp);
                return;
              }
              // 空 original + 非空 suggested → 数据异常（文件不可能从空变为大段内容），放弃 diff
              if (orig.length === 0) {
                console.warn("[ipc] edit_file END: original is empty string (anomaly), skip diff", { fp });
                editFileOriginals.delete(fp);
                return;
              }
              const changed = suggested !== orig;
              console.debug("[ipc] edit_file DONE", { fp, changed, sugLen: suggested.length, origLen: orig.length });
              if (changed) {
                // 行尾归一化 original（防御）：磁盘 CRLF 文本的 \r 会污染 diff 比较；
                // suggested 保持磁盘原样（不归一化），避免 Monaco model 行尾变为 LF
                // 导致 Accept/保存时文件行尾漂移（CRLF → LF）。
                const normalizeEol = (s: string): string =>
                  s.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
                useFileStore.getState().showSuggestion(fp, normalizeEol(orig), suggested);
              } else {
                // original 与 suggested 相同（竞态失败：候选都拿到修改后内容）→ 显式留痕，
                // 不再静默——后续若频繁出现可考虑让后端事件携带编辑前原文根治。
                console.warn("[ipc] edit_file SKIP: candidates all match suggested (race likely), no diff", { fp });
              }
              editFileOriginals.delete(fp);
            })
            .catch((e) => {
              console.debug("[ipc] edit_file FAIL", { fp, err: String(e) });
              editFileOriginals.delete(fp);
            });
        }
        break;
      }

      case "REPLY_END": {
        const replyId = msg.reply_id ?? msg.msg_id;
        if (!replyId) {
          console.warn("[ipc] REPLY_END: missing reply_id / msg_id", msg);
          break;
        }
        const sid = sidOf(msg);
        if (!sid) break;
        // P4：后端权威收尾到达 → 清取消中间态超时保护，避免其在下一轮误触发。
        const g = stopGuardRef.current.get(sid);
        if (g) { clearTimeout(g); stopGuardRef.current.delete(sid); }
        chat.finishStreaming(sid, replyId, msg.output, msg.status === "halted", msg.usage);
        break;
      }

      case "HITL_REQUEST": {
        // 按 session 归档到 hitlStore（弹窗渲染唯一真相源，见 HitlModal → useCurrentPrompt）
        const sid = msg.session_id ?? sidOf(msg);
        if (sid && msg.approval_id) {
          useHitlStore.getState().addPrompt({
            approvalId: msg.approval_id,
            sessionId: sid,
            toolName: msg.tool_name ?? "",
            toolArgsSummary: msg.tool_args_summary ?? {},
            runId: msg.run_id ?? "",
            createdAt: Date.now(),
          });
        }
        break;
      }

      case "INTERACTION_REQUEST": {
        const runId = String(msg.run_id ?? "");
        const replyId = String(msg.reply_id ?? msg.msg_id);
        const questions = Array.isArray(msg.questions) ? msg.questions : [];
        const sid = sidOf(msg);
        if (!sid) break;
        const buffer = useChatStore.getState().buffers.get(sid);
        const target = buffer?.messages.find((m) => m.id === replyId);
        if (target && (target.kind === "streaming" || target.role === "assistant")) {
          chat.addQuestionnaire(
            sid, target.id, runId, questions,
            { request_id: msg.request_id, tool_name: msg.tool_name }
          );
        } else {
          console.warn("[ipc] INTERACTION_REQUEST: no target message for reply_id=" + replyId);
        }
        break;
      }

      case "PLAN_APPROVAL_REQUEST": {
        // 按 session 归档到 planApprovalStore（弹窗渲染唯一真相源，见 PlanApprovalModal
        // → useCurrentPlan），彻底消除「在 A 窗口弹出 B 会话计划审批」的跨 session 错弹。
        const planMsg = msg as PlanApprovalRequestMsg;
        const sid = planMsg.session_id ?? sidOf(msg);
        if (sid) {
          usePlanApprovalStore.getState().addPlan(sid, planMsg);
        } else {
          console.warn("[ipc] PLAN_APPROVAL_REQUEST: missing session_id, drop", planMsg);
        }
        break;
      }

      case "APPROVAL_RESULT": {
        const approvalId = (msg as { approval_id?: string }).approval_id;
        if (approvalId) {
          useHitlStore.getState().removePromptByApproval(approvalId);
        }
        break;
      }

      case "USER_INPUT_ECHO":
        if (msg.content) {
          const sid = sidOf(msg);
          if (sid) chat.addUserMessage(sid, msg.content);
        }
        break;

      case "TASK_NOTIFICATION": {
        // 5.2 扁平：直接传
        setPendingTaskNotification(msg);
        const sid = sidOf(msg);
        if (sid) {
          if (msg.level === "error") {
            chat.addSystemMessage(sid, `❌ ${msg.title}\n📋 ${msg.body ?? ""}`);
          } else {
            chat.addSystemMessage(sid, `⏰ ${msg.title}\n📋 ${msg.body ?? ""}`);
          }
        }
        break;
      }

      case "AGENT_HALTED":
      case "PERMISSION_DENIED": {
        console.warn("[ipc] agent event:", msg.type);
        const sid = sidOf(msg);
        if (sid) {
          // P4：清取消中间态超时保护（本事件即后端收尾信号之一）。
          const g = stopGuardRef.current.get(sid);
          if (g) { clearTimeout(g); stopGuardRef.current.delete(sid); }
          const buffer = useChatStore.getState().buffers.get(sid);
          const streaming = buffer
            ? [...buffer.messages].reverse().find((m) => m.kind === "streaming")
            : null;
          if (streaming) {
            chat.finishStreaming(sid, streaming.id, undefined, true);
          }
          if (msg.type === "AGENT_HALTED") {
            const halted = msg as AgentHaltedMsg;
            const icon = HALT_KIND_ICON[halted.halt_kind ?? ""] ?? "⏹";
            chat.addSystemMessage(
              sid,
              `${icon} ${halted.reason || "Agent 已停止"}`
            );
          }
        }
        break;
      }

      case "AGENT_REPLY": {
        const replyId = msg.reply_id ?? msg.msg_id;
        const sid = sidOf(msg);
        if (!sid) break;
        chat.startStreaming(sid, replyId);
        chat.finishStreaming(sid, replyId, msg.content);
        break;
      }

      case "AGENT_TASK_EVENT": {
        const atMsg = msg as AgentTaskEventMsg;
        const taskStore = useAgentTaskStore.getState();
        if (atMsg.event === "deleted") {
          taskStore.removeTask(atMsg.task_id);
        } else if (atMsg.task) {
          taskStore.upsertTask(atMsg.task);
        }
        break;
      }

      case "QUICK_APP_DATA":
        // Quick App framework removed; event silently consumed
        break;

      case "SKILL_PROGRESS": {
        const spMsg = msg as SkillProgressEventMsg;
        const sid = sidOf(msg);
        if (!sid || !spMsg.activity) break;
        chat.applySkillProgress(sid, {
          activity: spMsg.activity,
          phase: spMsg.phase,
          status: spMsg.status,
        });
        break;
      }

      case "ERROR":
        if (msg.error_message) {
          chat.addSystemMessage(sidOf(msg), `⚠️ ${msg.error_message}`);
        }
        // 宠物出错反应（failed 动作）
        usePetStore.getState().pulse("failed", 2000);
        break;

      case "PONG":
        break;

      // ── JWT 自动续期（全局级控制面消息，不带 session_id）──
      case "AUTH_TOKEN_REFRESHED": {
        // Gateway refresh 成功 → 回写 auth_store.json + 同步前端内存 token。
        // 回写失败仅影响下次冷启动体验（需重登一次），当前会话不受影响。
        const rMsg = msg as AuthTokenRefreshedMsg;
        if (rMsg.token) {
          invoke("auth_update_token", { token: rMsg.token }).catch((e) =>
            console.error("[ipc] auth_update_token failed:", e),
          );
          useAuthStore.setState({ token: rMsg.token });
        }
        break;
      }

      case "AUTH_EXPIRED": {
        // Refresh 被 Relay 401 拒绝（超宽限期/签名无效）→ 登出；
        // status=unauthenticated 驱动路由守卫自动跳登录页，
        // error 横幅在登录页展示「登录已过期，请重新登录」。
        console.warn("[ipc] AUTH_EXPIRED received, logging out");
        useAuthStore.setState({ error: "登录已过期，请重新登录" });
        void useAuthStore.getState().logout();
        break;
      }

      case "SCHEDULED_TASK_LIST": {
        // D1 Pull 响应 / D2 Push 全量
        const listMsg = msg as ScheduledTaskListMsg;
        useTaskSchedulerStore.getState().replaceAll(listMsg.tasks ?? []);
        break;
      }

      case "DASHBOARD_DATA": {
        const dashMsg = msg as DashboardDataMsg;
        useDashboardStore.getState().setSnapshot({
          global: dashMsg.global,
          sessions: dashMsg.sessions ?? [],
          degradations: dashMsg.degradations ?? [],
        });
        break;
      }

      case "BUDGET_STATUS": {
        const bMsg = msg as BudgetStatusMsg;
        useBudgetStore.getState().setBudgets(bMsg.budgets ?? []);
        break;
      }

      case "MODEL_LIST": {
        const mMsg = msg as ModelListMsg;
        // 规则 2：展示名 = model_id 本身（系统不内置模型数据，不做美化映射）。
        // 后端 to_model_list_payload 已下发 display_name=model_id；兜底用 model_id。
        // 删除旧 .filter(m => m.enabled)：无白名单了，下发的全部可用。
        // ⚠️ price_source 仅用于标注（「待补价」徽标），**不得**据此过滤清单，
        //    否则就是 _DECLARED_MODELS 白名单换马甲复活（PRD R11 / AC-06）。
        const models = (mMsg.models ?? []).map((m) => ({
          id: m.model_id,
          name: m.display_name || m.model_id,
          provider: m.provider,
          priceSource: m.price_source,
        }));
        useModelStore
          .getState()
          .setAvailableModels(models, mMsg.default_model_id ?? "");
        break;
      }

      // ── LLM 凭据管理 出站消息处理 ──
      case "CREDENTIALS_LIST": {
        console.log("[credentials-ipc] <<< CREDENTIALS_LIST", msg);
        const cMsg = msg as CredentialsListMsg;
        useCredentialStore
          .getState()
          ._setCredentialsFromBackend(cMsg.credentials as any);
        break;
      }

      case "CREDENTIALS_SAVED": {
        console.log("[credentials-ipc] <<< CREDENTIALS_SAVED", msg);
        const cMsg = msg as CredentialsSavedMsg;
        useCredentialStore
          .getState()
          ._setSaveResult(cMsg.success, cMsg.error);
        break;
      }

      case "CREDENTIALS_VERIFIED": {
        console.log("[credentials-ipc] <<< CREDENTIALS_VERIFIED", msg);
        const cMsg = msg as CredentialsVerifiedMsg;
        const results: VerifyResult[] = (cMsg.results ?? []).map((r) => ({
          provider: r.provider as LLMProvider,
          status: r.success ? "passed" as const : "failed" as const,
          error: r.error,
        }));
        // 传入后端权威 success：results 为空时不得推导出「全部通过」
        useCredentialStore.getState()._setVerifyResults(results, !!cMsg.success);
        break;
      }

      case "CREDENTIALS_STATUS": {
        console.log("[credentials-ipc] <<< CREDENTIALS_STATUS", msg);
        const cMsg = msg as CredentialsStatusMsg;
        useCredentialStore.getState()._updateStatus({
          configured: cMsg.configured,
          credential_count: cMsg.credential_count,
          default_model_id: cMsg.default_model_id ?? null,
          // 这两个只有后端判得出（v1 文件前端读不到、默认解析要比对清单），
          // 漏传即等于后端白判一场（协议契约：CLAUDE.md §七）
          legacy_format: cMsg.legacy_format,
          default_resolvable: cMsg.default_resolvable,
        });
        break;
      }

      case "SCHEDULED_TASK_CHANGED": {
        // D2 Push 增量：单任务变更
        const changedMsg = msg as ScheduledTaskChangedMsg;
        if (changedMsg.change_type === "deleted") {
          useTaskSchedulerStore.getState().removeTask(changedMsg.task.task_id);
        } else {
          useTaskSchedulerStore.getState().upsertTask(changedMsg.task);
        }
        break;
      }

      case "SEARCH_RESULT": {
        const searchMsg = msg as SearchResultMsg;
        useSearchStore.getState().setResult(
          searchMsg.query ?? "",
          searchMsg.sessions ?? [],
          searchMsg.messages ?? [],
        );
        break;
      }

      case "SKILL_LIST_RESULT": {
        const skillListMsg = msg as SkillListResultMsg;
        // 补全可能缺失的新字段默认值
        const skills = (skillListMsg.skills ?? []).map((s) => ({
          ...s,
          type: s.type ?? "KNOWLEDGE",
          allow_auto_trigger: s.allow_auto_trigger ?? true,
        }));
        useSkillStore.getState().replaceAll(skills);
        break;
      }

      case "SKILL_GET_RESULT": {
        // 详情写入 store：列表行 + 详情面板
        const detailMsg = msg as SkillGetResultMsg;
        const skillItem = {
          name: detailMsg.skill_name,
          description: detailMsg.description,
          when_to_use: detailMsg.when_to_use,
          tags: detailMsg.tags ?? [],
          source: detailMsg.source,
          size: detailMsg.size,
          modified_at: detailMsg.modified_at,
          from_cache: detailMsg.from_cache,
          type: detailMsg.skill_type ?? "KNOWLEDGE",
          allow_auto_trigger: detailMsg.allow_auto_trigger ?? true,
        };
        useSkillStore.getState().upsertSkill(skillItem);
        useSkillStore.getState().setDetailSkill({
          ...skillItem,
          content: detailMsg.content ?? "",
        });
        break;
      }

      case "SKILL_SAVED": {
        // 后端已保存成功 → 自动刷新 Skill 列表
        invoke("request_skill_list", {
          msgId: crypto.randomUUID(),
        }).catch((e) => console.error("[ipc] auto-refresh skill list failed:", e));
        break;
      }

      case "SKILL_DELETED": {
        // 后端已删除成功 → 自动刷新 Skill 列表 + toast
        const deletedMsg = msg as SkillDeletedMsg;
        const skillName = deletedMsg.skill_name ?? "";
        invoke("request_skill_list", {
          msgId: crypto.randomUUID(),
        }).catch((e) => console.error("[ipc] auto-refresh skill list failed:", e));
        toast.success("已删除", skillName);
        break;
      }

      case "SKILL_ACTIVATED": {
        const activatedMsg = msg as SkillActivatedMsg;
        useSkillStore.getState().setActivatedSkill({
          skill_name: activatedMsg.skill_name,
          skill_type: activatedMsg.skill_type,
          tools: activatedMsg.tools,
        });
        break;
      }

      case "SKILL_CLEARED": {
        useSkillStore.getState().clearActivatedSkill();
        break;
      }

      case "SKILL_IMPORTED": {
        const importedMsg = msg as SkillImportedMsg;
        if (importedMsg.success) {
          invoke("request_skill_list", {
            msgId: crypto.randomUUID(),
          }).catch((e) => console.error("[ipc] auto-refresh after import failed:", e));
          toast.success("导入成功", importedMsg.skill_name);
        } else {
          toast.error(`导入失败: ${importedMsg.error ?? "未知错误"}`);
        }
        break;
      }

      case "SKILL_EXPORTED": {
        const exportedMsg = msg as SkillExportedMsg;
        toast.success("导出成功", exportedMsg.file_path);
        break;
      }

      case "SESSION_CONCURRENCY": {
        const cMsg = msg as SessionConcurrencyMsg;
        useSessionConcurrencyStore.getState().update(cMsg.session_id, {
          status: cMsg.status,
          queue_position: cMsg.queue_position,
          queue_length: cMsg.queue_length,
          running_count: cMsg.running_count,
          max_concurrent: cMsg.max_concurrent,
        });
        break;
      }

      // ── 会话列表（v003）──────────────────────────
      case "SESSION_LIST": {
        const listMsg = msg as SessionListMsg;
        const gid = listMsg.group_id;
        // 路由不变式：侧边栏只请求 "all" / ""(无分组) / null；
        // 分组详情页请求具体分组 id（grp-xxx）。据此分流，避免竞态串扰：
        //   - 具体分组 id 的响应 → 仅当与当前详情页 groupId 一致时写入 groupViewStore；
        //     否则是「已切走的陈旧响应」，直接丢弃，绝不污染侧边栏。
        //   - "all" / "" / null → 侧边栏对话列表。
        if (gid && gid !== "all") {
          const gvs = useGroupViewStore.getState();
          if (gid === gvs.groupId) {
            const page = listMsg.page ?? 1;
            if (page > 1) {
              gvs.appendSessions(listMsg.sessions ?? [], !!listMsg.has_more);
            } else {
              gvs.setSessions(gid, listMsg.sessions ?? [], !!listMsg.has_more, page);
            }
          }
          // else：陈旧分组响应，丢弃
          break;
        }
          useSessionStore.getState().setSessions(
            listMsg.sessions ?? [],
            !!listMsg.has_more,
          listMsg.page ?? 1,
          );
        // group_filter 只在前端主动 REQUEST 时才切；这里不覆盖
        break;
      }

      case "SESSION_SWITCHED": {
        const swMsg = msg as SessionSwitchedMsg;
        useSessionStore.getState().switchSession(swMsg.session_id);
        useChatStore.getState().setContextStatus(
          swMsg.session_id, swMsg.context_status,
        );
        if (swMsg.context_status === "degraded") {
          console.warn(
            "[ipc] SESSION_SWITCHED degraded, context lost for %s",
            swMsg.session_id,
          );
        }
        // 切入已有会话（非全新空 session）且本地 buffer 尚未加载消息时，
        // 主动拉取 raw_log 历史回补——否则重启/切换后聊天区空白。
        // fresh = 全新空 session，无历史可拉；restored/degraded 都可能有历史。
        if (swMsg.session_id && swMsg.context_status !== "fresh") {
          const buf = useChatStore.getState().buffers.get(swMsg.session_id);
          if (!buf || buf.messages.length === 0) {
            sendSessionIpc({
              type: "SESSION_HISTORY_REQUEST",
              msg_id: crypto.randomUUID(),
              session_id: swMsg.session_id,
              limit: 50,
            });
          }
        }
        // 补发：空状态下用户发的消息在此刻拿到会话，自动发送
        const queued = pendingSendRef.current;
        if (queued && swMsg.session_id) {
          pendingSendRef.current = null;
          doSend(swMsg.session_id, queued.content, queued.options);
        }
        break;
      }

      case "SESSION_UPDATED": {
        const uMsg = msg as SessionUpdatedMsg;
        if (uMsg.session_info) {
          useSessionStore.getState().upsertSession(uMsg.session_info);
          // 分组详情页同步（命中当前分组则 upsert，被移出则移除）
          useGroupViewStore.getState().upsertSession(uMsg.session_info);
        }
        break;
      }

      case "SESSION_DELETED": {
        const dMsg = msg as SessionDeletedMsg;
        useSessionStore.getState().dropSession(dMsg.session_id);
        useGroupViewStore.getState().dropSession(dMsg.session_id);
        useChatStore.getState().clearSession(dMsg.session_id);
        useHitlStore.getState().dropSession(dMsg.session_id);
        usePlanApprovalStore.getState().dropSession(dMsg.session_id);
        if (dMsg.routing) {
          useSessionStore.getState().applyRouting(dMsg.routing);
          const nextSessionId =
            dMsg.routing.action === "switch" && dMsg.routing.target_session_id
              ? dMsg.routing.target_session_id
              : dMsg.routing.action === "empty_state"
                ? ""
                : null;
          if (nextSessionId !== null) {
            invoke("set_current_session_id", { sessionId: nextSessionId }).catch(
              (e) => console.error("[ipc] set_current_session_id after delete failed:", e),
            );
          }
        }
        break;
      }

      case "SESSION_GROUP_LIST": {
        const gMsg = msg as SessionGroupListMsg;
        useSessionStore.getState().setGroups(gMsg.groups ?? []);
        break;
      }

      case "SESSION_HISTORY_LIST": {
        const hMsg = msg as SessionHistoryListMsg;
        if (hMsg.session_id && Array.isArray(hMsg.messages)) {
          useChatStore.getState().loadHistory(hMsg.session_id, hMsg.messages);
        }
        break;
      }
    }
    // sendSessionIpc 引用于 SESSION_SWITCHED 历史回补：其身份稳定（useCallback []），
    // 且在 handleIncoming 之后声明，不能进依赖数组（会触发 const TDZ）。故此处刻意省略。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [doSend]);

  // ── IPC 生命周期 ────────────────────────────────────────────────────────
  //
  // 设计要点（与 AuthGuard → WorkspaceGate → CredentialGate 的依赖链匹配）：
  //   - 本 Provider 不主动启动 sidecar。sidecar 的启动入口是 Rust 端的
  //     `start_sidecar` 命令，由 CredentialGate 在 LLM 凭据检查通过后触发。
  //   - 本 Provider 只做两件事：
  //       1. 挂监听 `backend-event` / `backend-crashed` / `backend-ready`
  //       2. 收到 `backend-ready` 后，标记 ready、冲刷排队回调、发 MODEL_LIST_REQUEST
  //          和 LOAD_CREDENTIALS、必要时兜底建会话
  //   - `backend-ready` 由 Rust `spawn_sidecar` 在 sidecar 打印 `PANDAPAL_READY` 后
  //     emit；若 sidecar 已在运行（热重启），`spawn_sidecar` 会重新 emit 一次，本
  //     Provider 仍能正确收到。
  //   - `onBackendReady` 做幂等保护：重复触发不会重复发 LOAD_CREDENTIALS 等。

  useEffect(() => {
    const abortController = new AbortController();
    const cleanups: (() => void)[] = [];

    // sidecar 就绪后的统一收尾动作（幂等）。
    // 抽出来是因为 backend-ready 可能在多条路径下触发：
    //   1. CredentialGate 首次调 start_sidecar → spawn_sidecar 新启动 → PANDAPAL_READY → emit
    //   2. sidecar 已存活（热重启）→ start_sidecar → spawn_sidecar 走「已运行」分支 re-emit
    // 两条路径都走同一个收尾，避免逻辑分叉。
    const onBackendReady = (token: string, model?: string, provider?: string) => {
      if (abortController.signal.aborted) return;
      if (readyRef.current) {
        // 已就绪过：backend-ready 重复到达，忽略。
        return;
      }
      console.log("[ipc] backend ready, token length=", token.length);

      // 后端握手携带的真实模型信息 → 注入 modelStore（无则显示 No Model）
      useModelStore.getState().setBackendModel(model ?? "", provider);

      readyRef.current = true;
      setStatus("connected");

      // 冲刷所有在 IPC 未就绪期间排队的回调
      const pending = pendingCallbacksRef.current;
      pendingCallbacksRef.current = [];
      pending.forEach((cb) => cb());

      // 模型选择：ready 后主动拉取可选模型清单（MODEL_LIST_REQUEST → 后端回 MODEL_LIST）。
      // 拉取而非等推送——IPC 消息无重放，拉取由前端控制时序，规避推送早于订阅丢失。
      invoke("send_session_ipc", {
        payload: { type: "MODEL_LIST_REQUEST", msg_id: crypto.randomUUID() },
      }).catch((e) =>
        console.error("[ipc] MODEL_LIST_REQUEST failed:", e),
      );

      // BYOK：ready 后加载用户 LLM 凭据 → CREDENTIALS_LIST → 存入 credentialStore
      // 这是 CredentialGate 把 credLoading 置为 false 的唯一来源。
      console.log("[credentials-ipc] ===== backend-ready, requesting credentials =====");
      const loadMsgId = crypto.randomUUID();
      console.log("[credentials-ipc] >>> LOAD_CREDENTIALS msg_id=", loadMsgId);
      invoke("send_session_ipc", {
        payload: { type: "LOAD_CREDENTIALS", msg_id: loadMsgId },
      })
        .then(() => console.log("[credentials-ipc] >>> LOAD_CREDENTIALS invoke OK"))
        .catch((e) => {
          console.error("[credentials-ipc] >>> LOAD_CREDENTIALS invoke FAILED:", e);
          // 失败时释放 loading，避免 CredentialGate 永远卡在「正在检查配置...」
          useCredentialStore.getState().setLoading(false);
        });

      // 兜底：sidecar 启动引导会在 PANDAPAL_READY 之前广播启动首屏
      // （SESSION_LIST / SESSION_GROUP_LIST / SESSION_SWITCHED），但若进程存活
      // 已久（热重启）不会再广播 SESSION_SWITCHED，此时若本地无活动会话，主动建一个
      // （后端会复用已有空会话，不产生重复）。
      if (!useSessionStore.getState().currentSessionId) {
        invoke("send_session_ipc", {
          payload: { type: "SESSION_CREATE", msg_id: crypto.randomUUID() },
        }).catch((e) =>
          console.error("[ipc] startup SESSION_CREATE failed:", e),
        );
      }
    };

    const setup = async () => {
      try {
        console.log("[ipc] setup: starting");
        setStatus("connecting");

        // 先注册 backend-event / backend-crashed，再注册 backend-ready。
        // 关键：后端 bootstrap 会在打印 PANDAPAL_READY 之前就广播启动首屏
        // （SESSION_LIST / SESSION_GROUP_LIST / SESSION_SWITCHED）。若等 ready
        // 之后再挂监听，这些一次性广播会在监听前发出而被漏掉，导致
        // currentSessionId 一直为 null（发送时报 "no currentSessionId"）。
        const unlistenEvent = await listen<string>("backend-event", (event) => {
          console.log("[ipc] backend-event received:", event.payload?.slice(0, 100));
          let msg: OutboundApiMessage;
          try {
            msg = JSON.parse(event.payload) as OutboundApiMessage;
          } catch {
            console.warn("[ipc] invalid event payload:", event.payload);
            return;
          }
          try {
            handleIncoming(msg);
          } catch (err) {
            console.error("[ipc] handleIncoming threw for", msg.type, msg, err);
          }
        });
        cleanups.push(unlistenEvent);

        const unlistenCrash = await listen<string>("backend-crashed", (event) => {
          console.error("[ipc] backend-crashed:", event.payload);
          setError(`后端进程崩溃：${event.payload}`);
          setStatus("closed");
          readyRef.current = false;
          // 释放 credLoading：避免 CredentialGate 在 sidecrashed 后仍卡在 loading
          // （虽然新 CredentialGate 不再依赖 credLoading，但保留兼容）
          useCredentialStore.getState().setLoading(false);
        });
        cleanups.push(unlistenCrash);

        // 监听 backend-ready：由 CredentialGate 触发的 start_sidecar → spawn_sidecar
        // 在 sidecar 打印 PANDAPAL_READY 后 emit。本 Provider 不主动启动 sidecar。
        const unlistenReady = await listen<{ token: string; model?: string; provider?: string }>(
          "backend-ready",
          (event) => {
            onBackendReady(event.payload.token, event.payload.model, event.payload.provider);
          },
        );
        cleanups.push(unlistenReady);

        // 初始连接状态重置：避免旧 sidecar 被 kill 后 "connected" 残留导致
        // CredentialGate 误判。connectionStore 由本 Provider 单一写入。
        //
        // ⚠️ 这里**不能**用 get_auth_token 的返回值判断 sidecar 是否在运行：
        //    PANDAPAL_READY 握手不带 token（run_local.py 只写 model=/provider=），
        //    BackendToken 恒为空串，据此判断必然得出「未运行」。热重启的重新握手
        //    由 spawn_sidecar 的「已在运行 → 无条件 re-emit backend-ready」分支
        //    保证（见 sidecar.rs），本处不再自行推断。
        //
        // readyRef 守卫：backend-ready 有可能在本行之前就已到达（CredentialGate
        // 与本 effect 并发），此时不得把 connected 打回 waiting。
        if (!readyRef.current) {
          setStatus("waiting");
        }
      } catch (err) {
        console.error("[ipc] setup failed:", err);
        setError(`IPC 初始化失败：${err}`);
        setStatus("closed");
      }
    };

    setup().catch((e) => {
      console.error("[ipc] setup() threw:", e);
      setStatus("error");
      setError(`IPC setup failed: ${e}`);
    });

    return () => {
      abortController.abort();
      cleanups.forEach((fn) => fn());
      readyRef.current = false;
      pendingCallbacksRef.current = [];
    };
  }, [setStatus, setError, handleIncoming]);

  // ── 发送方法 ────────────────────────────────────────────────────────────

  const sendMessage = useCallback((content: string, options?: SendMessageOptions) => {
    if (!readyRef.current) {
      console.warn("[ipc] not ready, cannot send");
      return;
    }
    const sid = useSessionStore.getState().currentSessionId ?? "";
    if (!sid) {
      // 空状态（无活动会话，如删光全部会话后）：不静默丢弃，
      // 而是暂存本条消息 + 主动建会话，待 SESSION_SWITCHED 到达后自动补发。
      console.debug("[ipc] sendMessage: no session, creating one and queuing");
      pendingSendRef.current = { content, options };
      invoke("send_session_ipc", {
        payload: { type: "SESSION_CREATE", msg_id: crypto.randomUUID() },
      }).catch((e) => console.error("[ipc] SESSION_CREATE failed:", e));
      return;
    }
    doSend(sid, content, options);
  }, [doSend]);

  const stopGeneration = useCallback(() => {
    if (!readyRef.current) {
      console.warn("[ipc] not ready, cannot stop");
      return;
    }
    // ★ 必须显式指定要停止的 session：否则 Rust 会回退到「当前正在看的会话」全局单例，
    //   在多会话并发下可能停掉别的会话正在跑的长任务（跨会话误杀）。
    const sid = useSessionStore.getState().currentSessionId;
    if (!sid) {
      console.warn("[ipc] stop_generation: no current session, skip");
      return;
    }
    // P4：不再「本地立即 finishStreaming」（那会造成前后端错位假象），改为进入取消
    //     中间态，等后端 REPLY_END(halted) / AGENT_HALTED 事件驱动真正收尾（见契约 §7）。
    useChatStore.getState().markStopping(sid);
    invoke("stop_generation", {
      msgId: crypto.randomUUID(),
      sessionId: sid,
    }).catch((e) => console.error("[ipc] stop_generation failed:", e));
    console.debug("[ipc] stop_generation sent session=" + sid);

    // 超时保护：STOP_GUARD_MS 内后端仍无收尾事件 → 本地强制收尾兜底，避免卡死在 loading。
    const guards = stopGuardRef.current;
    const existing = guards.get(sid);
    if (existing) clearTimeout(existing);
    guards.set(
      sid,
      setTimeout(() => {
        guards.delete(sid);
        const buffer = useChatStore.getState().buffers.get(sid);
        const streaming = buffer
          ? [...buffer.messages].reverse().find((m) => m.kind === "streaming")
          : null;
        if (streaming) {
          console.warn(`[ipc] stop_generation: no backend REPLY_END within ${STOP_GUARD_MS}ms, forcing local finalize session=${sid}`);
          useChatStore.getState().finishStreaming(sid, streaming.id, undefined, true);
        }
      }, STOP_GUARD_MS),
    );
  }, []);

  const sendHitlDecision = useCallback(
    (runId: string, decision: "approved" | "rejected", approvalId: string, sessionId: string) => {
      // ★ 只用「被操作的那条 prompt 自带的 sessionId」，缺失即报错——绝不回退到当前视图。
      //   遵循 SESSION_ID 契约：不降级、不用语义不同的 id 替代。
      if (!sessionId) {
        console.error("[ipc] send_hitl_decision: missing sessionId, abort");
        return;
      }
      invoke("send_hitl_decision", {
        msgId: crypto.randomUUID(),
        runId,
        decision,
        approvalId,
        sessionId,
      }).catch((e) => console.error("[ipc] send_hitl_decision failed:", e));
      // 乐观移除（APPROVAL_RESULT 到达时也会移除，idempotent）
      useHitlStore.getState().removePromptByApproval(approvalId);
    },
    []
  );

  const sendInteractionResponse = useCallback(
    (runId: string, response: string, sessionId: string) => {
      // ★ 必须携带问卷所属 sessionId：否则 Rust 会回退到「当前正在看的会话」全局单例，
      //   用户切换会话后作答会串到别的会话（与 plan 审批同源的跨会话污染）。
      if (!sessionId) {
        console.error("[ipc] send_interaction_response: missing sessionId, abort");
        return;
      }
      invoke("send_interaction_response", {
        msgId: crypto.randomUUID(),
        runId,
        response,
        sessionId,
      }).catch((e) => console.error("[ipc] send_interaction_response failed:", e));
    },
    []
  );

  const sendPlanApprovalDecision = useCallback(
    (runId: string, planAction: "approve" | "refine" | "abandon", sessionId: string, userId: string | null, userText?: string, editedPlanContent?: string | null) => {
      // ★ sessionId 由调用方（PlanApprovalModal）从「被操作的那条计划自带的 session_id」显式传入，
      //   缺失即报错——绝不回退到当前视图。遵循 SESSION_ID 契约：不降级、不替代。
      if (!sessionId) {
        console.error("[ipc] send_plan_approval_decision: missing sessionId, abort");
        return;
      }
      invoke("send_plan_approval_decision", {
        msgId: crypto.randomUUID(),
        runId,
        planAction: planAction,
        sessionId,
        userId: userId ?? null,
        userText: userText ?? "",
        editedPlanContent: editedPlanContent ?? null,
      }).catch((e) => console.error("[ipc] send_plan_approval_decision failed:", e));
      // 乐观移除该 session 的待审批计划（决策已发出）
      usePlanApprovalStore.getState().removeBySession(sessionId);
    },
    []
  );

  const requestScheduledTasks = useCallback(() => {
    if (!readyRef.current) {
      console.warn("[ipc] not ready, cannot request scheduled tasks - queued for retry");
      useTaskSchedulerStore.getState().setLoading(true);
      pendingCallbacksRef.current.push(() => {
        invoke("request_scheduled_tasks", {
          msgId: crypto.randomUUID(),
        }).catch((e) => console.error("[ipc] request_scheduled_tasks failed:", e));
      });
      return;
    }
    useTaskSchedulerStore.getState().setLoading(true);
    invoke("request_scheduled_tasks", {
      msgId: crypto.randomUUID(),
    }).catch((e) => console.error("[ipc] request_scheduled_tasks failed:", e));
  }, []);

  const requestDashboard = useCallback(() => {
    const fire = () =>
      invoke("request_dashboard", { msgId: crypto.randomUUID() })
        .catch((e) => console.error("[ipc] request_dashboard failed:", e));
    useDashboardStore.getState().setLoading(true);
    if (!readyRef.current) {
      pendingCallbacksRef.current.push(fire);
      return;
    }
    fire();
  }, []);

  const deleteScheduledTask = useCallback((taskId: string) => {
    if (!taskId) return;
    if (!readyRef.current) {
      console.warn("[ipc] not ready, cannot delete scheduled task");
      return;
    }
    invoke("delete_scheduled_task", {
      msgId: crypto.randomUUID(),
      taskId,
    }).catch((e) => console.error("[ipc] delete_scheduled_task failed:", e));
  }, []);

  const searchRequest = useCallback((query: string) => {
    const q = query.trim();
    // 记录当前查询词（过期响应保护）；空词直接清空、不发请求
    if (!q) {
      useSearchStore.getState().clear();
      return;
    }
    useSearchStore.getState().beginQuery(q);
    if (!readyRef.current) {
      console.warn("[ipc] not ready, cannot search - dropped");
      return;
    }
    invoke("search_request", {
      msgId: crypto.randomUUID(),
      query: q,
    }).catch((e) => console.error("[ipc] search_request failed:", e));
  }, []);

  const requestSkillList = useCallback(() => {
    if (!readyRef.current) {
      console.warn("[ipc] not ready, cannot request skill list - queued for retry");
      useSkillStore.getState().setLoading(true);
      pendingCallbacksRef.current.push(() => {
        invoke("request_skill_list", {
          msgId: crypto.randomUUID(),
        }).catch((e) => console.error("[ipc] skill_list failed:", e));
      });
      return;
    }
    useSkillStore.getState().setLoading(true);
    invoke("request_skill_list", {
      msgId: crypto.randomUUID(),
    }).catch((e) => console.error("[ipc] skill_list failed:", e));
  }, []);

  const requestSkillDetail = useCallback((skillName: string) => {
    const fire = () => {
      // 清空旧详情 + 置加载态，避免响应到达前渲染旧 skill 内容或误报"未找到技能"
      useSkillStore.getState().setDetailSkill(null);
      useSkillStore.getState().setDetailLoading(true);
      invoke("request_skill_detail", {
        msgId: crypto.randomUUID(),
        skillName,
      }).catch((e) => {
        console.error("[ipc] skill_get failed:", e);
        useSkillStore.getState().setDetailLoading(false);
      });
    };
    if (!readyRef.current) {
      // 对齐 requestSkillList：IPC 未就绪时排队，ready 后自动补发（此前直接丢弃导致首次详情永久丢失）
      console.warn("[ipc] not ready, cannot request skill detail - queued for retry");
      pendingCallbacksRef.current.push(fire);
      return;
    }
    fire();
  }, []);

  const saveSkill = useCallback((skillName: string, description: string, whenToUse: string, content: string, tags?: string[]) => {
    if (!readyRef.current) {
      console.warn("[ipc] not ready, cannot save skill");
      return;
    }
    invoke("save_skill", {
      msgId: crypto.randomUUID(),
      skillName,
      description,
      whenToUse,
      content,
      tags,
    }).catch((e) => console.error("[ipc] skill_save failed:", e));
  }, []);

  const deleteSkill = useCallback((skillName: string) => {
    if (!readyRef.current) {
      console.warn("[ipc] not ready, cannot delete skill");
      return;
    }
    invoke("delete_skill", {
      msgId: crypto.randomUUID(),
      skillName,
    }).catch((e) => console.error("[ipc] skill_delete failed:", e));
  }, []);

  const importSkill = useCallback((content: string, format: "zip" | "folder", overwrite?: boolean, sourcePath?: string) => {
    if (!readyRef.current) {
      console.warn("[ipc] not ready, cannot import skill");
      return;
    }
    invoke("import_skill", {
      msgId: crypto.randomUUID(),
      content,
      format,
      overwrite,
      sourcePath,
    }).catch((e) => console.error("[ipc] skill_import failed:", e));
  }, []);

  const exportSkill = useCallback((skillName: string, format: "zip" | "folder", targetPath?: string) => {
    if (!readyRef.current) {
      console.warn("[ipc] not ready, cannot export skill");
      return;
    }
    invoke("export_skill", {
      msgId: crypto.randomUUID(),
      skillName,
      format,
      targetPath,
    }).catch((e) => console.error("[ipc] skill_export failed:", e));
  }, []);

  const clearTaskNotification = useCallback(() => setPendingTaskNotification(null), []);

  // ── 会话列表（v003）─────────────────────────────────────────
  const sendSessionIpc = useCallback((payload: Record<string, unknown>) => {
    if (!readyRef.current) {
      console.warn("[ipc] not ready, cannot send session ipc");
      return;
    }
    invoke("send_session_ipc", { payload }).catch((e) =>
      console.error("[ipc] send_session_ipc failed:", e),
    );
  }, []);

  const requestSessionList = useCallback(
    (groupId: string | null, page: number, limit: number) => {
      sendSessionIpc({
        type: "SESSION_LIST_REQUEST",
        msg_id: crypto.randomUUID(),
        group_id: groupId,
        page,
        limit,
      });
    },
    [sendSessionIpc],
  );

  const requestGroupSessions = useCallback(
    (groupId: string, page: number, limit: number) => {
      // 路由由 SESSION_LIST 处理器按 group_id 与 groupViewStore.groupId 匹配决定，
      // 无需额外标记。reset 会把 groupViewStore.groupId 置为目标分组。
      if (page <= 1) useGroupViewStore.getState().reset(groupId);
      else useGroupViewStore.getState().setLoading(true);
      sendSessionIpc({
        type: "SESSION_LIST_REQUEST",
        msg_id: crypto.randomUUID(),
        group_id: groupId,
        page,
        limit,
      });
    },
    [sendSessionIpc],
  );

  const createSession = useCallback(() => {
    sendSessionIpc({
      type: "SESSION_CREATE",
      msg_id: crypto.randomUUID(),
    });
  }, [sendSessionIpc]);

  const switchSession = useCallback(
    (targetSessionId: string) => {
      if (!targetSessionId) return;
      // 乐观：立刻切前端视图
      useSessionStore.getState().switchSession(targetSessionId);
      // Rust 侧同步 session_id（下次 send_message 会用它）
      invoke("set_current_session_id", { sessionId: targetSessionId }).catch(
        (e) => console.error("[ipc] set_current_session_id failed:", e),
      );
      // 通知后端：广播 SESSION_SWITCHED 携带 context_status
      sendSessionIpc({
        type: "SESSION_SWITCH",
        msg_id: crypto.randomUUID(),
        target_session_id: targetSessionId,
      });
    },
    [sendSessionIpc],
  );

  const deleteSession = useCallback(
    (sessionId: string) => {
      const current = useSessionStore.getState().currentSessionId;
      sendSessionIpc({
        type: "SESSION_DELETE",
        msg_id: crypto.randomUUID(),
        session_id: sessionId,
        current_view_session_id: current,
      });
    },
    [sendSessionIpc],
  );

  const toggleFavoriteSession = useCallback(
    (sessionId: string) => {
      sendSessionIpc({
        type: "SESSION_FAVORITE_TOGGLE",
        msg_id: crypto.randomUUID(),
        session_id: sessionId,
      });
    },
    [sendSessionIpc],
  );

  const groupMutate = useCallback(
    (payload: Record<string, unknown>) => {
      sendSessionIpc({
        type: "SESSION_GROUP_MUTATE",
        msg_id: crypto.randomUUID(),
        ...payload,
      });
    },
    [sendSessionIpc],
  );

  const requestSessionHistory = useCallback(
    (sessionId: string, limit: number = 50) => {
      sendSessionIpc({
        type: "SESSION_HISTORY_REQUEST",
        msg_id: crypto.randomUUID(),
        session_id: sessionId,
        limit,
      });
    },
    [sendSessionIpc],
  );

  // ── 预算额度（按 provider 分账）· 复用通用 send_session_ipc 透传，无需新 Rust 命令 ──
  const setBudget = useCallback(
    (provider: string, currency: string, limitNative: number) => {
      sendSessionIpc({
        type: "SET_BUDGET",
        msg_id: crypto.randomUUID(),
        provider,
        currency,
        limit_native: limitNative,
      });
    },
    [sendSessionIpc],
  );

  const budgetQuery = useCallback(() => {
    // BudgetBar 挂载即查询，可能早于 IPC 就绪；未就绪时排队补发（同 requestDashboard），
    // 否则静默丢失、只能靠 bootstrap 广播兜底（而广播若早于 listener 也会错过）。
    const payload = { type: "BUDGET_QUERY", msg_id: crypto.randomUUID() };
    if (!readyRef.current) {
      pendingCallbacksRef.current.push(() => sendSessionIpc(payload));
      return;
    }
    sendSessionIpc(payload);
  }, [sendSessionIpc]);

  return (
    <BackendContext.Provider
      value={{
        sendMessage,
        stopGeneration,
        sendHitlDecision,
        sendInteractionResponse,
        sendPlanApprovalDecision,
        requestScheduledTasks,
        requestDashboard,
        deleteScheduledTask,
        searchRequest,
        requestSkillList,
        requestSkillDetail,
        saveSkill,
        deleteSkill,
        importSkill,
        exportSkill,
        pendingTaskNotification,
        clearTaskNotification,
        requestSessionList,
        requestGroupSessions,
        createSession,
        switchSession,
        deleteSession,
        toggleFavoriteSession,
        groupMutate,
        requestSessionHistory,
        setBudget,
        budgetQuery,
      }}
    >
      {children}
    </BackendContext.Provider>
  );
}
