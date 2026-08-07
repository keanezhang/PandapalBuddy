/**
 * CodeRenderer 编辑模式变更追踪 — 组件测试
 *
 * 覆盖设计文档 CT-1 ~ CT-14
 * 策略: component(fake) — FakeMonacoEditor + FakeDecoCollection + FakeTimers
 *
 * 前置: 需扩展 createFakeMonaco() 包含 editor.OverviewRulerLane.Right（已完成）
 */
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act } from "@testing-library/react";
import { CodeRenderer } from "../../src/CodeRenderer";
import { computeDiff } from "../../src/engine/diff";
import {
  createFakeEditor,
  createFakeMonaco,
  type FakeEditor,
  type FakeDecoCollection,
} from "../fixtures/fake-monaco";

// ── Spy on computeDiff ─────────────────────────────────────────────────
// vitest hoists vi.mock above imports. The factory wraps the real
// computeDiff with vi.fn() so CodeRenderer gets the spied version.
// Tests import the same spied version for assertions.
vi.mock("../../src/engine/diff", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../../src/engine/diff")>();
  return {
    ...mod,
    computeDiff: vi.fn(
      (...args: Parameters<typeof mod.computeDiff>) => mod.computeDiff(...args),
    ),
  };
});

// ── Module-level captured instances ────────────────────────────────────
// Stored when the mock Editor fires onMount. Tests access these to verify
// decorations, model state, and ref lifecycle.
let capturedEditor: FakeEditor | null = null;
let capturedDecoCol: FakeDecoCollection | null = null;

// ── Mock @monaco-editor/react ──────────────────────────────────────────
vi.mock("@monaco-editor/react", async () => {
  const React = await import("react");
  const { createFakeEditor, createFakeMonaco } = await import(
    "../fixtures/fake-monaco"
  );
  return {
    default: ({
      value,
      defaultValue,
      onMount,
    }: {
      value?: string;
      defaultValue?: string;
      onMount?: (editor: unknown, monaco: unknown) => void;
      onChange?: (v: string | undefined) => void;
      [key: string]: unknown;
    }) => {
      const initialValue = value ?? defaultValue ?? "";
      const containerRef = React.useRef<HTMLDivElement | null>(null);

      React.useEffect(() => {
        if (onMount && containerRef.current) {
          const editor = createFakeEditor(initialValue, containerRef.current);
          const monaco = createFakeMonaco();
          capturedEditor = editor as FakeEditor;
          capturedDecoCol = editor._decoCol;
          onMount(
            editor as unknown as ReturnType<typeof createFakeEditor>,
            monaco as unknown as ReturnType<typeof createFakeMonaco>,
          );
        }
      }, []);

      // Sync value prop changes into the fake model so getValue() /
      // getLineCount() reflect the latest content during debounce callbacks.
      React.useEffect(() => {
        if (capturedEditor && value !== undefined) {
          capturedEditor.getModel().setValue(value);
        }
      }, [value]);

      return React.createElement("div", {
        ref: containerRef,
        "data-testid": "monaco-editor",
        "data-value": initialValue,
      });
    },
  };
});

// ── Helpers ────────────────────────────────────────────────────────────

/** Advance fake timers by N ms, wrapped in act() for React state flushing. */
async function advanceTimers(ms: number) {
  await act(async () => {
    vi.advanceTimersByTime(ms);
  });
}

/** Mount in edit mode + one frame for onMount effects to fire. */
async function mountEditMode(
  content: string,
  extraProps: Record<string, unknown> = {},
) {
  const result = render(
    React.createElement(CodeRenderer, {
      content,
      language: "python",
      readOnly: false,
      ...extraProps,
    }),
  );
  await advanceTimers(16);
  return result;
}

/** Rerender with new content + advance debounce window (300ms). */
async function changeAndFlush(
  rerender: (ui: React.ReactElement) => void,
  content: string,
  extraProps: Record<string, unknown> = {},
) {
  rerender(
    React.createElement(CodeRenderer, {
      content,
      language: "python",
      readOnly: false,
      ...extraProps,
    }),
  );
  await advanceTimers(300);
}

/** Get the first decoration's options bag (type-erased for flexible access). */
function firstDecoOpts(): Record<string, unknown> {
  return (capturedDecoCol!._lastDecos![0] as Record<string, unknown>)
    .options as Record<string, unknown>;
}

