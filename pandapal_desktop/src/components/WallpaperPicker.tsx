/**
 * src/components/WallpaperPicker.tsx
 *
 * 壁纸选择器 — 缩略图网格，点击切换。
 *
 * 样式统一在 global-v2.css · SECTION 26（.wallpaper-*）集中管理，
 * 组件仅负责结构与数据驱动的 background 值，方便前端整体切换风格。
 */

import { PRESET_WALLPAPERS, useWallpaperStore } from "../store/wallpaperStore";

export function WallpaperPicker() {
  const activeId = useWallpaperStore((s) => s.activeId);
  const setWallpaper = useWallpaperStore((s) => s.setWallpaper);

  return (
    <div className="wallpaper-grid">
      {PRESET_WALLPAPERS.map((w) => {
        const isActive = w.id === activeId;
        return (
          <button
            key={w.id}
            type="button"
            className={`wallpaper-item${isActive ? " active" : ""}`}
            onClick={() => setWallpaper(w.id)}
            title={w.name}
          >
            {/* 缩略图色块 — background 由壁纸数据动态注入 */}
            <div className="wallpaper-thumb" style={{ background: w.preview }} />

            {/* 名称 */}
            <div className="wallpaper-name">{w.name}</div>

            {/* 选中标记 */}
            {isActive && <div className="wallpaper-check">✓</div>}
          </button>
        );
      })}
    </div>
  );
}
