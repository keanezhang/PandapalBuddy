/**
 * engine 层 —— 纯算法，零运行时依赖，Node.js / 浏览器通用。
 *
 * @example
 * ```ts
 * import { computeDiff, groupHunks, hashStr } from "monaco-inline-diff-review/engine";
 * ```
 */
export { computeDiff } from "./diff";
export { groupHunks, hashStr } from "./hunk";
export type { DiffEntry, Hunk, HunkType } from "./types";
