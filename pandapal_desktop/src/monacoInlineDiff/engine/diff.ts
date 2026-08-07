import type { DiffEntry } from "./types";

/**
 * 基于 LCS（最长公共子序列）计算两个文本之间的 diff。
 *
 * 时间复杂度 O(n*m)，空间复杂度 O(n*m)，适用于中小文件（<5000 行）。
 * 返回按行顺序排列的 DiffEntry 数组。
 *
 * @param orig 原始文本
 * @param cur  当前/建议文本
 * @returns diff 条目数组，每个条目标记为 ctx/del/add
 *
 * @example
 * ```ts
 * const entries = computeDiff("a\nb\nc", "a\nx\nc");
 * // [{ kind:"ctx", text:"a" }, { kind:"del", text:"b" }, { kind:"add", text:"x" }, { kind:"ctx", text:"c" }]
 * ```
 */
export function computeDiff(orig: string, cur: string): DiffEntry[] {
  // 行尾归一化：CRLF / 孤立 CR → LF。
  // 背景：Windows 工作区文件常为 CRLF（git autocrlf），而 AI 侧（Python universal
  // newline）文本为 LF。若两侧行尾不一致，split("\n") 后行尾残留 \r，
  // "xxx\r" !== "xxx"，整篇内容会被误判为 del+add（UI 全绿/全黄）。
  // normalize 只影响比较与 hunk 文本（行内容不含 \r），Monaco model 行内容
  // 本就不含 \r（EOL 是分隔符），故对 Apply/Reject/保存无副作用。
  const normalizeEol = (s: string): string =>
    s.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  const normOrig = normalizeEol(orig);
  const normCur = normalizeEol(cur);
  // 空字符串视为 0 行（而非 split 产生的 [""] = 1 空行）
  const o = normOrig === "" ? [] : normOrig.split("\n");
  const c = normCur === "" ? [] : normCur.split("\n");
  const ol = o.length;
  const cl = c.length;

  if (ol === 0 && cl === 0) return [];
  if (ol === 0) return c.map((t) => ({ kind: "add" as const, text: t }));
  if (cl === 0) return o.map((t) => ({ kind: "del" as const, text: t }));

  // LCS DP 表
  const dp: number[][] = Array.from({ length: ol + 1 }, () =>
    new Array(cl + 1).fill(0),
  );
  for (let i = 1; i <= ol; i++)
    for (let j = 1; j <= cl; j++)
      dp[i][j] =
        o[i - 1] === c[j - 1]
          ? dp[i - 1][j - 1] + 1
          : Math.max(dp[i - 1][j], dp[i][j - 1]);

  // 回溯构建 diff 序列
  const result: DiffEntry[] = [];
  let i = ol;
  let j = cl;
  while (i > 0 && j > 0) {
    if (o[i - 1] === c[j - 1]) {
      result.unshift({ kind: "ctx", text: o[i - 1] });
      i--;
      j--;
    } else if (dp[i - 1][j] > dp[i][j - 1]) {
      result.unshift({ kind: "del", text: o[i - 1] });
      i--;
    } else {
      result.unshift({ kind: "add", text: c[j - 1] });
      j--;
    }
  }
  while (i > 0) {
    result.unshift({ kind: "del", text: o[i - 1] });
    i--;
  }
  while (j > 0) {
    result.unshift({ kind: "add", text: c[j - 1] });
    j--;
  }
  return result;
}
