/**
 * src/components/fileRenderers/index.ts
 *
 * 文件渲染器注册中心 — 根据文件扩展名分发到对应的渲染组件
 *
 * 使用方式：
 *   const mode = resolveViewerMode(extension);   // 来自 ./fileTypes
 *   const Renderer = RENDERER_MAP[mode];
 *   <Renderer {...props} />
 */
export type { ViewerMode } from "./fileTypes";
export { resolveViewerMode, toMonacoLang, fileIcon, VIEWER_MAP, MONACO_LANG } from "./fileTypes";

export { CodeRenderer } from "monaco-inline-diff-review";
export { MarkdownRenderer } from "./MarkdownRenderer";
export { HtmlRenderer } from "./HtmlRenderer";
export { LogRenderer } from "./LogRenderer";
export { ImageRenderer } from "./ImageRenderer";
export { PdfRenderer } from "./PdfRenderer";
export { TableRenderer } from "./TableRenderer";
export { ErrorRenderer } from "./ErrorRenderer";

import type { FC } from "react";
import type { ViewerMode } from "./fileTypes";
import { CodeRenderer } from "monaco-inline-diff-review";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { HtmlRenderer } from "./HtmlRenderer";
import { LogRenderer } from "./LogRenderer";
import { ImageRenderer } from "./ImageRenderer";
import { PdfRenderer } from "./PdfRenderer";
import { TableRenderer } from "./TableRenderer";
import { ErrorRenderer } from "./ErrorRenderer";

/** 每种查看器模式对应的渲染组件 */
export const RENDERER_MAP: Record<ViewerMode, FC<any>> = {
  code: CodeRenderer,
  markdown: MarkdownRenderer,
  html: HtmlRenderer,
  image: ImageRenderer,
  pdf: PdfRenderer,
  table: TableRenderer,
  log: LogRenderer,
  text: CodeRenderer,        // 纯文本复用 Monaco（只读/可编辑均可）
};

/** 错误渲染器 */
export { ErrorRenderer as Error };
