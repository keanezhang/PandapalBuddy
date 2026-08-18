/**
 * src/store/credentialStore.ts
 *
 * LLM 凭据管理 Store（BYOK — 用户自填 LLM 配置）。
 *
 * 职责：
 *   - 管理多组 provider 凭据的客户端状态
 *   - 驱动「首次配置向导」和「设置页·模型服务」的 UI
 *   - 提供门禁判断（configured：是否存在至少一组完整凭据）
 *
 * 数据流：
 *   用户填写 → 前端状态 → IPC LOAD/SAVE/VERIFY → 后端凭据文件 → 注入环境变量
 *
 * 真相源：本机用户配置目录下的 llm_credentials.toml（通过后端 IPC 互通）。
 *
 * ── Provider 元信息（PRD §模型管理 规则 1）──────────────────────────────
 *   - provider 固定：前端 provider 选择为下拉框，选项 = Rust 命令 get_provider_catalog
 *     运行时直读 provider_catalog.toml（无 IPC 往返：首次配置时 sidecar 尚未启动）
 *   - 不再硬编码 PROVIDER_META / ALL_PROVIDERS 常量（单一真相源 = provider_catalog.toml）
 *   - sidecar 未就绪 / catalog 未拉到时，凭据表单 disabled
 *
 * ── 模型清单（PRD §模型管理 规则 2 / 4.3.1-2）───────────────────────────
 *   - model_id 完全用户填：系统不内置任何模型数据
 *   - emptyCredential 的 model_id 初始为空串（不再预填候选模型）
 *   - recommendedModels 只是 combobox 的**引导**清单，绝不限制可填值（R11）
 *
 * ── 主键（PRD §3.4 对象 C）──────────────────────────────────────────────
 *   - 凭据主键 = (provider, model_id)：一个 provider 可配 N 个模型
 *   - 因此「一个 provider 只能有一条凭据」的旧假设全部作废
 */

import { create } from "zustand";
import { invoke } from "@tauri-apps/api/core";
import i18n from "../i18n";
import type {
  ModelPriceEntry,
  ModelPricesPayload,
  ProviderMeta,
} from "../types/api";

// ── 类型定义 ─────────────────────────────────────────────────────────────

/**
 * Provider id 类型（白名单由后端 catalog 决定，前端不硬编码）。
 * 保留命名以兼容现有引用，实质为 string。
 */
export type LLMProvider = string;

/**
 * 单组 Provider 凭据。
 *
 * 主键 = `(provider, model_id)`：同一 provider 下可以有多条（PRD G1 / Story 1）。
 *
 * `is_default`：不落盘。用户 toml 只有一个顶层 `default_model_id`；后端下发时把
 * 「model_id === default_model_id」的那条标 true，提交时再由后端反推回去。
 *
 * `api_key`：
 *   - 后端下发（CREDENTIALS_LIST）时是**脱敏值**，只能看不能回写
 *   - 提交时若用户未点「更换密钥」，字段必须**整个省略**（R3），后端沿用旧值。
 *     回写脱敏值会把真 key 永久毁掉——这是本次重构要修的原始事故（R2 / AC-07）。
 *
 * 单价三级回落（R5）：用户填写值 > 系统默认表 > **拒绝保存**。
 * 留空即表示「用系统默认价」；绝不在前端补 0（§九：金额类字段缺失绝不默认 0）。
 */
export interface ProviderCredential {
  provider: LLMProvider;
  /** 未更换密钥时省略该字段（R3）；有值必须是明文真 key */
  api_key?: string;
  model_id: string;
  base_url?: string;
  is_default: boolean;
  /** 输入单价（CNY / 1k token）；与 output 同填同空 */
  input_price_per_1k?: number;
  /** 输出单价（CNY / 1k token）；与 input 同填同空 */
  output_price_per_1k?: number;
  /** 缓存命中单价（CNY / 1k token）；可选，留空取生效输入价（R6） */
  cache_read_price_per_1k?: number;
  /** 高峰时段输入单价（CNY / 1k token）；可选，留空 = 不分时 */
  peak_input_price_per_1k?: number;
  /** 高峰时段输出单价（CNY / 1k token）；可选，留空 = 不分时 */
  peak_output_price_per_1k?: number;
  /** 高峰时段缓存命中单价（CNY / 1k token）；可选，留空 = 不分时 */
  peak_cache_read_price_per_1k?: number;
}

