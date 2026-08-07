import type monaco from "monaco-editor";
import type { Hunk } from "../engine/types";

/* ── HTML 转义 ────────────────────────────────────────────────────────── */

export const escHtml = (s: string) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

/* ── 回调类型 ─────────────────────────────────────────────────────────── */

export interface ZoneCallbacks {
  onApply: (hunkId: string, hunkKey: string) => void;
  onReject: (hunk: Hunk) => void;
}

/** hunk 类型别名（用于 buildAddOverlays 区分 add / modify 样式） */
export type AddHunkType = "add" | "modify";

/* ── 构建删除行 ViewZone DOM（不含按钮）───────────────────────────────── */

export function buildDelZone(hunk: Hunk): HTMLDivElement {
  const dom = document.createElement("div");
  Object.assign(dom.style, {
    boxSizing: "border-box",
    padding: "0 8px",
    background: "rgba(239,68,68,0.08)",
    fontFamily: "'SF Mono','Cascadia Code','Fira Code',monospace",
    fontSize: "13px",
    lineHeight: "19px",
    position: "relative",
    zIndex: "1",
  });
  dom.innerHTML = hunk.delLines
    .map(
      (l) =>
        `<div class="mid-del-line" style="display:flex;align-items:center;white-space:pre;text-decoration:line-through;color:rgba(239,68,68,0.72);line-height:19px;overflow:hidden;text-overflow:ellipsis;"><span class="mid-del-marker"></span><span>${escHtml(l)}</span></div>`,
    )
    .join("");
  return dom;
}

/* ── 构建按钮 ViewZone DOM ────────────────────────────────────────────── */

export function buildBtnZone(
  hunk: Hunk,
  monaco: typeof import("monaco-editor"),
  callbacks: ZoneCallbacks,
): HTMLDivElement {
  const dom = document.createElement("div");
  // override Monaco .view-zones aria-hidden="true" to keep buttons focusable
  dom.setAttribute("aria-hidden", "false");
  Object.assign(dom.style, {
    boxSizing: "border-box",
    position: "relative",
    zIndex: "1",
  });
  dom.style.setProperty("pointer-events", "auto", "important");

  dom.innerHTML = `<div class="mid-zone-btn-bar">
    <button class="mid-btn mid-btn-apply">Apply</button>
    <button class="mid-btn mid-btn-reject">Reject</button>
  </div>`;

  dom.querySelector(".mid-btn-apply")?.addEventListener("click", (e) => {
    e.stopPropagation();
    callbacks.onApply(hunk.id, hunk.contentKey);
  });

  dom.querySelector(".mid-btn-reject")?.addEventListener("click", (e) => {
    e.stopPropagation();
    callbacks.onReject(hunk);
  });

  return dom;
}

/* ── 构建新增行绿色底 decorations ────────────────────────────────────── */

export function buildAddOverlays(
  hunk: Hunk,
  afterLine: number,
  editor: monaco.editor.IStandaloneCodeEditor,
  monaco: typeof import("monaco-editor"),
  addDecos: monaco.editor.IModelDeltaDecoration[],
  hunkType: AddHunkType = "add",
): void {
  const firstLn = afterLine + 1;
  const lineCount = editor.getModel()?.getLineCount() ?? 0;

  const isModify = hunkType === "modify";
  const lineClass = isModify ? "mid-modify-line" : "mid-add-line";
  const gutterClass = isModify ? "mid-modify-gutter" : "mid-add-gutter";

  for (let r = 0; r < hunk.addLines.length; r++) {
    const ln = firstLn + r;
    if (ln > lineCount) break;
    addDecos.push({
      range: new monaco.Range(ln, 1, ln, 1),
      options: {
        isWholeLine: true,
        className: lineClass,
        glyphMarginClassName: gutterClass,
        overviewRuler: {
          color: isModify ? "rgba(234,179,8,0.7)" : "rgba(34,197,94,0.7)",
          position: monaco.editor.OverviewRulerLane.Right,
        },
      },
    });
  }
}
