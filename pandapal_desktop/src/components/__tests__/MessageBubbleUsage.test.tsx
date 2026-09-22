/**
 * MessageBubbleUsage.test.tsx — footer 上下文进度条 / 组成明细的渲染契约。
 *
 * 这是端到端的显示层：喂进来的 payload 逐字段对齐后端 REPLY_END.usage
 * （真相源 pandapal/config/budget/guard.py::RunUsageSummary.to_dict，
 *  链路由 pandapal/config/tests/test_e2e_context_footer.py 覆盖）。
 *
 * 断言口径：zh-CN golden 值（fmtTok 对 <10k 不缩写，故 3400 显示为 "3400"）。
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

/** 1M 档的一次真实回复：累计 8M（20 步），当前占用 601k。 */
const USAGE: ReplyUsage = {
  model: "x-1m-y",
  net_cost_usd: 1.234,
  full_cost_usd: 2.0,
  saved_usd: 0.766,
  input_tokens: 8_000_000,        // 跨步累计
  cached_tokens: 7_200_000,
  miss_tokens: 800_000,
  cache_creation_tokens: 0,
  output_tokens: 45_000,
  reply_tokens: 40_000,
  reasoning_tokens: 5_000,
  hit_rate: 0.9,
  duration_ms: 32_100,
  last_input_tokens: 601_000,     // 单次占用
  step_count: 20,
  context_window: 1_000_000,
  compact_threshold: 563_000,
  // 四段之和必须等于 last_input_tokens（后端不变式）：17600+3400+0+580000 = 601000
  context_breakdown: { system: 17_600, tools: 3_400, attachments: 0, history: 580_000 },
  context_quotas: { system_prompt: 24_000, tool_schema: 8_000 },
};

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

beforeEach(async () => {
  await i18n.changeLanguage("zh-CN");
});

describe("footer 上下文进度条", () => {
  it("同时展示累计输入 / 当前占用 / 各段组成（实占/配额）", () => {
    const { container } = render(<MessageBubble message={makeMessage(USAGE)} />);
    const text = textOf(container);

    // 累计输入（跨步）+ 步数解释项
    expect(text).toContain("8.0M");
    expect(text).toContain("20 步");

    // 当前占用 / 模型窗口（单次口径，受窗口约束）
    expect(text).toContain("601.0k / 1.0M (60%)");

    // 压缩线标记
    expect(text).toContain("563.0k");

    // 组成明细：system / tools 带配额，history 无配额
    expect(text).toContain("17.6k / 24.0k");
    expect(text).toContain("3400 / 8000");
    expect(text).toContain("580.0k");
  });

  it("未采集组成明细（旧 sidecar）时退化为单色进度条，其余照常", () => {
    const { container } = render(
      <MessageBubble message={makeMessage({ ...USAGE, context_breakdown: null })} />,
    );
    const text = textOf(container);

    expect(text).toContain("601.0k / 1.0M (60%)");
    expect(text).toContain("563.0k");
    expect(text).not.toContain("580.0k");       // 无分段图例
  });

  it("后端完全未注入配置时不渲染进度行（向后兼容）", () => {
    const { container } = render(
      <MessageBubble
        message={makeMessage({
          ...USAGE,
          context_window: null,
          compact_threshold: null,
          context_breakdown: null,
          context_quotas: null,
        })}
      />,
    );
    const text = textOf(container);

    expect(text).not.toContain("上下文");
    expect(text).toContain("8.0M");             // 老的累计行仍在
  });

  it("无 usage 的消息不渲染任何页脚", () => {
    const { container } = render(<MessageBubble message={makeMessage(undefined)} />);
    expect(textOf(container)).not.toContain("上下文");
    expect(textOf(container)).not.toContain("8.0M");
  });
});
