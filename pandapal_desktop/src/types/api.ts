/**
 * src/types/api.ts
 *
 * IPC 消息类型定义（5.2 干净版）。
 * 通信路径：前端 → invoke → Rust → Python stdin / Python stdout → Rust → emit → 前端
 * 真相源：pandapal/desktop_ipc/ipc_transport.py:_to_ipc_schema()
 *
 * 设计原则：
 *   - 字段命名与 Python 端完全一致（不加 deprecated 兜底）
 *   - 不暴露 helper 转换函数（前端直接消费扁平 wire format）
 *   - AgentTaskData 是 UI-internal 类型（store 用），不在 wire 消息中
 */

import type { DashboardSnapshot } from "./dashboard";

// ── LLM 凭据配置类型（BYOK）──────────────────────────────────────────────

/**
 * Provider id 类型。
 *
 * 设计约束（PRD §模型管理 规则 1）：provider 固定，前端下拉选取，不能手填。
 * 白名单 = 后端 ``provider_catalog.toml`` 派生的 ``PROVIDER_CATALOG``（单一真相源），
 * 不再在前端硬编码联合类型——新增/移除 provider 只改 toml 一处。
 *
 * 此处保留为 ``string`` 别名以保持类型兼容；具体可选值由运行时拉取的 catalog 决定。
 */
export type LLMProvider = string;

/**
 * 单组 Provider 凭据。
 *
 * 主键 = `(provider, model_id)`（PRD §3.4 对象 C）——一个 provider 可配 N 个模型，
 * 不再是「一个 provider 一条凭据」。
 *
 * `is_default`：不是落盘字段。用户 toml 里只有一个顶层 `default_model_id`，
 * 后端下发 CREDENTIALS_LIST 时把「model_id === default_model_id」的那条标为 true，
 * 前端提交时后端再从 is_default 反推回 default_model_id。前端按布尔用即可。
 *
 * 单价字段（CNY / 1k token，PRD §3.4）：
 *   - 留空（undefined）= 用系统默认表的价（三级回落第 ②级）
 *   - 填了 = 用户覆盖价（第 ①级）
 *   - 两者皆无 → 后端**拒绝保存**（第 ③级），前端不得自作主张补 0（§九 金额类字段）
 *
 * `api_key` 为可选：编辑已保存凭据而**未更换密钥**时，提交体必须**整个省略**该字段
 * （PRD R3），后端沿用旧值。绝不能把界面上的脱敏值回写（PRD R2 / AC-07）。
 */
export interface ProviderCredential {
  provider: LLMProvider;
  /** 未改密钥时省略（R3）；有值时必须是明文真 key，禁止是脱敏值（R2） */
  api_key?: string;
  model_id: string;
  base_url?: string;
  is_default: boolean;
  /** 输入单价（CNY / 1k token）；与 output 必须同填同空 */
  input_price_per_1k?: number;
  /** 输出单价（CNY / 1k token）；与 input 必须同填同空 */
  output_price_per_1k?: number;
  /** 缓存命中单价（CNY / 1k token）；可选，留空取生效的输入价（R6 保守估高） */
  cache_read_price_per_1k?: number;
}

/** 凭据校验结果 */
export interface CredentialVerifyResult {
  provider: LLMProvider;
  success: boolean;
  error?: string;
}

/**
 * 配置状态。
 *
 * `default_model_id` 取代了旧的 `default_provider`：切换的粒度是模型不是 provider
 * （PRD §3.4 关键变更），且同一 provider 下可以有多个模型，provider 已不足以定位默认项。
 */
export interface ConfigStatusPayload {
  configured: boolean;
  credential_count: number;
  default_model_id: string | null;
  /**
   * 检测到 v1 版凭据文件（含 `default_provider`）。
   * v2 结构与 v1 不兼容且**刻意不做兼容读取**——前端据此引导用户备份后重新配置。
   * 真相源：pandapal/config/llm/credentials_handler.py handle_status
   */
  legacy_format: boolean;
}

// ── 消息类型常量（与 Python IpcMessageType 对应） ───────────────────────────

