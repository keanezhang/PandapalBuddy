/**
 * MessageBubbleContextBar.test.tsx — 上下文进度条降级/判定契约（设计文档 6.12）。
 *
 * 覆盖用例：FE-1（context_window=null 不渲染）、FE-2（overLine 红色判定）、
 * FE-3（未超线不红）、FE-4（ctxPct/markPct 上限 100）、FE-5（parts 全零退化单段）、
 * FE-6（多段渲染 + v>0 过滤 + 配额显示）、FE-7（context_breakdown 缺省单段）、
 * FE-8（标记线绘制条件 0 < markPct < 100）。
 *
 * Oracle：pct = min(100, x / context_window × 100)，golden 值独立按公式手算。
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render } from "@testing-library/react";
import { MessageBubble } from "../ChatArea/MessageBubble";
import type { CompletedMessage } from "../../store/chatStore";
import type { ReplyUsage } from "../../types/api";
import "../../i18n";
import i18n from "i18next";

vi.mock("../../providers/BackendProvider", () => ({
  useBackend: () => ({ sendInteractionResponse: vi.fn() }),
}));

function makeUsage(overrides: Partial<ReplyUsage> = {}): ReplyUsage {
  return {
    model: "x-1m-y",
    net_cost_usd: 0,
    full_cost_usd: 0,
    saved_usd: 0,
    input_tokens: 0,
    cached_tokens: 0,
    miss_tokens: 0,
    cache_creation_tokens: 0,
    output_tokens: 0,
    reply_tokens: 0,
    reasoning_tokens: 0,
    hit_rate: 0,
    duration_ms: 0,
    last_input_tokens: 0,
    step_count: 0,
    context_window: null,
    compact_threshold: null,
    ...overrides,
  };
}

function makeMessage(usage?: ReplyUsage): CompletedMessage {
  return {
    kind: "completed",
    id: "r-1",
    role: "assistant",
    text: "done",
    timeline: [{ kind: "text", content: "done" }] as CompletedMessage["timeline"],
    toolCalls: [],
    timestamp: 1_700_000_000_000,
    usage,
  };
}

const textOf = (c: HTMLElement) => c.textContent ?? "";

/** 按精确文本定位叶子节点（用于占比文本的样式断言）。 */
function findLeafByText(container: HTMLElement, text: string): HTMLElement | null {
  const els = Array.from(container.querySelectorAll<HTMLElement>("*"));
  return els.find((el) => el.children.length === 0 && (el.textContent ?? "").trim() === text) ?? null;
}

/** 进度条容器：relative 定位 + 6px 高，唯一。 */
function getBar(container: HTMLElement): HTMLElement {
  const bar = Array.from(container.querySelectorAll<HTMLElement>("span"))
    .find((s) => s.style.position === "relative" && s.style.height === "6px");
  if (!bar) throw new Error("上下文进度条容器未找到");
  return bar;
}

/** 进度条内的填充段（排除 absolute 的标记线）。 */
function getFillSpans(container: HTMLElement): HTMLElement[] {
  const bar = getBar(container);
  return Array.from(bar.children).filter(
    (c) => (c as HTMLElement).style.position !== "absolute",
  ) as HTMLElement[];
}

/** 自动压缩标记线：absolute + 2px + text-primary。 */
function findMarkLine(container: HTMLElement): HTMLElement | null {
  return Array.from(container.querySelectorAll<HTMLElement>("span")).find(
    (s) =>
      s.style.position === "absolute" &&
      s.style.width === "2px" &&
      s.style.background === "var(--text-primary)",
  ) ?? null;
}

beforeEach(async () => {
  await i18n.changeLanguage("zh-CN");
});

