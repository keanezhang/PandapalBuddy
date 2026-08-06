/**
 * src/components/InputBar.tsx — v2 重设计版
 *
 * 胶囊输入栏（垂直布局）：
 *   ┌───────────────────────────────────────────┐
 *   │  Textarea（大输入区，无高度限制）            │
 *   │  ─────────────────────────────────────────  │
 *   │  [📎 文件] [🧠 深度思考] [模型 ▼]   … [发送] │
 *   └───────────────────────────────────────────┘
 * 全部使用 var(--xxx) v2 Token
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { useConnectionStore } from "../store/connectionStore";
import { useIsStreaming, useIsStopping } from "../store/chatStore";
import { usePreferenceStore } from "../store/preferenceStore";
import { useModelStore } from "../store/modelStore";

interface InputBarProps {
  value: string;
  onChange: (val: string) => void;
  onSend: () => void;
  onAttach?: () => void;
  onStop?: () => void;
}

export function InputBar({ value, onChange, onSend, onAttach, onStop }: InputBarProps) {
  const status = useConnectionStore((s) => s.status);
  const isStreaming = useIsStreaming();
  const isStopping = useIsStopping();
  const deepThinking = usePreferenceStore((s) => s.deepThinking);
  const toggleDeepThinking = usePreferenceStore((s) => s.toggleDeepThinking);

  const currentModelId = useModelStore((s) => s.currentModelId);
  const availableModels = useModelStore((s) => s.availableModels);
  const switchModel = useModelStore((s) => s.switchModel);
  const currentModel = availableModels.find((m) => m.id === currentModelId);

  const [modelDropdownOpen, setModelDropdownOpen] = useState(false);
  const modelRef = useRef<HTMLDivElement>(null);

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const isComposingRef = useRef(false);

  const isConnected = status === "connected";
  const canSend = isConnected && !isStreaming && value.trim().length > 0;

  useEffect(() => {
    if (isConnected && !isStreaming) {
      requestAnimationFrame(() => { textareaRef.current?.focus(); });
    }
  }, [isConnected, isStreaming]);

  // 自动增高：无高度上限，随内容增长
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${ta.scrollHeight}px`;
  }, [value]);

  useEffect(() => {
    if (!modelDropdownOpen) return;
    const handler = (e: MouseEvent) => {
      if (modelRef.current && !modelRef.current.contains(e.target as Node)) {
        setModelDropdownOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [modelDropdownOpen]);

  const handleCompositionStart = useCallback(() => { isComposingRef.current = true; }, []);
  const handleCompositionEnd = useCallback(() => { isComposingRef.current = false; }, []);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.nativeEvent.isComposing || isComposingRef.current) return;
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (canSend) onSend();
    }
  };

  const placeholder = !isConnected
    ? "等待后端连接中…"
    : isStopping
      ? "正在停止…"
      : isStreaming
        ? "AI 正在回复中…"
        : "随心输入，Enter 发送，Shift+Enter 换行";

  // 统一的三态胶囊按钮：rest（静默）→ hover（浅底高亮）→ active/selected（紫色底）
  const PILL_ACTIVE_BG = "rgba(124,58,237,0.12)";
  const PILL_ACTIVE_COLOR = "var(--accent-soft)";
  const pillBtnStyle: React.CSSProperties = {
    display: "inline-flex", alignItems: "center", gap: 6,
    height: 30, padding: "0 10px", borderRadius: "var(--radius-full)",
    border: "none",
    background: "transparent",
    color: "var(--text-tertiary)", fontSize: 12, cursor: "pointer",
    transition: "background var(--duration-fast), color var(--duration-fast)",
    whiteSpace: "nowrap",
  };
  /** 鼠标进入：active 保持选中态，否则浅底高亮 */
  const pillHover = (el: HTMLButtonElement, active: boolean) => {
    if (active) return;
    el.style.background = "var(--bg-hover)";
    el.style.color = "var(--text-primary)";
  };
  /** 鼠标离开：回到 active 选中态或静默态 */
  const pillRest = (el: HTMLButtonElement, active: boolean) => {
    el.style.background = active ? PILL_ACTIVE_BG : "transparent";
    el.style.color = active ? PILL_ACTIVE_COLOR : "var(--text-tertiary)";
  };
  /** active/selected 的基础内联样式 */
  const pillActiveStyle = (active: boolean): React.CSSProperties =>
    active ? { background: PILL_ACTIVE_BG, color: PILL_ACTIVE_COLOR } : {};

  return (
    <div style={{ padding: "var(--space-4) var(--space-6) var(--space-5)" }}>
      <div style={{ width: "100%", maxWidth: 1200, margin: "0 auto" }}>
        <div
          className="input-capsule"
          style={{
            display: "flex", flexDirection: "column", gap: "var(--space-2)",
            padding: "var(--space-3)",
            background: "var(--bg-elevated)",
            border: "1px solid var(--border-strong)",
            borderRadius: "var(--radius-xl)",
            transition: "border-color var(--duration-fast)",
          }}
        >
          {/* 输入框 */}
          <textarea
            ref={textareaRef}
            className="chat-input-textarea"
            value={value}
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={handleKeyDown}
            onCompositionStart={handleCompositionStart}
            onCompositionEnd={handleCompositionEnd}
            placeholder={placeholder}
            disabled={!isConnected}
            autoFocus
            rows={2}
            style={{
              width: "100%", padding: "4px 6px",
              border: "none", background: "transparent",
              color: isConnected ? "var(--text-primary)" : "var(--text-muted)",
              fontFamily: "var(--font-sans)", fontSize: 15, lineHeight: 1.6,
              resize: "none", outline: "none", boxShadow: "none",
              minHeight: 52,
            }}
          />

          {/* 底部控制行：文件选择 · 深度思考 · 模型选择 …… 发送 */}
          <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
            {/* 文件选择 */}
            <button
              type="button"
              onClick={onAttach}
              disabled={!isConnected || isStreaming}
              style={{
                ...pillBtnStyle,
                cursor: isConnected && !isStreaming ? "pointer" : "not-allowed",
                opacity: isConnected ? 1 : 0.4,
              }}
              onMouseEnter={(e) => { if (isConnected && !isStreaming) pillHover(e.currentTarget, false); }}
              onMouseLeave={(e) => pillRest(e.currentTarget, false)}
              title="添加附件"
            >
              Files
            </button>

            {/* 深度思考 */}
            <button
              type="button"
              onClick={toggleDeepThinking}
              style={{ ...pillBtnStyle, ...pillActiveStyle(deepThinking) }}
              onMouseEnter={(e) => pillHover(e.currentTarget, deepThinking)}
              onMouseLeave={(e) => pillRest(e.currentTarget, deepThinking)}
              title="深度思考模式"
            >
              DeepThinking
            </button>

            {/* 模型选择 */}
            <div ref={modelRef} style={{ position: "relative" }}>
              <button
                type="button"
                onClick={() => setModelDropdownOpen((o) => !o)}
                disabled={availableModels.length === 0}
                style={{
                  ...pillBtnStyle,
                  ...pillActiveStyle(modelDropdownOpen),
                  cursor: availableModels.length > 0 ? "pointer" : "not-allowed",
                }}
                onMouseEnter={(e) => { if (availableModels.length > 0) pillHover(e.currentTarget, modelDropdownOpen); }}
                onMouseLeave={(e) => pillRest(e.currentTarget, modelDropdownOpen)}
                title="model"
              >
                {currentModel?.name || "No Model"}
                {availableModels.length > 0 && <span style={{ fontSize: 7, opacity: 0.5 }}>▼</span>}
              </button>

              {modelDropdownOpen && availableModels.length > 0 && (
                <div className="dropdown-menu" style={{ bottom: "calc(100% + 6px)", top: "auto" }}>
                  {/* ⚠️ 清单不做任何按 priceSource 的过滤/置灰：定价表不是白名单，
                      「待补价」只是标注，不影响该模型可选可用（PRD R11 / AC-06）。 */}
                  {availableModels.map((m) => (
                    <div
                      key={m.id}
                      onClick={() => { switchModel(m.id); setModelDropdownOpen(false); }}
                      className={`dropdown-item${m.id === currentModelId ? " selected" : ""}`}
                      title={
                        m.priceSource === "missing"
                          ? "该模型单价已失效，请到设置补填，期间消费不计入预算"
                          : m.priceSource === "user"
                            ? "使用你填写的单价核算"
                            : "使用系统默认价核算"
                      }
                    >
                      {currentModelId === m.id && "✓ "}{m.name}
                      {m.priceSource === "missing" && (
                        <span style={{ marginLeft: 6, fontSize: 9.5, color: "var(--warning)" }}>
                          待补价
                        </span>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* 弹性占位 */}
            <div style={{ flex: 1 }} />

            {/* 停止 / 收尾中 / 发送 */}
            {isStreaming ? (
              isStopping ? (
                <button
                  type="button"
                  disabled
                  title="正在停止…"
                  className="stop-btn stopping"
                >
                  <span className="stop-spinner" />
                </button>
              ) : (
                <button
                  type="button"
                  onClick={onStop}
                  title="停止生成"
                  className="stop-btn"
                >
                  ■
                </button>
              )
            ) : (
              <button
                onClick={canSend ? onSend : undefined}
                disabled={!canSend}
                title="发送 (Enter)"
                style={{
                  width: 32, height: 32,
                  borderRadius: "var(--radius-full)",
                  border: "none",
                  background: canSend ? "var(--accent)" : "rgba(255,255,255,0.08)",
                  color: canSend ? "#fff" : "var(--text-muted)",
                  fontSize: 16,
                  cursor: canSend ? "pointer" : "not-allowed",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  flexShrink: 0,
                  transition: "background var(--duration-fast), transform var(--duration-fast)",
                }}
                onMouseEnter={(e) => { if (canSend) { e.currentTarget.style.background = "var(--accent-soft)"; e.currentTarget.style.transform = "scale(1.05)"; } }}
                onMouseLeave={(e) => { e.currentTarget.style.background = canSend ? "var(--accent)" : "rgba(255,255,255,0.08)"; e.currentTarget.style.transform = "scale(1)"; }}
              >
                ↑
              </button>
            )}
          </div>
        </div>

        <div style={{
          textAlign: "center", fontSize: 10, color: "var(--text-muted)",
          marginTop: "var(--space-2)",
        }}>
          模型可能会产生错误信息，请核实重要信息。 <span style={{ color: "var(--text-tertiary)" }}>按 ⌘K 可以使用 PandaPal</span>
        </div>
      </div>
    </div>
  );
}
