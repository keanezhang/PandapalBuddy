/**
 * src/components/FileViewerPanel.tsx
 *
 * 文件查看/编辑面板
 *
 * 布局：TabBar → [建议栏]/[文件信息栏] → 内容渲染器 → [底栏按钮]
 *
 * 两种模式：
 *   1. 手动编辑 — 可编辑的 Monaco Editor
 *   2. AI 建议 — readOnly InlineDiffEditor，对比 original vs suggested
 */
import React, { useState, useEffect, useRef, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { useFileStore } from "../store/fileStore";
import { RENDERER_MAP, ErrorRenderer, resolveViewerMode, toMonacoLang } from "./fileRenderers";

/* ── 扩展名 → ViewerMode / Monaco 语言：见 ./fileRenderers/fileTypes ─────── */

const Labels: Record<string, string> = {
  code: "CODE", markdown: "DOC", html: "HTML", image: "IMAGE",
  pdf: "PDF", table: "DATA", log: "LOG", text: "TEXT",
};

/* ── FileViewerPanel ─────────────────────────────────────────────────── */

export function FileViewerPanel({ width }: { width: string }) {
  const { t } = useTranslation();
  const {
    openFiles, activeFileId, fileContents, closeFile, switchActiveFile,
    saveCurrentFile, pickAndSaveAs,
    suggestions, clearSuggestion, updateSuggestion, markHunkApplied,
    acceptSuggestion, rejectSuggestion,
    loadAndOpenFile,
  } = useFileStore();

  const [dirty, setDirty] = useState(false);
  const [cur, setCur] = useState("");
  const [orig, setOrig] = useState("");
  const rCur = useRef("");
  const origRef_ = useRef("");
  const prevFileRef = useRef<string | null>(null);
  const currentPathRef = useRef<string | null>(null);

  /* ── 建议到达时自动打开对应文件 ─────────────────────────────────── */
  const [suggestionAutoOpened, setSuggestionAutoOpened] = useState<string | null>(null);
  /** 去抖：记录每个 path 最近一次 auto-open 的时间戳，防止短时间内反复打开同一文件 */
  const lastAutoOpenRef = useRef<Record<string, number>>({});
  const AUTO_OPEN_DEBOUNCE_MS = 2000;
  useEffect(() => {
    const pendingPaths = Object.keys(suggestions).filter(
      p => !openFiles.some(f => f.path === p),
    );
    if (pendingPaths.length === 0) {
      setSuggestionAutoOpened(null);
      return;
    }
    const next = pendingPaths[0];
    if (suggestionAutoOpened !== next) {
      // ★ 去抖：同一文件在 2 秒内不重复 open，阻断因 MAX_TABS 挤出 + suggestion 未清导致的循环
      const now = Date.now();
      const last = lastAutoOpenRef.current[next] ?? 0;
      if (now - last < AUTO_OPEN_DEBOUNCE_MS) {
        console.debug("[file-v] auto-open: debounced", { path: next, elapsed: now - last });
        // 跳过当前 pending，让 suggestionAutoOpened 不更新，等 effect 以 openFiles/suggestions 自然驱动
        return;
      }
      console.debug("[file-v] auto-open", { path: next, pending: pendingPaths.length });
      lastAutoOpenRef.current[next] = now;
      setSuggestionAutoOpened(next);
      loadAndOpenFile(next);
    }
  }, [suggestions, openFiles, suggestionAutoOpened, loadAndOpenFile]);

  const af = openFiles.find(f => f.id === activeFileId) ?? openFiles[0] ?? null;
  const txt = af ? (fileContents[af.path] ?? "") : "";
  currentPathRef.current = af?.path ?? null;

  /* ── 文件切换 / 内容变更 ─────────────────────────────────────────── */
  useEffect(() => {
    const fileChanged = activeFileId !== prevFileRef.current;
    console.debug("[file-v] file-change", { activeFileId, prev: prevFileRef.current, switched: fileChanged, txtLen: txt.length, hasSuggestion: !!(suggestions[activeFileId ?? ""]) });
    prevFileRef.current = activeFileId;
    if (fileChanged) {
      setCur(txt);
      setOrig(txt);
      origRef_.current = txt;
      rCur.current = txt;
      setDirty(false);
    } else {
      setCur(txt);
      rCur.current = txt;
      setDirty(txt !== origRef_.current);
    }
  }, [activeFileId, txt]);

  /* ── 编辑回调 ─────────────────────────────────────────────────────── */
  const onChg = useCallback((v: string) => {
    if (v === rCur.current) return; // 避免 Monaco 受控模式下的循环 re-render
    rCur.current = v;
    setCur(v);
    setDirty(v !== origRef_.current);
  }, []);

  /* ── 保存 ──────────────────────────────────────────────────────────── */
  const onSave = useCallback(async () => {
    if (!dirty || !af) return;
    await saveCurrentFile(rCur.current);
    setOrig(rCur.current);
    origRef_.current = rCur.current;
    setDirty(false);
  }, [dirty, af, saveCurrentFile]);

  const onSaveAs = useCallback(async () => {
    await pickAndSaveAs(rCur.current);
  }, [pickAndSaveAs]);

  /* ── 键盘快捷键 ────────────────────────────────────────────────────── */
  useEffect(() => {
    if (openFiles.length === 0) return;
    const h = (e: KeyboardEvent) => {
      const m = e.metaKey || e.ctrlKey;
      if (m && e.key === "s" && e.shiftKey) { e.preventDefault(); onSaveAs(); return; }
      if (m && e.key === "s") { e.preventDefault(); onSave(); return; }
      if (m && e.key === "w") { e.preventDefault(); if (activeFileId) closeFile(activeFileId); return; }
      if (e.ctrlKey && e.key === "Tab") {
        e.preventDefault();
        const i = openFiles.findIndex(f => f.id === activeFileId);
        const n = e.shiftKey
          ? (i - 1 + openFiles.length) % openFiles.length
          : (i + 1) % openFiles.length;
        switchActiveFile(openFiles[n].id);
      }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [openFiles, activeFileId, closeFile, switchActiveFile, onSave, onSaveAs]);

  // 单个 hunk Apply/Reject 后即时落盘 + 同步状态（防止切文件后丢失）
  const handlePartialSave = useCallback((content: string, hunkKey: string) => {
    const p = currentPathRef.current;
    if (p) {
      console.debug("[file-v] partial-save", p, content.length, hunkKey ? "(apply)" : "(reject)");
      saveCurrentFile(content);
      updateSuggestion(p, content);
      // Apply 需持久化 hunk contentKey，切文件回来后 appliIdsRef 不会丢失
      if (hunkKey) markHunkApplied(p, hunkKey);
    }
  }, [saveCurrentFile, updateSuggestion, markHunkApplied]);

  // 所有 hunk 被逐项 Apply/Reject 后自动退出建议模式
  // 必须在所有 early return 之前定义（React hooks 规则）
  const handleAllResolved = useCallback((savedContent: string) => {
    const p = currentPathRef.current;
    if (p) {
      console.debug("[file-v] onAllResolved → done", p, savedContent.length);
      // 兜底写盘（覆盖无 hunk / 提前同步等边界情况；已有逐项落盘时幂等）
      saveCurrentFile(savedContent);
      clearSuggestion(p);
      setCur(savedContent);
      setOrig(savedContent);
      origRef_.current = savedContent;
      rCur.current = savedContent;
      setDirty(false);
    }
  }, [clearSuggestion, saveCurrentFile]);

  // 模式切换日志（提前计算 isSuggestion，放在 early return 之前）
  const isSuggestion = af ? (suggestions[af.path] != null) : false;
  useEffect(() => {
    if (isSuggestion) {
      const s = af ? suggestions[af.path] : null;
      if (s) console.debug("[file-v] suggestion-mode", { path: af.path, o: s.original.length, s: s.suggested.length });
    }
  }, [isSuggestion]);

  const ps: React.CSSProperties = {
    width, minWidth: 0, display: "flex", flexDirection: "column",
    background: "var(--bg-root)", borderLeft: "1px solid var(--border-subtle)",
    overflow: "hidden",
  };

  /* ── Tab 栏 ────────────────────────────────────────────────────────── */
  const TabBar = openFiles.length > 0 ? (
    <div style={{
      display: "flex", overflowX: "auto", background: "var(--bg-panel)",
      borderBottom: "1px solid var(--border-subtle)", flexShrink: 0, minHeight: 32,
    }}>
      {openFiles.map(f => {
        const a = f.id === activeFileId;
        return (
          <div
            key={f.id}
            onClick={() => switchActiveFile(f.id)}
            style={{
              display: "flex", alignItems: "center", gap: 4, padding: "4px 10px",
              fontSize: "var(--text-xs)", cursor: "pointer",
              color: a ? "var(--text-primary)" : "var(--text-tertiary)",
              background: a ? "var(--bg-root)" : "transparent",
              borderRight: "1px solid var(--border-subtle)",
              whiteSpace: "nowrap", userSelect: "none", flexShrink: 0,
            }}
          >
            <span style={{ fontSize: "var(--text-sm)" }}>📄</span>
            <span style={{ maxWidth: 120, overflow: "hidden", textOverflow: "ellipsis" }}>
              {f.name}
            </span>
            <span
              onClick={e => { e.stopPropagation(); closeFile(f.id); }}
              style={{
                marginLeft: 4, fontSize: "var(--text-sm)", color: "var(--text-tertiary)",
                cursor: "pointer", padding: "0 2px",
              }}
              title="Ctrl+W"
            >✕</span>
          </div>
        );
      })}
    </div>
  ) : null;

  /* ── 空状态 / 错误状态 ────────────────────────────────────────────── */
  if (openFiles.length === 0 || !af) {
    return (
      <div style={ps}>
        <div style={{
          flex: 1, display: "flex", flexDirection: "column",
          alignItems: "center", justifyContent: "center", gap: 12,
          color: "var(--text-tertiary)", padding: 40,
        }}>
          <div style={{ fontSize: "var(--icon-empty)", opacity: 0.3 }}>📄</div>
          <div style={{ fontSize: "var(--text-base)" }}>{t("fileViewer.empty")}</div>
        </div>
      </div>
    );
  }

  const ext = af.extension;
  const md = resolveViewerMode(ext);
  const Renderer = RENDERER_MAP[md] ?? RENDERER_MAP.text;

  if (txt.startsWith("__ERROR__:")) {
    return (
      <div style={ps}>
        {TabBar}
        <ErrorRenderer error={txt.replace("__ERROR__:", "").trim()} />
      </div>
    );
  }

  /* ── AI 建议模式 ───────────────────────────────────────────────────── */

  const activeSuggestion = suggestions[af.path] ?? null;

  if (isSuggestion) {
    const rendererProps: Record<string, any> = {
      content: activeSuggestion.suggested,
      original: activeSuggestion.original,
      language: toMonacoLang(ext),
      path: af.path,
      readOnly: true,
      fileId: af.id,
      onAllResolved: handleAllResolved,
      onPartialSave: handlePartialSave,
      initialAppliedKeys: activeSuggestion.appliedContentKeys,
    };

    return (
      <div style={ps}>
        {TabBar}
        <div style={{
          display: "flex", alignItems: "center", gap: 8, padding: "4px 10px",
          background: "var(--bg-panel)",
          borderBottom: "1px solid var(--success)", fontSize: "var(--text-sm)", flexShrink: 0,
        }}>
          <span style={{ color: "var(--accent)", fontSize: "var(--text-base)" }}>🤖</span>
          <span style={{
            color: "var(--text-primary)", flex: 1,
            overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
          }}>{t("fileViewer.suggestionTitle", { path: af.path })}</span>
          <button
            onClick={() => void acceptSuggestion(af.path)}
            title={t("fileViewer.acceptAllTitle")}
            style={{
              padding: "2px 10px", fontSize: "var(--text-xs)", cursor: "pointer",
              border: "1px solid var(--success)", borderRadius: 4,
              background: "transparent", color: "var(--success)", fontWeight: 600,
            }}
          >{t("fileViewer.acceptAll")}</button>
          <button
            onClick={() => void rejectSuggestion(af.path)}
            title={t("fileViewer.rejectAllTitle")}
            style={{
              padding: "2px 10px", fontSize: "var(--text-xs)", cursor: "pointer",
              border: "1px solid var(--border-subtle)", borderRadius: 4,
              background: "transparent", color: "var(--text-tertiary)",
            }}
          >{t("fileViewer.rejectAll")}</button>
        </div>
        <div style={{ flex: 1, minHeight: 0, overflow: "hidden", display: "flex" }}>
          <Renderer {...rendererProps} />
        </div>
      </div>
    );
  }

  /* ── 正常编辑模式 ──────────────────────────────────────────────────── */

  // 可编辑模式：文本类走 Monaco，可保存；图片/PDF/表格为只读预览
  const editable = md === "code" || md === "markdown" || md === "html" || md === "log" || md === "text";

  const rendererProps: Record<string, any> = {
    content: cur,
    language: toMonacoLang(ext),
    path: af.path,
    fileId: af.id,
  };
  if (editable) {
    rendererProps.original = orig;
    rendererProps.onChange = onChg;
  }

  return (
    <div style={ps}>
      {TabBar}
      <div style={{
        display: "flex", alignItems: "center", gap: 8, padding: "3px 8px",
        background: "var(--bg-panel)", borderBottom: "1px solid var(--border-subtle)",
        fontSize: "var(--text-xs)", color: "var(--text-tertiary)", flexShrink: 0,
      }}>
        <span style={{
          padding: "1px 6px", borderRadius: 3, background: "var(--bg-elevated)",
          fontWeight: 600, color: "var(--accent)", fontSize: "var(--text-2xs)",
        }}>{Labels[md] ?? "TEXT"}</span>
        <span>{ext || "plain"}</span>
        <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {af.path}
        </span>
        {dirty && <span style={{ color: "var(--accent-2)", fontSize: "var(--text-2xs)" }}>● {t("fileViewer.modified")}</span>}
        <span style={{ fontSize: "var(--text-2xs)", opacity: 0.5 }}>{t("fileViewer.shortcuts")}</span>
      </div>
      <div style={{ flex: 1, minHeight: 0, overflow: "hidden", display: "flex" }}>
        <Renderer {...rendererProps} />
      </div>
      {/* ── 底栏按钮：保存 / 另存为 / 关闭（补齐快捷键的可视入口）───────────── */}
      <div style={{
        display: "flex", alignItems: "center", gap: 8, padding: "4px 10px",
        background: "var(--bg-panel)", borderTop: "1px solid var(--border-subtle)",
        flexShrink: 0,
      }}>
        {editable && (
          <>
            <button
              onClick={() => void onSave()}
              disabled={!dirty}
              title="Ctrl+S"
              style={{
                padding: "3px 12px", fontSize: "var(--text-xs)",
                cursor: dirty ? "pointer" : "default",
                border: "1px solid var(--border-subtle)", borderRadius: 4,
                background: dirty ? "var(--accent)" : "transparent",
                color: dirty ? "var(--text-on-accent)" : "var(--text-muted)",
                fontWeight: dirty ? 600 : 400, opacity: dirty ? 1 : 0.6,
              }}
            >{t("common.save")}</button>
            <button
              onClick={() => void onSaveAs()}
              title="Ctrl+Shift+S"
              style={{
                padding: "3px 12px", fontSize: "var(--text-xs)", cursor: "pointer",
                border: "1px solid var(--border-subtle)", borderRadius: 4,
                background: "transparent", color: "var(--text-secondary)",
              }}
            >{t("common.saveAs")}</button>
          </>
        )}
        <span style={{ flex: 1 }} />
        <button
          onClick={() => af && closeFile(af.id)}
          title="Ctrl+W"
          style={{
            padding: "3px 12px", fontSize: "var(--text-xs)", cursor: "pointer",
            border: "1px solid var(--border-subtle)", borderRadius: 4,
            background: "transparent", color: "var(--text-tertiary)",
          }}
        >{t("common.close")}</button>
      </div>
    </div>
  );
}
