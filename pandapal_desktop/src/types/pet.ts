/**
 * src/types/pet.ts
 *
 * 桌面宠物（Petdex 兼容）类型与「帧布局约定」。
 *
 * 关键事实：pet.json 只有 { id, displayName, description, spritesheetPath }，
 * 不含帧布局。帧布局是 Codex/Petdex 渲染端的固定约定，这里用一张常量表复刻：
 *   - 单帧 192×208，spritesheet 8列×9行（推荐 1536×1872）
 *   - 每行 = 一个动作状态；各行有效帧数不同，行尾可能是空帧
 *   - 播放帧率约 8fps（官方默认 ~1100ms/6帧 ≈ 5.5fps，这里取 8fps 更活泼）
 *
 * ⚠ 各宠物的行序在官方是同一约定，但个别宠物可能有出入；如遇不匹配，
 *   仅需在此表调整 row/frames，全体宠物共用，无需改渲染逻辑。
 */

/** 单帧尺寸（px） */
export const FRAME_W = 192;
export const FRAME_H = 208;
/** 播放帧率 */
export const PET_FPS = 8;

/** 一个动作在精灵图中的定义 */
export interface AnimSpec {
  /** 所在行（0-based） */
  row: number;
  /** 该行有效帧数 */
  frames: number;
  /** 是否循环播放（false = 播放一次后由上层切回 idle） */
  loop: boolean;
}

/** 我们使用到的动作状态名 */
export type PetAnimState =
  | "idle"
  | "running"
  | "waving"
  | "failed"
  | "jumping"
  | "waiting"
  | "review";

/**
 * 帧布局约定表（复刻自 kenshin 官方精灵图，9 行）。
 * row 顺序：idle / runRight / runLeft / waving / jumping / failed / waiting / running / review
 */
export const PET_ANIMATIONS: Record<PetAnimState, AnimSpec> = {
  idle: { row: 0, frames: 6, loop: true },
  running: { row: 7, frames: 6, loop: true },
  waving: { row: 3, frames: 4, loop: false },
  failed: { row: 5, frames: 8, loop: false },
  jumping: { row: 4, frames: 5, loop: false },
  waiting: { row: 6, frames: 6, loop: true },
  review: { row: 8, frames: 6, loop: true },
};

/** 与 Rust `PetMeta` 对应（serde 默认 snake_case → 这里手动声明） */
export interface PetMeta {
  slug: string;
  displayName: string;
  description: string;
  spritesheetPath: string;
  petJsonPath: string;
}

/** Rust 侧 serde 序列化为 snake_case，此处做一次归一 */
export interface RawPetMeta {
  slug: string;
  display_name: string;
  description: string;
  spritesheet_path: string;
  pet_json_path: string;
}

/** 宠物商店目录项（来自官方 manifest，Rust 侧已 camelCase 序列化） */
export interface CatalogEntry {
  slug: string;
  displayName: string;
  kind: string;
  submittedBy: string;
  spritesheetUrl: string;
  petJsonUrl: string;
}

export function normalizePetMeta(raw: RawPetMeta): PetMeta {
  return {
    slug: raw.slug,
    displayName: raw.display_name,
    description: raw.description,
    spritesheetPath: raw.spritesheet_path,
    petJsonPath: raw.pet_json_path,
  };
}
