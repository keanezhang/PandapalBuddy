/**
 * KBFileTree.test.tsx — 文件树面板（D 组用例 19-29）
 *
 * Mock 策略（设计 §2）：只 mock ../fileRenderers 的 fileIcon（隔离图标依赖，避免 Monaco）。
 * 树数据用真实 KBDocNode；受控 searchQuery 由 wrapper state 驱动。
 */
import { useState } from "react";
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/react";
import { KBFileTree, type KBFileTreeProps } from "../KBFileTree";
import type { KBDocNode } from "../../../types/api";

const { mockFileIcon } = vi.hoisted(() => ({
  mockFileIcon: vi.fn((name: string) => name),
}));

vi.mock("../../fileRenderers", () => ({ fileIcon: mockFileIcon }));

function file(path: string, name = path.split("/").pop()!): KBDocNode {
  return { path, name, is_dir: false, size: 0, suffix: "", mtime: 0, children: null };
}

function dir(path: string, children: KBDocNode[] = [], name = path.split("/").pop()!): KBDocNode {
  return { path, name, is_dir: true, size: 0, suffix: "", mtime: 0, children };
}

function makeTreeProps(overrides: Partial<KBFileTreeProps> = {}): KBFileTreeProps {
  return {
    tree: [],
    selectedPath: null,
    searchQuery: "",
    onSearchChange: vi.fn(),
    onSelect: vi.fn(),
    onContextMenu: vi.fn(),
    onMoveNode: vi.fn(),
    onRename: vi.fn(),
    externalDropTarget: null,
    uploadFeedback: null,
    renameRequest: null,
    ...overrides,
  };
}

function renderTree(overrides: Partial<KBFileTreeProps> = {}) {
  const props = makeTreeProps(overrides);
  const utils = render(<KBFileTree {...props} />);
  return { ...utils, props };
}

/** 受控搜索框：searchQuery 由本地 state 驱动 */
function TreeHarness({ tree }: { tree: KBDocNode[] }) {
  const [q, setQ] = useState("");
  return <KBFileTree {...makeTreeProps({ tree, searchQuery: q, onSearchChange: setQ })} />;
}

/** drag 事件的 dataTransfer 桩（jsdom 无 DataTransfer） */
function makeDataTransfer() {
  return { setData: vi.fn(), effectAllowed: "", dropEffect: "" };
}

beforeEach(() => {
  mockFileIcon.mockClear();
});

