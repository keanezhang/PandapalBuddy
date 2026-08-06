/**
 * useDiffSuggestions Hook 测试
 *
 * 覆盖 test-design.md §7: HOK-1 ~ HOK-9
 */
import { describe, it, expect } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useDiffSuggestions } from "../../src/useDiffSuggestions";

describe("useDiffSuggestions", () => {
  // ── HOK-1: 新增建议 ──────────────────────────────────────────────────
  it("showSuggestion 应创建新建议条目 (HOK-1)", () => {
    const { result } = renderHook(() => useDiffSuggestions());

    act(() => {
      result.current.showSuggestion("/a.py", "orig", "sugg");
    });

    expect(result.current.suggestions["/a.py"]).toEqual({
      original: "orig",
      suggested: "sugg",
      appliedContentKeys: [],
    });
  });

  // ── HOK-2: 覆盖已有建议时重置 appliedContentKeys ─────────────────────
  it("覆盖已有建议应重置 appliedContentKeys (HOK-2)", () => {
    const { result } = renderHook(() => useDiffSuggestions());

    act(() => {
      result.current.showSuggestion("/a.py", "orig1", "sugg1");
    });

    // 先标记一些 applied
    act(() => {
      result.current.markApplied("/a.py", "k1");
      result.current.markApplied("/a.py", "k2");
    });

    // 覆盖建议
    act(() => {
      result.current.showSuggestion("/a.py", "newO", "newS");
    });

    expect(result.current.suggestions["/a.py"]).toEqual({
      original: "newO",
      suggested: "newS",
      appliedContentKeys: [],
    });
  });

  // ── HOK-3: markApplied 追加 key ──────────────────────────────────────
  it("markApplied 应追加 contentKey 到数组 (HOK-3)", () => {
    const { result } = renderHook(() => useDiffSuggestions());

    act(() => {
      result.current.showSuggestion("/a.py", "orig", "sugg");
    });

    act(() => {
      result.current.markApplied("/a.py", "k1");
    });
    act(() => {
      result.current.markApplied("/a.py", "k2");
    });

    expect(result.current.suggestions["/a.py"].appliedContentKeys).toEqual([
      "k1",
      "k2",
    ]);
  });

  // ── HOK-4: markApplied 去重 ──────────────────────────────────────────
  it("markApplied 不应重复添加已存在的 key (HOK-4)", () => {
    const { result } = renderHook(() => useDiffSuggestions());

    act(() => {
      result.current.showSuggestion("/a.py", "orig", "sugg");
      result.current.markApplied("/a.py", "k1");
    });

    // 重复标记同一个 key
    act(() => {
      result.current.markApplied("/a.py", "k1");
    });

    expect(result.current.suggestions["/a.py"].appliedContentKeys).toEqual([
      "k1",
    ]);
  });

  // ── HOK-5: markApplied 不存在的路径不抛异常 ──────────────────────────
  it("markApplied 不存在的路径不应抛异常 (HOK-5)", () => {
    const { result } = renderHook(() => useDiffSuggestions());

    expect(() => {
      act(() => {
        result.current.markApplied("/nonexist.py", "k1");
      });
    }).not.toThrow();

    expect(result.current.suggestions).toEqual({});
  });

  // ── HOK-6: updateSuggestion 更新建议内容 ─────────────────────────────
  it("updateSuggestion 应更新 suggested 字段但保留 original 和 appliedContentKeys (HOK-6)", () => {
    const { result } = renderHook(() => useDiffSuggestions());

    act(() => {
      result.current.showSuggestion("/a.py", "orig", "v1");
      result.current.markApplied("/a.py", "k1");
    });

    act(() => {
      result.current.updateSuggestion("/a.py", "v2");
    });

    expect(result.current.suggestions["/a.py"]).toEqual({
      original: "orig",
      suggested: "v2",
      appliedContentKeys: ["k1"],
    });
  });

  // ── HOK-7: updateSuggestion 不存在的路径不抛异常 ─────────────────────
  it("updateSuggestion 不存在的路径不应抛异常 (HOK-7)", () => {
    const { result } = renderHook(() => useDiffSuggestions());

    expect(() => {
      act(() => {
        result.current.updateSuggestion("/nonexist.py", "v");
      });
    }).not.toThrow();

    expect(result.current.suggestions).toEqual({});
  });

  // ── HOK-8: clearSuggestion 清除指定路径 ───────────────────────────────
  it("clearSuggestion 应清除指定路径的建议 (HOK-8)", () => {
    const { result } = renderHook(() => useDiffSuggestions());

    act(() => {
      result.current.showSuggestion("/a.py", "origA", "suggA");
      result.current.showSuggestion("/b.py", "origB", "suggB");
    });

    act(() => {
      result.current.clearSuggestion("/a.py");
    });

    // 仅剩 /b.py
    expect(result.current.suggestions["/a.py"]).toBeUndefined();
    expect(result.current.suggestions["/b.py"]).toBeDefined();
    expect(Object.keys(result.current.suggestions)).toEqual(["/b.py"]);
  });

  // ── HOK-9: 多次快速 markApplied ─────────────────────────────────────
  it("同帧多次 markApplied 应全部保留 (HOK-9)", () => {
    const { result } = renderHook(() => useDiffSuggestions());

    act(() => {
      result.current.showSuggestion("/a.py", "orig", "sugg");
    });

    act(() => {
      result.current.markApplied("/a.py", "k1");
      result.current.markApplied("/a.py", "k2");
      result.current.markApplied("/a.py", "k3");
    });

    expect(result.current.suggestions["/a.py"].appliedContentKeys).toEqual([
      "k1",
      "k2",
      "k3",
    ]);
  });

  // ── 额外: 空初始状态 ─────────────────────────────────────────────────
  it("初始状态 suggestions 应为空对象", () => {
    const { result } = renderHook(() => useDiffSuggestions());

    expect(result.current.suggestions).toEqual({});
  });

  // ── 额外: 多路径独立管理 ─────────────────────────────────────────────
  it("不同路径的建议应相互独立", () => {
    const { result } = renderHook(() => useDiffSuggestions());

    act(() => {
      result.current.showSuggestion("/a.py", "origA", "suggA");
      result.current.showSuggestion("/b.py", "origB", "suggB");
      result.current.markApplied("/a.py", "k1");
    });

    expect(result.current.suggestions["/a.py"].appliedContentKeys).toEqual([
      "k1",
    ]);
    expect(result.current.suggestions["/b.py"].appliedContentKeys).toEqual([]);
  });
});
