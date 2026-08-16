/**
 * InteractionInline.test.tsx
 *
 * 覆盖 QuestionCard 的「自由输入」显式化修复（经 InteractionInline 间接渲染，必须传 tool_name）：
 *   - FE-01/02/03: 自由输入入口渲染数量、旧硬编码入口消失、label 精确匹配
 *   - FE-04/05/06/07: FREE_INPUT_PREFIX 状态机（选中→输入→提交 / 清空 / 切普通选项 / 返回选项）
 *
 * Mock 策略：vi.mock useBackend（Tauri IPC 无 jsdom 运行时）；i18n 用真实 zh-CN 实例。
 * 确定性控制：beforeEach 钉死 i18n.changeLanguage("zh-CN")，文本断言用 zh-CN golden 值。
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { InteractionInline } from "../InteractionInline";
import type { QuestionItem } from "../../types/api";
import "../../i18n";
import i18n from "i18next";

// ── useBackend mock（vi.hoisted：避免 mock factory 闭包引用 TDZ）──
const { mockSendInteractionResponse } = vi.hoisted(() => ({
  mockSendInteractionResponse: vi.fn(),
}));

vi.mock("../../providers/BackendProvider", () => ({
  useBackend: () => ({ sendInteractionResponse: mockSendInteractionResponse }),
}));

const FREE_INPUT_LABEL = "自由输入";
/** 旧硬编码入口文案 = t("interaction.freeInput") = "自由输入..." */
const OLD_HARDCODED_ENTRY = "自由输入...";
const SUBMIT_ALL = "提交全部回答";
const FREE_INPUT_PLACEHOLDER = "输入你的想法...";
const BACK_TO_OPTIONS = "← 返回选项";
const PENDING_ONE = "请回答所有问题（1 题未答）";

function renderCard(options: QuestionItem["options"]) {
  const onResolved = vi.fn();
  const utils = render(
    <InteractionInline
      questions={[{ question: "Q?", header: "主题", multiSelect: false, options }]}
      run_id="r1"
      tool_name="ask_user"
      sessionId="s1"
      onResolved={onResolved}
    />,
  );
  return { ...utils, onResolved };
}

