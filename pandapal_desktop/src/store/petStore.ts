/**
 * src/store/petStore.ts
 *
 * 桌面宠物状态管理。
 *
 * 两类动画来源：
 *   1. baseAnim —— 由「Agent 是否在工作」推导的常态（idle / running），可被 usePetReactions 持续驱动。
 *   2. pulse   —— 一次性覆盖动画（完成挥手 waving / 出错 failed），播完自动回落到 baseAnim。
 *
 * 持久化：仅 currentSlug + enabled（宠物列表运行时从磁盘重新枚举，不入 localStorage）。
 */

import { create } from "zustand";
import { persist } from "zustand/middleware";
import { invoke } from "@tauri-apps/api/core";
import {
  type PetMeta,
  type RawPetMeta,
  type PetAnimState,
  type CatalogEntry,
  normalizePetMeta,
} from "../types/pet";

/** pulse 定时器句柄（模块级，避免进 state 触发无谓渲染） */
let pulseTimer: ReturnType<typeof setTimeout> | null = null;

/** 安装成功后：并入列表（去重）+ 切换为当前 + 清 installing */
function applyInstalled(
  set: (fn: (s: PetState) => Partial<PetState>) => void,
  pet: PetMeta,
): void {
  set((s) => {
    const rest = s.installedPets.filter((p) => p.slug !== pet.slug);
    return {
      installedPets: [...rest, pet].sort((a, b) =>
        a.displayName.localeCompare(b.displayName),
      ),
      currentSlug: pet.slug,
      installing: null,
    };
  });
}

interface PetState {
  /** 已安装宠物 */
  installedPets: PetMeta[];
  /** 当前展示的宠物 slug */
  currentSlug: string | null;
  /** 是否显示浮游宠物 */
  enabled: boolean;
  /** 常态动画（由 Agent 活动推导） */
  baseAnim: PetAnimState;
  /** 实际展示动画（pulse 覆盖时 ≠ baseAnim） */
  anim: PetAnimState;
  /** 安装中的 slug（用于按钮 loading / 防重复） */
  installing: string | null;

  /** 宠物商店总清单（懒加载，只拉一次） */
  catalog: CatalogEntry[];
  /** 清单加载中 */
  catalogLoading: boolean;

  /** 用户给宠物起的别名（按 slug 覆盖原名，持久化） */
  aliases: Record<string, string>;

  /** 当前宠物元数据（派生） */
  current: () => PetMeta | null;
  /** 宠物展示名：有别名用别名，否则用原名 */
  nameOf: (pet: PetMeta) => string;
  /** 给宠物起/改别名（空字符串=清除别名，回落原名） */
  setAlias: (slug: string, alias: string) => void;

  /** 枚举已安装宠物 */
  refresh: () => Promise<void>;
  /** 拉取商店总清单（已加载则跳过，force=true 强制刷新） */
  fetchCatalog: (force?: boolean) => Promise<void>;
  /** 从商店目录项直链安装 */
  installFromCatalog: (entry: CatalogEntry) => Promise<void>;
  /** 删除一只宠物 */
  remove: (slug: string) => Promise<void>;
  /** 切换当前宠物 */
  setCurrent: (slug: string) => void;
  /** 显隐开关 */
  setEnabled: (v: boolean) => void;

  /** 设置常态活动（true=Agent 工作中 → running；false → idle） */
  setBaseActivity: (running: boolean) => void;
  /** 播放一次性动画，durationMs 后回落到 baseAnim */
  pulse: (anim: PetAnimState, durationMs?: number) => void;
}

export const usePetStore = create<PetState>()(
  persist(
    (set, get) => ({
      installedPets: [],
      currentSlug: null,
      enabled: true,
      baseAnim: "idle",
      anim: "idle",
      installing: null,
      catalog: [],
      catalogLoading: false,
      aliases: {},

      current: () => {
        const { installedPets, currentSlug } = get();
        return installedPets.find((p) => p.slug === currentSlug) ?? null;
      },

      nameOf: (pet) => {
        const alias = get().aliases[pet.slug]?.trim();
        return alias || pet.displayName;
      },

      setAlias: (slug, alias) => {
        const v = alias.trim();
        set((s) => {
          const next = { ...s.aliases };
          if (v) next[slug] = v;
          else delete next[slug]; // 清空=回落原名
          return { aliases: next };
        });
      },

      refresh: async () => {
        const raw = await invoke<RawPetMeta[]>("list_pets");
        const pets = raw.map(normalizePetMeta);
        set((s) => {
          // 若当前 slug 已失效，回落到第一只
          const stillValid = s.currentSlug && pets.some((p) => p.slug === s.currentSlug);
          const currentSlug = stillValid ? s.currentSlug : (pets[0]?.slug ?? null);
          return { installedPets: pets, currentSlug };
        });
      },

      fetchCatalog: async (force = false) => {
        const s = get();
        if (s.catalogLoading) return;
        if (s.catalog.length > 0 && !force) return;
        set({ catalogLoading: true });
        try {
          const list = await invoke<CatalogEntry[]>("fetch_pet_catalog");
          set({ catalog: list, catalogLoading: false });
        } catch (e) {
          set({ catalogLoading: false });
          throw e;
        }
      },

      installFromCatalog: async (entry) => {
        if (get().installing) return;
        set({ installing: entry.slug });
        try {
          const raw = await invoke<RawPetMeta>("install_pet_urls", {
            slug: entry.slug,
            petJsonUrl: entry.petJsonUrl,
            spritesheetUrl: entry.spritesheetUrl,
            displayName: entry.displayName,
          });
          applyInstalled(set, normalizePetMeta(raw));
        } catch (e) {
          set({ installing: null });
          throw e;
        }
      },

      remove: async (slug) => {
        await invoke("remove_pet", { slug });
        // 顺带清掉该宠物的别名，避免残留
        set((s) => {
          if (!(slug in s.aliases)) return {};
          const next = { ...s.aliases };
          delete next[slug];
          return { aliases: next };
        });
        await get().refresh();
      },

      setCurrent: (slug) => set({ currentSlug: slug }),
      setEnabled: (v) => set({ enabled: v }),

      setBaseActivity: (running) => {
        const base: PetAnimState = running ? "running" : "idle";
        set((s) => ({
          baseAnim: base,
          // 没有 pulse 在播时，实际动画立即跟随 base
          anim: pulseTimer ? s.anim : base,
        }));
      },

      pulse: (anim, durationMs = 1600) => {
        if (pulseTimer) clearTimeout(pulseTimer);
        set({ anim });
        pulseTimer = setTimeout(() => {
          pulseTimer = null;
          set((s) => ({ anim: s.baseAnim }));
        }, durationMs);
      },
    }),
    {
      name: "pandapal-pet",
      partialize: (s) => ({ currentSlug: s.currentSlug, enabled: s.enabled, aliases: s.aliases }),
    },
  ),
);