/** 凭据校验状态 */
export type CredentialVerifyStatus =
  | "idle"
  | "verifying"
  | "success"
  | "failed";

/** 单组凭据校验结果 */
export interface VerifyResult {
  provider: LLMProvider;
  status: "idle" | "verifying" | "passed" | "failed";
  error?: string;
}

/** 配置状态（供门禁判断） */
export interface ConfigStatus {
  configured: boolean;
  credential_count: number;
  /** 由 default_provider 改名：切换粒度是模型不是 provider（PRD §3.4 关键变更） */
  default_model_id: string | null;
  /** v1 格式凭据文件：需引导用户备份后重新配置（后端不做兼容读取） */
  legacy_format?: boolean;
  /** default_model_id 是否指向真实存在的模型；false = 有凭据但默认失效 */
  default_resolvable?: boolean;
}

// ── Store 接口 ───────────────────────────────────────────────────────────

interface CredentialState {
  /** 已保存的凭据列表（来源：后端同步，api_key 脱敏） */
  credentials: ProviderCredential[];

  /** 凭据加载状态（仅 sidecar 启动后 LOAD_CREDENTIALS 期间为 true；初始 false） */
  loading: boolean;

  /** 整体校验状态 */
  verifyStatus: CredentialVerifyStatus;

  /** 逐组校验结果 */
  verifyResults: VerifyResult[];

  /** 保存中 */
  saving: boolean;

  /** 上一次保存的错误信息 */
  saveError: string | null;

  // ── Provider 元信息（catalog，从后端拉取，单一真相源）──
  /** 系统预置 provider 元信息列表（前端下拉源）；空数组 = catalog 未拉到 */
  providerCatalog: ProviderMeta[];
  /** catalog 是否已拉取（首次拉到后置 true；凭据表单据此决定是否可渲染） */
  catalogReady: boolean;
  /** catalog 拉取中 */
  catalogLoading: boolean;

  // ── 系统默认单价表（model_prices.toml，同样走 Rust 命令运行时读）──
  /**
   * 推荐模型清单 ≡ 系统默认单价表（PRD §4.3.1-2「数据复用」：同一份 toml 两个用途）。
   *
   * ⚠️ 它是**引导**，不是白名单（R11 / AC-06）。清单外的 model_id 必须照样能填、
   *    能存、能路由。任何据此过滤/禁用可选模型的代码都是白名单复活。
   */
  recommendedModels: ModelPriceEntry[];
  /** 汇率（1 USD = N CNY）；null = 尚未拉到。金额类字段，前端不得兜底默认值 */
  exchangeRateUsd: number | null;
  /** 单价表是否已拉到 */
  pricesReady: boolean;
  /** 单价表拉取中 */
  pricesLoading: boolean;
  /**
   * 单价表拉取失败原因（null = 无错）。
   * 必须在 UI 上可见：拉不到默认价意味着所有模型都要用户手填价，
   * 静默失败会让用户以为「这模型本来就没默认价」（§九：降级必留痕）。
   */
  pricesError: string | null;

  // ── 动作 ──

