/**
 * src/components/WorkspaceGate.tsx
 *
 * 工作区门控：登录之后、进入 Agent 之前的一道关卡。
 *
 * Agent 文件工具的根目录只能由用户显式选择（不做任何探测），因此在用户
 * 「打开一个文件夹」之前，sidecar 不启动、Agent 不可用，界面停在本组件。
 *
 * - current !== null              → 已打开工作区，渲染子组件（切换期间不回退）
 * - status === "opening"（首次）  → 显示「正在打开工作区」
 * - 其它（picking / error）       → 显示「打开文件夹」界面 + 最近打开列表
 *
 * 样式：global-v2.css SECTION 30（.gate-*），无组件内样式对象。
 */

import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import { useWorkspaceStore } from "../store/workspaceStore";
import { GateScreen, GateLoading } from "./ui";

interface WorkspaceGateProps {
  children: React.ReactNode;
}

export function WorkspaceGate({ children }: WorkspaceGateProps) {
  const { t } = useTranslation();
  const { current, status, recent, error, init, openWorkspace, pickAndOpen } =
    useWorkspaceStore();

  useEffect(() => {
    if (status === "idle") {
      void init();
    }
  }, [status, init]);

  // 已打开工作区：直接渲染（切换工作区时 current 保持非空，不闪回门控）
  if (current !== null) {
    return <>{children}</>;
  }

  if (status === "opening" || status === "idle") {
    return <GateLoading text={t("workspace.opening")} />;
  }

  // picking / error：打开文件夹界面
  return (
    <GateScreen>
      <div className="gate-logo">🐼</div>
      <h1 className="gate-title">{t("workspace.title")}</h1>
      <p className="gate-subtitle">
        {t("workspace.subtitle")}
      </p>

      <button className="gate-btn" onClick={() => void pickAndOpen()}>
        {t("workspace.pickFolder")}
      </button>

      {error && <p className="gate-error-text">{t("workspace.openFailed", { error })}</p>}

      {recent.length > 0 && (
        <div className="gate-recent-box">
          <p className="gate-recent-title">{t("workspace.recentTitle")}</p>
          <ul className="gate-recent-list">
            {recent.map((path) => (
              <li key={path}>
                <button
                  className="gate-recent-item"
                  title={path}
                  onClick={() => void openWorkspace(path)}
                >
                  <span className="gate-recent-name">{basename(path)}</span>
                  <span className="gate-recent-path">{path}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </GateScreen>
  );
}

/** 取路径最后一段作为显示名（兼容 / 与 \ 分隔符）。 */
function basename(path: string): string {
  const parts = path.split(/[/\\]/).filter(Boolean);
  return parts.length > 0 ? parts[parts.length - 1] : path;
}
