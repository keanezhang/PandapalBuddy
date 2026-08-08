/**
 * src/components/fileRenderers/TableRenderer.tsx
 *
 * CSV / TSV 表格渲染器
 *   - 健壮解析：支持引号包裹、字段内换行、"" 转义（RFC 4180 风格）
 *   - 分隔符自动探测（逗号 / 制表符）
 *   - 虚拟滚动：仅渲染可视行，去掉此前的 500 行硬截断，可承载十万级行数
 */
import { useMemo, useRef, useState, useEffect } from "react";

interface TableRendererProps {
  content: string;
}

const ROW_H = 28;      // 单行高度（px），虚拟滚动依赖固定行高
const OVERSCAN = 12;   // 视口上下额外渲染的行数

/** 探测分隔符：比较首个非空行中的制表符与逗号数量 */
function detectDelimiter(text: string): string {
  const firstLine = text.split("\n").find((l) => l.trim().length > 0) ?? "";
  const tabs = (firstLine.match(/\t/g) || []).length;
  const commas = (firstLine.match(/,/g) || []).length;
  return tabs > commas ? "\t" : ",";
}

/** RFC 4180 风格解析：正确处理引号、字段内换行与 "" 转义 */
function parseDelimited(text: string, delimiter: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  const n = text.length;

  for (let i = 0; i < n; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"') {
        if (text[i + 1] === '"') { cell += '"'; i++; }   // "" → 字面量引号
        else quoted = false;
      } else {
        cell += ch;
      }
      continue;
    }
    if (ch === '"') { quoted = true; }
    else if (ch === delimiter) { row.push(cell); cell = ""; }
    else if (ch === "\r") { /* 忽略，等待 \n */ }
    else if (ch === "\n") { row.push(cell); rows.push(row); row = []; cell = ""; }
    else { cell += ch; }
  }
  // 收尾：处理最后一个未以换行结束的字段/行
  if (cell.length > 0 || row.length > 0) { row.push(cell); rows.push(row); }
  return rows;
}

export function TableRenderer({ content }: TableRendererProps) {
  const rows = useMemo(() => {
    if (!content.trim()) return [];
    try {
      return parseDelimited(content, detectDelimiter(content));
    } catch {
      return [];
    }
  }, [content]);

  const header = rows[0] ?? [];
  const body = useMemo(() => rows.slice(1), [rows]);
  const colCount = useMemo(
    () => rows.reduce((m, r) => Math.max(m, r.length), 0),
    [rows],
  );

  // ── 虚拟滚动状态 ─────────────────────────────────────────────────────
  const scrollRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportH, setViewportH] = useState(400);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    setViewportH(el.clientHeight);
    const ro = new ResizeObserver(() => setViewportH(el.clientHeight));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  if (!rows.length) {
    return (
      <div style={{
        flex: 1, display: "flex", alignItems: "center", justifyContent: "center",
        color: "var(--text-muted)", fontSize: "var(--text-base)",
      }}>
        无法解析表格
      </div>
    );
  }

  const total = body.length;
  const start = Math.max(0, Math.floor(scrollTop / ROW_H) - OVERSCAN);
  const end = Math.min(total, Math.ceil((scrollTop + viewportH) / ROW_H) + OVERSCAN);
  const visible = body.slice(start, end);

  const gridCols = `48px repeat(${colCount}, minmax(100px, 1fr))`;
  const cellStyle: React.CSSProperties = {
    padding: "0 12px", display: "flex", alignItems: "center",
    height: ROW_H, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
    borderBottom: "1px solid var(--border-subtle)", fontSize: 12,
  };

  return (
    <div
      ref={scrollRef}
      onScroll={(e) => setScrollTop((e.target as HTMLDivElement).scrollTop)}
      style={{ flex: 1, overflow: "auto" }}
    >
      {/* 表头（sticky） */}
      <div style={{
        display: "grid", gridTemplateColumns: gridCols,
        position: "sticky", top: 0, zIndex: 1, background: "var(--bg-panel)",
      }}>
        <div style={{ ...cellStyle, color: "var(--text-muted)", fontWeight: 600, borderBottom: "2px solid var(--border-default)" }}>#</div>
        {Array.from({ length: colCount }).map((_, i) => (
          <div key={i} style={{
            ...cellStyle, fontWeight: 600, color: "var(--text-primary)",
            borderBottom: "2px solid var(--border-default)",
          }}>
            {header[i] ?? ""}
          </div>
        ))}
      </div>

      {/* 虚拟滚动主体：上下用 padding 撑开总高度 */}
      <div style={{ paddingTop: start * ROW_H, paddingBottom: (total - end) * ROW_H }}>
        {visible.map((r, vi) => {
          const ri = start + vi;
          return (
            <div key={ri} style={{
              display: "grid", gridTemplateColumns: gridCols,
              background: ri % 2 === 0 ? "transparent" : "var(--bg-card-subtle)",
            }}>
              <div style={{ ...cellStyle, color: "var(--text-muted)", opacity: 0.6, userSelect: "none" }}>
                {ri + 1}
              </div>
              {Array.from({ length: colCount }).map((_, ci) => (
                <div key={ci} style={{ ...cellStyle, color: "var(--text-secondary)" }} title={r[ci] ?? ""}>
                  {r[ci] ?? ""}
                </div>
              ))}
            </div>
          );
        })}
      </div>
    </div>
  );
}