export const ApiMessageType = {
  // 入站（前端 → Python）
  SEND_MESSAGE:              "SEND_MESSAGE",
  HITL_DECISION:             "HITL_DECISION",
  INTERACTION_RESPONSE:      "INTERACTION_RESPONSE",
  PLAN_APPROVAL_DECISION:    "PLAN_APPROVAL_DECISION",
  PING:                      "PING",
  STOP_GENERATION:           "STOP_GENERATION",
  MODEL_LIST_REQUEST:        "MODEL_LIST_REQUEST",
  AUTH_READY:                "AUTH_READY",
  REQUEST_SCHEDULED_TASKS:   "REQUEST_SCHEDULED_TASKS",
  DELETE_SCHEDULED_TASK:     "DELETE_SCHEDULED_TASK",
  DASHBOARD_REQUEST:         "DASHBOARD_REQUEST",
  SET_BUDGET:                "SET_BUDGET",
  BUDGET_QUERY:              "BUDGET_QUERY",
  SKILL_LIST:               "SKILL_LIST",
  SKILL_GET:                "SKILL_GET",
  SKILL_SAVE:               "SKILL_SAVE",
  SKILL_DELETE:             "SKILL_DELETE",
  SKILL_IMPORT:             "SKILL_IMPORT",
  SKILL_EXPORT:             "SKILL_EXPORT",
  SEARCH:                    "SEARCH",
  // 会话列表入站
  SESSION_LIST_REQUEST:      "SESSION_LIST_REQUEST",
  SESSION_CREATE:            "SESSION_CREATE",
  SESSION_SWITCH:            "SESSION_SWITCH",
  SESSION_DELETE:            "SESSION_DELETE",
  SESSION_RENAME:            "SESSION_RENAME",
  SESSION_GROUP_MUTATE:      "SESSION_GROUP_MUTATE",
  SESSION_HISTORY_REQUEST:   "SESSION_HISTORY_REQUEST",

  // 出站（Python → 前端）
  REPLY_START:           "REPLY_START",
  TOKEN:                 "TOKEN",
  REASONING_TOKEN:       "REASONING_TOKEN",
  TOOL_START:            "TOOL_START",
  TOOL_END:              "TOOL_END",
  HITL_REQUEST:          "HITL_REQUEST",
  APPROVAL_RESULT:       "APPROVAL_RESULT",
  TASK_NOTIFICATION:     "TASK_NOTIFICATION",
  USER_INPUT_ECHO:       "USER_INPUT_ECHO",
  PERMISSION_DENIED:     "PERMISSION_DENIED",
  REPLY_END:             "REPLY_END",
  AGENT_HALTED:          "AGENT_HALTED",
  AGENT_REPLY:           "AGENT_REPLY",
  INTERACTION_REQUEST:   "INTERACTION_REQUEST",
  PLAN_APPROVAL_REQUEST: "PLAN_APPROVAL_REQUEST",
  AGENT_TASK_EVENT:      "AGENT_TASK_EVENT",
  QUICK_APP_DATA:        "QUICK_APP_DATA",
  SKILL_PROGRESS:        "SKILL_PROGRESS",
  ERROR:                 "ERROR",
  PONG:                  "PONG",
  AUTH_VERIFIED:         "AUTH_VERIFIED",
  AUTH_REJECTED:         "AUTH_REJECTED",
  SYSTEM_READY:          "SYSTEM_READY",
  SCHEDULED_TASK_LIST:   "SCHEDULED_TASK_LIST",
  SCHEDULED_TASK_CHANGED: "SCHEDULED_TASK_CHANGED",
  DASHBOARD_DATA:        "DASHBOARD_DATA",
  BUDGET_STATUS:         "BUDGET_STATUS",
  SKILL_LIST_RESULT:   "SKILL_LIST_RESULT",
  SKILL_GET_RESULT:    "SKILL_GET_RESULT",
  SKILL_SAVED:           "SKILL_SAVED",
  SKILL_DELETED:         "SKILL_DELETED",
  SKILL_ACTIVATED:       "SKILL_ACTIVATED",
  SKILL_CLEARED:         "SKILL_CLEARED",
  SKILL_IMPORTED:        "SKILL_IMPORTED",
  SKILL_EXPORTED:        "SKILL_EXPORTED",
  // 会话列表出站
  SESSION_LIST:          "SESSION_LIST",
  SESSION_SWITCHED:      "SESSION_SWITCHED",
  SESSION_CONCURRENCY:   "SESSION_CONCURRENCY",
  SESSION_UPDATED:       "SESSION_UPDATED",
  SESSION_DELETED:       "SESSION_DELETED",
  SESSION_GROUP_LIST:    "SESSION_GROUP_LIST",
  SESSION_HISTORY_LIST:  "SESSION_HISTORY_LIST",
  // 全局搜索出站
  SEARCH_RESULT:         "SEARCH_RESULT",
  // 模型选择出站
  MODEL_LIST:            "MODEL_LIST",

  // ── LLM 凭据管理（BYOK）─────────────────────────────────
  // 入站
  LOAD_CREDENTIALS:        "LOAD_CREDENTIALS",
  SAVE_LLM_CREDENTIALS:   "SAVE_LLM_CREDENTIALS",
  VERIFY_CREDENTIALS:     "VERIFY_CREDENTIALS",
  GET_CREDENTIALS_STATUS: "GET_CREDENTIALS_STATUS",
  // 出站
  CREDENTIALS_LIST:       "CREDENTIALS_LIST",
  CREDENTIALS_SAVED:      "CREDENTIALS_SAVED",
  CREDENTIALS_VERIFIED:   "CREDENTIALS_VERIFIED",
  CREDENTIALS_STATUS:     "CREDENTIALS_STATUS",

  // ── 认证会话（JWT 自动续期）────────────────────────────
  // 真相源：与 pandapal/desktop_ipc/message_codec.py 的 IpcMessageType 必须同步更新
  AUTH_TOKEN_REFRESHED:   "AUTH_TOKEN_REFRESHED",
  AUTH_EXPIRED:           "AUTH_EXPIRED",
} as const;

export type ApiMessageType = (typeof ApiMessageType)[keyof typeof ApiMessageType];

// ── 通用字段（5.2：所有出站消息都带 msg_id + timestamp；reply_id/run_id 可选） ─

export interface IpcMessageBase {
  msg_id: string;
  timestamp?: number;
  reply_id?: string;
  reply_scope?: "normal" | "hitl_resume" | "system" | "task" | "error";
  run_id?: string;
  /** v003：session_id 透出到消息头，前端按 sid 分发流事件到对应 buffer */
  session_id?: string;
}

// ── 入站消息（前端发出） ────────────────────────────────────────────────────

/** Agent 人格模式（与后端 pandapal/local/prompts.py 的 PERSONAS 键一致）。 */
export type AgentMode = "coding" | "office";

export interface SendMessagePayload {
  type: "SEND_MESSAGE";
  msg_id: string;
  content: string;
  /**
   * 人格模式（coding=编码 / office=办公助手）。可选：不传时后端保持该 session
   * 当前绑定（新 session 缺省为 office）。真相源见 pandapal/desktop_ipc/stdio_ipc.py。
   */
  mode?: AgentMode;
  /**
   * 逐条消息的模型选择。可选：不传/非法时后端回落 default 模型。
   * Rust send_message 命令已透传 payload["model_id"]；真相源见 stdio_ipc.py。
   */
  model_id?: string;
}

export interface HitlDecisionPayload {
  type: "HITL_DECISION";
  msg_id: string;
  run_id: string;
  decision: "approved" | "rejected";
  approval_id: string;
}

