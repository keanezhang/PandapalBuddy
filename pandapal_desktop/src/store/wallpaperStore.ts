/**
 * src/store/wallpaperStore.ts
 *
 * 主题 / 色系状态管理 — Zustand + localStorage 持久化。
 *
 * 每个主题只是一个 id：真正的配色由 global-v2.css 的
 * `:root[data-theme="<id>"]` 集中覆写设计 token 决定（统一管理、方便切换）。
 * 仅「图片」主题需要额外把壁纸打到 <body>，故带 bodyBackground 字段。
 */

import { create } from "zustand";
import { persist } from "zustand/middleware";

export interface WallpaperItem {
  id: string;
  name: string;
  /** 选择器缩略图用的 CSS background 值 */
  preview: string;
  /** 仅「图片」主题需要：应用到 <body> 的背景值 */
  bodyBackground?: string;
}

/** 预设主题 / 色系 */
export const PRESET_WALLPAPERS: WallpaperItem[] = [
  {
    id: "panda",
    name: "图片",
    preview: "url(/wallpapers/panda.jpg) center / cover no-repeat",
    bodyBackground: "url(/wallpapers/panda.jpg) center / cover no-repeat",
  },
  {
    id: "default",
    name: "默认",
    // 紫 → 金，示意 accent + accent-2
    preview: "linear-gradient(135deg, #7C3AED, #EAB308)",
  },
  {
    id: "blue",
    name: "亮蓝 · 金",
    preview: "linear-gradient(135deg, #3B82F6, #EAB308)",
  },
  {
    id: "green",
    name: "亮绿 · 金",
    preview: "linear-gradient(135deg, #22C55E, #EAB308)",
  },
  {
    id: "mars",
    name: "马尔斯绿 · 金",
    preview: "linear-gradient(135deg, #018B8D, #FDEB55)",
  },
  {
    id: "light",
    name: "白色",
    preview: "linear-gradient(135deg, #FFFFFF, #E5E7EB)",
  },
];

interface WallpaperState {
  /** 当前激活的主题 ID */
  activeId: string;
  /** 主动切换主题 */
  setWallpaper: (id: string) => void;
}

export const useWallpaperStore = create<WallpaperState>()(
  persist(
    (set) => ({
      activeId: "default",
      setWallpaper: (id: string) => set({ activeId: id }),
    }),
    {
      name: "pandapal-wallpaper",
      version: 2,
    }
  )
);

/** 便捷选择器：根据 activeId 拿到完整主题对象 */
export function useActiveWallpaper(): WallpaperItem {
  const activeId = useWallpaperStore((s) => s.activeId);
  return (
    PRESET_WALLPAPERS.find((w) => w.id === activeId) ??
    PRESET_WALLPAPERS.find((w) => w.id === "default")!
  );
}
