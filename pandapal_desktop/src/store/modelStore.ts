/**
 * src/store/modelStore.ts
 *
 * 模型管理 Store。
 * 数据来源：后端 sidecar 握手（PANDAPAL_READY model=... provider=...）
 *   → Rust 解析 → "backend-ready" 事件 → BackendProvider → setBackendModel()。
 *
 * ── 展示名（PRD §模型管理 规则 2）───────────────────────────────────────
 *   - model_id 完全用户填：系统不内置任何模型数据
 *   - 展示名 = model_id 本身（不做美化映射）
 *   - 删除旧 MODEL_NAME_MAP / PROVIDER_NAME_MAP / resolveModelName（系统不内置模型数据）
 *
 * - 有模型时：展示真实 model_id
 * - 无模型时：availableModels 为空，UI 显示 "No Model"
 */

import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { ModelPriceSource } from "../types/api";
import i18n from "../i18n";

export interface ModelInfo {
  id: string;
  name: string;
  provider?: string;
  /**
   * 单价来源（后端 MODEL_LIST 下发）。
   * "missing" = 待补价：消费进未定价兜底桶，界面应打「待补价」徽标。
   *
   * ⚠️ 它**只影响标注**，绝不影响该模型能否被选中使用（PRD R11 / AC-06）。
   *    任何 `.filter(m => m.price_source !== "missing")` 都是白名单复活。
   */
  priceSource?: ModelPriceSource;
}

interface ModelState {
  currentModelId: string;
  availableModels: ModelInfo[];
  loading: boolean;

  /**
   * 上次「持久化选择已失效、被迫改选」的留痕（null = 无）。
   * PRD §4.3.3 异常分支：落到首个可用项时**必须留痕告警，禁止静默重置**——
   * 用户以为还在用 A，实际已经在用 B 且计费口径完全不同。
   */
  droppedSelection: { previous: string; fallback: string } | null;
  /** 用户已读该提示后清除 */
  clearDroppedSelection: () => void;

  /** 后端握手（backend-ready）注入当前激活/默认模型；不覆盖已由 MODEL_LIST 填充的清单 */
  setBackendModel: (modelId: string, provider?: string) => void;
  /** 后端 MODEL_LIST 下发的可选清单（真相源）；协调 currentModelId（尊重持久化选择，否则回落 default） */
  setAvailableModels: (models: ModelInfo[], defaultModelId: string) => void;
  /** 用户在 InputBar 选择模型（持久化，逐条消息发送时透传 model_id） */
  switchModel: (modelId: string) => void;
}

export const useModelStore = create<ModelState>()(
  persist(
    (set) => ({
      currentModelId: "",
      availableModels: [],
      loading: false,
      droppedSelection: null,

      clearDroppedSelection: () => set({ droppedSelection: null }),

      setBackendModel: (modelId, provider) => {
        const id = (modelId || "").trim();
        set((s) => {
          if (!id) {
            // 无模型：清单已由 MODEL_LIST 填充则保留，否则清空（UI 显示 No Model）
            return s.availableModels.length
              ? {}
              : { availableModels: [], currentModelId: "", loading: false };
          }
          const next: Partial<ModelState> = { loading: false };
          // 清单为空时用握手模型兜底（旧后端 / MODEL_LIST 尚未到达）
          // 展示名 = model_id 本身（规则 2：系统不内置模型数据，不做美化映射）
          if (!s.availableModels.length) {
            next.availableModels = [
              { id, name: id, provider },
            ];
          }
          // 尊重持久化选择：仅当当前为空时，用握手默认作为初值
          if (!s.currentModelId) next.currentModelId = id;
          return next;
        });
        console.debug("[model] backend model:", id);
      },

      setAvailableModels: (models, defaultModelId) => {
        set((s) => {
          const stillValid = models.some((m) => m.id === s.currentModelId);
          if (stillValid) {
            return { availableModels: models, loading: false, droppedSelection: null };
          }
          const fallback = defaultModelId || models[0]?.id || "";
          // 持久化的选择已不在清单中 → 落到兜底项，但**必须留痕**（PRD §4.3.3 / AC-11）。
          // 静默重置的危害在于：用户以为还在用原模型，实际已换了一个计费口径
          // 完全不同的模型，账单出来才发现（§九：降级必留痕）。
          const dropped = s.currentModelId
            ? { previous: s.currentModelId, fallback }
            : null;
          if (dropped) {
            console.warn(
              `[model] 持久化选择的模型已不可用：${dropped.previous} → 已切换至 ${
                dropped.fallback || i18n.t("model.noModels")
              }`,
            );
          }
          return {
            availableModels: models,
            currentModelId: fallback,
            loading: false,
            droppedSelection: dropped,
          };
        });
        console.debug(
          "[model] MODEL_LIST:",
          models.map((m) => `${m.id}(${m.priceSource ?? "?"})`),
          "default=",
          defaultModelId,
        );
      },

      switchModel: (modelId) => {
        set({ currentModelId: modelId });
        console.debug("[model] switch:", modelId);
      },
    }),
    {
      name: "pandapal-model",
      // 仅持久化用户选择；清单每次由后端 MODEL_LIST 重新下发（避免陈旧清单）
      partialize: (s) => ({ currentModelId: s.currentModelId }),
    },
  ),
);
