/**
 * ToolFeedbackBanner — ToolFeedbackProvider 反馈的统一呈现。
 *
 * 挂在 registry.renderTool 的**公共分发点**，不进任何具体渲染器：反馈与"是哪个工具"
 * 正交（门控今天管 write_file/edit_file，明天加密钥扫描就是别的工具），塞进
 * WriteRenderer 意味着每加一个 provider 都要巡一遍所有渲染器。
 *
 * ── 三态，且第三态必须是沉默 ────────────────────────────────────
 *   severity=info  → 绿：查过了，干净（后端 llm_visible=false，LLM 不花 token）
 *   severity=error/warning → 红/黄：查过了，有问题
 *   feedback 缺失  → **什么都不渲染**
 *
 * 最后一条是硬规则：feedback 缺失既可能是"没有 provider 关心这次调用"，也可能是
 * provider 降级了（ruff 没装/超时/崩溃）。这两者在线上无法区分，故此处**绝不**
 * 因"没有坏消息"就渲染绿灯 —— 那是在检查根本没跑时告诉用户"通过"。
 * 绿灯只认后端明确发来的 info 反馈，不靠"缺失"反推。
 */
import React from "react";
import type { ToolFeedback } from "../../../types/api";

type Tone = {
  color: string;
  bg: string;
  icon: string;
  /** 底部安心文案；null = 不显示 */
  footer: string | null;
};

const TONE: Record<ToolFeedback["severity"], Tone> = {
  error: {
    color: "var(--danger)",
    bg: "rgba(239,68,68,0.08)",
    icon: "✗",
    footer: "已把诊断交给 Agent，正在修改…",
  },
  warning: {
    color: "var(--warning)",
    bg: "rgba(245,158,11,0.08)",
    icon: "!",
    footer: "已提示 Agent 关注，不影响继续执行。",
  },
  info: {
    color: "var(--success)",
    bg: "rgba(34,197,94,0.08)",
    icon: "✓",
    footer: null,   // 通过态正文已自解释，再加一句footer 是噪音
  },
};

/** source → 给人看的名字。未知 source 原样显示，不隐藏（新 provider 无需改这里也能用）。 */
const SOURCE_LABEL: Record<string, string> = {
  code_quality_gate: "代码质量检查",
};

export function ToolFeedbackBanner({ feedback }: { feedback?: ToolFeedback | null }) {
  if (!feedback) return null;                       // 见文件头：缺失 ≠ 通过
  const tone = TONE[feedback.severity] ?? TONE.info;
  const label = SOURCE_LABEL[feedback.source] ?? feedback.source;
  const passed = feedback.severity === "info";

  return (
    <div
      style={{
        marginTop: 4,
        borderLeft: `2px solid ${tone.color}`,
        background: tone.bg,
        borderRadius: "0 4px 4px 0",
        padding: "6px var(--space-3)",
        fontSize: 11,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <span style={{ color: tone.color, fontWeight: 700, width: 10, textAlign: "center" }}>
          {tone.icon}
        </span>
        <span style={{ color: tone.color, fontWeight: 600 }}>
          {label}
          <span style={{ fontWeight: 400, marginLeft: 4 }}>
            {passed ? "通过" : "未通过"}
          </span>
        </span>
      </div>

      {/* 通过态：正文是一句话，用正常字体；未通过态：诊断是 `file:line:col CODE msg`
          的等宽文本，pre-wrap 保行结构 + 限高滚动（单文件几百条 error 不撑爆时间线）。 */}
      <pre
        style={{
          margin: "4px 0 0",
          maxHeight: passed ? undefined : 180,
          overflowY: passed ? undefined : "auto",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          fontFamily: passed ? "inherit" : "var(--font-mono)",
          fontSize: passed ? 11 : 10.5,
          lineHeight: 1.55,
          color: "var(--text-secondary)",
        }}
      >
        {feedback.text}
      </pre>

      {tone.footer && (
        <div style={{ marginTop: 4, fontSize: 10.5, color: "var(--text-tertiary)" }}>
          {tone.footer}
        </div>
      )}
    </div>
  );
}
