/**
 * computeDiff unit tests — ENG-1 ~ ENG-8
 *
 * Pure function, zero mock.
 * Oracle: golden values (hand-calculable).
 */

import { describe, it, expect } from "vitest";
import { computeDiff } from "../../../src/engine/diff";

// ─── ENG-1：两文本完全相同 → 全 ctx ───────────────────────────────────────

describe("computeDiff", () => {
  it("ENG-1: identical texts → all ctx entries", () => {
    const result = computeDiff("a\nb\nc", "a\nb\nc");
    expect(result).toEqual([
      { kind: "ctx", text: "a" },
      { kind: "ctx", text: "b" },
      { kind: "ctx", text: "c" },
    ]);
  });

  // ── ENG-2：纯新增 → add 条目 ──────────────────────────────────────────

  it("ENG-2: pure addition → add entry", () => {
    const result = computeDiff("a\nb", "a\nx\nb");
    expect(result).toEqual([
      { kind: "ctx", text: "a" },
      { kind: "add", text: "x" },
      { kind: "ctx", text: "b" },
    ]);
  });

  // ── ENG-3：纯删除 → del 条目 ──────────────────────────────────────────

  it("ENG-3: pure deletion → del entry", () => {
    const result = computeDiff("a\nb\nc", "a\nc");
    expect(result).toEqual([
      { kind: "ctx", text: "a" },
      { kind: "del", text: "b" },
      { kind: "ctx", text: "c" },
    ]);
  });

  // ── ENG-4：修改（del+add 相邻）→ 相邻 del/add 条目 ────────────────────

  it("ENG-4: modification → adjacent del+add entries", () => {
    const result = computeDiff("a\nb\nc", "a\nx\nc");
    expect(result).toEqual([
      { kind: "ctx", text: "a" },
      { kind: "del", text: "b" },
      { kind: "add", text: "x" },
      { kind: "ctx", text: "c" },
    ]);
  });

  // ── ENG-5：两者皆空 → 空数组 ──────────────────────────────────────────

  it("ENG-5: both empty → empty array", () => {
    expect(computeDiff("", "")).toEqual([]);
  });

  // ── ENG-6：original 为空 → 全 add ─────────────────────────────────────

  it("ENG-6: empty original → all add", () => {
    const result = computeDiff("", "a\nb");
    expect(result).toEqual([
      { kind: "add", text: "a" },
      { kind: "add", text: "b" },
    ]);
  });

  // ── ENG-7：current 为空 → 全 del ──────────────────────────────────────

  it("ENG-7: empty current → all del", () => {
    const result = computeDiff("a\nb", "");
    expect(result).toEqual([
      { kind: "del", text: "a" },
      { kind: "del", text: "b" },
    ]);
  });

  // ── ENG-8：LCS 多路径分支（dp[i-1][j] >= dp[i][j-1]）──────────────────

  it("ENG-8: LCS multi-path → correct entries order", () => {
    // "a\nb\nc" vs "a\nx\nc"
    // LCS = ["a", "c"], so del="b", add="x"
    const result = computeDiff("a\nb\nc", "a\nx\nc");
    expect(result).toHaveLength(4);

    // Verify entries are in line order
    const kinds = result.map((e) => e.kind);
    expect(kinds).toEqual(["ctx", "del", "add", "ctx"]);

    // Verify ctx lines match LCS
    const ctxLines = result.filter((e) => e.kind === "ctx").map((e) => e.text);
    expect(ctxLines).toEqual(["a", "c"]);
  });

  // ── Additional: last line without newline ──────────────────────────────

  it("handles texts without trailing newline", () => {
    // Both 2 lines, last line different
    const result = computeDiff("a\nb", "a\nx");
    expect(result).toEqual([
      { kind: "ctx", text: "a" },
      { kind: "del", text: "b" },
      { kind: "add", text: "x" },
    ]);
  });

  // ── Additional: single-line texts ──────────────────────────────────────

  it("handles single-line texts", () => {
    expect(computeDiff("hello", "hello")).toEqual([
      { kind: "ctx", text: "hello" },
    ]);
    expect(computeDiff("hello", "world")).toEqual([
      { kind: "del", text: "hello" },
      { kind: "add", text: "world" },
    ]);
  });

  // ── Additional: multi-line with empty lines ────────────────────────────

  it("handles empty lines in text", () => {
    const result = computeDiff("a\n\nb", "a\n\nx");
    expect(result).toEqual([
      { kind: "ctx", text: "a" },
      { kind: "ctx", text: "" },
      { kind: "del", text: "b" },
      { kind: "add", text: "x" },
    ]);
  });

  // ── ENG-9: 行尾归一化（CRLF/CR → LF），修复整篇误标 ─────────────────────

  it("ENG-9a: orig CRLF vs cur LF with identical content → all ctx (no false positives)", () => {
    // 回归：Windows 磁盘 CRLF vs AI 侧 LF，内容相同但行尾不同。
    // 修复前 split("\n") 后行尾残留 \r，整篇被判为 del+add。
    const result = computeDiff("a\r\nb\r\nc", "a\nb\nc");
    expect(result).toEqual([
      { kind: "ctx", text: "a" },
      { kind: "ctx", text: "b" },
      { kind: "ctx", text: "c" },
    ]);
  });

  it("ENG-9b: orig CRLF vs cur LF with one real change → only that line differs", () => {
    const result = computeDiff("a\r\nb\r\nc", "a\nx\nc");
    expect(result).toEqual([
      { kind: "ctx", text: "a" },
      { kind: "del", text: "b" },
      { kind: "add", text: "x" },
      { kind: "ctx", text: "c" },
    ]);
  });

  it("ENG-9c: both CRLF with one change → only that line differs (no \\r in texts)", () => {
    // hunk 文本不应携带 \r，否则 contentKey 哈希与 Reject 重建文本会被污染
    const result = computeDiff("a\r\nb\r\nc", "a\r\nx\r\nc");
    expect(result).toEqual([
      { kind: "ctx", text: "a" },
      { kind: "del", text: "b" },
      { kind: "add", text: "x" },
      { kind: "ctx", text: "c" },
    ]);
  });

  it("ENG-9d: lone CR line endings (old Mac) are normalized too", () => {
    const result = computeDiff("a\rb\rc", "a\nb\nx");
    expect(result).toEqual([
      { kind: "ctx", text: "a" },
      { kind: "ctx", text: "b" },
      { kind: "del", text: "c" },
      { kind: "add", text: "x" },
    ]);
  });
});
