/**
 * src/providers/editDiffReconstructor.ts
 *
 * 用 TOOL_END 事件自带的 unified diff（后端 EditResult.__tool_format_for_llm__
 * 格式化输出） + 磁盘上的「修改后」内容，**反推「修改前」原文**。
 *
 * 为什么需要它：前端在 TOOL_START 时读盘捕获 original 与后端 edit_file 写盘存在
 * **必然性竞态**——后端 emit TOOL_START 后同步执行写盘（≈1-3ms），而 TOOL_START
 * 事件经 IPC 到达前端再读盘 ≈5-20ms，对从未打开过的文件几乎总是晚于写盘，拿到
 * 的是「修改后」内容。本模块用事件自带的 diff 反推原文，不依赖任何读盘时机，零竞态。
 *
 * 纯函数、零依赖（不 import React / Tauri API），独立成模块以便单测。
 */

export interface DiffHunk {
  /** original 侧起始行（1-based，difflib 在新增文件时为 0） */
  oldStart: number;
  /** original 侧行（去前缀前：'-' 删除行 + ' ' 上下文行） */
  oldLines: string[];
  /** updated 侧起始行（1-based） */
  newStart: number;
  /** updated 侧行（去前缀前：'+' 添加行 + ' ' 上下文行） */
  newLines: string[];
}

const HUNK_HEAD_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

/**
 * 从 EditResult 的完整格式化字符串中提取 unified diff 部分。
 *
 * 格式化输出形如：
 *   ✅ 已编辑：{path}（替换 N 处）
 *      变更：+X 行  -Y 行
 *
 *   --- a/{path}
 *   +++ b/{path}
 *   @@ -27,7 +27,7 @@
 *   ...
 *
 * 找不到 `--- ` 头（空 diff / 格式异常）时返回 null。
 */
export function extractUnifiedDiff(fullResult: string | null | undefined): string | null {
  if (!fullResult) return null;
  const m = fullResult.match(/^--- .*$/m);
  if (!m || m.index === undefined) return null;
  return fullResult.slice(m.index);
}

/**
 * 解析 unified diff 文本为 hunk 列表。
 *
 * 失败返回 null（调用方 fallback）：
 * - 无 hunk（空 diff）
 * - hunk 头格式异常
 * - hunk 内 old/new 行数计数与 @@ 声明不符（含 difflib 截断标记 "..."）
 *
 * 说明：difflib 的 `\ No newline at end of file` 行（'\' 前缀）不参与计数，跳过。
 */
export function parseUnifiedDiff(diff: string): DiffHunk[] | null {
  // 注意：用 split("\n") 而非 split(/\r?\n/)——CRLF 文件的内容行内嵌 \r，
  // /\r?\n/ 会把行尾 \r 一并消费掉，导致反推时行尾丢失；split("\n") 保留 \r，
  // 与 suggested（磁盘读出的 CRLF 原样）保持一致，normCrlf 比较时再忽略。
  const lines = diff.split("\n");
  const hunks: DiffHunk[] = [];
  let i = 0;
  // 跳过 `--- ` / `+++ ` 文件头（若存在）
  while (i < lines.length && (lines[i].startsWith("--- ") || lines[i].startsWith("+++ "))) {
    i++;
  }
  for (; i < lines.length; ) {
    const line = lines[i];
    if (line.startsWith("@@ ")) {
      const m = line.match(HUNK_HEAD_RE);
      if (!m) return null; // hunk 头格式异常
      const oldStart = Number(m[1]);
      const oldCount = m[2] ? Number(m[2]) : 1;
      const newStart = Number(m[3]);
      const newCount = m[4] ? Number(m[4]) : 1;
      i++;
      const oldLines: string[] = [];
      const newLines: string[] = [];
      while (i < lines.length && !lines[i].startsWith("@@ ")) {
        const l = lines[i];
        if (l.startsWith("...")) return null; // difflib 截断标记 → 无法完整反推
        if (l.startsWith("-") || l.startsWith(" ")) oldLines.push(l);
        if (l.startsWith("+") || l.startsWith(" ")) newLines.push(l);
        i++;
      }
      if (oldLines.length !== oldCount || newLines.length !== newCount) return null;
      hunks.push({ oldStart, oldLines, newStart, newLines });
    } else {
      // 非 hunk 行（理论不应出现）：跳过
      i++;
    }
  }
  return hunks.length > 0 ? hunks : null;
}

const normCrlf = (s: string): string => s.replace(/\r$/, "");

/**
 * 用 suggested（磁盘「修改后」全文）+ diff 反推 original（「修改前」全文）。
 *
 * 算法：unified diff 逆应用——从 suggested 出发，从**最后一个 hunk** 开始
 * （先替换后面的行号不会漂移），把每个 hunk 的 updated 侧区间（'+'/' ' 行）
 * 替换回 original 侧区间（'-'/' ' 行）。替换前逐行校验 suggested 与 hunk
 * 的 updated 侧内容一致（忽略行尾 \r），不一致说明磁盘状态与 diff 不符
 * （diff 截断 / 编辑后被外部修改），返回 null 让调用方 fallback。
 */
export function reconstructOriginal(suggested: string, diff: string): string | null {
  const hunks = parseUnifiedDiff(diff);
  if (!hunks) return null;
  const work = suggested.split("\n");
  for (let h = hunks.length - 1; h >= 0; h--) {
    const hunk = hunks[h];
    const newLines = hunk.newLines.map((l) => l.slice(1)); // 去 '+'/' ' 前缀
    const oldLines = hunk.oldLines.map((l) => l.slice(1)); // 去 '-'/' ' 前缀
    const startIdx = hunk.newStart - 1;
    if (startIdx < 0 || startIdx + newLines.length > work.length) return null; // 行号越界
    for (let k = 0; k < newLines.length; k++) {
      if (normCrlf(work[startIdx + k]) !== normCrlf(newLines[k])) {
        return null; // 与磁盘内容不符 → 放弃
      }
    }
    work.splice(startIdx, newLines.length, ...oldLines);
  }
  return work.join("\n");
}

/**
 * 组合入口：从事件 result_full + 磁盘 suggested 反推 original。
 * 任一步失败返回 null。
 */
export function reconstructOriginalFromResult(
  suggested: string,
  resultFull: string | null | undefined,
): string | null {
  const diff = extractUnifiedDiff(resultFull);
  if (diff == null) return null;
  return reconstructOriginal(suggested, diff);
}
