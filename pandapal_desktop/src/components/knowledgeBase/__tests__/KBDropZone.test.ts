/**
 * KBDropZone.test.ts — 外部拖拽落点解析（G 组用例 40-43）
 *
 * jsdom 限制（设计 §0）：无法复现 Tauri onDragDropEvent → 只测纯函数 resolveDropDirAt。
 * Mock 策略：mock @tauri-apps/api/webviewWindow 防导入崩；stub document.elementFromPoint。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import type { MockInstance } from "vitest";
import { resolveDropDirAt } from "../KBDropZone";

vi.mock("@tauri-apps/api/webviewWindow", () => ({
  getCurrentWebviewWindow: () => ({ onDragDropEvent: () => Promise.resolve(() => {}) }),
}));

let container: HTMLDivElement;
let elementFromPointSpy: MockInstance<typeof document.elementFromPoint>;

/** 构造 <div data-drop-dir="..."><span/></div> 并挂到 body */
function mountDropRow(dropDir: string | null): HTMLElement {
  const holder = document.createElement("div");
  if (dropDir !== null) holder.setAttribute("data-drop-dir", dropDir);
  const leaf = document.createElement("span");
  holder.appendChild(leaf);
  container.appendChild(holder);
  return leaf;
}

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  // jsdom 未实现 elementFromPoint → 先补一个占位实现，再 spy 覆盖
  if (typeof (document as unknown as { elementFromPoint?: unknown }).elementFromPoint !== "function") {
    (document as unknown as { elementFromPoint: () => null }).elementFromPoint = () => null;
  }
  elementFromPointSpy = vi.spyOn(document, "elementFromPoint");
});

afterEach(() => {
  container.remove();
  vi.restoreAllMocks();
});

describe("KBDropZone.resolveDropDirAt", () => {
  // inv-22 [P1]：命中文件夹行 → 其 path
  it("用例40 命中文件夹行 → 返回其 path", () => {
    elementFromPointSpy.mockReturnValue(mountDropRow("docs") as unknown as Element);

    expect(resolveDropDirAt(100, 100)).toBe("docs");
  });

  // inv-22 + R3/R8 [P0]：命中文件行 → 返回其父目录
  it("用例41【P0】命中文件行 → 返回其父目录", () => {
    elementFromPointSpy.mockReturnValue(mountDropRow("docs/sub") as unknown as Element);

    expect(resolveDropDirAt(1, 1)).toBe("docs/sub");
  });

  // inv-22 [P1]：命中树容器（data-drop-dir=""）→ 空串而非 null
  it('用例42 命中树容器（data-drop-dir=""）→ 返回空串', () => {
    elementFromPointSpy.mockReturnValue(mountDropRow("") as unknown as Element);

    expect(resolveDropDirAt(1, 1)).toBe("");
  });

  // inv-22 + R8 [P1]：无命中 / 无祖先 → null
  it("用例43 无命中元素或元素无 [data-drop-dir] 祖先 → null", () => {
    elementFromPointSpy.mockReturnValue(null);
    expect(resolveDropDirAt(1, 1)).toBeNull();

    const bare = document.createElement("div");
    container.appendChild(bare);
    elementFromPointSpy.mockReturnValue(bare);
    expect(resolveDropDirAt(1, 1)).toBeNull();
  });
});
