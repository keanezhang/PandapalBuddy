/**
 * src/pages/ChatPage.tsx — v2 重设计版
 *
 * 从 ChatLayout（Shell）拿到 Sidebar + WallpaperBackground。
 * 本组件仅渲染 TopToolbar + ChatView（ChatPanel + FileViewerPanel）。
 */

import { useState, useRef } from "react";
import { useTranslation } from "react-i18next";
import { MessageList } from "../components/ChatArea/MessageList";
import { InputBar } from "../components/InputBar";
import { TaskPanel } from "../components/TaskPanel/TaskPanel";
import { HitlModal } from "../components/HitlModal";
import { PlanApprovalModal } from "../components/PlanApprovalModal";
import { TaskNotificationModal } from "../components/TaskNotificationModal";
import { TopToolbar } from "../components/TopToolbar";
import { SplitDivider } from "../components/SplitDivider";
import { FileViewerPanel } from "../components/FileViewerPanel";
import { FloatingPet } from "../components/pet/FloatingPet";
import { usePetReactions } from "../hooks/usePetReactions";
import { useBackend } from "../providers/BackendProvider";
import { usePreferenceStore } from "../store/preferenceStore";
import { useModelStore } from "../store/modelStore";
import { useFileStore } from "../store/fileStore";
import { useSkillStore } from "../store/skillStore";

export function ChatPage() {
  const [inputValue, setInputValue] = useState("");
  const { sendMessage, stopGeneration } = useBackend();
  const containerRef = useRef<HTMLDivElement>(null);

  // 宠物随 Agent 活动做动作（streaming → running，结束 → 挥手）
  usePetReactions();

  const { splitRatio, viewerVisible, swapped, deepThinking, mode } = usePreferenceStore();
  const currentModelId = useModelStore((s) => s.currentModelId);
  // 模型信息由后端握手（backend-ready）注入，见 BackendProvider。

  const handleSend = () => {
    const text = inputValue.trim();
    if (!text) return;
    sendMessage(text, { deepThinking, modelId: currentModelId, mode });
    setInputValue("");
  };

  const handleStop = () => {
    stopGeneration();
  };

  const handleAttach = () => {
    useFileStore.getState().pickAndOpenFile();
  };

  const chatWidth = `${splitRatio * 100}%`;
  const viewerWidth = `${(1 - splitRatio) * 100}%`;
  const showViewer = viewerVisible();

  return (
    <>
      <TopToolbar />

      <div ref={containerRef} style={{
        flex: 1,
        display: "flex",
        minHeight: 0,
      }}>
        {swapped ? (
          <>
            {showViewer && <FileViewerPanel width={viewerWidth} />}
            <SplitDivider containerRef={containerRef} />
            <ChatPanel
              width={showViewer ? chatWidth : undefined}
              inputValue={inputValue}
              setInputValue={setInputValue}
              onSend={handleSend}
              onStop={handleStop}
              onAttach={handleAttach}
            />
          </>
        ) : (
          <>
            <ChatPanel
              width={showViewer ? chatWidth : undefined}
              inputValue={inputValue}
              setInputValue={setInputValue}
              onSend={handleSend}
              onStop={handleStop}
              onAttach={handleAttach}
            />
            <SplitDivider containerRef={containerRef} />
            {showViewer && <FileViewerPanel width={viewerWidth} />}
          </>
        )}
      </div>

      <HitlModal />
      <PlanApprovalModal />
      <TaskNotificationModal />
      <FloatingPet />
    </>
  );
}

function ChatPanel({
  inputValue,
  setInputValue,
  onSend,
  onStop,
  onAttach,
  width,
}: {
  inputValue: string;
  setInputValue: (v: string) => void;
  onSend: () => void;
  onStop?: () => void;
  onAttach?: () => void;
  width?: string;
}) {
  const activatedSkill = useSkillStore((s) => s.activatedSkill);
  const clearActivatedSkill = useSkillStore((s) => s.clearActivatedSkill);

  return (
    <div style={{
      ...(width ? { width } : { flex: 1 }),
      minWidth: 0,
      display: "flex",
      flexDirection: "column",
      height: "100%",
      overflow: "hidden",
    }}>
      {activatedSkill && (
        <ActivatedSkillBadge skill={activatedSkill} onClear={clearActivatedSkill} />
      )}
      <MessageList />
      <TaskPanel />
      <InputBar
        value={inputValue}
        onChange={setInputValue}
        onSend={onSend}
        onStop={onStop}
        onAttach={onAttach}
      />
    </div>
  );
}

import type { ActivatedSkill } from "../store/skillStore";

function ActivatedSkillBadge({ skill, onClear }: { skill: ActivatedSkill; onClear: () => void }) {
  const { t } = useTranslation();
  return (
    <div style={{
      margin: "0 var(--space-4) var(--space-1)",
      padding: "6px 12px",
      background: "color-mix(in srgb, var(--success) 6%, transparent)",
      border: "1px solid rgba(34,197,94,0.12)",
      borderRadius: "var(--radius-sm)",
      display: "flex",
      alignItems: "center",
      gap: "var(--space-2)",
      fontSize: "var(--text-xs)",
      color: "var(--success)",
    }}>
      <span style={{ fontWeight: 600 }}>
        {skill.skill_type === "ACTION" ? "⚡" : "📚"} {skill.skill_name}
      </span>
      {skill.tools.length > 0 && (
        <span style={{ fontSize: "var(--text-2xs)", opacity: 0.7 }}>
          ({skill.tools.join(", ")})
        </span>
      )}
      <button
        onClick={onClear}
        title={t("chat.disableSkill")}
        style={{
          marginLeft: "auto",
          background: "none",
          border: "none",
          color: "var(--success)",
          cursor: "pointer",
          fontSize: "var(--text-md)",
          lineHeight: 1,
          padding: "0 4px",
          fontFamily: "inherit",
        }}
      >
        ✕
      </button>
    </div>
  );
}
