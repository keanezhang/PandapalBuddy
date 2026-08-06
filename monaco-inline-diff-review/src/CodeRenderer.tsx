/**
 * CodeRenderer — 代码渲染器的智能包装器。
 *
 * 根据 readOnly + original 的存在性自动切换：
 *   - suggestion 模式 → InlineDiffEditor（inline diff + Apply/Reject）
 *   - edit 模式       → 纯 Monaco Editor + 未保存修改标记（overview ruler）
 */
import Editor from "@monaco-editor/react";
import { useRef, useEffect } from "react";
import { InlineDiffEditor } from "./editor/InlineDiffEditor";
import { computeDiff } from "./engine";
import type monaco from "monaco-editor";

/** CodeRenderer 组件属性 */
export interface CodeRendererProps {
  /** 当前内容（编辑模式）或 AI 建议内容（suggestion 模式） */
  content: string;
  /** Monaco 语言标识符 */
  language: string;
  /** 原始内容（存在时进入 suggestion 模式） */
  original?: string;
  /** 编辑回调（编辑模式） */
  onChange?: (value: string) => void;
  /** 是否只读 */
  readOnly?: boolean;
  /** 文件标识（用于 React key，防止跨文件状态残留） */
  fileId?: string;
  /** 所有 hunk 处理完后回调 */
  onAllResolved?: (savedContent: string) => void;
  /** 逐项 Apply/Reject 后回调 */
  onPartialSave?: (content: string, hunkKey: string) => void;
  /** 已 Apply 的 hunk contentKey 列表 */
  initialAppliedKeys?: string[];
}

export function CodeRenderer({
  content,
  language,
  original,
  onChange,
  readOnly,
  fileId,
  onAllResolved,
  onPartialSave,
  initialAppliedKeys,
}: CodeRendererProps) {
  const wrapperStyle = {
    flex: 1,
    overflow: "hidden",
    display: "flex",
  } as const;

  const mode: "suggestion" | "edit" =
    readOnly && original != null ? "suggestion" : "edit";

  // 只在模式切换时输出日志（避免每次键入都刷屏）
  const prevModeRef = useRef<"suggestion" | "edit" | null>(null);
  useEffect(() => {
    if (prevModeRef.current !== mode) {
      prevModeRef.current = mode;
      console.debug("[code-r] →", mode, fileId);
    }
  }, [mode, fileId]);

  // ── 编辑模式：未保存修改标记 ──────────────────────────────────────
  const editorRef = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const decoColRef = useRef<monaco.editor.IEditorDecorationsCollection | null>(null);
  const monacoRef = useRef<typeof monaco | null>(null);
  const originalRef = useRef(content);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // fileId 变化 → 重置快照 + 清除标记
  useEffect(() => {
    originalRef.current = content;
    decoColRef.current?.clear();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fileId]);

  // content 变化 → 300ms 防抖 → diff → decorations
  useEffect(() => {
    if (mode !== "edit") return;
    const editor = editorRef.current;
    const decoCol = decoColRef.current;
    const m = monacoRef.current;
    if (!editor || !decoCol || !m) return;

    if (debounceRef.current) clearTimeout(debounceRef.current);

    debounceRef.current = setTimeout(() => {
      const original = originalRef.current;
      if (content === original) {
        decoCol.clear();
        return;
      }

      const model = editor.getModel();
      if (!model) return;
      const totalLines = model.getLineCount();
      // 大文件跳过（>3000 行，LCS O(n*m) 会卡）
      if (totalLines > 3000) return;

      const entries = computeDiff(original, content);
      const decos: monaco.editor.IModelDeltaDecoration[] = [];
      let lineNum = 1;
      let i = 0;

      while (i < entries.length) {
        const entry = entries[i];
        if (entry.kind === "ctx") {
          lineNum++;
          i++;
        } else if (entry.kind === "add") {
          const prevIsDel = i > 0 && entries[i - 1].kind === "del";
          if (lineNum <= totalLines) {
            decos.push({
              range: new m.Range(lineNum, 1, lineNum, 1),
              options: {
                isWholeLine: true,
                className: prevIsDel ? "mid-modify-line" : "mid-add-line",
                glyphMarginClassName: prevIsDel ? "mid-modify-gutter" : "mid-add-gutter",
                overviewRuler: {
                  color: prevIsDel ? "rgba(234,179,8,0.7)" : "rgba(34,197,94,0.7)",
                  position: m.editor.OverviewRulerLane.Right,
                },
              },
            });
          }
          lineNum++;
          i++;
        } else {
          // del: 若下一项是 add → modify 情况，跳过（由 add 黄色标记覆盖）
          const nextIsAdd = i + 1 < entries.length && entries[i + 1].kind === "add";
          if (!nextIsAdd) {
            if (lineNum <= totalLines) {
              decos.push({
                range: new m.Range(lineNum, 1, lineNum, 1),
                options: {
                  isWholeLine: true,
                  glyphMarginClassName: "mid-del-gutter",
                  overviewRuler: {
                    color: "rgba(239,68,68,0.7)",
                    position: m.editor.OverviewRulerLane.Right,
                  },
                },
              });
            } else if (totalLines > 0) {
              decos.push({
                range: new m.Range(totalLines, 1, totalLines, 1),
                options: {
                  isWholeLine: true,
                  glyphMarginClassName: "mid-del-gutter",
                  overviewRuler: {
                    color: "rgba(239,68,68,0.7)",
                    position: m.editor.OverviewRulerLane.Right,
                  },
                },
              });
            }
          }
          i++;
        }
      }

      decoCol.set(decos);
    }, 300);

    return () => {
      if (debounceRef.current) {
        clearTimeout(debounceRef.current);
      }
    };
  }, [content, mode]);

  // suggestion 模式：委托给 InlineDiffEditor
  if (mode === "suggestion") {
    return (
      <div style={wrapperStyle}>
        <InlineDiffEditor
          key={fileId}
          original={original!}
          current={content}
          language={language}
          onAllResolved={onAllResolved}
          onPartialSave={onPartialSave}
          initialAppliedKeys={initialAppliedKeys}
        />
      </div>
    );
  }

  // 编辑模式：纯 Monaco Editor
  return (
    <div style={wrapperStyle}>
      <Editor
        height="100%"
        value={content}
        language={language}
        theme="vs-dark"
        onChange={(v) => onChange?.(v ?? "")}
        onMount={(editor, monaco) => {
          editorRef.current = editor;
          monacoRef.current = monaco;
          decoColRef.current = editor.createDecorationsCollection();
        }}
        options={{
          readOnly,
          minimap: { enabled: true, scale: 2, showSlider: "mouseover" },
          lineNumbers: "on",
          fontSize: 13,
          fontFamily:
            "'SF Mono','Cascadia Code','Fira Code',monospace",
          scrollBeyondLastLine: false,
          tabSize: 4,
          renderWhitespace: "selection",
          padding: { top: 8 },
          automaticLayout: true,
          glyphMargin: true,
          folding: true,
          matchBrackets: "always",
          bracketPairColorization: { enabled: true },
          guides: { bracketPairs: true, indentation: true },
          renderLineHighlight: "all",
          cursorBlinking: "smooth",
          smoothScrolling: true,
          wordWrap: "on",
          quickSuggestions: true,
          suggestOnTriggerCharacters: true,
          parameterHints: { enabled: true },
          hover: { enabled: true },
          links: true,
          contextmenu: true,
        }}
        loading={
          <div
            style={{
              flex: 1,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: "var(--text-muted, #888)",
              fontSize: 13,
            }}
          >
            加载编辑器...
          </div>
        }
      />
    </div>
  );
}
