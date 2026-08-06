/**
 * src/components/ChatLayout.tsx — v2 持久布局壳
 *
 * Sidebar（持久） + Outlet（右侧区域，技能/任务/聊天全覆盖）
 * 聊天页的 TopToolbar 留在 ChatPage 内部，不被此 Layout 控制。
 */

import { Outlet } from "react-router-dom";
import { WallpaperBackground } from "./WallpaperBackground";
import { LeftSidebar } from "./LeftSidebar";

export function ChatLayout() {
  return (
    <WallpaperBackground>
      <div style={{
        display: "flex",
        height: "100vh",
        overflow: "hidden",
      }}>
        <LeftSidebar />
        <div style={{
          flex: 1,
          display: "flex",
          flexDirection: "column",
          minWidth: 0,
          height: "100%",
        }}>
          <Outlet />
        </div>
      </div>
    </WallpaperBackground>
  );
}
