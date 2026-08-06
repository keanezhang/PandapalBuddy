/**
 * 公开发布的类型定义。
 *
 * 引擎层类型从 engine/types 重新导出，
 * 组件层类型直接在 InlineDiffEditor / CodeRenderer 中定义并导出。
 */

// ── 引擎层类型 ────────────────────────────────────────────────────────
export type { DiffEntry, Hunk, HunkType } from "./engine/types";

// ── 组件层类型 ────────────────────────────────────────────────────────
export type { InlineDiffEditorProps } from "./editor/InlineDiffEditor";
export type { CodeRendererProps } from "./CodeRenderer";

// ── Hook 类型 ─────────────────────────────────────────────────────────
export type { Suggestion } from "./useDiffSuggestions";