describe("KBFileTree", () => {
  // inv-18 + R12 [P2]：空树 → 空态 + 统计 0
  it("用例19 空树 → 空态文案 + 统计 0", () => {
    const { getByText } = renderTree({ tree: [] });

    expect(getByText("还没有文件，拖拽或上传文档开始")).toBeTruthy();
    expect(getByText("0 个文档")).toBeTruthy();
    expect(getByText("0 个文件夹")).toBeTruthy();
  });

  // inv-14 + R4 [P1]：搜索命中保留祖先链
  it("用例20a 搜索命中深层文件 → 仅显示命中节点及其祖先链", () => {
    const tree = [
      dir("docs", [
        dir("docs/sub", [file("docs/sub/report.md")]),
        file("docs/other.md"),
      ]),
      dir("photos", [file("photos/pic.png")]),
    ];
    const { getByTestId, queryByTestId } = render(<TreeHarness tree={tree} />);

    fireEvent.change(getByTestId("kb-tree-search"), { target: { value: "report" } });

    expect(getByTestId("kb-node-docs/sub/report.md")).toBeTruthy();
    expect(queryByTestId("kb-node-photos/pic.png")).toBeNull();
  });

  // inv-14 [P1]：无命中显示提示
  it("用例20b 搜索无命中 → 显示「无匹配文件」且无任何节点", () => {
    const tree = [dir("docs", [file("docs/a.md")])];
    const { getByTestId, getByText, queryByTestId } = render(<TreeHarness tree={tree} />);

    fireEvent.change(getByTestId("kb-tree-search"), { target: { value: "zzz" } });

    expect(getByText("无匹配文件")).toBeTruthy();
    expect(queryByTestId("kb-node-docs")).toBeNull();
  });

  // inv-14 [P1]：清空搜索恢复全量（顶层节点层面）
  it("用例20c 清空搜索 → 恢复全量顶层节点", () => {
    const tree = [
      dir("docs", [dir("docs/sub", [file("docs/sub/report.md")]), file("docs/other.md")]),
      dir("photos", [file("photos/pic.png")]),
    ];
    const { getByTestId } = render(<TreeHarness tree={tree} />);
    const search = getByTestId("kb-tree-search");

    fireEvent.change(search, { target: { value: "report" } });
    expect(getByTestId("kb-node-docs/sub/report.md")).toBeTruthy();

    fireEvent.change(search, { target: { value: "" } });

    expect(getByTestId("kb-node-docs")).toBeTruthy();
    expect(getByTestId("kb-node-photos")).toBeTruthy();
  });

  // inv-16 补充 + 设计 F15：清空搜索 → 恢复完整树并「还原用户展开态」
  // （用户此前未手动展开 → 文件夹收起；搜索期的自动展开是临时态）
  it("用例20c 清空搜索后还原用户展开态（未手动展开过 → 子树收起）", () => {
    const tree = [
      dir("docs", [dir("docs/sub", [file("docs/sub/report.md")]), file("docs/other.md")]),
      dir("photos", [file("photos/pic.png")]),
    ];
    const { getByTestId, queryByTestId } = render(<TreeHarness tree={tree} />);
    const search = getByTestId("kb-tree-search");

    fireEvent.change(search, { target: { value: "report" } });
    // 搜索态：命中的祖先链自动展开
    expect(getByTestId("kb-node-docs/sub/report.md")).toBeTruthy();

    fireEvent.change(search, { target: { value: "" } });
    // 清空后还原用户展开态（无手动展开）→ 文件夹收起，子树不可见
    expect(queryByTestId("kb-node-docs/other.md")).toBeNull();
    expect(queryByTestId("kb-node-photos/pic.png")).toBeNull();
    // 完整树恢复：顶层目录均在
    expect(getByTestId("kb-node-docs")).toBeTruthy();
    expect(getByTestId("kb-node-photos")).toBeTruthy();
  });

  // inv-16 [P1]：文件夹默认收起，点击展开/再点收起
  it("用例21 文件夹默认收起，点击展开（onSelect 1 次），再点收起", () => {
    const { getByTestId, queryByTestId, props } = renderTree({
      tree: [dir("docs", [file("docs/a.md")])],
    });

    expect(queryByTestId("kb-node-docs/a.md")).toBeNull();

    fireEvent.click(getByTestId("kb-node-docs"));
    expect(getByTestId("kb-node-docs/a.md")).toBeTruthy();
    expect(props.onSelect).toHaveBeenCalledTimes(1);

    fireEvent.click(getByTestId("kb-node-docs"));
    expect(queryByTestId("kb-node-docs/a.md")).toBeNull();
  });

  // inv-15 + R5 [P1]：Enter 提交（trim 后）
  it("用例22 内联重命名 Enter 提交（trim 后）→ onRename(node, newName)", async () => {
    const nodeA = file("a.md");
    const { getByTestId, queryByTestId, props } = renderTree({
      tree: [nodeA],
      renameRequest: { path: "a.md", token: 1 },
    });

    await waitFor(() => expect(getByTestId("kb-rename-input-a.md")).toBeTruthy());
    const input = getByTestId("kb-rename-input-a.md") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "  new.md  " } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(props.onRename).toHaveBeenCalledTimes(1);
    expect(props.onRename).toHaveBeenCalledWith(nodeA, "new.md");
    expect(queryByTestId("kb-rename-input-a.md")).toBeNull();
  });

  // inv-15 + R5 [P1]：空名 / 纯空白 / 同名 → 不提交
  it.each([["", "空名"], ["   ", "纯空白"], ["a.md", "与原同名"]])(
    "用例23 重命名输入 %j（%s）→ 不触发 onRename，输入框关闭",
    async (value) => {
      const { getByTestId, queryByTestId, props } = renderTree({
        tree: [file("a.md")],
        renameRequest: { path: "a.md", token: 1 },
      });

      await waitFor(() => expect(getByTestId("kb-rename-input-a.md")).toBeTruthy());
      const input = getByTestId("kb-rename-input-a.md") as HTMLInputElement;
      fireEvent.change(input, { target: { value: value as string } });
      fireEvent.keyDown(input, { key: "Enter" });

      expect(props.onRename).not.toHaveBeenCalled();
      expect(queryByTestId("kb-rename-input-a.md")).toBeNull();
    },
  );

  // inv-15 [P1]：Escape 取消
  it("用例24 重命名 Escape → 取消，不触发 onRename，行文本不变", async () => {
    const { getByTestId, queryByTestId, props } = renderTree({
      tree: [file("a.md")],
      renameRequest: { path: "a.md", token: 1 },
    });

    await waitFor(() => expect(getByTestId("kb-rename-input-a.md")).toBeTruthy());
    const input = getByTestId("kb-rename-input-a.md") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "changed.md" } });
    fireEvent.keyDown(input, { key: "Escape" });

    expect(props.onRename).not.toHaveBeenCalled();
    expect(queryByTestId("kb-rename-input-a.md")).toBeNull();
    // 行文本仍为原名（fileIcon 被 stub 成原名，故按 label span 的 title 精确定位）
    const row = getByTestId("kb-node-a.md");
    expect(row.querySelector('span[title="a.md"]')?.textContent).toBe("a.md");
  });

  // inv-17 [P1]：落到文件夹自身 path
  it("用例25 拖拽 drop 到文件夹行 → onMoveNode(src, folderPath)", () => {
    const { getByTestId, props } = renderTree({
      tree: [file("a.md"), dir("docs")],
    });
    const dataTransfer = makeDataTransfer();

    fireEvent.dragStart(getByTestId("kb-node-a.md"), { dataTransfer });
    fireEvent.dragOver(getByTestId("kb-node-docs"), { dataTransfer });
    fireEvent.drop(getByTestId("kb-node-docs"), { dataTransfer });

    expect(props.onMoveNode).toHaveBeenCalledWith("a.md", "docs");
  });

  // inv-17 [P1]：drop 到文件夹行恰好触发 1 次 onMoveNode
  // （修复：行内 onDrop/onDragOver 加 stopPropagation，避免冒泡到容器再追加一次 handleDrop("")）
  it("用例25 附加：drop 到文件夹行应只触发一次 onMoveNode", () => {
    const { getByTestId, props } = renderTree({
      tree: [file("a.md"), dir("docs")],
    });
    const dataTransfer = makeDataTransfer();

    fireEvent.dragStart(getByTestId("kb-node-a.md"), { dataTransfer });
    fireEvent.dragOver(getByTestId("kb-node-docs"), { dataTransfer });
    fireEvent.drop(getByTestId("kb-node-docs"), { dataTransfer });

    expect(props.onMoveNode).toHaveBeenCalledTimes(1);
    expect(props.onMoveNode).toHaveBeenCalledWith("a.md", "docs");
  });

  // inv-17 + R3/R8 [P0]：落到文件行的父目录
  it("用例26【P0】拖拽 drop 到嵌套文件行 → onMoveNode(src, 父目录)", () => {
    const { getByTestId, props } = renderTree({
      tree: [file("old.md"), dir("docs", [dir("docs/sub", [file("docs/sub/y.md")])])],
    });

    // 展开 docs / docs/sub 以露出嵌套文件行
    fireEvent.click(getByTestId("kb-node-docs"));
    fireEvent.click(getByTestId("kb-node-docs/sub"));

    const dataTransfer = makeDataTransfer();
    fireEvent.dragStart(getByTestId("kb-node-old.md"), { dataTransfer });
    fireEvent.dragOver(getByTestId("kb-node-docs/sub/y.md"), { dataTransfer });
    fireEvent.drop(getByTestId("kb-node-docs/sub/y.md"), { dataTransfer });

    expect(props.onMoveNode).toHaveBeenCalledWith("old.md", "docs/sub");
  });

  // inv-17 [P1]：落到树容器空白 → 根
  it('用例27 拖拽 drop 到树容器空白 → onMoveNode(src, "")', () => {
    const { getByTestId, props } = renderTree({
      tree: [dir("docs", [file("docs/a.md")])],
    });
    fireEvent.click(getByTestId("kb-node-docs"));

    const dataTransfer = makeDataTransfer();
    fireEvent.dragStart(getByTestId("kb-node-docs/a.md"), { dataTransfer });
    fireEvent.dragOver(getByTestId("kb-tree-scroll"), { dataTransfer });
    fireEvent.drop(getByTestId("kb-tree-scroll"), { dataTransfer });

    expect(props.onMoveNode).toHaveBeenCalledWith("docs/a.md", "");
  });

  // inv-17 [P1]：拖拽文件夹到自身 → src === targetDir 短路，不触发 onMoveNode
  // （修复：行内 onDrop/onDragOver 加 stopPropagation，阻断容器追加 handleDrop("")）
  it("用例28 拖拽文件夹到自身 → 不触发 onMoveNode", () => {
    const { getByTestId, props } = renderTree({ tree: [dir("docs", [])] });
    const dataTransfer = makeDataTransfer();

    fireEvent.dragStart(getByTestId("kb-node-docs"), { dataTransfer });
    fireEvent.dragOver(getByTestId("kb-node-docs"), { dataTransfer });
    fireEvent.drop(getByTestId("kb-node-docs"), { dataTransfer });

    expect(props.onMoveNode).not.toHaveBeenCalled();
  });

  // inv-18 [P1]：上传反馈按 uploaded / rejected 渲染
  it("用例29 上传反馈渲染 uploaded / rejected 文案", () => {
    const { getByText } = renderTree({
      tree: [],
      uploadFeedback: {
        uploaded: [{ name: "a.md", suffix: ".md" }],
        rejected: [{ name: "b.exe", reason: "不支持的类型" }],
      },
    });

    expect(getByText("✓ 已上传 a.md")).toBeTruthy();
    expect(getByText("⚠ 已跳过 b.exe（不支持的类型）")).toBeTruthy();
  });
});