  /** 从后端加载凭据列表（LOAD_CREDENTIALS，需 sidecar 已启动） */
  loadCredentials: () => void;
  /** 连通性校验（VERIFY_CREDENTIALS，需 sidecar 已启动），不落盘 */
  verifyCredentials: (creds: ProviderCredential[]) => void;
  /** 查询门禁状态（GET_CREDENTIALS_STATUS） */
  checkStatus: () => void;
  /**
   * 拉取系统预置 provider 元信息。
   * 走 Rust 命令 `get_provider_catalog` 直读 toml——**没有** PROVIDER_CATALOG IPC 往返：
   * 首次配置时 sidecar 尚未启动，走 IPC 会死锁。
   */
  loadCatalog: () => void;
  /**
   * 拉取系统默认单价表。
   * 同样走 Rust 命令 `get_model_prices` 直读 toml，不依赖 sidecar（理由同 loadCatalog）。
   */
  loadModelPrices: () => void;
  /** 设置加载态 */
  setLoading: (v: boolean) => void;
  /** 设置凭据列表（由 BackendProvider 在收到 CREDENTIALS_LIST 时调用） */
  _setCredentialsFromBackend: (creds: ProviderCredential[]) => void;
  /** 设置校验结果（由 BackendProvider 在收到 CREDENTIALS_VERIFIED 时调用） */
  /**
   * @param backendSuccess 后端 CREDENTIALS_VERIFIED.success —— 权威判定，
   *   不可用 `results.every(...)` 替代：results 为空时 every 恒为 true，
   *   而后端正是用 `{success:false, results:[]}` 表达「入参非法/无可校验项」，
   *   据此推导会把后端明确的失败翻成绿灯（决策类字段，§九 fail-closed）。
   */
  _setVerifyResults: (results: VerifyResult[], backendSuccess: boolean) => void;
  /** 设置保存结果（由 BackendProvider 在收到 CREDENTIALS_SAVED 时调用） */
  _setSaveResult: (success: boolean, error?: string) => void;
  /**
   * 后端下发的权威配置状态（CREDENTIALS_STATUS）；null = 尚未收到。
   * 与 getConfigStatus(credentials) 的**本地派生**不同：只有后端能判断
   * legacy_format（v1 文件前端根本读不到）和 default_resolvable。
   */
  backendStatus: ConfigStatus | null;
  /** 更新状态（由 BackendProvider 在收到 CREDENTIALS_STATUS 时调用） */
  _updateStatus: (status: ConfigStatus) => void;
  /** 直接注入 catalog（供测试 / 预热用；正常路径走 loadCatalog 的 Rust 命令） */
  _setCatalog: (providers: ProviderMeta[]) => void;
  /** 重置校验状态 */
  resetVerify: () => void;
  /** 清除保存错误（切换配置界面时调用，避免上一处的失败信息串场） */
  clearSaveError: () => void;

  // ── 本地操作（不依赖 sidecar，Rust 直接读写 toml）──

  /** 检查本地 toml 是否已配置（Rust 直接读文件，返回 boolean） */
  checkLocal: () => Promise<boolean>;
  /** 保存凭据到本地 toml（Rust 直接写文件，用于首次配置，sidecar 尚未启动） */
  saveLocal: (creds: ProviderCredential[]) => Promise<void>;
}

// ── 内部工具 ─────────────────────────────────────────────────────────────

/**
 * 判断一个 api_key 是否是后端下发的**脱敏值**（形如 `sk-r***3456`）。
 *
 * 用途：提交前拦截——脱敏值一旦被回写就会把真 key 永久覆盖且不可恢复
 * （PRD R2 / AC-07，这正是本次重构要修的原始事故）。
 */
export function isMaskedKey(key: string | undefined | null): boolean {
  return typeof key === "string" && key.includes("***");
}

function _invokeIpc(type: string, extra: Record<string, unknown> = {}) {
  invoke("send_session_ipc", {
    payload: { type, msg_id: crypto.randomUUID(), ...extra },
  }).catch((e) => console.error(`[credentials-ipc] >>> ${type} failed:`, e));
}

// ── Store 实现 ───────────────────────────────────────────────────────────

