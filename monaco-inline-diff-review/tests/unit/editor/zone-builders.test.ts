/**
 * zone-builders unit tests — ENG-26 ~ ENG-27
 *
 * Pure DOM operations, jsdom provides real DOM API.
 * Zero mock needed (except monaco.Range for buildBtnZone).
 */

import { describe, it, expect, vi } from "vitest";
import {
  buildDelZone,
  buildBtnZone,
  buildAddOverlays,
  escHtml,
} from "../../../src/editor/zone-builders";
import type { Hunk } from "../../../src/engine/types";
import { createFakeMonaco, createFakeEditor } from "../../fixtures/fake-monaco";

// ─── Helper: create a minimal Hunk for testing ────────────────────────────

function makeHunk(overrides: Partial<Hunk> = {}): Hunk {
  return {
    id: "add#0-0#abc#def",
    contentKey: "add#0-0#abc#def",
    type: "add",
    entries: [{ kind: "add", text: "new line" }],
    startIdx: 0,
    endIdx: 0,
    origStart: 0,
    origEnd: 0,
    delLines: [],
    addLines: ["new line"],
    ...overrides,
  };
}

// ─── escHtml ──────────────────────────────────────────────────────────────

describe("escHtml", () => {
  it("escapes < and > characters", () => {
    expect(escHtml("<div>")).toBe("&lt;div&gt;");
  });

  it("escapes & character", () => {
    expect(escHtml("a & b")).toBe("a &amp; b");
  });

  it("leaves normal text unchanged", () => {
    expect(escHtml("hello world")).toBe("hello world");
  });
});

// ─── ENG-26：buildDelZone 渲染删除行 ─────────────────────────────────────

describe("buildDelZone", () => {
  it("ENG-26: renders deleted lines with line-through style", () => {
    const hunk = makeHunk({
      type: "del",
      delLines: ["line1", "line2"],
      addLines: [],
    });
    const dom = buildDelZone(hunk);

    expect(dom).toBeInstanceOf(HTMLDivElement);
    // jsdom normalizes rgba() with spaces: "rgba(239, 68, 68, 0.08)"
    expect(dom.style.background).toMatch(/rgba\(239,\s*68,\s*68/);

    // Should contain both lines
    const html = dom.innerHTML;
    expect(html).toContain("line1");
    expect(html).toContain("line2");
    expect(html).toContain("text-decoration:line-through");

    // Should have 2 line divs
    const lineDivs = dom.querySelectorAll(
      'div[style*="text-decoration:line-through"]',
    );
    expect(lineDivs.length).toBe(2);
  });

  it("escapes HTML in deleted line content", () => {
    const hunk = makeHunk({
      type: "del",
      delLines: ["<script>alert('xss')</script>"],
      addLines: [],
    });
    const dom = buildDelZone(hunk);
    expect(dom.innerHTML).toContain("&lt;script&gt;");
    expect(dom.innerHTML).not.toContain("<script>");
  });
});

// ─── ENG-27：buildBtnZone 绑定事件回调 ───────────────────────────────────

describe("buildBtnZone", () => {
  it("ENG-27: binds Apply and Reject callbacks", () => {
    const fakeMonaco = createFakeMonaco() as any;
    const hunk = makeHunk();
    const onApply = vi.fn();
    const onReject = vi.fn();

    const dom = buildBtnZone(hunk, fakeMonaco, { onApply, onReject });

    expect(dom).toBeInstanceOf(HTMLDivElement);
    expect(dom.querySelector(".mid-btn-apply")).not.toBeNull();
    expect(dom.querySelector(".mid-btn-reject")).not.toBeNull();

    // Click Apply
    const applyBtn = dom.querySelector(".mid-btn-apply")!;
    applyBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(onApply).toHaveBeenCalledTimes(1);
    expect(onApply).toHaveBeenCalledWith(hunk.id, hunk.contentKey);

    // Click Reject
    const rejectBtn = dom.querySelector(".mid-btn-reject")!;
    rejectBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(onReject).toHaveBeenCalledTimes(1);
    expect(onReject).toHaveBeenCalledWith(hunk);
  });

  it("pointer-events is set to auto on button bar", () => {
    const fakeMonaco = createFakeMonaco() as any;
    const hunk = makeHunk();
    const dom = buildBtnZone(hunk, fakeMonaco, {
      onApply: vi.fn(),
      onReject: vi.fn(),
    });
    expect(dom.style.getPropertyValue("pointer-events")).toBe("auto");
  });
});

// ─── buildAddOverlays ─────────────────────────────────────────────────────

describe("buildAddOverlays", () => {
  it("pushes decoration entries for each add line", () => {
    const fakeMonaco = createFakeMonaco() as any;
    const fakeEditor = createFakeEditor("a\nx\ny\nb");
    const hunk = makeHunk({
      type: "add",
      addLines: ["x", "y"],
    });
    const addDecos: any[] = [];

    buildAddOverlays(hunk, 0, fakeEditor as any, fakeMonaco, addDecos);

    expect(addDecos).toHaveLength(2);
    expect(addDecos[0].options.className).toBe("mid-add-line");
    expect(addDecos[1].options.className).toBe("mid-add-line");
  });

  it("stops at line count boundary", () => {
    const fakeMonaco = createFakeMonaco() as any;
    const fakeEditor = createFakeEditor("single line");
    const hunk = makeHunk({
      type: "add",
      addLines: ["x", "y", "z"], // more than available lines
    });
    const addDecos: any[] = [];

    buildAddOverlays(hunk, 0, fakeEditor as any, fakeMonaco, addDecos);

    // Only 1 decoration possible (afterLine + 1 = 1) out of 3 add lines
    // Actually afterLine=0, firstLn=1, lineCount=1, so only ln=1 ≤ 1
    expect(addDecos.length).toBeLessThanOrEqual(1);
  });
});
