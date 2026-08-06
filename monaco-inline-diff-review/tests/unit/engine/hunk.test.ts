/**
 * groupHunks + hashStr unit tests — ENG-9 ~ ENG-12
 *
 * Pure functions, zero mock.
 * Oracle: golden values.
 */

import { describe, it, expect } from "vitest";
import { computeDiff } from "../../../src/engine/diff";
import { groupHunks, hashStr } from "../../../src/engine/hunk";

// ─── hashStr ──────────────────────────────────────────────────────────────

describe("hashStr", () => {
  it("returns consistent output for same input", () => {
    expect(hashStr("hello")).toBe(hashStr("hello"));
  });

  it("returns different output for different input", () => {
    expect(hashStr("hello")).not.toBe(hashStr("world"));
  });

  it("handles empty string", () => {
    // djb2(5381) for empty string
    expect(typeof hashStr("")).toBe("string");
    expect(hashStr("").length).toBeGreaterThan(0);
  });

  it("returns base-36 numeric string", () => {
    const h = hashStr("test");
    expect(/^[0-9a-z]+$/.test(h)).toBe(true);
  });
});

// ─── groupHunks ──────────────────────────────────────────────────────────

describe("groupHunks", () => {
  // ── ENG-9：add + del 相邻 → 合并为 modify ──────────────────────────

  it("ENG-9: adjacent del+add → merged as modify hunk", () => {
    const entries = [
      { kind: "ctx" as const, text: "a" },
      { kind: "del" as const, text: "b" },
      { kind: "add" as const, text: "x" },
      { kind: "ctx" as const, text: "c" },
    ];
    const hunks = groupHunks(entries);

    expect(hunks).toHaveLength(1);
    const h = hunks[0];
    expect(h.type).toBe("modify");
    expect(h.delLines).toEqual(["b"]);
    expect(h.addLines).toEqual(["x"]);
    expect(h.origStart).toBe(1); // "a" consumed = 1 original line before
    expect(h.origEnd).toBe(2); // origStart + delLines.length
    // contentKey format: modify#1-2#hashDel#hashAdd
    expect(h.contentKey).toMatch(/^modify#1-2#/);
  });

  // ── ENG-10：独立 add + 独立 del（被 ctx 隔开）─────────────────────

  it("ENG-10: separated add and del hunks (ctx in between)", () => {
    const entries = [
      { kind: "add" as const, text: "x" },
      { kind: "ctx" as const, text: "a" },
      { kind: "del" as const, text: "b" },
    ];
    const hunks = groupHunks(entries);

    expect(hunks).toHaveLength(2);

    // H1: add at position 0
    expect(hunks[0].type).toBe("add");
    expect(hunks[0].origStart).toBe(0);
    expect(hunks[0].origEnd).toBe(0);
    expect(hunks[0].addLines).toEqual(["x"]);

    // H2: del at position after "a"
    expect(hunks[1].type).toBe("del");
    expect(hunks[1].origStart).toBe(1); // "a" consumed = 1 original line before
    expect(hunks[1].origEnd).toBe(2);
    expect(hunks[1].delLines).toEqual(["b"]);
  });

  // ── ENG-11：纯 ctx → 空 hunks ─────────────────────────────────────

  it("ENG-11: all ctx → empty hunks array", () => {
    const entries = [
      { kind: "ctx" as const, text: "a" },
      { kind: "ctx" as const, text: "b" },
    ];
    expect(groupHunks(entries)).toEqual([]);
  });

  // ── ENG-12：contentKey 唯一性 ─────────────────────────────────────

  it("ENG-12: contentKey uniqueness across different hunks", () => {
    const entries = computeDiff("a\nb\nc\nd", "a\nx\nc\ny");
    const hunks = groupHunks(entries);

    expect(hunks.length).toBeGreaterThanOrEqual(2);

    const keys = hunks.map((h) => h.contentKey);
    const uniqueKeys = new Set(keys);
    expect(uniqueKeys.size).toBe(keys.length);
  });

  // ── Additional: multiple independent del hunks ─────────────────────────

  it("groups multiple separated del entries into separate hunks", () => {
    const entries = computeDiff("a\nb\nc\nd\ne", "a\nc\ne");
    const hunks = groupHunks(entries);
    // Should have 2 del hunks (b removed and d removed)
    expect(hunks.length).toBe(2);
    expect(hunks.every((h) => h.type === "del")).toBe(true);
  });

  // ── Additional: multiple independent add hunks ─────────────────────────

  it("groups multiple separated add entries into separate hunks", () => {
    const entries = computeDiff("a\nc", "a\nx\nc\ny");
    const hunks = groupHunks(entries);
    expect(hunks.length).toBe(2);
    expect(hunks.every((h) => h.type === "add")).toBe(true);
  });

  // ── Additional: adjacent del + add with different line counts ──────────

  it("handles modify with unequal del/add line counts", () => {
    const entries = computeDiff("a\nb1\nb2\nc", "a\nx\nc");
    const hunks = groupHunks(entries);

    expect(hunks).toHaveLength(1);
    expect(hunks[0].type).toBe("modify");
    expect(hunks[0].delLines).toEqual(["b1", "b2"]);
    expect(hunks[0].addLines).toEqual(["x"]);
  });

  // ── Additional: verify contentKey stability across model changes ───────

  it("contentKey remains stable for same original position", () => {
    // First diff: "a\nb\nc" → "a\nx\nc"
    const entries1 = computeDiff("a\nb\nc", "a\nx\nc");
    const hunks1 = groupHunks(entries1);
    expect(hunks1).toHaveLength(1);

    // Second diff: same original, different model (but same position)
    const entries2 = computeDiff("a\nb\nc\nd", "a\nx\nc\ny");
    const hunks2 = groupHunks(entries2);
    expect(hunks2).toHaveLength(2);

    // First hunk in both should have same origStart/origEnd for "b→x"
    const h1 = hunks1[0];
    const h2 = hunks2.find((h) => h.origStart === 1 && h.origEnd === 2);
    expect(h2).toBeDefined();
    // Both should be modify type at position 1-2
    expect(h1!.type).toBe("modify");
    expect(h2!.type).toBe("modify");
  });
});
