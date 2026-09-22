/**
 * ConfirmDialog.test.tsx — 通用二次确认弹窗（F 组用例 36-39）
 *
 * Mock 策略（设计 §2）：无 IO，真实渲染 + 真实 i18n（取消/确认走 common.* key）。
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ConfirmDialog } from "../ConfirmDialog";
import "../../../i18n";

describe("ConfirmDialog", () => {
  // inv-21 + R7 [P1]：open=false → 不渲染
  it("用例36 open=false → 不渲染任何 dialog，回调不触发", () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <ConfirmDialog
        open={false}
        title="删除文档"
        message="x"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );

    expect(screen.queryByTestId("kb-confirm-dialog")).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onConfirm).not.toHaveBeenCalled();
    expect(onCancel).not.toHaveBeenCalled();
  });

  // inv-21 + R7 [P1]：确认 → onConfirm；danger 决定按钮类名
  it("用例37 open=true 点确认 → onConfirm；danger 切换 btn-danger / btn-primary", () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();

    const r1 = render(
      <ConfirmDialog
        open
        danger
        title="删除文档"
        message="x"
        confirmLabel="确认删除"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );
    const dangerBtn = screen.getByRole("button", { name: "确认删除" });
    expect(dangerBtn.className).toContain("btn-danger");
    fireEvent.click(dangerBtn);
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onCancel).not.toHaveBeenCalled();
    r1.unmount();

    render(
      <ConfirmDialog
        open
        danger={false}
        title="删除文档"
        message="x"
        confirmLabel="确认删除"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "确认删除" }).className).toContain("btn-primary");
  });

  // inv-21 + R7 [P1]：取消 → onCancel，不触发 onConfirm
  it("用例38 点取消 → onCancel，不触发 onConfirm", () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <ConfirmDialog
        open
        title="删除文档"
        message="x"
        confirmLabel="确认删除"
        cancelLabel="取消"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "取消" }));

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  // inv-21 + R7 [P1]：遮罩关闭，内容区不关闭（stopPropagation）
  it("用例39 点击遮罩 → onCancel；点击内容区不关闭", () => {
    const onCancel = vi.fn();
    render(
      <ConfirmDialog open title="删除文档" message="x" onConfirm={vi.fn()} onCancel={onCancel} />,
    );

    fireEvent.click(screen.getByRole("dialog"));
    expect(onCancel).toHaveBeenCalledTimes(1);

    onCancel.mockClear();
    fireEvent.click(screen.getByTestId("kb-confirm-dialog"));
    expect(onCancel).not.toHaveBeenCalled();
  });
});
