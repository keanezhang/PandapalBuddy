/**
 * src/providers/editFileOriginalPicker.ts
 *
 * edit_file 事件流中「修改前 original」候选选择逻辑（纯函数，零依赖）。
 * 独立成模块以便单测：不 import React / Tauri API，测试环境无连带依赖。
 */

/**
 * 从候选 original 集中挑选「修改前」内容。
 *
 * 背景：TOOL_START 时的多个 original 数据源（suggestion 基线 / TOOL_START 读盘兜底 /
 * loadAndOpenFile 打开缓存）与后端 edit_file 写盘存在竞态，可能全部拿到「修改后」内容。
 * 优先取与 suggested 不同的候选（= 修改前）；全部为空返回 null（调用方放弃 diff）；
 * 全部与 suggested 相同则返回第一个有效候选（调用方经 changed===false 显式留痕跳过）。
 */
export function pickOriginalCandidate(
  candidates: ReadonlyArray<string | null | undefined>,
  suggested: string,
): string | null {
  const valid = candidates.filter((c): c is string => typeof c === "string" && c.length > 0);
  return valid.find((c) => c !== suggested) ?? valid[0] ?? null;
}
