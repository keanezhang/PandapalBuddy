/**
 * src/components/ui/GateScreen.tsx
 *
 * 全屏门禁/状态页骨架：启动加载、错误提示、工作区选择等场景共用。
 * 样式类：.gate-*（SECTION 30）。
 */

import type { ReactNode } from "react";

export function GateScreen({ children }: { children: ReactNode }) {
  return <div className="gate-screen">{children}</div>;
}

/** 加载态：脉冲动画图标 + 提示文字 */
export function GateLoading({ text, icon = "🐼" }: { text: string; icon?: string }) {
  return (
    <GateScreen>
      <div className="gate-spinner">{icon}</div>
      <p className="gate-text">{text}</p>
    </GateScreen>
  );
}