export const useCredentialStore = create<CredentialState>()((set, get) => ({
  credentials: [],
  loading: false,
  backendStatus: null,
  verifyStatus: "idle",
  verifyResults: [],
  saving: false,
  saveError: null,
  providerCatalog: [],
  catalogReady: false,
  catalogLoading: false,
  recommendedModels: [],
  exchangeRateUsd: null,
  pricesReady: false,
  pricesLoading: false,
  pricesError: null,

  loadCredentials: () => {
    set({ loading: true });
    _invokeIpc("LOAD_CREDENTIALS");
  },

  verifyCredentials: (creds) => {
    set({ verifyStatus: "verifying", verifyResults: [] });
    _invokeIpc("VERIFY_CREDENTIALS", { credentials: creds });
  },

  checkStatus: () => {
    _invokeIpc("GET_CREDENTIALS_STATUS");
  },

  loadCatalog: async () => {
    if (get().catalogReady || get().catalogLoading) return;
    set({ catalogLoading: true });
    // 走 Rust 命令**运行时**直读 toml，不依赖 sidecar：
    // catalog 是静态系统配置，首次配置场景（sidecar 未启动）也必须能拉到。
    try {
      const result = await invoke<{ providers: ProviderMeta[] }>(
        "get_provider_catalog",
      );
      set({
        providerCatalog: result.providers ?? [],
        catalogReady: true,
        catalogLoading: false,
      });
    } catch (e) {
      console.error("[catalog] get_provider_catalog failed:", e);
      set({ catalogLoading: false });
    }
  },

  loadModelPrices: async () => {
    if (get().pricesReady || get().pricesLoading) return;
    set({ pricesLoading: true, pricesError: null });
    try {
      const result = await invoke<ModelPricesPayload>("get_model_prices");
      set({
        recommendedModels: result.prices ?? [],
        // 汇率不给默认值：金额类字段缺失即报错（§九 / PRD AC-14）。
        // 后端在非法时已拒绝启动，这里只是不替它兜底。
        exchangeRateUsd: result.exchange_rate_usd ?? null,
        pricesReady: true,
        pricesLoading: false,
        pricesError: null,
      });
    } catch (e) {
      // fail-closed 且**可感知**：拉不到默认价表 → 视为全表为空 → 所有模型转
      // 「必须用户填价」（PRD §4.3.2 异常分支）。绝不静默按 0 计，也绝不静默灰化。
      const msg = typeof e === "string" ? e : String(e);
      console.error("[prices] get_model_prices failed:", e);
      set({
        recommendedModels: [],
        exchangeRateUsd: null,
        pricesReady: false,
        pricesLoading: false,
        pricesError: `系统默认价表不可用，请手动填写单价（${msg}）`,
      });
    }
  },

  setLoading: (v) => set({ loading: v }),

  _setCredentialsFromBackend: (creds) =>
    set({ credentials: creds, loading: false }),

  _setVerifyResults: (results, backendSuccess) => {
    // 后端 success 与逐行结果取「与」：任一为假即失败（fail-closed）
    const allPassed =
      backendSuccess &&
      results.length > 0 &&
      results.every((r) => r.status === "passed");
    set({ verifyResults: results, verifyStatus: allPassed ? "success" : "failed" });
  },

  _setSaveResult: (success, error) =>
    set({ saving: false, saveError: success ? null : (error ?? i18n.t("cred.errSave")) }),

  _updateStatus: (status) => {
    // 必须落盘到 store：此前这里是空实现，checkStatus() 发出的 IPC 回包
    // 直接进垃圾桶，导致 legacy_format（v1 文件）永远无法触达用户——
    // 后端专门设计了不抛异常的状态上报路径，却在最后一跳被丢弃（§九 降级必留痕）。
    if (status.legacy_format) {
      console.warn(
        "[credentials] 检测到 v1 格式凭据文件，需备份后重新配置（legacy_format=true）",
      );
    } else if (status.credential_count > 0 && status.default_resolvable === false) {
      console.warn(
        "[credentials] 已配置凭据但默认模型失效（default_model_id=%s），需重新指定默认",
        status.default_model_id,
      );
    }
    set({ backendStatus: status });
  },

  _setCatalog: (providers) =>
    set({
      providerCatalog: providers,
      catalogReady: true,
      catalogLoading: false,
    }),

  resetVerify: () => set({ verifyStatus: "idle", verifyResults: [] }),

  clearSaveError: () => set({ saveError: null }),

  // ── 本地操作（不依赖 sidecar）──

  checkLocal: async () => {
    const result = await invoke<{ configured: boolean }>("check_llm_credentials");
    return !!result.configured;
  },

  saveLocal: async (creds) => {
    set({ saving: true, saveError: null });
    try {
      await invoke("save_llm_credentials", { credentials: creds });
      set({ saving: false, saveError: null });
    } catch (e) {
      const msg = typeof e === "string" ? e : String(e);
      set({ saving: false, saveError: msg });
      throw e;
    }
  },
}));

// ── 派生工具函数 ─────────────────────────────────────────────────────────

/**
 * 按 id 查 catalog 中的 provider 元信息。
 * catalog 未拉到时返回 undefined（调用方应据此 disable 表单）。
 */
export function getProviderMeta(
  provider: LLMProvider,
  catalog: ProviderMeta[],
): ProviderMeta | undefined {
  return catalog.find((p) => p.id === provider);
}

