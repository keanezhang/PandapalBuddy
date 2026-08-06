/**
 * src/store/commandPaletteStore.ts
 *
 * 命令面板（⌘K 搜索弹层）开闭状态。
 *
 * 单一职责：只持有「面板是否打开」这一全局状态，
 * 供主导航「搜索」按钮、全局快捷键（⌘K / Ctrl+K）共同驱动。
 *
 * 检索逻辑与结果渲染在 CommandPalette 组件内，数据直接从
 * sessionStore / taskSchedulerStore / skillStore 派生，本 store 不缓存结果。
 *
 * 将来扩展 RAG / 知识库时，只需在 CommandPalette 增加结果分组，
 * 本 store 无需改动。
 */
import { create } from "zustand";

interface CommandPaletteState {
  open: boolean;
  openPalette: () => void;
  closePalette: () => void;
  toggle: () => void;
}

export const useCommandPaletteStore = create<CommandPaletteState>((set, get) => ({
  open: false,
  openPalette: () => set({ open: true }),
  closePalette: () => set({ open: false }),
  toggle: () => set({ open: !get().open }),
}));
