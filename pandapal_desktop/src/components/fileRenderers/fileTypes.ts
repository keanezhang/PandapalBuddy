/**
 * src/components/fileRenderers/fileTypes.ts
 *
 * 文件类型映射的「单一真相源」。
 *
 * 此前扩展名相关的映射散落在三处（fileStore.VIEWER_MAP、FileViewerPanel 本地副本、
 * FileExplorer.fileIcon），且彼此不一致。现全部收敛到此文件：
 *
 *   - ViewerMode           查看器模式枚举
 *   - VIEWER_MAP           扩展名 → ViewerMode
 *   - resolveViewerMode()  扩展名 → ViewerMode（带兜底）
 *   - MONACO_LANG          扩展名 → Monaco 语言标识
 *   - toMonacoLang()       扩展名 → Monaco 语言标识（带兜底）
 *   - fileIcon()           文件名 → emoji 图标（文件树用）
 */

/** 查看器模式：决定 RENDERER_MAP 分发到哪个渲染组件 */
export type ViewerMode = "code" | "markdown" | "html" | "image" | "pdf" | "table" | "log" | "text";

/** 扩展名 → 查看器模式 */
export const VIEWER_MAP: Record<string, ViewerMode> = {
  ts: "code", tsx: "code", js: "code", jsx: "code",
  py: "code", go: "code", rs: "code", java: "code",
  c: "code", cpp: "code", h: "code", hpp: "code",
  json: "code", yaml: "code", yml: "code", xml: "code",
  css: "code", scss: "code", less: "code",
  sql: "code", sh: "code", dockerfile: "code", toml: "code",
  html: "html", htm: "html",
  md: "markdown", mdx: "markdown",
  png: "image", jpg: "image", jpeg: "image",
  gif: "image", webp: "image", svg: "image", bmp: "image", ico: "image",
  pdf: "pdf",
  csv: "table", tsv: "table",
  log: "log", txt: "text", env: "text", gitignore: "text",
};

/** 扩展名 → 查看器模式（未知类型兜底为 text） */
export function resolveViewerMode(ext: string): ViewerMode {
  return VIEWER_MAP[ext.toLowerCase()] || "text";
}

/** 扩展名 → Monaco 语言标识 */
export const MONACO_LANG: Record<string, string> = {
  ts: "typescript", tsx: "typescript", js: "javascript", jsx: "javascript",
  py: "python", go: "go", rs: "rust", java: "java",
  c: "c", cpp: "cpp", h: "c", hpp: "cpp",
  json: "json", yaml: "yaml", yml: "yaml", xml: "xml",
  html: "html", htm: "html", css: "css", scss: "scss", less: "less",
  sql: "sql", sh: "shell", dockerfile: "dockerfile", toml: "toml",
  md: "markdown", mdx: "markdown",
  log: "plaintext", txt: "plaintext", env: "shell", gitignore: "shell",
};

/** 扩展名 → Monaco 语言标识（未知类型兜底为 plaintext） */
export function toMonacoLang(ext: string): string {
  return MONACO_LANG[ext.toLowerCase()] || "plaintext";
}

/** 文件名 → emoji 图标（按扩展名粗分类，供文件树展示） */
export function fileIcon(name: string): string {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  if (["ts", "tsx", "js", "jsx", "py", "go", "rs", "java", "c", "cpp", "h", "hpp"].includes(ext)) return "📜";
  if (["json", "yaml", "yml", "toml", "xml", "env"].includes(ext)) return "⚙️";
  if (["html", "htm"].includes(ext)) return "🌐";
  if (["md", "mdx", "txt"].includes(ext)) return "📝";
  if (["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico"].includes(ext)) return "🖼️";
  if (ext === "pdf") return "📕";
  if (["csv", "tsv"].includes(ext)) return "📊";
  return "📄";
}
