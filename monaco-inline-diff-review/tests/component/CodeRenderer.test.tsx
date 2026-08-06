/**
 * CodeRenderer 组件测试
 *
 * 覆盖 test-design.md §8: CR-1 ~ CR-4
 * 测试智能模式切换（suggestion vs edit）
 */
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, act } from "@testing-library/react";
import { CodeRenderer } from "../../src/CodeRenderer";
import {
  createFakeEditor,
  createFakeMonaco,
  type FakeEditor,
} from "../fixtures/fake-monaco";

// ── Mock @monaco-editor/react ───────────────────────────────────────────
// CodeRenderer uses Editor for both:
//  - suggestion mode → renders InlineDiffEditor → Editor
//  - edit mode → renders Editor directly
// Use the same fake-monaco approach as InlineDiffEditor tests.

const editorInstances: FakeEditor[] = [];

vi.mock("@monaco-editor/react", async () => {
  const React = await import("react");
  const { createFakeEditor, createFakeMonaco } = await import(
    "../fixtures/fake-monaco"
  );
  return {
    default: ({
      defaultValue,
      value,
      onMount,
    }: {
      defaultValue?: string;
      value?: string;
      onMount?: (
        editor: ReturnType<typeof createFakeEditor>,
        monaco: ReturnType<typeof createFakeMonaco>,
      ) => void;
      onChange?: (v: string | undefined) => void;
    }) => {
      const initialValue = value ?? defaultValue ?? "";
      const containerRef = React.useRef<HTMLDivElement | null>(null);

      React.useEffect(() => {
        if (onMount && containerRef.current) {
          const editor = createFakeEditor(initialValue, containerRef.current);
          const monaco = createFakeMonaco();
          editorInstances.push(editor as FakeEditor);
          onMount(
            editor as unknown as ReturnType<typeof createFakeEditor>,
            monaco as unknown as ReturnType<typeof createFakeMonaco>,
          );
        }
      }, []);

      return React.createElement("div", {
        ref: containerRef,
        "data-testid": "monaco-editor",
        "data-value": initialValue,
      });
    },
  };
});

// ── Helpers ────────────────────────────────────────────────────────────

async function nextFrame() {
  await act(async () => {
    vi.advanceTimersByTime(16);
  });
}

beforeEach(() => {
  editorInstances.length = 0;
});

// ── Tests ──────────────────────────────────────────────────────────────

describe("CodeRenderer", () => {
  // ── CR-1: suggestion 模式 → 渲染 InlineDiffEditor ────────────────────
  it("readOnly=true + original 存在 → suggestion 模式 (CR-1)", async () => {
    const { container } = render(
      <CodeRenderer
        content="a\nx\nb"
        original="a\nb"
        language="python"
        readOnly={true}
      />,
    );

    // Wait for mount + rebuild (InlineDiffEditor schedules RAF)
    await nextFrame();

    // Suggestion mode renders InlineDiffEditor with float bar
    const floatBar = container.querySelector(".mid-float-bar");
    expect(floatBar).not.toBeNull();
  });

  // ── CR-2: edit 模式 → 渲染纯 Monaco Editor ──────────────────────────
  it("readOnly=false → edit 模式 (CR-2)", async () => {
    const { container } = render(
      <CodeRenderer
        content="some code"
        language="python"
        original="original code"
        readOnly={false}
      />,
    );

    await nextFrame();

    // Edit mode should NOT have the float bar
    const floatBar = container.querySelector(".mid-float-bar");
    expect(floatBar).toBeNull();

    // Should render the editor in edit mode
    const editorEl = container.querySelector('[data-testid="monaco-editor"]');
    expect(editorEl).not.toBeNull();
  });

  // ── CR-3: original=undefined → edit 模式（即使 readOnly=true）────────
  it("original=undefined → edit 模式 (CR-3)", async () => {
    const { container } = render(
      <CodeRenderer
        content="some code"
        language="python"
        readOnly={true}
      />,
    );

    await nextFrame();

    // No original → edit mode → no float bar
    const floatBar = container.querySelector(".mid-float-bar");
    expect(floatBar).toBeNull();
  });

  // ── CR-4: fileId 变化 → key 强制重挂载 ──────────────────────────────
  it("fileId 变化 → InlineDiffEditor 重新挂载 (CR-4)", async () => {
    const { container, rerender } = render(
      <CodeRenderer
        content="a\nx\nb"
        original="a\nb"
        language="python"
        readOnly={true}
        fileId="f1"
      />,
    );

    await nextFrame();

    const floatBar1 = container.querySelector(".mid-float-bar");
    expect(floatBar1).not.toBeNull();

    // Switch file
    rerender(
      <CodeRenderer
        content="c\ny\nd"
        original="c\nd"
        language="python"
        readOnly={true}
        fileId="f2"
      />,
    );

    await nextFrame();

    // Should render new editor without errors
    const floatBar2 = container.querySelector(".mid-float-bar");
    expect(floatBar2).not.toBeNull();
  });

  // ── 额外: readOnly=false 无 original → edit 模式 ────────────────────
  it("readOnly=false 且无 original → edit 模式", async () => {
    const { container } = render(
      <CodeRenderer content="code here" language="javascript" readOnly={false} />,
    );

    await nextFrame();

    const floatBar = container.querySelector(".mid-float-bar");
    expect(floatBar).toBeNull();
  });
});
