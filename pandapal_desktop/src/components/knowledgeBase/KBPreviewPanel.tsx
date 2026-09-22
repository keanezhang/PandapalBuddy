/**
 * src/components/knowledgeBase/KBPreviewPanel.tsx
 *
 * 知识库文档在线预览（设计 F16 / R5 · 纯前端读盘，无后端往返）。
 *
 * 关键：`node.path` 是**相对 documents_dir 的 POSIX 路径**，需 `join(documents_dir, path)`
 * 还原绝对路径后再读盘（P0 修正，见设计门控）。
 *
 * 分派：pdf / image 组件自读盘；文本类 readTextFile（≤1MB 闸门）；
 * docx / 其它二进制**显式降级**（避免 Monaco 文本兜底产生乱码）。
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { readTextFile } from "@tauri-apps/plugin-fs";
import { join } from "@tauri-apps/api/path";
import {
  MarkdownRenderer,
  LogRenderer,
  HtmlRenderer,
  TableRenderer,
  PdfRenderer,
  ImageRenderer,
  CodeRenderer,
  toMonacoLang,
} from "../fileRenderers";

/** 文本类读取大小闸门（设计 §10.3） */
export const MAX_TEXT_BYTES = 1024 * 1024;

const MD = new Set(["md", "markdown"]);
const LOG = new Set(["txt", "log"]);
const HTML = new Set(["html", "htm"]);
const TABLE = new Set(["csv", "tsv"]);
const IMAGE = new Set(["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico"]);
const TEXT = new Set([...MD, ...LOG, ...HTML, ...TABLE, "json"]);
const DEGRADED = new Set(["doc", "docx"]);

export interface KBPreviewDoc {
  path: string;
  name: string;
  suffix: string;
  size: number;
}

export interface KBPreviewPanelProps {
  doc: KBPreviewDoc | null;
  /** 库的文档目录绝对路径（随 KBConfig 下发） */
  documentsDir: string;
}

type Mode = "empty" | "pdf" | "image" | "text" | "degraded";

/** 由后缀派生预览模式（导出便于单测）。 */
export function previewMode(suffix: string): Mode {
  const ext = suffix.replace(/^\./, "").toLowerCase();
  if (!ext) return "degraded";
  if (ext === "pdf") return "pdf";
  if (IMAGE.has(ext)) return "image";
  if (TEXT.has(ext)) return "text";
  return "degraded";
}

/** 文本类后缀 → 渲染器标识（导出便于单测）。 */
export function textRendererKind(suffix: string): "md" | "log" | "html" | "table" | "code" {
  const ext = suffix.replace(/^\./, "").toLowerCase();
  if (MD.has(ext)) return "md";
  if (LOG.has(ext)) return "log";
  if (HTML.has(ext)) return "html";
  if (TABLE.has(ext)) return "table";
  return "code";
}

