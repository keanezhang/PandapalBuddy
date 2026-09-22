/**
 * KnowledgeBaseEditorPage.test.tsx — 编辑页双栏 + 菜单动作 → IPC（J 组用例 50-59）
 *
 * Mock 策略（设计 §2）：mock useBackend（14 个 KB 方法，缺一即 undefined 调用崩溃）
 * + @tauri-apps/plugin-dialog(open) + plugin-fs(readTextFile) + api/path(join)
 * + api/webviewWindow（防 KBDropZone 导入/挂载崩）+ ../fileRenderers（隔离 Monaco）。
 * kbStore / i18n / react-router-dom 用真实实例（MemoryRouter 直接进编辑模式路由）。
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, fireEvent, waitFor, act } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { KnowledgeBaseEditorPage } from "../../../pages/KnowledgeBaseEditorPage";
import { useKbStore } from "../../../store/kbStore";
import type { KBDocNode, KBConfig, KBDetail } from "../../../types/api";
import "../../../i18n";
import i18n from "i18next";

// ── 依赖 mock（vi.hoisted：避免 mock factory 闭包引用 TDZ）──
const { mockOpen, mockReadTextFile, mockJoin, backend } = vi.hoisted(() => {
  const fns = {
    requestKbDetail: vi.fn(),
    requestKbTree: vi.fn(),
    createKb: vi.fn(),
    saveKb: vi.fn(),
    uploadKbDocument: vi.fn(),
    deleteKbDocument: vi.fn(),
    buildKb: vi.fn(),
    cancelBuild: vi.fn(),
    searchKb: vi.fn(),
    createKbFolder: vi.fn(),
    renameKbFolder: vi.fn(),
    deleteKbFolder: vi.fn(),
    renameKbDocument: vi.fn(),
    moveKbDocument: vi.fn(),
  };
  return {
    mockOpen: vi.fn(),
    mockReadTextFile: vi.fn(),
    mockJoin: vi.fn((a: string, b: string) => `${a}/${b}`),
    backend: fns,
  };
});

vi.mock("../../../providers/BackendProvider", () => ({ useBackend: () => backend }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: mockOpen }));
vi.mock("@tauri-apps/plugin-fs", () => ({ readTextFile: mockReadTextFile }));
vi.mock("@tauri-apps/api/path", () => ({ join: mockJoin }));
vi.mock("@tauri-apps/api/webviewWindow", () => ({
  getCurrentWebviewWindow: () => ({ onDragDropEvent: () => Promise.resolve(() => {}) }),
}));
vi.mock("../../fileRenderers", () => ({
  MarkdownRenderer: ({ content }: { content: string }) => (
    <div data-testid="md-renderer">{content}</div>
  ),
  LogRenderer: ({ content }: { content: string }) => (
    <div data-testid="log-renderer">{content}</div>
  ),
  HtmlRenderer: ({ content }: { content: string }) => (
    <div data-testid="html-renderer">{content}</div>
  ),
  TableRenderer: ({ content }: { content: string }) => (
    <div data-testid="table-renderer">{content}</div>
  ),
  CodeRenderer: ({ content }: { content: string }) => (
    <div data-testid="code-renderer">{content}</div>
  ),
  PdfRenderer: ({ path }: { path: string }) => (
    <div data-testid="pdf-renderer" data-path={path} />
  ),
  ImageRenderer: ({ path }: { path: string }) => (
    <div data-testid="image-renderer" data-path={path} />
  ),
  toMonacoLang: (ext: string) => ext,
  fileIcon: (name: string) => name,
}));

const EMPTY_CONFIG: KBConfig = {
  name: "",
  description: "",
  documents_dir: "",
  embedding_api_key: "",
  embedding_model: "",
  embedding_api_type: "text",
  embedding_api_url: "",
  embedding_dimension: 1024,
  llm_provider: "",
  llm_api_key: "",
  llm_model: "",
  llm_api_url: "",
  enable_bm25: true,
  enabled_in_chat: true,
  auto_rebuild: false,
  top_k: 5,
};

function file(path: string, suffix = ".md", size = 100): KBDocNode {
  return {
    path,
    name: path.split("/").pop()!,
    is_dir: false,
    size,
    suffix,
    mtime: 0,
    children: null,
  };
}

function dir(path: string, children: KBDocNode[] = []): KBDocNode {
  return {
    path,
    name: path.split("/").pop()!,
    is_dir: true,
    size: 0,
    suffix: "",
    mtime: 0,
    children,
  };
}

function makeDetail(): KBDetail {
  return {
    name: "MyKB",
    description: "",
    status: "ready",
    document_count: 0,
    enabled_in_chat: false,
    auto_rebuild: false,
    top_k: 5,
    enable_bm25: true,
    config: { ...EMPTY_CONFIG, name: "MyKB", documents_dir: "/kb/MyKB" },
    documents: [],
  };
}

function seed(tree: KBDocNode[]) {
  useKbStore.setState({
    detail: makeDetail(),
    treeByName: { MyKB: tree },
    selectedPath: null,
    rightTab: "preview",
  });
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/knowledge/MyKB/edit"]}>
      <Routes>
        <Route path="/knowledge/:name/edit" element={<KnowledgeBaseEditorPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

/** 新建文件夹弹窗的输入框（值默认「新建文件夹」，与搜索框区分） */
function folderInput(): HTMLInputElement | null {
  return (
    Array.from(document.querySelectorAll<HTMLInputElement>("input")).find(
      (el) => el.value === "新建文件夹",
    ) ?? null
  );
}

