/**
 * pandapal_desktop 内联的 Monaco inline diff 审阅模块（原独立包 monaco-inline-diff-review）。
 *
 * Inline code diff review for Monaco Editor —
 * per-hunk accept/reject, just like VS Code's built-in diff review.
 *
 * @example
 * ```tsx
 * // 完整方案：组件 + 状态管理
 * import { CodeRenderer, useDiffSuggestions } from "../monacoInlineDiff";
 *
 * // 只用 diff 组件
 * import { InlineDiffEditor } from "../monacoInlineDiff";
 *
 * // 纯算法（Node.js / 浏览器通用）
 * import { computeDiff, groupHunks } from "../monacoInlineDiff/engine";
 * ```
 */

// ── 组件 ───────────────────────────────────────────────────────────────
export { InlineDiffEditor } from "./editor/InlineDiffEditor";
export { CodeRenderer } from "./CodeRenderer";

// ── Hook ───────────────────────────────────────────────────────────────
export { useDiffSuggestions } from "./useDiffSuggestions";

// ── 类型 ───────────────────────────────────────────────────────────────
export type {
  DiffEntry,
  Hunk,
  HunkType,
  InlineDiffEditorProps,
  CodeRendererProps,
  Suggestion,
} from "./types";
