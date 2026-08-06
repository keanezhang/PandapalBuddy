/**
 * reject-logic unit tests — ENG-13 ~ ENG-24
 *
 * Uses FakeModel / FakeEditor from fixtures.
 * findFreshHunk: unit (injects fake model)
 * rejectHunk: component(fake) — FakeModel + FakeEditor
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  findFreshHunk,
  rejectHunk,
  lineNumberOfEntry,
  nonDelLinesBefore,
} from "../../../src/editor/reject-logic";
import { createFakeModel, createFakeEditor, createFakeMonaco } from "../../fixtures/fake-monaco";
import type { Hunk } from "../../../src/engine/types";
import { groupHunks, computeDiff } from "../../../src/engine";

// ─── Helpers ──────────────────────────────────────────────────────────────

function getHunk(
  original: string,
  current: string,
  index: number = 0,
): Hunk {
  const entries = computeDiff(original, current);
  return groupHunks(entries)[index];
}

// ─── findFreshHunk ────────────────────────────────────────────────────────

describe("findFreshHunk", () => {
  // ── ENG-13：通过 id 精确匹配 ───────────────────────────────────────

  it("ENG-13: matches by exact id", () => {
    const original = "a\nb\nc";
    const model = createFakeModel("a\nx\nc"); // modify: b→x
    const known = getHunk(original, "a\nx\nc", 0);

    const fresh = findFreshHunk(known.id, original, model as any);
    expect(fresh).toBeDefined();
    expect(fresh!.id).toBe(known.id);
  });

  // ── ENG-14：id 不匹配 → anchor 回退 ───────────────────────────────

  it("ENG-14: falls back to anchor matching when id hash differs", () => {
    const original = "a\nb\nc\nd";
    const model = createFakeModel("a\nx\nc\ny");
    // Construct id with correct anchor but wrong hash
    const badId = "modify#1-2#badHash1#badHash2";

    const fresh = findFreshHunk(badId, original, model as any);
    expect(fresh).toBeDefined();
    expect(fresh!.type).toBe("modify");
    expect(fresh!.origStart).toBe(1);
    expect(fresh!.origEnd).toBe(2);
  });

  // ── ENG-15：id 格式异常 → undefined ────────────────────────────────

  it("ENG-15: returns undefined for malformed id", () => {
    const original = "a\nb\nc";
    const model = createFakeModel("a\nx\nc");
    const result = findFreshHunk("garbage", original, model as any);
    expect(result).toBeUndefined();
  });

  it("returns undefined when no hunks match", () => {
    const original = "a\nb";
    const model = createFakeModel("a\nb");
    const result = findFreshHunk("del#0-1#abc#def", original, model as any);
    expect(result).toBeUndefined();
  });
});

// ─── lineNumberOfEntry ────────────────────────────────────────────────────

describe("lineNumberOfEntry", () => {
  it("returns correct line number for add entry", () => {
    const original = "a\nb";
    const model = createFakeModel("a\nx\nb");
    // entries: [ctx:a, add:x, ctx:b], add is at index 1
    const ln = lineNumberOfEntry(original, model as any, 1);
    expect(ln).toBe(2); // line 2 in model
  });

  it("returns correct line number for del entry", () => {
    const original = "a\nb\nc";
    const model = createFakeModel("a\nc");
    // entries: [ctx:a, del:b, ctx:c], del is at index 1
    const ln = lineNumberOfEntry(original, model as any, 1);
    expect(ln).toBe(2); // after line 1, del doesn't count → next is line 2
  });
});

// ─── nonDelLinesBefore ────────────────────────────────────────────────────

describe("nonDelLinesBefore", () => {
  it("counts non-del lines before given index", () => {
    const original = "a\nb\nc";
    const model = createFakeModel("a\nc");
    // entries: [ctx:a, del:b, ctx:c], startIdx=1
    const n = nonDelLinesBefore(original, model as any, 1);
    expect(n).toBe(1); // only "a" (ctx) before del at index 1
  });
});

// ─── rejectHunk ───────────────────────────────────────────────────────────

describe("rejectHunk", () => {
  let fakeMonaco: any;

  beforeEach(() => {
    fakeMonaco = createFakeMonaco();
  });

  // ── ENG-23：model 为 null → 不抛异常 ────────────────────────────────

  it("ENG-23: returns early when model is null", () => {
    const editor = { getModel: () => null } as any;
    const hunk = getHunk("a\nb", "a\nx\nb", 0);
    expect(() =>
      rejectHunk("add", hunk, "a\nb", editor, fakeMonaco),
    ).not.toThrow();
  });

  // ── ENG-24：findFreshHunk 返回 undefined → 不操作 ──────────────────

  it("ENG-24: returns early when findFreshHunk returns undefined", () => {
    const editor = createFakeEditor("a\nb"); // model == original, no hunks
    const hunk = getHunk("a\nb", "a\nx\nb", 0); // hunk from diff with "a\nx\nb"
    const initialValue = editor.getModel()!.getValue();

    rejectHunk("add", hunk, "a\nb", editor as any, fakeMonaco);
    expect(editor.getModel()!.getValue()).toBe(initialValue);
  });

  // ── ENG-16：reject add → 删除对应行 ────────────────────────────────

  it("ENG-16: reject add → deletes added lines", () => {
    const original = "a\nb";
    const editor = createFakeEditor("a\nx\nb");
    const hunk = getHunk(original, "a\nx\nb", 0); // add hunk for "x"

    rejectHunk("add", hunk, original, editor as any, fakeMonaco);
    expect(editor._model.getValue()).toBe("a\nb");
  });

  // ── ENG-17：reject del（中间位置）→ 恢复删除行 ────────────────────

  it("ENG-17: reject del (middle) → restores deleted lines", () => {
    const original = "a\nb\nc";
    const editor = createFakeEditor("a\nc");
    const hunk = getHunk(original, "a\nc", 0); // del hunk for "b"

    rejectHunk("del", hunk, original, editor as any, fakeMonaco);
    expect(editor._model.getValue()).toBe("a\nb\nc");
  });

  // ── ENG-18：reject del（开头位置，afterLine <= 0）──────────────────

  it("ENG-18: reject del at beginning (afterLine <= 0)", () => {
    const original = "a\nb";
    const editor = createFakeEditor("b"); // "a" deleted
    const entries = computeDiff(original, "b");
    const hunks = groupHunks(entries);
    expect(hunks).toHaveLength(1);
    expect(hunks[0].type).toBe("del");

    rejectHunk("del", hunks[0], original, editor as any, fakeMonaco);
    expect(editor._model.getValue()).toBe("a\nb");
  });

  // ── ENG-19：reject del（末尾位置，afterLine >= lineCount）───────────

  it("ENG-19: reject del at end (afterLine >= lineCount)", () => {
    const original = "a\nb";
    const editor = createFakeEditor("a"); // "b" deleted at end
    const entries = computeDiff(original, "a");
    const hunks = groupHunks(entries);
    expect(hunks).toHaveLength(1);
    expect(hunks[0].type).toBe("del");

    rejectHunk("del", hunks[0], original, editor as any, fakeMonaco);
    expect(editor._model.getValue()).toBe("a\nb");
  });

  // ── ENG-20：reject modify → model.setValue 全量替换 ────────────────

  it("ENG-20: reject modify → calls model.setValue for full replacement", () => {
    const original = "a\nb\nc";
    const editor = createFakeEditor("a\nx\nc");
    const hunk = getHunk(original, "a\nx\nc", 0);

    const setValueSpy = vi.spyOn(editor._model, "setValue");
    rejectHunk("modify", hunk, original, editor as any, fakeMonaco);

    expect(setValueSpy).toHaveBeenCalledTimes(1);
    // rebuilt should restore "b", remove "x"
    expect(editor._model.getValue()).toBe("a\nb\nc");
  });

  // ── ENG-21：reject modify 仅影响目标 hunk ──────────────────────────

  it("ENG-21: reject modify only affects target hunk, preserves others", () => {
    const original = "a\nb\nc\nd\ne";
    const editor = createFakeEditor("a\nx\nc\ny\ne"); // b→x, d→y
    const hunks = groupHunks(computeDiff(original, "a\nx\nc\ny\ne"));
    expect(hunks.length).toBeGreaterThanOrEqual(2);

    // Reject second modify (d→y)
    const hunk2 = hunks[1];
    rejectHunk("modify", hunk2, original, editor as any, fakeMonaco);

    const result = editor._model.getValue();
    // b→x should be preserved, d→y should be reverted
    expect(result).toContain("x"); // add from H1 preserved
    expect(result).toContain("d"); // del from H2 restored
    expect(result).not.toContain("y"); // add from H2 removed
  });

  // ── ENG-22：reject modify — del/add 行数不等 ───────────────────────

  it("ENG-22: reject modify with unequal del/add line counts", () => {
    const original = "a\nb1\nb2\nc";
    const editor = createFakeEditor("a\nx\nc"); // 2 del, 1 add
    const hunks = groupHunks(computeDiff(original, "a\nx\nc"));
    expect(hunks).toHaveLength(1);
    expect(hunks[0].type).toBe("modify");

    rejectHunk("modify", hunks[0], original, editor as any, fakeMonaco);
    expect(editor._model.getValue()).toBe(original);
  });

  // ── Additional: pushUndoStop is called ──────────────────────────────────

  it("calls pushUndoStop before and after edit for add", () => {
    const original = "a\nb";
    const editor = createFakeEditor("a\nx\nb");
    const hunk = getHunk(original, "a\nx\nb", 0);
    const undoSpy = vi.spyOn(editor as any, "pushUndoStop");

    rejectHunk("add", hunk, original, editor as any, fakeMonaco);
    expect(undoSpy).toHaveBeenCalledTimes(2);
  });

  it("calls pushUndoStop before and after edit for modify", () => {
    const original = "a\nb\nc";
    const editor = createFakeEditor("a\nx\nc");
    const hunk = getHunk(original, "a\nx\nc", 0);
    const undoSpy = vi.spyOn(editor as any, "pushUndoStop");

    rejectHunk("modify", hunk, original, editor as any, fakeMonaco);
    expect(undoSpy).toHaveBeenCalledTimes(2);
  });
});