describe("InteractionInline / QuestionCard 自由输入显式化", () => {
  beforeEach(async () => {
    mockSendInteractionResponse.mockReset();
    await i18n.changeLanguage("zh-CN");
  });

  // inv-F1 + inv-F2 + R-F1 [P0]
  it("FE-01 含精确「自由输入」时恰好 1 个入口，且无旧硬编码入口", () => {
    const { getByText, getAllByText, queryByText } = renderCard([
      { label: "A", description: "选项A" },
      { label: FREE_INPUT_LABEL, description: "自行填写" },
    ]);

    expect(getByText("A")).toBeTruthy();
    expect(getByText("— 选项A")).toBeTruthy();
    expect(getAllByText(FREE_INPUT_LABEL)).toHaveLength(1);
    expect(queryByText(OLD_HARDCODED_ENTRY)).toBeNull();
  });

  // inv-F1 + R-F2 异常数据 [P1]
  it("FE-02 不含「自由输入」时不崩溃、不渲染自由输入入口", () => {
    const { getByText, queryByText } = renderCard([
      { label: "A", description: "" },
      { label: "B", description: "" },
    ]);

    expect(getByText("A")).toBeTruthy();
    expect(getByText("B")).toBeTruthy();
    expect(queryByText(FREE_INPUT_LABEL)).toBeNull();
    expect(queryByText(OLD_HARDCODED_ENTRY)).toBeNull();
  });

  // inv-F1 + inv-F2 + R-F3 精确匹配 [P1]
  it("FE-03 相似 label「自由输入xxx」不被误吞，且与精确项互不干扰", () => {
    const { getByText, getAllByText } = renderCard([
      { label: "自由输入xxx", description: "" },
      { label: FREE_INPUT_LABEL, description: "" },
    ]);

    expect(getByText("自由输入xxx")).toBeTruthy();
    expect(getAllByText(FREE_INPUT_LABEL)).toHaveLength(1);
  });

  // inv-F3 + R-F4 FREE_INPUT_PREFIX 机制 [P1]
  it("FE-04 自由输入选中→输入→提交，response 剥离 FREE_INPUT_PREFIX", () => {
    const { getByText, getByPlaceholderText, onResolved } = renderCard([
      { label: "A", description: "" },
      { label: FREE_INPUT_LABEL, description: "" },
    ]);

    fireEvent.click(getByText(FREE_INPUT_LABEL));
    const textarea = getByPlaceholderText(FREE_INPUT_PLACEHOLDER);
    fireEvent.change(textarea, { target: { value: "自定义答案" } });
    fireEvent.click(getByText(SUBMIT_ALL));

    expect(mockSendInteractionResponse).toHaveBeenCalledTimes(1);
    expect(mockSendInteractionResponse).toHaveBeenCalledWith(
      "r1",
      "主题=自定义答案",
      "s1",
    );
    expect(onResolved).toHaveBeenCalledTimes(1);
    expect(onResolved).toHaveBeenCalledWith("用户选择了：主题=自定义答案");
  });

  // inv-F3 + R-F4 清空行为 [P1]
  it("FE-05 自由输入清空（空串）→ 回退未作答，提交按钮禁用", () => {
    const { getByText, getByPlaceholderText } = renderCard([
      { label: "A", description: "" },
      { label: FREE_INPUT_LABEL, description: "" },
    ]);

    fireEvent.click(getByText(FREE_INPUT_LABEL));
    const textarea = getByPlaceholderText(FREE_INPUT_PLACEHOLDER);
    fireEvent.change(textarea, { target: { value: "" } });

    const btn = getByText(PENDING_ONE) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(mockSendInteractionResponse).not.toHaveBeenCalled();
  });

  // inv-F3 + R-F4 切换行为 [P1]
  // [known-gap] 期望（设计文档）：编辑视图下普通选项仍可见，点「A」切回选项视图并选中 A；
  // 现状：编辑视图（freeInputActive=true）下普通选项被 textarea 整体替换，普通选项不可点击，
  // 「编辑视图 --点普通选项--> 选项视图」这条 0-switch 转换当前不可达。用 it.fails 显式标记，
  // 修复后意外通过即报警。
  it.fails("FE-06 自由输入激活时点击普通选项 → 切回普通选项，自由输入框消失", () => {
    const { container, getByText, queryByRole } = renderCard([
      { label: "A", description: "" },
      { label: FREE_INPUT_LABEL, description: "" },
    ]);

    fireEvent.click(getByText(FREE_INPUT_LABEL));
    expect(queryByRole("textbox")).not.toBeNull();

    fireEvent.click(getByText("A")); // 期望可点击；现状编辑视图下不渲染普通选项 → 抛「找不到元素」

    expect(queryByRole("textbox")).toBeNull();
    const selected = container.querySelector(".interaction-option.selected");
    expect(selected).not.toBeNull();
    expect(selected!.textContent).toContain("A");
    const submitBtn = getByText(SUBMIT_ALL) as HTMLButtonElement;
    expect(submitBtn.disabled).toBe(false);
  });

  // inv-F3 + R-F4 清空/返回行为 [P1]
  it("FE-07 点击「返回选项」→ 清空选择并返回选项列表", () => {
    const { getByText, queryByRole } = renderCard([
      { label: "A", description: "" },
      { label: FREE_INPUT_LABEL, description: "" },
    ]);

    fireEvent.click(getByText(FREE_INPUT_LABEL));
    expect(queryByRole("textbox")).not.toBeNull();

    fireEvent.click(getByText(BACK_TO_OPTIONS));

    expect(queryByRole("textbox")).toBeNull();
    expect(getByText("A")).toBeTruthy();
    expect(getByText(FREE_INPUT_LABEL)).toBeTruthy();
    const btn = getByText(PENDING_ONE) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(mockSendInteractionResponse).not.toHaveBeenCalled();
  });
});
