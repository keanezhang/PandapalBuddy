/**
 * src/components/WallpaperBackground.tsx
 *
 * 主题 / 壁纸容器：
 *   1. 在 <html> 上设 data-theme 属性；global-v2.css 的
 *      `:root[data-theme="<id>"]` 据此集中覆写设计 token（--accent / --bg-* /
 *      --text-* 等），所有引用这些 token 的组件自动换色 —— 配色统一在设计规范中管理。
 *   2. 仅「图片」主题需要把壁纸打到 <body>（其余色系纯靠 token 覆写）。
 *   3. 纯装饰行为，不阻挡任何交互事件。
 */

import type { ReactNode } from "react";
import { useLayoutEffect } from "react";
import { useActiveWallpaper } from "../store/wallpaperStore";

interface Props {
  children: ReactNode;
}

/** 应用主题：设置 html data-theme，并按需把壁纸打到 body */
function applyTheme(id: string, bodyBackground?: string) {
  document.documentElement.dataset.theme = id;

  if (bodyBackground) {
    document.body.style.background = bodyBackground;
    document.body.style.backgroundSize = "cover";
    document.body.style.backgroundPosition = "center";
    document.body.style.backgroundAttachment = "fixed";
    document.body.style.backgroundRepeat = "no-repeat";
  } else {
    // 非图片主题：清掉内联背景，回落到 CSS 的 var(--bg-root)（随主题 token 变化）
    document.body.style.background = "";
    document.body.style.backgroundSize = "";
    document.body.style.backgroundPosition = "";
    document.body.style.backgroundAttachment = "";
    document.body.style.backgroundRepeat = "";
  }
}

function resetTheme() {
  document.documentElement.dataset.theme = "default";
  document.body.style.background = "";
  document.body.style.backgroundSize = "";
  document.body.style.backgroundPosition = "";
  document.body.style.backgroundAttachment = "";
  document.body.style.backgroundRepeat = "";
}

export function WallpaperBackground({ children }: Props) {
  const theme = useActiveWallpaper();

  useLayoutEffect(() => {
    applyTheme(theme.id, theme.bodyBackground);
    return () => {
      resetTheme();
    };
  }, [theme.id, theme.bodyBackground]);

  return <>{children}</>;
}