async function openContextMenu(el: Element) {
  await act(async () => {
    fireEvent.contextMenu(el, { clientX: 10, clientY: 10 });
  });
}

beforeEach(async () => {
  vi.clearAllMocks();
  await i18n.changeLanguage("zh-CN");
  useKbStore.getState().reset();
  mockReadTextFile.mockResolvedValue("");
  mockOpen.mockResolvedValue(["/tmp/x.pdf"]);
});

describe("KnowledgeBaseEditorPage · 菜单动作 → IPC（inv-27）", () => {
  // inv-27 + R7 [P1]：删除文档 → 二次确认 → deleteKbDocument
  it("用例50 右键删除文档 → 二次确认后 deleteKbDocument(kb, path)", async () => {
    seed([file("a.md")]);
    const { getByTestId, getByText, getByRole } = renderPage();

    await openContextMenu(getByTestId("kb-node-a.md"));
    fireEvent.click(getByText("删除"));

    expect(getByRole("dialog")).toBeTruthy();

    fireEvent.click(getByRole("button", { name: "确认删除" }));

    expect(backend.deleteKbDocument).toHaveBeenCalledTimes(1);
    expect(backend.deleteKbDocument).toHaveBeenCalledWith("MyKB", "a.md");
    expect(backend.deleteKbFolder).not.toHaveBeenCalled();
  });

  // inv-27 + R7 [P1]：删除文件夹 → 文案含后代计数 + deleteKbFolder
  it("用例51 删除文件夹 → 确认文案含后代计数，确认后 deleteKbFolder(kb, path)", async () => {
    seed([dir("docs", [file("docs/a.md"), file("docs/b.md")])]);
    const { getByTestId, getByText, getByRole } = renderPage();

    await openContextMenu(getByTestId("kb-node-docs"));
    fireEvent.click(getByText("删除"));

    expect(getByText(/确定删除「docs」？其中含 2 个文档，/)).toBeTruthy();
    expect(getByText(/将从库内副本移除并触发索引更新。/)).toBeTruthy();

    fireEvent.click(getByRole("button", { name: "确认删除" }));

    expect(backend.deleteKbFolder).toHaveBeenCalledTimes(1);
    expect(backend.deleteKbFolder).toHaveBeenCalledWith("MyKB", "docs");
  });

  // inv-27 + R7 [P1]：取消删除 → 不调用 deleteKb*
  it("用例52 取消删除 → deleteKbDocument / deleteKbFolder 均未调用", async () => {
    seed([file("a.md")]);
    const { getByTestId, getByText, getByRole, queryByRole } = renderPage();

    await openContextMenu(getByTestId("kb-node-a.md"));
    fireEvent.click(getByText("删除"));
    fireEvent.click(getByRole("button", { name: "取消" }));

    expect(backend.deleteKbDocument).not.toHaveBeenCalled();
    expect(backend.deleteKbFolder).not.toHaveBeenCalled();
    expect(queryByRole("dialog")).toBeNull();
  });

  // inv-27 [P1]：点击文件行 → selectedPath + rightTab=preview + 预览非空态
  it("用例53 点击文件行 → selectedPath 写入、rightTab=preview、预览区进入非空态", () => {
    seed([file("a.md")]);
    const { getByTestId, queryByText } = renderPage();

    fireEvent.click(getByTestId("kb-node-a.md"));

    expect(useKbStore.getState().selectedPath).toBe("a.md");
    expect(useKbStore.getState().rightTab).toBe("preview");
    expect(queryByText("从左侧文件树选择一个文档进行预览")).toBeNull();
  });

  // inv-27 [P1]：新建文件夹 → 默认名 → Enter 提交 createKbFolder
  it("用例54 新建文件夹 → 默认名「新建文件夹」→ Enter 提交 createKbFolder(kb, '', name)", () => {
    seed([]);
    const { getByText } = renderPage();

    fireEvent.click(getByText("🗂 新建文件夹"));

    const input = folderInput();
    expect(input).not.toBeNull();
    expect(input!.value).toBe("新建文件夹");

    fireEvent.change(input!, { target: { value: "新目录" } });
    fireEvent.keyDown(input!, { key: "Enter" });

    expect(backend.createKbFolder).toHaveBeenCalledTimes(1);
    expect(backend.createKbFolder).toHaveBeenCalledWith("MyKB", "", "新目录");
  });

  // inv-27 + R5 邻接 [P1]：空名提交 → 不调用 createKbFolder
  it("用例55 新建文件夹空名提交 → 不调用 createKbFolder 且弹窗关闭", () => {
    seed([]);
    const { getByText, queryByText } = renderPage();

    fireEvent.click(getByText("🗂 新建文件夹"));
    expect(queryByText("新建文件夹")).toBeTruthy();

    const input = folderInput()!;
    fireEvent.change(input, { target: { value: "" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(backend.createKbFolder).not.toHaveBeenCalled();
    expect(queryByText("新建文件夹")).toBeNull();
  });

  // inv-27 + R3 [P0]：移到根目录 → moveKbDocument(kb, path, "")
  it('用例56 右键「移到根目录」→ moveKbDocument(kb, path, "")', async () => {
    seed([dir("docs", [file("docs/a.md")])]);
    const { getByTestId, getByText } = renderPage();

    fireEvent.click(getByTestId("kb-node-docs")); // 展开，露出 docs/a.md
    await openContextMenu(getByTestId("kb-node-docs/a.md"));
    fireEvent.click(getByText("移到根目录"));

    expect(backend.moveKbDocument).toHaveBeenCalledTimes(1);
    expect(backend.moveKbDocument).toHaveBeenCalledWith("MyKB", "docs/a.md", "");
  });

  // inv-27 + R1/R3/R8 [P0]：上传到此处 → targetDir 随 文件夹/文件 推导
  it("用例57【P0】右键「上传到此处」→ targetDir 正确推导（文件夹自身 / 文件父目录）", async () => {
    seed([dir("docs", [file("docs/a.md")])]);
    const { getByTestId, getByText } = renderPage();

    await openContextMenu(getByTestId("kb-node-docs"));
    fireEvent.click(getByText("上传到此处"));
    await waitFor(() =>
      expect(backend.uploadKbDocument).toHaveBeenCalledWith("MyKB", ["/tmp/x.pdf"], "docs"),
    );
    backend.uploadKbDocument.mockClear();

    fireEvent.click(getByTestId("kb-node-docs")); // 展开，露出 docs/a.md
    await openContextMenu(getByTestId("kb-node-docs/a.md"));
    fireEvent.click(getByText("上传到此处"));
    await waitFor(() =>
      expect(backend.uploadKbDocument).toHaveBeenCalledWith("MyKB", ["/tmp/x.pdf"], "docs"),
    );
  });

  // inv-28 [P1]：selectedPath 指向文件夹 → previewDoc === null（空态）
  it("用例58 selectedPath 指向文件夹 → 预览区为空态", () => {
    seed([dir("docs", [file("docs/a.md")])]);
    const { getByText } = renderPage();

    act(() => {
      useKbStore.getState().setSelectedPath("docs");
      useKbStore.getState().setRightTab("preview");
    });

    expect(getByText("从左侧文件树选择一个文档进行预览")).toBeTruthy();
  });

  // inv-27 [P1]：右键「重命名」→ renameRequest 注入 → 树进入内联重命名态
  it("用例59 右键「重命名」→ 树行进入内联重命名输入态", async () => {
    const nowSpy = vi.spyOn(Date, "now").mockReturnValue(1_700_000_000_000);
    try {
      seed([file("a.md")]);
      const { getByTestId, getByText } = renderPage();

      await openContextMenu(getByTestId("kb-node-a.md"));
      fireEvent.click(getByText("重命名"));

      await waitFor(() => expect(getByTestId("kb-rename-input-a.md")).toBeTruthy());
    } finally {
      nowSpy.mockRestore();
    }
  });
});