function formatSize(bytes: number): string {
  if (!bytes) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function KBPreviewPanel({ doc, documentsDir }: KBPreviewPanelProps) {
  const { t } = useTranslation();
  const [absPath, setAbsPath] = useState("");
  const [content, setContent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const mode: Mode = doc ? previewMode(doc.suffix) : "empty";
  const ext = doc ? doc.suffix.replace(/^\./, "").toLowerCase() : "";

  useEffect(() => {
    let cancelled = false;
    setAbsPath("");
    setContent(null);
    setError(null);
    setLoading(false);

    if (!doc) return;
    if (mode === "degraded") return; // 无需读盘

    if (!documentsDir) {
      setError(t("kb.preview.noDir", "未配置文档目录，无法预览"));
      return;
    }

    (async () => {
      try {
        const abs = await join(documentsDir, doc.path);
        if (cancelled) return;
        setAbsPath(abs);

        if (mode === "text") {
          if (doc.size > MAX_TEXT_BYTES) {
            setError(t("kb.preview.tooLarge", "文件过大，无法预览（超过 1MB）"));
            return;
          }
          setLoading(true);
          const txt = await readTextFile(abs);
          if (!cancelled) setContent(txt);
        }
      } catch (e) {
        if (!cancelled) setError(String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [doc?.path, documentsDir, mode]);

  // ── 空态 ──
  if (!doc) {
    return (
      <div style={emptyStyle}>
        <div style={{ fontSize: "var(--icon-empty)", opacity: 0.4 }}>📄</div>
        <div>从左侧文件树选择一个文档进行预览</div>
        <div style={{ color: "var(--text-tertiary)", fontSize: "var(--text-xs)" }}>
          Markdown / 文本支持预览；PDF / 图片走文件渲染器
        </div>
      </div>
    );
  }

  const header = (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 9,
        marginBottom: 16,
        paddingBottom: 12,
        borderBottom: "1px solid var(--border-subtle)",
        flexShrink: 0,
      }}
    >
      <span style={{ fontSize: "var(--text-md)", fontWeight: 600, color: "var(--text-primary)" }}>
        {doc.name}
      </span>
      <span style={{ marginLeft: "auto", color: "var(--text-tertiary)", fontSize: "var(--text-xs)" }}>
        {(ext || "file").toUpperCase()} · {formatSize(doc.size)}
      </span>
    </div>
  );

  // ── 降级态 ──
  if (mode === "degraded") {
    return (
      <div style={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0 }}>
        {header}
        <div style={emptyStyle}>
          <div style={{ fontSize: "var(--icon-empty)", opacity: 0.4 }}>📦</div>
          <div>暂不支持预览此格式，请在系统中打开</div>
          <div style={{ color: "var(--text-tertiary)", fontSize: "var(--text-xs)" }}>{doc.name}</div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0 }}>
        {header}
        <div style={{ ...emptyStyle, color: "var(--danger)" }}>{error}</div>
      </div>
    );
  }

  // ── PDF / 图片 ──
  if (mode === "pdf" || mode === "image") {
    return (
      <div style={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0 }}>
        {header}
        <div style={{ flex: 1, minHeight: 0, display: "flex" }}>
          {!absPath ? (
            <div style={emptyStyle}>{t("common.loading", "加载中")}...</div>
          ) : mode === "pdf" ? (
            <PdfRenderer path={absPath} />
          ) : (
            <ImageRenderer path={absPath} />
          )}
        </div>
      </div>
    );
  }

  // ── 文本类 ──
  const kind = textRendererKind(doc.suffix);
  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0 }}>
      {header}
      <div style={{ flex: 1, minHeight: 0, display: "flex", overflow: "hidden" }}>
        {loading && content === null ? (
          <div style={emptyStyle}>{t("common.loading", "加载中")}...</div>
        ) : content === null ? (
          <div style={emptyStyle}>—</div>
        ) : kind === "md" ? (
          <div style={{ flex: 1, overflow: "auto" }}>
            <MarkdownRenderer content={content} language={toMonacoLang(ext)} readOnly />
          </div>
        ) : kind === "log" ? (
          <div style={{ flex: 1, overflow: "auto" }}>
            <LogRenderer content={content} language={toMonacoLang(ext)} readOnly />
          </div>
        ) : kind === "html" ? (
          <div style={{ flex: 1, overflow: "auto" }}>
            <HtmlRenderer content={content} language={toMonacoLang(ext)} readOnly />
          </div>
        ) : kind === "table" ? (
          <div style={{ flex: 1, overflow: "auto" }}>
            <TableRenderer content={content} />
          </div>
        ) : (
          <CodeRenderer content={content} language={toMonacoLang(ext)} fileId={doc.path} readOnly />
        )}
      </div>
    </div>
  );
}

const emptyStyle: React.CSSProperties = {
  flex: 1,
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  justifyContent: "center",
  gap: 10,
  color: "var(--text-secondary)",
  textAlign: "center",
  padding: 40,
};

