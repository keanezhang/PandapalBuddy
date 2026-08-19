/**
 * src/store/skillStore.ts
 *
 * Skill 资源管理 Store。
 *
 * 消息流：
 *   前端发 SKILL_LIST → Python 回 SKILL_LIST_RESULT
 *   前端发 SKILL_GET  → Python 回 SKILL_GET_RESULT
 *   前端发 SKILL_SAVE / SKILL_DELETE → Python 回 SKILL_LIST_RESULT（全量刷新）
 *
 * 数据类型对齐后端 SkillSummary wire format。
 */

import { create } from "zustand";
import type { SkillItem } from "../types/api";

// ── 详情类型（含 content，来自 SKILL_GET_RESULT）
export interface SkillDetailData extends SkillItem {
  content: string;
}

// ── 草稿类型（localStorage 持久化，每个 skill 最多 1 份）
export interface SkillDraft {
  name: string;           // 空字符串 = 新建，有值 = 编辑
  description: string;
  when_to_use: string;
  content: string;
  tags: string[];
}

// ── 激活状态类型（来自 SKILL_ACTIVATED）
export interface ActivatedSkill {
  skill_name: string;
}

// ── 草稿 localStorage key
function draftKey(skillName: string): string {
  return skillName ? `skill_draft_${skillName}` : "skill_draft__new";
}

function loadDraft(skillName: string): SkillDraft | null {
  try {
    const raw = localStorage.getItem(draftKey(skillName));
    if (!raw) return null;
    return JSON.parse(raw) as SkillDraft;
  } catch {
    return null;
  }
}

function saveDraft(draft: SkillDraft): void {
  try {
    localStorage.setItem(draftKey(draft.name), JSON.stringify(draft));
  } catch {
    // localStorage 满或不可用时静默失败
  }
}

function removeDraft(skillName: string): void {
  try {
    localStorage.removeItem(draftKey(skillName));
  } catch {
    // 静默
  }
}

// ── 状态
interface SkillState {
  skills: SkillItem[];
  loading: boolean;

  // 列表数据
  setSkills: (skills: SkillItem[]) => void;
  setLoading: (loading: boolean) => void;

  // D2 Push：全量替换
  replaceAll: (skills: SkillItem[]) => void;

  // 增量（单条更新/删除会后端推全量列表，但 store 仍提供增量为乐观更新预留）
  upsertSkill: (skill: SkillItem) => void;
  removeSkill: (name: string) => void;

  // 详情面板选中态 + 内容
  selectedSkillName: string | null;
  selectSkill: (name: string) => void;
  clearSelection: () => void;

  detailSkill: SkillDetailData | null;
  detailLoading: boolean;
  setDetailSkill: (detail: SkillDetailData | null) => void;
  setDetailLoading: (loading: boolean) => void;

  // ── 草稿 ─────────────────────────────────
  draft: SkillDraft | null;
  setDraft: (draft: SkillDraft | null) => void;
  clearDraft: () => void;
  /** 检查指定 skill 是否有草稿（不加载到 state，仅查询） */
  hasDraft: (skillName: string) => boolean;

  // ── 搜索过滤 ─────────────────────────────
  searchQuery: string;
  setSearchQuery: (query: string) => void;

  // ── 运行时激活状态 ───────────────────────
  activatedSkill: ActivatedSkill | null;
  setActivatedSkill: (skill: ActivatedSkill | null) => void;
  clearActivatedSkill: () => void;
}

export const useSkillStore = create<SkillState>((set, get) => ({
  skills: [],
  loading: false,

  setSkills: (skills) =>
    set({
      skills,
      loading: false,
    }),

  setLoading: (loading) => set({ loading }),

  replaceAll: (skills) => set({ skills, loading: false }),

  upsertSkill: (skill) => {
    const skills = [...get().skills];
    const idx = skills.findIndex((s) => s.name === skill.name);
    if (idx >= 0) {
      skills[idx] = skill;
    } else {
      skills.push(skill);
    }
    set({ skills });
  },

  removeSkill: (name) => {
    set({ skills: get().skills.filter((s) => s.name !== name) });
  },

  selectedSkillName: null,
  selectSkill: (name) => set({ selectedSkillName: name }),
  clearSelection: () => set({ selectedSkillName: null }),

  detailSkill: null,
  detailLoading: false,
  setDetailSkill: (detail) => set({ detailSkill: detail, detailLoading: false }),
  setDetailLoading: (loading) => set({ detailLoading: loading }),

  // ── 草稿 ─────────────────────────────────
  draft: null,
  setDraft: (draft) => {
    if (draft) {
      saveDraft(draft);
    }
    set({ draft });
  },
  clearDraft: () => {
    const current = get().draft;
    if (current) {
      removeDraft(current.name);
    }
    set({ draft: null });
  },
  hasDraft: (skillName: string) => {
    return loadDraft(skillName) !== null;
  },

  // ── 搜索过滤 ─────────────────────────────
  searchQuery: "",
  setSearchQuery: (query) => set({ searchQuery: query }),

  // ── 运行时激活状态 ───────────────────────
  activatedSkill: null,
  setActivatedSkill: (skill) => set({ activatedSkill: skill }),
  clearActivatedSkill: () => set({ activatedSkill: null }),
}));

// ── 派生选择器 ──────────────────────────────────────────────────────────

/** 按 searchQuery 过滤的 skill 列表 */
export function useFilteredSkills(): { system: SkillItem[]; user: SkillItem[] } {
  const skills = useSkillStore((s) => s.skills);
  const query = useSkillStore((s) => s.searchQuery).toLowerCase().trim();

  if (!query) {
    return {
      system: skills.filter((s) => s.source === "system"),
      user: skills.filter((s) => s.source === "user"),
    };
  }

  const match = (s: SkillItem): boolean =>
    s.name.toLowerCase().includes(query) ||
    s.description.toLowerCase().includes(query) ||
    s.tags.some((t) => t.toLowerCase().includes(query));

  return {
    system: skills.filter((s) => s.source === "system" && match(s)),
    user: skills.filter((s) => s.source === "user" && match(s)),
  };
}