export interface PingPayload {
  type: "PING";
  msg_id: string;
}

export interface StopGenerationPayload {
  type: "STOP_GENERATION";
  msg_id: string;
}

export interface InteractionResponsePayload {
  type: "INTERACTION_RESPONSE";
  msg_id: string;
  run_id: string;
  response: string;
}

export interface PlanApprovalDecisionPayload {
  type: "PLAN_APPROVAL_DECISION";
  msg_id: string;
  run_id: string;
  plan_action: "approve" | "refine" | "abandon";
  user_text?: string;
  edited_plan_content?: string | null;
}

/** D1 Pull：前端主动请求定时任务列表 */
export interface RequestScheduledTasksPayload {
  type: "REQUEST_SCHEDULED_TASKS";
  msg_id: string;
}

/** 请求可选模型清单（前端 ready 后拉取；后端回 MODEL_LIST） */
export interface ModelListRequestPayload {
  type: "MODEL_LIST_REQUEST";
  msg_id: string;
}

/** 确定性删除定时任务（绕过 LLM，直连 task_scheduler.unregister_task_definition） */
export interface DeleteScheduledTaskPayload {
  type: "DELETE_SCHEDULED_TASK";
  msg_id: string;
  task_id: string;
}

/** Skill 资源管理 - 列表查询 */
export interface SkillListPayload {
  type: "SKILL_LIST";
  msg_id: string;
}

/** Skill 资源管理 - 获取单个详情 */
export interface SkillGetPayload {
  type: "SKILL_GET";
  msg_id: string;
  skill_name: string;
}

/** Skill 资源管理 - 创建/更新 */
export interface SkillSavePayload {
  type: "SKILL_SAVE";
  msg_id: string;
  skill_name: string;
  description: string;
  when_to_use: string;
  content: string;
  tags?: string[];
}

/** Skill 资源管理 - 删除 */
export interface SkillDeletePayload {
  type: "SKILL_DELETE";
  msg_id: string;
  skill_name: string;
}

/** Skill 资源管理 - 导入 */
export interface SkillImportPayload {
  type: "SKILL_IMPORT";
  msg_id: string;
  format: "zip" | "folder";
  overwrite?: boolean;
}

/** Skill 资源管理 - 导出 */
export interface SkillExportPayload {
  type: "SKILL_EXPORT";
  msg_id: string;
  skill_name: string;
  format: "zip" | "folder";
}

// ── LLM 凭据管理 入站 payload ──────────────────────────────────

/** 加载已有凭据（设置页回填 / 向导已有配置回显） */
export interface LoadCredentialsPayload {
  type: "LOAD_CREDENTIALS";
  msg_id: string;
}

/** 保存凭据：前端提交完整凭据列表给后端写入存储 */
export interface SaveCredentialsPayload {
  type: "SAVE_LLM_CREDENTIALS";
  msg_id: string;
  credentials: ProviderCredential[];
}

/** 连通性校验：前端发一组凭据，后端探测是否可用 */
export interface VerifyCredentialsPayload {
  type: "VERIFY_CREDENTIALS";
  msg_id: string;
  credentials: ProviderCredential[];
}

/** 查询凭据配置状态（门禁用） */
export interface CredentialsStatusPayload {
  type: "GET_CREDENTIALS_STATUS";
  msg_id: string;
}

export type InboundApiMessage =
  | SendMessagePayload
  | HitlDecisionPayload
  | InteractionResponsePayload
  | PlanApprovalDecisionPayload
  | RequestScheduledTasksPayload
  | DeleteScheduledTaskPayload
  | SkillListPayload
  | SkillGetPayload
  | SkillSavePayload
  | SkillDeletePayload
  | SkillImportPayload
  | SkillExportPayload
  | PingPayload
  | StopGenerationPayload
  | SessionListRequestPayload
  | SessionCreatePayload
  | SessionSwitchPayload
  | SessionDeletePayload
  | SessionRenamePayload
  | SessionGroupMutatePayload
  | SessionHistoryRequestPayload
  | LoadCredentialsPayload
  | SaveCredentialsPayload
  | VerifyCredentialsPayload
  | CredentialsStatusPayload;

// ── 出站消息（Python 推送） ─────────────────────────────────────────────────

export interface ReplyStartMsg extends IpcMessageBase {
  type: "REPLY_START";
}

export interface TokenMsg extends IpcMessageBase {
  type: "TOKEN";
  token: string;
  snapshot?: string;
}

export interface ReasoningTokenMsg extends IpcMessageBase {
  type: "REASONING_TOKEN";
  token: string;
  snapshot?: string;
}

export interface ToolStartMsg extends IpcMessageBase {
  type: "TOOL_START";
  tool_name: string;
  tool_call_id: string;
  /** 5.2：Python 发完整 dict，前端需要 string 时自己 JSON.stringify */
  tool_args?: Record<string, unknown>;
}

/**
 * 工具执行后由 ToolFeedbackProvider 贡献的反馈（如代码质量门控的 lint 诊断）。
 *
 * 真相源：pandapal/desktop_ipc/ipc_transport.py 的 TOOL_END 分支
 *        ← pandapal/events/normalized.py tool_end()
 *        ← pandaren/engine/run_core.py feedback_to_event_data()
 *
 * 与 result_full 的区别：result_full 是**工具自己说的**（"✅ 已创建新文件"），
 * feedback 是**第三方对这次调用的评价**（"但它有 15 个 lint error"）。
 * 两者都要展示，别把 feedback 当成 result 的一部分渲染。
 *
 * 字段无 = 无反馈（绝大多数工具调用），不是"检查通过"——
 * 门控通过时同样不发 feedback，故**不可**据此渲染 ✅。
 */