/** Get the first decoration's range. */
function firstDecoRange(): Record<string, number> {
  return (capturedDecoCol!._lastDecos![0] as Record<string, unknown>)
    .range as Record<string, number>;
}

/** Get a decoration by index. */
function decoAt(i: number): Record<string, unknown> {
  return capturedDecoCol!._lastDecos![i] as Record<string, unknown>;
}

// ── Lifecycle ──────────────────────────────────────────────────────────

beforeEach(() => {
  capturedEditor = null;
  capturedDecoCol = null;
  (computeDiff as ReturnType<typeof vi.fn>).mockClear?.();
});

afterEach(() => {
  // Flush any pending timers from unmounted components to prevent
  // cross-test leakage (same pattern as InlineDiffEditor tests).
  try {
    vi.runAllTimers();
  } catch {
    /* stale callbacks may reference unmounted component state */
  }
});

// ════════════════════════════════════════════════════════════════════════
// Tests
// ════════════════════════════════════════════════════════════════════════

describe("CodeRenderer change tracking (edit mode)", () => {
  // ──────────────────────────────────────────────────────────────────────
  // P0 用例
  // ──────────────────────────────────────────────────────────────────────

  describe("P0", () => {
    // CT-7 / inv-3 suggestion 跳过 / Risk-1 模式混淆
    it("CT-7: suggestion mode skips change tracking entirely", async () => {
      const { rerender, container } = render(
        <CodeRenderer
          content="a\nx\nb"
          original="a\nb"
          language="python"
          readOnly={true}
        />,
      );
      await advanceTimers(16);

      // Verify suggestion mode UI renders without crash.
      expect(container.querySelector(".mid-float-bar")).not.toBeNull();

      // Rerender with new content (simulating AI suggestion update),
      // advance well past 300ms debounce window.
      rerender(
        <CodeRenderer
          content="a\ny\nb"
          original="a\nb"
          language="python"
          readOnly={true}
        />,
      );
      await advanceTimers(500);

      // Component still renders without crash after content change.
      expect(container.querySelector(".mid-float-bar")).not.toBeNull();

      // TODO(feature): once change tracking is implemented in CodeRenderer,
      // tighten this test: verify that change tracking's computeDiff is NOT
      // called in suggestion mode (currently InlineDiffEditor calls
      // computeDiff internally, so a global spy can't distinguish callers).
    });

    // CT-6 / inv-2 fileId 清除+重置 / Risk-2 残留标记
    it("CT-6: fileId switch clears decorations and resets snapshot", async () => {
      const { rerender } = await mountEditMode("a\nb\nc", { fileId: "f1" });

      // Make an edit to produce decorations.
      await changeAndFlush(rerender, "a\nx\nc", { fileId: "f1" });
      expect(capturedDecoCol?._lastDecos?.length).toBeGreaterThan(0);

      (computeDiff as ReturnType<typeof vi.fn>).mockClear?.();

      // Switch file: new fileId + new content (fileId effect is synchronous).
      rerender(
        <CodeRenderer
          content={"d\ne\nf"}
          language="python"
          readOnly={false}
          fileId="f2"
        />,
      );

      // Decorations must be cleared immediately.
      expect(capturedDecoCol?._lastDecos).toEqual([]);

      // Snapshot reset: subsequent edit diffs against the new baseline.
      (computeDiff as ReturnType<typeof vi.fn>).mockClear?.();
      await changeAndFlush(rerender, "d\nmodified\nf", { fileId: "f2" });
      expect(computeDiff).toHaveBeenCalledTimes(1);
      expect(computeDiff).toHaveBeenCalledWith("d\ne\nf", "d\nmodified\nf");
    });

    // CT-5 / inv-1 content===original → 清空
    it("CT-5: reverting content to original clears decorations", async () => {
      const { rerender } = await mountEditMode("a\nb");

      // Make a change → decorations appear.
      await changeAndFlush(rerender, "a\nx\nb");
      expect(capturedDecoCol?._lastDecos?.length).toBeGreaterThan(0);

      (computeDiff as ReturnType<typeof vi.fn>).mockClear?.();

      // Revert to original.
      await changeAndFlush(rerender, "a\nb");

      // clear() → _lastDecos empty; computeDiff NOT called.
      expect(capturedDecoCol?._lastDecos).toEqual([]);
      expect(computeDiff).not.toHaveBeenCalled();
    });
  });

  // ──────────────────────────────────────────────────────────────────────
  // P1 用例
  // ──────────────────────────────────────────────────────────────────────

  describe("P1", () => {
    // CT-1 / inv-7 挂载 snapshot / Risk-6 ref null
    it("CT-1: mount captures snapshot, refs are set, no decorations", async () => {
      await mountEditMode("a\nb\nc");

      // onMount populated all refs.
      expect(capturedEditor).not.toBeNull();
      expect(capturedDecoCol).not.toBeNull();

      // No content change → no decorations.
      expect(capturedDecoCol?._lastDecos).toEqual([]);

      // Model holds initial content.
      expect(capturedEditor?.getModel().getValue()).toBe("a\nb\nc");

      // No diff on mount (content === original snapshot).
      expect(computeDiff).not.toHaveBeenCalled();
    });

    // CT-2 / inv-6 add → 绿色
    it("CT-2: pure addition → green decorations", async () => {
      const { rerender } = await mountEditMode("a\nb");
      await changeAndFlush(rerender, "a\nx\nb");

      expect(capturedDecoCol?._lastDecos?.length).toBe(1);
      const opts = firstDecoOpts();
      const range = firstDecoRange();
      const ovr = opts.overviewRuler as Record<string, unknown>;

      expect(opts.className).toBe("mid-add-line");
      expect(opts.glyphMarginClassName).toBe("mid-add-gutter");
      expect(opts.isWholeLine).toBe(true);
      expect(ovr.color).toBe("rgba(34,197,94,0.7)");
      expect(ovr.position).toBe(4); // OverviewRulerLane.Right
      expect(range.startLineNumber).toBe(2);

      expect(computeDiff).toHaveBeenCalledWith("a\nb", "a\nx\nb");
    });

    // CT-3 / inv-6 del → 红色 / Risk-8 删除位置
    it("CT-3: pure deletion → red decorations (no className)", async () => {
      const { rerender } = await mountEditMode("a\nb\nc");
      await changeAndFlush(rerender, "a\nc");

      expect(capturedDecoCol?._lastDecos?.length).toBe(1);
      const opts = firstDecoOpts();
      const range = firstDecoRange();
      const ovr = opts.overviewRuler as Record<string, unknown>;

      // del entries have no className (inline decoration).
      expect(opts.className).toBeUndefined();
      expect(opts.glyphMarginClassName).toBe("mid-del-gutter");
      expect(ovr.color).toBe("rgba(239,68,68,0.7)");
      expect(ovr.position).toBe(4);
      // Gap at line 2 where "b" was in 3→2 line model.
      expect(range.startLineNumber).toBe(2);
    });

    // CT-4 / inv-6 modify → 黄色 / Risk-5 类型混淆
    it("CT-4: modify (add after del) → yellow decorations", async () => {
      const { rerender } = await mountEditMode("a\nb\nc");
      await changeAndFlush(rerender, "a\nx\nc");

      expect(capturedDecoCol?._lastDecos?.length).toBe(1);
      const opts = firstDecoOpts();
      const range = firstDecoRange();
      const ovr = opts.overviewRuler as Record<string, unknown>;

      // prevIsDel=true → yellow, NOT green.
      expect(opts.className).toBe("mid-modify-line");
      expect(opts.glyphMarginClassName).toBe("mid-modify-gutter");
      expect(opts.isWholeLine).toBe(true);
      expect(ovr.color).toBe("rgba(234,179,8,0.7)");
      expect(ovr.position).toBe(4);
      expect(range.startLineNumber).toBe(2);
    });

    // CT-4b / inv-6 多行修改块（d2a2）→ 整块黄色，不得出现误标绿色
    it("CT-4b: multi-line modify block → ALL add lines yellow, none green", async () => {
      // original="a\nb\nc\nd" → content="a\nx\ny\nd"
      // diff: [ctx a, del b, del c, add x, add y, ctx d] → 一个 d2a2 modify 块。
      // 早期实现用单行 prevIsDel 判断：add x 的 prev 是 del → 黄，但 add y 的
      // prev 是 add → 被误标成纯新增（绿）。修复后整块都必须黄。
      const { rerender } = await mountEditMode("a\nb\nc\nd");
      await changeAndFlush(rerender, "a\nx\ny\nd");

      const decos = capturedDecoCol?._lastDecos as Record<string, unknown>[];
      // del b（非紧邻 add）渲染红色 gutter；add x / add y 渲染黄色 → 共 3 条。
      expect(decos?.length).toBe(3);

      // deco[0]: del "b" → 红色 gutter（原有 del 逻辑不变）
      expect(
        (decoAt(0).options as Record<string, unknown>).glyphMarginClassName,
      ).toBe("mid-del-gutter");

      // deco[1] / deco[2]: 两个 add 行都必须标成 modify（黄），而不是 add（绿）。
      for (let i = 1; i < 3; i++) {
        const opts = decoAt(i).options as Record<string, unknown>;
        expect(opts.className).toBe("mid-modify-line");
        expect(opts.glyphMarginClassName).toBe("mid-modify-gutter");
        expect(
          (opts.overviewRuler as Record<string, unknown>).color,
        ).toBe("rgba(234,179,8,0.7)");
      }
      expect(
        (decoAt(1).range as Record<string, number>).startLineNumber,
      ).toBe(2);
      expect(
        (decoAt(2).range as Record<string, number>).startLineNumber,
      ).toBe(3);
    });

    // CT-8 / inv-4 >3000 行跳过 / Risk-4 阈值误判
    it("CT-8: large file (>3000 lines) skips diff without crashing", async () => {
      const bigContent = Array.from({ length: 3001 }, (_, i) => `line${i}`).join(
        "\n",
      );
      const { rerender } = await mountEditMode(bigContent);

      (computeDiff as ReturnType<typeof vi.fn>).mockClear?.();

      const modified = bigContent.replace("line1500", "MODIFIED");
      await changeAndFlush(rerender, modified);

      // computeDiff must NOT be called for files > 3000 lines.
      expect(computeDiff).not.toHaveBeenCalled();
      expect(capturedDecoCol).not.toBeNull(); // no crash
    });

    // CT-9 / inv-5 防抖合并
    it("CT-9: 300ms debounce merges rapid successive changes", async () => {
      const { rerender } = await mountEditMode("a\nb");

      // 3 changes at 100ms intervals, all within 300ms window.
      rerender(
        <CodeRenderer content={"a\nx\nb"} language="python" readOnly={false} />,
      );
      await advanceTimers(100);

      rerender(
        <CodeRenderer content={"a\ny\nb"} language="python" readOnly={false} />,
      );
      await advanceTimers(100);

      rerender(
        <CodeRenderer content={"a\nz\nb"} language="python" readOnly={false} />,
      );
      await advanceTimers(100);

      // < 300ms from last change → no diff yet.
      expect(computeDiff).not.toHaveBeenCalled();

      // Advance to 300ms past the final change.
      await advanceTimers(200);

      // Only one diff call, with the final content.
      expect(computeDiff).toHaveBeenCalledTimes(1);
      expect(computeDiff).toHaveBeenCalledWith("a\nb", "a\nz\nb");
      expect(capturedDecoCol?._lastDecos?.length).toBe(1);
    });

    // CT-10 / inv-8 unmount 清理 / Risk-3 stale closure
    it("CT-10: unmount during debounce cancels pending timeout", async () => {
      const { rerender, unmount } = await mountEditMode("a\nb");

      // Trigger content change.
      rerender(
        <CodeRenderer content="a\nx\nb" language="python" readOnly={false} />,
      );
      await advanceTimers(100); // only 100ms, not yet 300ms

      // Unmount before debounce fires.
      unmount();
      await advanceTimers(500);

      // computeDiff must NOT be called — cleanup cleared the timeout.
      expect(computeDiff).not.toHaveBeenCalled();
    });

    // CT-11 / inv-6 类型映射
    it("CT-11: mixed changes → correct decoration types", async () => {
      // original="a\nb\nc\nd", content="x\na\nc\ny"
      // diff: [add x, ctx a, del b, ctx c, del d, add y] → 3 decorations.
      const { rerender } = await mountEditMode("a\nb\nc\nd");
      await changeAndFlush(rerender, "x\na\nc\ny");

      const decos = capturedDecoCol?._lastDecos as Record<string, unknown>[];
      expect(decos?.length).toBe(3);

      // Decoration 1: add "x" → green, line 1.
      expect(
        (decoAt(0).options as Record<string, unknown>).className,
      ).toBe("mid-add-line");
      expect(
        (decoAt(0).options as Record<string, unknown>).glyphMarginClassName,
      ).toBe("mid-add-gutter");
      expect(
        (decoAt(0).range as Record<string, number>).startLineNumber,
      ).toBe(1);

      // Decoration 2: del "b" → red, no className.
      const d2Opts = decoAt(1).options as Record<string, unknown>;
      expect(d2Opts.className).toBeUndefined();
      expect(d2Opts.glyphMarginClassName).toBe("mid-del-gutter");
      expect(
        (d2Opts.overviewRuler as Record<string, string>).color,
      ).toBe("rgba(239,68,68,0.7)");

      // Decoration 3: modify "d"→"y" → yellow, line 4.
      expect(
        (decoAt(2).options as Record<string, unknown>).className,
      ).toBe("mid-modify-line");
      expect(
        (decoAt(2).options as Record<string, unknown>).glyphMarginClassName,
      ).toBe("mid-modify-gutter");
      expect(
        (decoAt(2).options as Record<string, unknown>).overviewRuler,
      ).toEqual({ color: "rgba(234,179,8,0.7)", position: 4 });
      expect(
        (decoAt(2).range as Record<string, number>).startLineNumber,
      ).toBe(4);

      expect(computeDiff).toHaveBeenCalledWith("a\nb\nc\nd", "x\na\nc\ny");
    });

    // CT-14 / inv-6 overviewRuler 颜色（三种类型全覆盖）
    it("CT-14: overviewRuler colors — green (add), yellow (modify), red (del)", async () => {
      // Sub-case A: add → green.
      {
        const { rerender, unmount } = await mountEditMode("a\nb");
        await changeAndFlush(rerender, "a\nx\nb");
        const ovr = firstDecoOpts().overviewRuler as Record<string, unknown>;
        expect(ovr.color).toBe("rgba(34,197,94,0.7)");
        expect(ovr.position).toBe(4);
        unmount();
      }

      // Sub-case B: modify → yellow.
      {
        const { rerender, unmount } = await mountEditMode("a\nb\nc");
        await changeAndFlush(rerender, "a\nx\nc");
        const ovr = firstDecoOpts().overviewRuler as Record<string, unknown>;
        expect(ovr.color).toBe("rgba(234,179,8,0.7)");
        expect(ovr.position).toBe(4);
        unmount();
      }

      // Sub-case C: del → red.
      {
        const { rerender } = await mountEditMode("a\nb\nc");
        await changeAndFlush(rerender, "a\nc");
        const ovr = firstDecoOpts().overviewRuler as Record<string, unknown>;
        expect(ovr.color).toBe("rgba(239,68,68,0.7)");
        expect(ovr.position).toBe(4);
      }
    });
  });

  // ──────────────────────────────────────────────────────────────────────
  // P2 用例
  // ──────────────────────────────────────────────────────────────────────

  describe("P2", () => {
    // CT-13 / inv-6 + Risk-7 空文件 off-by-one
    it("CT-13: empty file → adding content produces correct decorations", async () => {
      const { rerender } = await mountEditMode("");
      await changeAndFlush(rerender, "hello\nworld");

      const decos = capturedDecoCol?._lastDecos as Record<string, unknown>[];
      expect(decos?.length).toBe(2);

      // Both entries are "add" → green.
      for (let i = 0; i < 2; i++) {
        const dOpts = decoAt(i).options as Record<string, unknown>;
        expect(dOpts.className).toBe("mid-add-line");
        expect(dOpts.glyphMarginClassName).toBe("mid-add-gutter");
        expect(
          (decoAt(i).range as Record<string, number>).startLineNumber,
        ).toBe(i + 1);
      }

      expect(computeDiff).toHaveBeenCalledWith("", "hello\nworld");
    });

    // CT-12 / inv-6 + Risk-8 末尾删除 fallback
    it("CT-12: deletion at end of file falls back to marking last line", async () => {
      const { rerender } = await mountEditMode("a\nb");
      await changeAndFlush(rerender, "a");

      // diff: [ctx a, del b]. lineNum for "b"=2, totalLines(new)=1 → fallback.
      const decos = capturedDecoCol?._lastDecos as Record<string, unknown>[];
      expect(decos?.length).toBe(1);

      const opts = decoAt(0).options as Record<string, unknown>;
      const range = decoAt(0).range as Record<string, number>;

      expect(opts.glyphMarginClassName).toBe("mid-del-gutter");
      expect(
        (opts.overviewRuler as Record<string, string>).color,
      ).toBe("rgba(239,68,68,0.7)");

      // Fallback: lineNum > totalLines → anchored at last line (line 1).
      expect(range.startLineNumber).toBe(1);
      expect(range.endLineNumber).toBe(1);
    });
  });
});
