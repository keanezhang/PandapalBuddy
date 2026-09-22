/**
 * KBContextMenu.test.tsx — 文件树右键菜单（E 组用例 30-35）
 *
 * Mock 策略（设计 §2）：无 IO，真实渲染（菜单文案为源码硬编码，不依赖 i18n）。
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { KBContextMenu } from "../KBContextMenu";
import type { KBDocNode } from "../../../types/api";

function nodeOf(path: string, is_dir: boolean): KBDocNode {
  return {
    path,
    name: path.split("/").pop()!,
    is_dir,
    size: 0,
    suffix: "",
    mtime: 0,
    children: is_dir ? [] : null,
  };
}

/** 菜单项 label 序列（menuitem = [icon span, label span]） */
function menuLabels(): string[] {
  return Array.from(document.querySelectorAll('[role="menuitem"]')).map(
    (el) => (el.lastElementChild as HTMLElement)?.textContent ?? "",
  );
}

function renderMenu(node: KBDocNode, onAction = vi.fn(), onClose = vi.fn()) {
  const utils = render(
    <KBContextMenu x={10} y={10} node={node} onAction={onAction} onClose={onClose} />,
  );
  return { ...utils, onAction, onClose };
}

describe("KBContextMenu", () => {
  // inv-19 + R6 [P1]：文件节点含「预览」，集合完整
  it("用例30 文件节点菜单含「预览」且项集合完整", () => {
    renderMenu(nodeOf("a.md", false));

    expect(menuLabels()).toEqual(["预览", "新建文件夹", "上传到此处", "重命名", "删除"]);
    expect(screen.queryByText("移到根目录")).toBeNull();
  });

  // inv-19 + R6 [P1]：文件夹节点不含「预览」
  it("用例31 文件夹节点菜单不含「预览」", () => {
    renderMenu(nodeOf("docs", true));

    expect(screen.queryByText("预览")).toBeNull();
    expect(menuLabels()).toEqual(["新建文件夹", "上传到此处", "重命名", "删除"]);
  });

  // inv-19 + R6 [P1]：「移到根目录」随 path 是否含 "/" 显隐
  it('用例32 「移到根目录」仅当 path 含 "/" 时出现', () => {
    const r1 = renderMenu(nodeOf("docs/a.md", false));
    expect(screen.getByText("移到根目录")).toBeTruthy();
    r1.unmount();

    renderMenu(nodeOf("docs", true));
    expect(screen.queryByText("移到根目录")).toBeNull();
  });

  // inv-19 + R6 [P1]：仅删除项为 danger
  it("用例33 删除项为 danger 色，其余项为常规色", () => {
    renderMenu(nodeOf("a.md", false));

    const del = screen.getByText("删除").closest('[role="menuitem"]') as HTMLElement;
    expect(del.style.color).toBe("var(--danger)");

    const ren = screen.getByText("重命名").closest('[role="menuitem"]') as HTMLElement;
    expect(ren.style.color).toBe("var(--text-primary)");
  });

  // inv-20 [P1]：点击项 → onAction(action) + onClose
  it("用例34 点击菜单项 → onAction(action) 与 onClose 各一次", () => {
    const { onAction, onClose } = renderMenu(nodeOf("a.md", false));

    fireEvent.click(screen.getByText("重命名"));

    expect(onAction).toHaveBeenCalledTimes(1);
    expect(onAction).toHaveBeenCalledWith("rename");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  // inv-20 [P1]：Esc / 外部点击 → onClose
  it("用例35 Esc 与外部 mousedown → onClose", () => {
    const { onClose } = renderMenu(nodeOf("a.md", false));

    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);

    fireEvent.mouseDown(document.body);
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});