export interface ToolFeedback {
  /** 反馈正文（多行，形如 `output/x.py:1:1 F401 ...`） */
  text: string;
  /** 严重度（小写）。渲染角标颜色用 */
  severity: "info" | "warning" | "error";
  /** 来源标识，如 "code_quality_gate"；"composite" = 多源合并，text 内各段自带标签 */
  source: string;
}

export interface ToolEndMsg extends IpcMessageBase {
  type: "TOOL_END";
  tool_name?: string;
  tool_call_id: string;
  is_error?: boolean;
  result_full?: string | null;
  result_error?: string | null;
  result_preview?: string;
  result_mime_type?: string;
  result_size_bytes?: number;
  result_truncated?: boolean;
  duration_ms?: number | null;
  tool_args?: Record<string, unknown>;
  feedback?: ToolFeedback | null;
}

export interface HitlRequestMsg extends IpcMessageBase {
  type: "HITL_REQUEST";
  approval_id?: string;
  tool_name?: string;
  tool_args_summary?: Record<string, unknown>;
  session_id?: string;
  extra?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface PermissionDeniedMsg extends IpcMessageBase {
  type: "PERMISSION_DENIED";
  tool_name?: string;
  reason?: string;
}

/** 本轮对话消耗汇总（应用层 CostBudgetGuard.summary 精算；REPLY_END 携带，前端只展示不重算）。
 *  后端未注入 guard / 本 run 无 LLM 调用 / 旧 sidecar → 字段缺省，前端降级不显示。 */
export interface ReplyUsage {
  model: string;
  net_cost_usd: number;   // 实际净费用（主口径）
  full_cost_usd: number;  // 全价基线（无缓存假设）
  saved_usd: number;      // 命中节省 = full − net
  input_tokens: number;   // 输入总量
  cached_tokens: number;  // 命中
  miss_tokens: number;    // 未命中 = input − cached
  cache_creation_tokens: number; // 新写入缓存
  output_tokens: number;  // 输出总量（含推理）
  reply_tokens: number;   // llm 回复 = output − reasoning
  reasoning_tokens: number; // 推理
  hit_rate: number;       // 命中率 0~1
  duration_ms: number;    // 本轮耗时（executor 墙钟）
}

export interface ReplyEndMsg extends IpcMessageBase {
  type: "REPLY_END";
  output?: string;
  status?: "ok" | "error" | "halted";
  usage?: ReplyUsage;
  halt_kind?: string; // "budget_exhausted" 等专属停机类型；供前端区分渲染
}

export interface AgentHaltedMsg extends IpcMessageBase {
  type: "AGENT_HALTED";
  reason?: string;
  halt_kind?: string; // "budget_exhausted"=预算耗尽前置拦截；空=普通停机
}

export interface AgentReplyMsg extends IpcMessageBase {
  type: "AGENT_REPLY";
  reply_id?: string;
  content: string;
  session_id?: string;
}

export interface PlanApprovalRequestMsg extends IpcMessageBase {
  type: "PLAN_APPROVAL_REQUEST";
  plan_path: string;
  plan_content: string;
  run_id?: string;
  session_id?: string;
  user_id?: string;
}

export interface ErrorMsg extends IpcMessageBase {
  type: "ERROR";
  error_code?: string;
  error_message: string;
  error_detail?: string;
}

export interface PongMsg extends IpcMessageBase {
  type: "PONG";
}

export interface ApprovalResultMsg extends IpcMessageBase {
  type: "APPROVAL_RESULT";
  approval_id?: string;
  decision?: string;
  tool_name?: string;
}

// ── 交互型工具消息 ─────────────────────────────────

export interface QuestionOption {
  label: string;
  description: string;
}

/** 单个问题结构（与 Python ask_user 工具的 question schema 对齐）
 *  options 中必须包含一个 label 固定为「自由输入」的选项，前端识别后渲染为自由输入框。
 */
export interface QuestionItem {
  question: string;
  header: string;
  options: QuestionOption[];
  multiSelect: boolean;
}

/**
 * INTERACTION_REQUEST：一次携带全部问题（不再限制单 question）。
 */
export interface InteractionRequestMsg extends IpcMessageBase {
  type: "INTERACTION_REQUEST";
  request_id?: string;
  questions: QuestionItem[];
  tool_name?: string;
}

export interface UserInputEchoMsg extends IpcMessageBase {
  type: "USER_INPUT_ECHO";
  user_id?: string;
  content?: string;
  session_id?: string;
  source_channel_id?: string;
}

// ── Task / Scheduled Task 通知（5.2 扁平化） ─────────────────────────────

/**
 * 5.2：Python 定时任务通知扁平结构。
 * 不再发 trigger_type / cron_expression / status / completed_at 等冗余字段——
 * 通知就是"用户看的标题 + 主体内容 + 严重级别"，其他都是 Python 内部状态。
 */
export interface TaskNotificationMsg extends IpcMessageBase {
  type: "TASK_NOTIFICATION";
  task_id: string;
  title: string;
  body?: string;
  level?: "info" | "warning" | "error";
}

// ── 认证会话（JWT 自动续期）─────────────────────────────────────────────

/**
 * AUTH_TOKEN_REFRESHED：Gateway 刷新 JWT 成功后推送（全局级，不带 session_id）。
 * 前端收到后 invoke("auth_update_token", { token }) 回写 auth_store.json。
 * 真相源：pandapal/desktop_ipc/message_codec.py 的 IpcMessageType。
 */
export interface AuthTokenRefreshedMsg extends IpcMessageBase {
  type: "AUTH_TOKEN_REFRESHED";
  token: string;
}

/**
 * AUTH_EXPIRED：登录态彻底失效（refresh 被 Relay 401 拒绝，超出宽限期）。
 * 前端收到后登出并跳登录页（全局级，不带 session_id）。
 */
export interface AuthExpiredMsg extends IpcMessageBase {
  type: "AUTH_EXPIRED";
}

/**
 * SessionAgentPool 并发三态（queued / started / released）+ 排队反馈。
 * 真相源：pandapal/desktop_ipc/ipc_transport.py 的 SESSION_CONCURRENCY 分支。
 */
export interface SessionConcurrencyMsg extends IpcMessageBase {
  type: "SESSION_CONCURRENCY";
  session_id: string;
  status: "queued" | "started" | "released";
  running_count: number;
  max_concurrent: number;
  queue_position: number;
  queue_length: number;
}

// ── AgentTask 事件消息（5.2 扁平化：只发事件，不发完整 task 对象） ─────────

/**
 * UI 内部：完整的 AgentTask 数据（store 用）。
 * 注意：这是 UI 内部类型，**不**在 wire 消息中传输。
 * Python 端 5.2 起只发扁平 `task_id` + `event`，完整数据由前端 fetch 或其他来源填充。
 */
export type AgentTaskStatus =
  | "pending"
  | "in_progress"
  | "completed"
  | "failed"
  | "cancelled";

export interface AgentTaskData {
  task_id: string;
  session_id: string;
  user_id: string;
  subject: string;
  description: string;
  status: AgentTaskStatus;
  active_form: string;
  order: number;
  blocks: string[];
  blocked_by: string[];
  created_at: string | null;
  updated_at: string | null;
  completed_at: string | null;
}

/**
 * AGENT_TASK_EVENT：Python 端透传完整 task 对象（见 ipc_transport.py AGENT_TASK_EVENT 分支）。
 * created / updated 携带完整 `task`；deleted 时 `task` 可为 null，用 task_id 定位删除。
 * 真相源：pandapal/desktop_ipc/ipc_transport.py + pandapal/tools/agent_task_tools.py::_push_event
 */
export interface AgentTaskEventMsg extends IpcMessageBase {
  type: "AGENT_TASK_EVENT";
  task_id: string;
  event: "created" | "updated" | "deleted";
  task: AgentTaskData | null;
}

// ── 技能进度心跳（对话内进度块，LLM 主动上报） ──────────────────────────

/**
 * SKILL_PROGRESS：LLM 执行长任务/技能时主动上报的进度心跳，渲染进对话时间线。
 * 同一 activity 的多次事件在前端归并成一个进度块。
 * 真相源：pandapal/tools/progress_tools.py::report_progress + ipc_transport.py
 */
export interface SkillProgressEventMsg extends IpcMessageBase {
  type: "SKILL_PROGRESS";
  activity: string;
  phase: string;
  status: "running" | "completed" | "failed";
  detail: string;
  session_id: string;
}

// ── Quick App 数据推送（AI Quick App 框架通道③） ──────────────────────────

/**
 * QUICK_APP_DATA：AI 向快应用面板推送结构化数据。
 * Python push_app_data 工具 → NormalizedEvent.quick_app_data() → 前端 AppEventRouter。
 */
export interface QuickAppDataEventMsg extends IpcMessageBase {
  type: "QUICK_APP_DATA";
  app_id: string;
  data_type: string;
  data: Record<string, unknown>;
  session_id: string;
}

// ── 定时任务列表（D1 Pull + D2 Push） ──────────────────────────────────────

/** 与后端 ScheduledTaskItem 一一对应 */
export interface ScheduledTaskItem {
  task_id: string;
  name: string;
  trigger_type: string;       // "recurring" | "oneshot" | "event" | "manual"
  cron_expression: string;
  task_prompt: string;
  session_id: string;
  sensitivity: string;        // "low" | "medium" | "high" | "critical"
  created_at: string;
  /** 最近一次执行状态（后端查询执行记录填充，空=从未执行） */
  last_status?: string;       // "pending" | "running" | "completed" | "failed" | "cancelled"
  last_run_at?: string;       // ISO，空=从未执行
  /** D2 Push 专用：单任务变更时的操作类型 */
  change_type?: "created" | "updated" | "deleted";
}

/** D1 Pull 响应 / D2 Push 全量：Python 推送任务列表 */
export interface ScheduledTaskListMsg extends IpcMessageBase {
  type: "SCHEDULED_TASK_LIST";
  tasks: ScheduledTaskItem[];
}

/** D2 Push 增量：单个任务变更 */
export interface ScheduledTaskChangedMsg extends IpcMessageBase {
  type: "SCHEDULED_TASK_CHANGED";
  task: ScheduledTaskItem;
  change_type: "created" | "updated" | "deleted";
}

/** Skill 资源摘要（列表用） */
export interface SkillItem {
  name: string;
  description: string;
  when_to_use: string;
  tags: string[];
  /** "system" 或 "user" */
  source: "system" | "user";
  /** 技能脚本大小（bytes） */
  size: number;
  /** ISO 时间字符串 */
  modified_at: string;
  /** true 表示从 cache 读取，非实时 */
  from_cache?: boolean;
  /** 是否允许自动触发 */
  allow_auto_trigger: boolean;
}

/** Skill 列表响应 */
export interface SkillListResultMsg extends IpcMessageBase {
  type: "SKILL_LIST_RESULT";
  skills: SkillItem[];
}

/** Skill 详情响应 */
export interface SkillGetResultMsg extends IpcMessageBase {
  type: "SKILL_GET_RESULT";
  skill_name: string;
  description: string;
  when_to_use: string;
  content: string;
  tags: string[];
  source: "system" | "user";
  size: number;
  modified_at: string;
  from_cache?: boolean;
  allow_auto_trigger: boolean;
}

/** Skill 保存成功确认 */
export interface SkillSavedMsg extends IpcMessageBase {
  type: "SKILL_SAVED";
  skill: {
    name: string;
    description: string;
    when_to_use: string;
    source: "system" | "user";
    tags: string[];
  };
}

/** Skill 删除成功确认 */
export interface SkillDeletedMsg extends IpcMessageBase {
  type: "SKILL_DELETED";
  skill_name: string;
}

/** Skill 激活通知（search_skills 成功后） */
export interface SkillActivatedMsg extends IpcMessageBase {
  type: "SKILL_ACTIVATED";
  skill_name: string;
}

/** Skill 清除通知（Turn 结束时） */
export interface SkillClearedMsg extends IpcMessageBase {
  type: "SKILL_CLEARED";
  skill_name: string;
}

/** Skill 导入结果 */
export interface SkillImportedMsg extends IpcMessageBase {
  type: "SKILL_IMPORTED";
  success: boolean;
  skill_name: string;
  error?: string;
}

/** Skill 导出结果 */
export interface SkillExportedMsg extends IpcMessageBase {
  type: "SKILL_EXPORTED";
  file_path: string;
  format: "zip" | "folder";
}

// ── 全局搜索（命令面板 ⌘K）── 与后端 NormalizedEvent.search_result 一致

/** 会话标题命中项 */
export interface SearchSessionHit {
  session_id: string;
  title: string;
  preview: string;
  updated_at: string;
}

/** 消息全文命中项 */
export interface SearchMessageHit {
  session_id: string;
  title: string;      // 所属会话标题
  snippet: string;    // 命中词周围片段
  role: string;       // "user" | "assistant" | ...
  timestamp: string;
}

/** SEARCH_RESULT：搜索应答（会话标题 + 消息全文） */
export interface SearchResultMsg extends IpcMessageBase {
  type: "SEARCH_RESULT";
  query: string;
  sessions: SearchSessionHit[];
  messages: SearchMessageHit[];
}

// ── 出站消息联合类型 ─────────────────────────────────────────────────────

export type OutboundApiMessage =
  | ReplyStartMsg
  | TokenMsg
  | ReasoningTokenMsg
  | ToolStartMsg
  | ToolEndMsg
  | HitlRequestMsg
  | ApprovalResultMsg
  | TaskNotificationMsg
  | SessionConcurrencyMsg
  | UserInputEchoMsg
  | AgentTaskEventMsg
  | QuickAppDataEventMsg
  | SkillProgressEventMsg
  | ScheduledTaskListMsg
  | ScheduledTaskChangedMsg
  | SkillListResultMsg
  | SkillGetResultMsg
  | SkillSavedMsg
  | SkillDeletedMsg
  | SkillActivatedMsg
  | SkillClearedMsg
  | SkillImportedMsg
  | SkillExportedMsg
  | PermissionDeniedMsg
  | ReplyEndMsg
  | AgentHaltedMsg
  | AgentReplyMsg
  | InteractionRequestMsg
  | PlanApprovalRequestMsg
  | ErrorMsg
  | PongMsg
  | SessionListMsg
  | SessionSwitchedMsg
  | SessionUpdatedMsg
  | SessionDeletedMsg
  | SessionGroupListMsg
  | SessionHistoryListMsg
  | SearchResultMsg
  | DashboardDataMsg
  | BudgetStatusMsg
  | ModelListMsg
  | CredentialsListMsg
  | CredentialsSavedMsg
  | CredentialsVerifiedMsg
  | CredentialsStatusMsg
  | AuthTokenRefreshedMsg
  | AuthExpiredMsg;

// ─────────────────────────────────────────────────────────────
// Dashboard 看板（入站请求 payload + 出站数据消息）
// ─────────────────────────────────────────────────────────────

/** 前端主动请求看板快照 */
export interface RequestDashboardPayload {
  type: "DASHBOARD_REQUEST";
  msg_id: string;
}

/** Python 推送看板快照（payload = { global, sessions }，见 types/dashboard.ts） */
export interface DashboardDataMsg extends IpcMessageBase {
  type: "DASHBOARD_DATA";
  global: DashboardSnapshot["global"];
  sessions: DashboardSnapshot["sessions"];
  /** 降级事件明细（非会话级）。真相源：DashboardSnapshot.to_dict() 的 "degradations" 段。
   *  旧 sidecar 二进制不带此段 → 可选，前端按空数组降级。 */
  degradations?: DashboardSnapshot["degradations"];
}

// ─────────────────────────────────────────────────────────────
// 预算额度（按 provider 分账）
//   真相源：pandapal/config/llm_pricing.py BudgetView.to_dict() +
//           pandapal/budget/handler.py。内部记账 USD，native 为该额度币种展示值。
// ─────────────────────────────────────────────────────────────

/** 前端设/改某 provider 额度（内部记账 USD，用户可设币种，默认 USD） */
export interface SetBudgetPayload {
  type: "SET_BUDGET";
  msg_id: string;
  provider: string;        // dashscope | volcengine | openai | deepseek
  currency: string;        // 展示/输入币种，默认 "USD"
  limit_native: number;    // 该币种下的额度
}

/** 前端查询全部 provider 额度态（额度条首屏/刷新） */
export interface BudgetQueryPayload {
  type: "BUDGET_QUERY";
  msg_id: string;
}

/** 单个 provider 的额度视图（对应后端 BudgetView.to_dict()） */
export interface BudgetView {
  provider: string;
  currency: string;
  limit_native: number | null;      // null = 未设额度
  spent_native: number;
  remaining_native: number | null;  // null = 未设额度
  usage_ratio: number;              // spent_usd / limit_usd（未设→0）
  state: "unset" | "normal" | "near" | "exhausted";
  spent_usd: number;
  limit_usd: number | null;
}

/** Python 推送每 provider 额度态（额度条渲染；SET_BUDGET/BUDGET_QUERY 应答 + 首屏 + run 后刷新） */
export interface BudgetStatusMsg extends IpcMessageBase {
  type: "BUDGET_STATUS";
  budgets: BudgetView[];
}

// ─────────────────────────────────────────────────────────────
// 模型选择 — 出站（MODEL_LIST_REQUEST 应答）
// 真相源：pandapal/config/model_registry.py:to_model_list_payload
// ─────────────────────────────────────────────────────────────

/**
 * 单价来源标记（PRD §4.3.2 / Story 4）。
 *
 * 界面必须区分「系统默认价」与「我填的价」，否则用户会把系统的估算价
 * 误当成自己的真实采购成本，账目失真却毫不知情。
 *
 * - "user"    : 用户在凭据里填了自己的单价（三级回落第 ①级）
 * - "system"  : 命中系统默认表（第 ②级）
 * - "missing" : 两者皆无 —— 待补价（R10：默认表升级后移除了该 model_id）。
 *               注意：missing **不代表模型不可用**，只代表其消费进未定价兜底桶（R11）。
 *
 * 字符串取值与 Python 端逐字节一致（协议字符串，禁止在此处美化）。
 */
export type ModelPriceSource = "user" | "system" | "missing";

/** 单个可选模型（字段与后端 AvailableModel 一致） */
export interface ModelListItem {
  model_id: string;
  display_name: string;
  provider: string;
  /** 单价来源；用于下拉里的「待补价」徽标与「系统默认价 / 我填的价」标签 */
  price_source: ModelPriceSource;
}

/** Python 下发可选模型清单 + 默认模型（MODEL_LIST_REQUEST 应答） */
export interface ModelListMsg extends IpcMessageBase {
  type: "MODEL_LIST";
  models: ModelListItem[];
  default_model_id: string;
}

// ── LLM 凭据管理 出站消息 ─────────────────────────────────────

/** 已有凭据列表（api_key 脱敏，仅露首尾若干位） */
export interface CredentialsListMsg extends IpcMessageBase {
  type: "CREDENTIALS_LIST";
  credentials: ProviderCredential[];
}

/** 凭据保存结果 */
export interface CredentialsSavedMsg extends IpcMessageBase {
  type: "CREDENTIALS_SAVED";
  success: boolean;
  error?: string;
}

/** 单组凭据校验结果 */
export interface VerifiedCredentialItem {
  provider: LLMProvider;
  success: boolean;
  error?: string;
}

/** 凭据校验结果 */
export interface CredentialsVerifiedMsg extends IpcMessageBase {
  type: "CREDENTIALS_VERIFIED";
  /** 全部通过为 true */
  success: boolean;
  results: VerifiedCredentialItem[];
}

/** 凭据配置状态 */
export interface CredentialsStatusMsg extends IpcMessageBase {
  type: "CREDENTIALS_STATUS";
  configured: boolean;
  credential_count: number;
  /** 默认模型（由 default_provider 改名而来，见 ConfigStatusPayload 注释） */
  default_model_id: string | null;
  /**
   * v1 格式凭据文件（含已废弃的 default_provider 字段）。
   * 真相源 credentials_store.get_status()；不做兼容读取，需引导用户备份后重配。
   */
  legacy_format: boolean;
  /** default_model_id 是否指向清单中真实存在的模型；false = 有凭据但默认失效 */
  default_resolvable: boolean;
}

// ─────────────────────────────────────────────────────────────
// Provider 元信息 — Rust 命令 get_provider_catalog 返回
// 真相源：pandapal/config/llm/provider_catalog.toml
//
// ⚠️ 是**运行时**读文件，不是 include_str! 编译期嵌入（PRD G5）。
//    编译期嵌入会让 Rust 手里的是一份**拷贝**——Python 运行时读的文件一改，
//    两边就悄悄不一致了，且没有任何运行时检测能发现。单一真相源指的是
//    「同一个文件」，不是「同一份内容曾经相等」。
//
// 拉取通道：Rust 命令（不走 IPC，不依赖 sidecar）。
//   - catalog 是静态系统配置，随软件发布，运行时不变
//   - 首次配置场景（sidecar 未启动）也能拉到，避免死锁
//   - 与用户凭据（llm_credentials.toml，走 IPC / Rust 命令双通道）各自独立管理
//
// 设计约束（与 PRD §模型管理 三条规则一致）：
//   - provider 固定：前端 provider 选择为下拉框，不能手填，选项 = 此 catalog
//   - 系统不内置模型数据：本 catalog 不含任何模型清单 / candidateModels
//   - env_prefix / verify_url 是后端专用字段，不下发给前端
// ─────────────────────────────────────────────────────────────

/** 单个系统预置 provider 的元信息（下发给前端的公开字段） */
export interface ProviderMeta {
  /** provider id（dashscope / volcengine / openai / deepseek） */
  id: string;
  /** 前端展示名（如「通义千问 (DashScope)」） */
  display_name: string;
  /** 用户获取 API Key 的官方指引链接 */
  guide_url: string;
  /** 官方默认 API 地址（用户未填 base_url 时用此） */
  default_base_url: string;
}

// ─────────────────────────────────────────────────────────────
// 系统默认单价表 — Rust 命令 get_model_prices 返回
// 真相源：pandapal/config/llm/model_prices.toml（同样是**运行时**读，非编译期嵌入）
//
// ╔═══════════════════════════════════════════════════════════════════════╗
// ║ ⚠️ 本表**不是**可用性白名单（PRD R11 / AC-06）。                        ║
// ║   表中没有的 model_id 照样可配置、可装配、可路由——只是保存时必须由      ║
// ║   用户自己填单价。任何「不在本表内就不展示 / 不允许保存 / 置灰」的       ║
// ║   逻辑，都是已删除的 _DECLARED_MODELS 白名单换马甲复活，明令禁止。      ║
// ║   本表在前端只有两个用途：① combobox 推荐清单；② 默认价回填展示。       ║
// ╚═══════════════════════════════════════════════════════════════════════╝
// ─────────────────────────────────────────────────────────────

/** 系统默认单价表中的一条（单位固定 CNY / 1k token） */
export interface ModelPriceEntry {
  model_id: string;
  /** 归属 provider，用于 combobox 按 provider 归类推荐 */
  provider: string;
  input_price_per_1k: number;
  output_price_per_1k: number;
  /** 可选；缺省时取 input_price_per_1k（R6 保守估高） */
  cache_read_price_per_1k?: number;
  /** 高峰时段输入单价（可选；缺省 = 不分时，高峰取单档价） */
  peak_input_price_per_1k?: number;
  /** 高峰时段输出单价（可选） */
  peak_output_price_per_1k?: number;
  /** 高峰时段缓存命中单价（可选） */
  peak_cache_read_price_per_1k?: number;
}

/** get_model_prices 的返回体 */
export interface ModelPricesPayload {
  /** 1 USD = N CNY。金额类字段，缺失/非法由后端拒绝启动，前端不得兜底（PRD AC-14） */
  exchange_rate_usd: number;
  prices: ModelPriceEntry[];
}


// ─────────────────────────────────────────────────────────────
// 会话列表（UI 会话管理）— 入站 payload
// ─────────────────────────────────────────────────────────────

export interface SessionListRequestPayload {
  type: "SESSION_LIST_REQUEST";
  msg_id: string;
  /** "all" | null(=无分组) | 具体 group_id */
  group_id: string | null;
  page: number;
  limit: number;
}

export interface SessionCreatePayload {
  type: "SESSION_CREATE";
  msg_id: string;
}

export interface SessionSwitchPayload {
  type: "SESSION_SWITCH";
  msg_id: string;
  target_session_id: string;
}

export interface SessionDeletePayload {
  type: "SESSION_DELETE";
  msg_id: string;
  session_id: string;
  current_view_session_id: string | null;
}

export interface SessionRenamePayload {
  type: "SESSION_RENAME";
  msg_id: string;
  session_id: string;
  title: string;
}

export type SessionGroupOp =
  | { op: "create"; name: string }
  | { op: "rename"; group_id: string; new_name: string }
  /** delete_sessions=true 时连同组内会话一并软删除；缺省/false 仅解绑保留会话 */
  | { op: "delete"; group_id: string; delete_sessions?: boolean }
  | { op: "assign"; session_id: string; group_id: string | null };

export interface SessionGroupMutatePayload extends Record<string, unknown> {
  type: "SESSION_GROUP_MUTATE";
  msg_id: string;
  op: "create" | "rename" | "delete" | "assign";
}

export interface SessionHistoryRequestPayload {
  type: "SESSION_HISTORY_REQUEST";
  msg_id: string;
  session_id: string;
  /** 期望回补的最近消息条数，默认 50 */
  limit?: number;
  /** 分页游标：已加载条数（0 = 最新一页）；缺省按 0 处理 */
  offset?: number;
}


// ─────────────────────────────────────────────────────────────
// 会话列表 — 出站 payload
// ─────────────────────────────────────────────────────────────

export interface SessionInfo {
  session_id: string;
  title: string;
  preview: string;
  message_count: number;
  is_empty: boolean;
  group_id: string | null;
  group_name: string | null;
  updated_at: string;
  /** 创建时间（ISO），会话列表排序键（created_at DESC） */
  created_at: string;
}

export interface SessionGroupInfo {
  id: string;
  user_id: string;
  name: string;
  created_at: string;
}

export interface SessionRoutingResult {
  action: "no_change" | "switch" | "empty_state";
  target_session_id: string | null;
}

export interface SessionListMsg extends IpcMessageBase {
  type: "SESSION_LIST";
  sessions: SessionInfo[];
  has_more: boolean;
  page: number;
  /** "all" | null(=无分组) | 具体 group_id */
  group_id: string | null;
}

export interface SessionSwitchedMsg extends IpcMessageBase {
  type: "SESSION_SWITCHED";
  session_id: string;
  context_status: "fresh" | "restored" | "degraded";
}

export interface SessionUpdatedMsg extends IpcMessageBase {
  type: "SESSION_UPDATED";
  session_info: SessionInfo;
  reason: "created" | "first_message" | "activity" | "renamed" | "group_changed";
}

export interface SessionDeletedMsg extends IpcMessageBase {
  type: "SESSION_DELETED";
  session_id: string;
  routing: SessionRoutingResult;
}

export interface SessionGroupListMsg extends IpcMessageBase {
  type: "SESSION_GROUP_LIST";
  groups: SessionGroupInfo[];
}

/** 历史投影：单个工具调用（与后端 session_list_manager.get_session_history 富投影对齐） */
export interface HistoryToolCall {
  tool_call_id: string;
  tool_name: string;
  args?: Record<string, unknown>;
  status: "done" | "error";
  result?: { preview?: string; full?: string | null; error?: string | null };
}

/** 历史投影：有序时间线段（reasoning 用单个 text 承载整段思考） */
export type HistoryTimelineItem =
  | { kind: "reasoning"; text: string }
  | { kind: "text"; content: string }
  | { kind: "tool"; tool_call_id: string };

/** 历史投影：单条消息（user/system 为纯文本；assistant 带 timeline + tool_calls） */
export interface HistoryMessage {
  role: string;
  content: string;
  timestamp?: string | null;
  timeline?: HistoryTimelineItem[];
  tool_calls?: HistoryToolCall[];
}

export interface SessionHistoryListMsg extends IpcMessageBase {
  type: "SESSION_HISTORY_LIST";
  session_id: string;
  messages: HistoryMessage[];
  /** 分页游标：本页首条在全量历史中的偏移（已加载条数，0 = 最新一页） */
  offset?: number;
  /** 是否还有更早的历史可继续向上翻页 */
  has_more?: boolean;
}