describe("上下文进度条（inv-7 前端降级 / R9）", () => {
  it("FE-1: context_window=null 不渲染进度条", () => {
    const { container } = render(
      <MessageBubble
        message={makeMessage(
          makeUsage({
            context_window: null,
            compact_threshold: 500,
            last_input_tokens: 300,
            context_breakdown: null,
          }),
        )}
      />,
    );
    const text = textOf(container);
    expect(text).not.toContain("上下文");
    expect(text).not.toMatch(/\(\d+%\)/);
  });

  type Fe2 = [last: number, fillWidth: string, pctText: string];
  it.each<Fe2>([
    [500, "50%", "500 / 1000 (50%)"], // 恰好等于 compact_threshold
    [600, "60%", "600 / 1000 (60%)"], // 超过 compact_threshold
  ])("FE-2: last_input_tokens=%i 触线 → 占比文本与单段 bar 变红", (last, fillWidth, pctText) => {
    const { container } = render(
      <MessageBubble
        message={makeMessage(
          makeUsage({
            context_window: 1000,
            compact_threshold: 500,
            last_input_tokens: last,
            context_breakdown: null,
          }),
        )}
      />,
    );

    const pct = findLeafByText(container, pctText);
    expect(pct).not.toBeNull();
    expect(pct!.style.color).toBe("var(--danger)");

    const fills = getFillSpans(container);
    expect(fills.length).toBe(1);
    expect(fills[0].style.width).toBe(fillWidth);
    expect(fills[0].style.background).toBe("var(--danger)");
  });

  it("FE-3: 未超线不红（last < compact_threshold）", () => {
    const { container } = render(
      <MessageBubble
        message={makeMessage(
          makeUsage({
            context_window: 1000,
            compact_threshold: 500,
            last_input_tokens: 499,
            context_breakdown: null,
          }),
        )}
      />,
    );

    const pct = findLeafByText(container, "499 / 1000 (50%)");
    expect(pct).not.toBeNull();
    expect(pct!.style.color).toBe("var(--text-tertiary)");

    const fills = getFillSpans(container);
    expect(fills.length).toBe(1);
    expect(fills[0].style.width).toBe("49.9%");
    expect(fills[0].style.background).toBe("var(--accent)");
  });

  it("FE-4: ctxPct / markPct 上限 100（分子与标记均超分母）", () => {
    const { container } = render(
      <MessageBubble
        message={makeMessage(
          makeUsage({
            context_window: 1000,
            last_input_tokens: 1500,
            compact_threshold: 1500,
            context_breakdown: null,
          }),
        )}
      />,
    );

    expect(textOf(container)).toContain("(100%)");

    const fills = getFillSpans(container);
    expect(fills.length).toBe(1);
    expect(fills[0].style.width).toBe("100%");

    expect(findMarkLine(container)).toBeNull();
  });

  it("FE-5: parts 全零退化单段（无四段 legend）", () => {
    const { container } = render(
      <MessageBubble
        message={makeMessage(
          makeUsage({
            context_window: 1000,
            compact_threshold: 500,
            last_input_tokens: 300,
            context_breakdown: { system: 0, tools: 0, attachments: 0, history: 0 },
          }),
        )}
      />,
    );

    const fills = getFillSpans(container);
    expect(fills.length).toBe(1);
    expect(fills[0].style.width).toBe("30%");
    expect(fills[0].style.background).toBe("var(--accent)");

    const text = textOf(container);
    expect(text).not.toContain("system");
    expect(text).not.toContain("工具");
    expect(text).not.toContain("历史");
    expect(text).not.toContain("回注");
  });

  it("FE-6: 多段渲染 + v>0 过滤 + 配额显示", () => {
    const { container } = render(
      <MessageBubble
        message={makeMessage(
          makeUsage({
            context_window: 1_000_000,
            compact_threshold: null,
            last_input_tokens: 527_374,
            context_breakdown: { system: 24_000, tools: 3_374, attachments: 0, history: 500_000 },
            context_quotas: { system_prompt: 24_000, tool_schema: 8_000 },
          }),
        )}
      />,
    );

    const fills = getFillSpans(container);
    expect(fills.length).toBe(3);
    expect(fills[0].style.width).toBe("2.4%");
    expect(fills[0].style.background).toBe("var(--info)");
    expect(fills[1].style.width).toBe("0.3374%");
    expect(fills[1].style.background).toBe("var(--success)");
    expect(fills[2].style.width).toBe("50%");
    expect(fills[2].style.background).toBe("var(--accent)");

    const text = textOf(container);
    expect(text).toContain("24.0k / 24.0k");
    expect(text).toContain("3374 / 8000");
    expect(text).toContain("500.0k");
    expect(text).not.toContain("500.0k /");
    expect(text).not.toContain("回注");
  });

  it.each<ReplyUsage["context_breakdown"]>([null, undefined])(
    "FE-7: context_breakdown=%s 缺省 → 单段 fallback（无 legend）",
    (bd) => {
      const { container } = render(
        <MessageBubble
          message={makeMessage(
            makeUsage({
              context_window: 1000,
              compact_threshold: 500,
              last_input_tokens: 300,
              context_breakdown: bd,
            }),
          )}
        />,
      );

      const fills = getFillSpans(container);
      expect(fills.length).toBe(1);
      expect(fills[0].style.width).toBe("30%");

      const text = textOf(container);
      expect(text).not.toContain("system");
      expect(text).not.toContain("工具");
      expect(text).not.toContain("历史");
      expect(text).not.toContain("回注");
    },
  );

  type Fe8 = [threshold: number, hasMark: boolean, expectedLeft: string | null];
  it.each<Fe8>([
    [0, false, null], // markPct=0
    [500, true, "50%"], // 0 < markPct < 100
    [1000, false, null], // markPct=100（阈值 ≥ 窗口）
  ])("FE-8: compact_threshold=%i 标记线绘制条件（0 < markPct < 100）", (threshold, hasMark, expectedLeft) => {
    const { container } = render(
      <MessageBubble
        message={makeMessage(
          makeUsage({
            context_window: 1000,
            compact_threshold: threshold,
            last_input_tokens: 300,
            context_breakdown: null,
          }),
        )}
      />,
    );

    const mark = findMarkLine(container);
    if (hasMark) {
      expect(mark).not.toBeNull();
      expect(mark!.style.left).toBe(expectedLeft);
    } else {
      expect(mark).toBeNull();
    }
  });
});
