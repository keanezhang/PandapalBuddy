/**
 * KBPreviewPanel.test.tsx — 预览分派（B 组用例 9-10）+ 预览组件（C 组用例 11-18）
 *
 * Mock 策略（设计 §2）：mock @tauri-apps/plugin-fs(readTextFile)、@tauri-apps/api/path(join)
 * 与 ../fileRenderers（轻量 stub，避免 Monaco 重依赖）；i18n 用真实实例；文案断言用源码
 * 硬编码 golden 字面量（zh-CN 无 kb.preview.* key，走 t() fallback）。
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";
import {
  KBPreviewPanel,
  previewMode,
  textRendererKind,
  MAX_TEXT_BYTES,
} from "../KBPreviewPanel";
import "../../../i18n";

// ── 依赖 mock（vi.hoisted：避免 mock factory 闭包引用 TDZ）──
const { mockReadTextFile, mockJoin } = vi.hoisted(() => ({
  mockReadTextFile: vi.fn(),
  mockJoin: vi.fn((a: string, b: string) => `${a}/${b}`),
}));

vi.mock("@tauri-apps/plugin-fs", () => ({ readTextFile: mockReadTextFile }));
vi.mock("@tauri-apps/api/path", () => ({ join: mockJoin }));

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

beforeEach(() => {
  mockReadTextFile.mockReset();
  mockJoin.mockClear();
});

describe("previewMode / textRendererKind（unit，零 mock 行为）", () => {
  // inv-7 + R2 [P0]：docx 绝不落 text
  it("用例9 previewMode 后缀 → 预览模式全量分派", () => {
    expect(previewMode("pdf")).toBe("pdf");
    expect(previewMode(".PDF")).toBe("pdf");

    for (const e of ["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico"]) {
      expect(previewMode(e)).toBe("image");
    }

    for (const e of ["md", "markdown", "txt", "log", "html", "htm", "csv", "tsv", "json"]) {
      expect(previewMode(e)).toBe("text");
    }
    expect(previewMode("Md")).toBe("text");

    for (const e of ["docx", "doc", "xls", "zip", "bin", "unknown", ""]) {
      expect(previewMode(e)).toBe("degraded");
    }
    expect(previewMode(".DocX")).toBe("degraded");
  });

  // inv-8 文本子类 → 渲染器标识
  it("用例10 textRendererKind 文本子类 → 渲染器标识", () => {
    expect(textRendererKind("md")).toBe("md");
    expect(textRendererKind("markdown")).toBe("md");
    expect(textRendererKind("txt")).toBe("log");
    expect(textRendererKind("log")).toBe("log");
    expect(textRendererKind("html")).toBe("html");
    expect(textRendererKind("htm")).toBe("html");
    expect(textRendererKind("csv")).toBe("table");
    expect(textRendererKind("tsv")).toBe("table");
    for (const e of ["json", "xml", "py", "yaml", ""]) {
      expect(textRendererKind(e)).toBe("code");
    }
  });
});

describe("KBPreviewPanel（component）", () => {
  // inv-9 + R12 [P2]
  it("用例11 doc=null → 空态，且不 join / 不读盘", () => {
    const { getByText } = render(<KBPreviewPanel doc={null} documentsDir="/kb/demo" />);

    expect(getByText("从左侧文件树选择一个文档进行预览")).toBeTruthy();
    expect(mockJoin).not.toHaveBeenCalled();
    expect(mockReadTextFile).not.toHaveBeenCalled();
  });

  // inv-11 + R1 [P0]：join 入参必须是「相对 documents_dir 的 path」
  it("用例12【P0】文本类 ≤1MB → join(documentsDir, 相对path) 后 readTextFile(absPath)", async () => {
    mockReadTextFile.mockResolvedValue("# 标题");
    const doc = { path: "docs/sub/a.md", name: "a.md", suffix: ".md", size: 5000 };

    const { getByTestId } = render(<KBPreviewPanel doc={doc} documentsDir="/kb/d1" />);

    await waitFor(() => expect(getByTestId("md-renderer")).toBeTruthy());
    expect(mockJoin).toHaveBeenCalledWith("/kb/d1", "docs/sub/a.md");
    expect(mockReadTextFile).toHaveBeenCalledWith("/kb/d1/docs/sub/a.md");
    expect(getByTestId("md-renderer").textContent).toBe("# 标题");
  });

  // inv-11 + R12 [P2]：>1MB 闸门边界对
  it("用例13 1MB 闸门边界：=MAX 读盘；>MAX 只提示过大、不读盘", async () => {
    const base = { path: "a.txt", name: "a.txt", suffix: ".txt" };

    mockReadTextFile.mockResolvedValue("ok");
    const r1 = render(
      <KBPreviewPanel doc={{ ...base, size: MAX_TEXT_BYTES }} documentsDir="/kb/d1" />,
    );
    await waitFor(() => expect(r1.getByTestId("log-renderer")).toBeTruthy());
    expect(mockReadTextFile).toHaveBeenCalledTimes(1);
    r1.unmount();

    mockReadTextFile.mockClear();
    const r2 = render(
      <KBPreviewPanel doc={{ ...base, size: MAX_TEXT_BYTES + 1 }} documentsDir="/kb/d1" />,
    );
    await waitFor(() => expect(r2.getByText("文件过大，无法预览（超过 1MB）")).toBeTruthy());
    expect(mockReadTextFile).not.toHaveBeenCalled();
  });

  // inv-12 未配置目录 → 早退，不 join / 不读盘
  it("用例14 documentsDir 为空 → 提示未配置，不 join 不读盘", () => {
    const doc = { path: "a.md", name: "a.md", suffix: ".md", size: 10 };

    const { getByText } = render(<KBPreviewPanel doc={doc} documentsDir="" />);

    expect(getByText("未配置文档目录，无法预览")).toBeTruthy();
    expect(mockJoin).not.toHaveBeenCalled();
    expect(mockReadTextFile).not.toHaveBeenCalled();
  });

  // inv-10 + R2 [P0]：docx 显式降级，绝不进入文本兜底
  it("用例15【P0】degraded(docx) → 降级文案，不 join 不读盘不渲染文本渲染器", () => {
    const doc = { path: "契约.docx", name: "契约.docx", suffix: ".docx", size: 8000 };

    const { getByText, getAllByText, queryByTestId } = render(
      <KBPreviewPanel doc={doc} documentsDir="/kb/d1" />,
    );

    expect(getByText("暂不支持预览此格式，请在系统中打开")).toBeTruthy();
    expect(getAllByText("契约.docx").length).toBeGreaterThan(0);
    expect(mockJoin).not.toHaveBeenCalled();
    expect(mockReadTextFile).not.toHaveBeenCalled();
    expect(queryByTestId("code-renderer")).toBeNull();
    expect(queryByTestId("md-renderer")).toBeNull();
  });

  // R1 [P0]：媒体类 join 还原绝对路径并透传给渲染器
  it("用例16【P0】pdf / image → join 还原绝对路径后透传渲染器，不读盘", async () => {
    const r1 = render(
      <KBPreviewPanel
        doc={{ path: "a.pdf", name: "a.pdf", suffix: ".pdf", size: 100 }}
        documentsDir="/kb/d1"
      />,
    );
    await waitFor(() => expect(r1.getByTestId("pdf-renderer")).toBeTruthy());
    expect(mockJoin).toHaveBeenCalledWith("/kb/d1", "a.pdf");
    expect(r1.getByTestId("pdf-renderer").getAttribute("data-path")).toBe("/kb/d1/a.pdf");
    expect(mockReadTextFile).not.toHaveBeenCalled();
    r1.unmount();

    const r2 = render(
      <KBPreviewPanel
        doc={{ path: "a.png", name: "a.png", suffix: ".png", size: 100 }}
        documentsDir="/kb/d1"
      />,
    );
    await waitFor(() => expect(r2.getByTestId("image-renderer")).toBeTruthy());
    expect(r2.getByTestId("image-renderer").getAttribute("data-path")).toBe("/kb/d1/a.png");
    expect(mockReadTextFile).not.toHaveBeenCalled();
  });

  // inv-13 + R11 [P1]【故障注入】readTextFile reject → 展示错误，组件不崩溃
  it("用例17【故障注入】读盘 reject → 展示错误文案，组件不抛异常", async () => {
    mockReadTextFile.mockRejectedValue(new Error("EACCES"));
    const doc = { path: "a.txt", name: "a.txt", suffix: ".txt", size: 100 };

    const { getByText } = render(<KBPreviewPanel doc={doc} documentsDir="/kb/d1" />);

    await waitFor(() => expect(getByText(/EACCES/)).toBeTruthy());
  });

  // inv-11（effect 依赖 doc?.path）：切换文档重新读盘并清空旧内容
  it("用例18 切换 doc → 以新 path 重新读盘，旧内容被清空", async () => {
    mockReadTextFile.mockResolvedValueOnce("A").mockResolvedValueOnce("B");
    const docA = { path: "a.md", name: "a.md", suffix: ".md", size: 10 };
    const docB = { path: "b.md", name: "b.md", suffix: ".md", size: 20 };

    const { getByTestId, rerender } = render(
      <KBPreviewPanel doc={docA} documentsDir="/kb/d1" />,
    );
    await waitFor(() => expect(getByTestId("md-renderer").textContent).toBe("A"));

    rerender(<KBPreviewPanel doc={docB} documentsDir="/kb/d1" />);
    await waitFor(() => expect(getByTestId("md-renderer").textContent).toBe("B"));
    expect(mockReadTextFile).toHaveBeenNthCalledWith(2, "/kb/d1/b.md");
  });
});