/** 判断当前是否已配置（≥1 组完整凭据） */
export function isConfigured(credentials: ProviderCredential[]): boolean {
  if (!Array.isArray(credentials)) return false;
  return credentials.some(
    (c) =>
      c != null &&
      typeof c.provider === "string" &&
      c.provider.length > 0 &&
      typeof c.api_key === "string" &&
      c.api_key.trim().length >= 8 &&
      typeof c.model_id === "string" &&
      c.model_id.trim().length > 0,
  );
}

/** 获取配置状态（供门禁使用） */
export function getConfigStatus(credentials: ProviderCredential[]): ConfigStatus {
  const configured = isConfigured(credentials);
  const defaultCred = credentials.find((c) => c.is_default);
  return {
    configured,
    credential_count: credentials.length,
    default_model_id: defaultCred?.model_id ?? null,
  };
}

/**
 * 生成空白的凭据表单模板。
 * 规则 2：系统不内置模型数据 → model_id 初始为空串（不再预填候选模型）。
 * 单价字段初始为 undefined，表示「还没决定」——不是 0（§九：金额类不给默认）。
 */
export function emptyCredential(provider: LLMProvider): ProviderCredential {
  return {
    provider,
    api_key: "",
    model_id: "",
    base_url: "",
    is_default: false,
  };
}

/**
 * 「添加模型」时的 provider 候选 = 系统目录全量。
 *
 * ⚠️ 这里刻意**不排除**已配置过的 provider：主键已改为 `(provider, model_id)`，
 *    同一 provider 可以配 N 个模型（PRD G1 / Story 1）。旧的
 *    unconfiguredProviders 会把 dashscope 配了 qwen-max 之后就不让再配
 *    qwen-turbo，正是要被删掉的那条容量限制。
 */
export function addableProviders(catalog: ProviderMeta[]): LLMProvider[] {
  return catalog.map((p) => p.id);
}

/**
 * 查某个 model_id 在系统默认单价表里的条目（**精确匹配**）。
 *
 * 不做前缀/模糊匹配：模糊匹配会让 qwen3-max 悄悄套用 qwen-max 的价，
 * 属于金额类字段的静默降级（PRD §3.4 匹配语义 / §九）。
 */
export function findSystemPrice(
  modelId: string,
  prices: ModelPriceEntry[],
): ModelPriceEntry | undefined {
  const id = modelId.trim();
  if (!id) return undefined;
  return prices.find((p) => p.model_id === id);
}

/**
 * 某 provider 下的推荐模型清单（combobox 数据源）。
 * 仅用于引导展示；清单为空时 combobox 退化为纯文本框（PRD §4.3.1-2）。
 */
export function recommendedForProvider(
  provider: LLMProvider,
  prices: ModelPriceEntry[],
): ModelPriceEntry[] {
  return prices.filter((p) => p.provider === provider);
}

/**
 * 判断一条凭据的单价来源（用于「系统默认价 / 我填的价 / 待补价」标签）。
 * 与后端 price_source 语义一致（PRD §4.3.2）。
 */
export function resolvePriceSource(
  cred: ProviderCredential,
  prices: ModelPriceEntry[],
): "user" | "system" | "missing" {
  if (cred.input_price_per_1k != null && cred.output_price_per_1k != null) {
    return "user";
  }
  return findSystemPrice(cred.model_id, prices) ? "system" : "missing";
}

/**
 * 提交前构造安全的凭据体。
 *
 * 核心：**未更换密钥就不带 api_key 字段**（PRD R3）。带上脱敏值会把真 key
 * 永久毁掉且不可恢复（R2 / AC-07）——这是必须守住的那条线。
 *
 * @param cred        本地编辑态凭据
 * @param keyChanged  用户是否点过「更换密钥」并重填了 key
 */
export function toSubmittable(
  cred: ProviderCredential,
  keyChanged: boolean,
): ProviderCredential {
  const { api_key, ...rest } = cred;
  if (!keyChanged || isMaskedKey(api_key) || !api_key) {
    // 省略字段本身，而不是传空串/传 null——空串也可能被后端当成「清空 key」。
    return rest as ProviderCredential;
  }
  return { ...rest, api_key };
}
