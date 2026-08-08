/**
 * InlineDiffEditor — 基于 Monaco Editor 的内联 diff 审阅组件。
 *
 * 设计：
 *   - defaultValue + 命令式 model 操作，非受控，React 不会回滚 Reject 后的 model
 *   - OverlayManager 统一管理 decoration / contentWidget 生命周期
 *   - 每次 rebuild 全量清除 → 全量重建
 *   - Hunk 以内容哈希标识，不依赖漂移的行号
 */
import "./styles.css";
import { useRef, useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import Editor from "@monaco-editor/react";
import type monaco from "monaco-editor";

import { computeDiff, groupHunks } from "../engine";
import type { Hunk } from "../engine/types";
import { OverlayManager } from "./overlay-manager";
import { rejectHunk } from "./reject-logic";
import {
  buildDelZone,
  buildBtnZone,
  buildAddOverlays,
} from "./zone-builders";
import type { AddHunkType } from "./zone-builders";

/* ── 公开 Props ────────────────────────────────────────────────────────── */

/** InlineDiffEditor 组件属性 */
export interface InlineDiffEditorProps {
  /** 原始文本 */
  original: string;
  /** 当前 / AI 建议文本 */
  current: string;
  /** Monaco 语言标识符（如 "python", "typescript"） */
  language: string;
  /** 所有 hunk 被逐项 Apply/Reject 完毕后回调，携带最终 model 文本 */
  onAllResolved?: (savedContent: string) => void;
  /** 单个 hunk Apply/Reject 后即时回调，携带当前 model 文本 */
  onPartialSave?: (content: string, hunkKey: string) => void;
  /** 已 Apply 的 hunk contentKey 列表（持久化状态，防止切文件丢失） */
  initialAppliedKeys?: string[];
}

/* ── 日志 ──────────────────────────────────────────────────────────────── */

const L = (...args: unknown[]) => console.debug("[mid]", ...args);

/* ── 组件 ──────────────────────────────────────────────────────────────── */

export function InlineDiffEditor({
  original,
  current,
  language,
  onAllResolved,
  onPartialSave,
  initialAppliedKeys,
}: InlineDiffEditorProps) {
  const { t } = useTranslation();
  /* ── Refs ──────────────────────────────────────────────────────────── */
  const editorRef = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const monacoRef = useRef<typeof monaco | null>(null);
  const overlayRef = useRef(new OverlayManager());
  const decoColRef =
    useRef<monaco.editor.IEditorDecorationsCollection | null>(null);
  const originalRef = useRef(original);
  const appliedIdsRef = useRef<Set<string>>(
    new Set(initialAppliedKeys ?? []),
  );
  const rebuildPendingRef = useRef(false);
  const resolvedRef = useRef<string | null>(null);
  /** 是否已做过「首 hunk 滚入视口」的兜底（每次 original/current 变化后重置） */
  const initialRevealDoneRef = useRef(false);
  const [hasPending, setHasPending] = useState(false);
  const onPartialSaveRef = useRef(onPartialSave);
  onPartialSaveRef.current = onPartialSave;

  /* ── 同步 original prop → ref ────────────────────────────────────── */
  useEffect(() => {
    const changed = originalRef.current !== original;
    L("original changed", {
      prev: originalRef.current.length,
      next: original.length,
      changed,
    });
    originalRef.current = original;
    if (changed) {
      appliedIdsRef.current = new Set();
      resolvedRef.current = null;
      initialRevealDoneRef.current = false;
    }
    const ed = editorRef.current;
    if (ed) {
      const model = ed.getModel();
      if (model && model.getValue() === current) scheduleRebuild();
    }
  }, [original, current]);

  /* ── 同步 current prop → model ───────────────────────────────────── */
  useEffect(() => {
    const ed = editorRef.current;
    if (!ed) return;
    const model = ed.getModel();
    if (!model) return;
    const modelVal = model.getValue();
    L("current changed", {
      propLen: current.length,
      modelLen: modelVal.length,
      same: modelVal === current,
    });
    if (modelVal === current) {
      scheduleRebuild();
      return;
    }
    model.setValue(current);
    resolvedRef.current = null;
    initialRevealDoneRef.current = false;
    scheduleRebuild();
  }, [current]);

  /* ── 核心：全量 rebuild diff UI ──────────────────────────────────── */
  const rebuild = useCallback(() => {
    rebuildPendingRef.current = false;
    const ed = editorRef.current;
    const m = monacoRef.current;
    if (!ed || !m) return;

    const model = ed.getModel();
    if (!model) return;

    // 1. 清除所有 overlay
    overlayRef.current.clearAll(ed);

    // 2. 计算 diff
    const curVal = model.getValue();
    const origVal = originalRef.current;
    L("rebuild", { orig: origVal.length, cur: curVal.length });
    const noDiff = origVal === curVal;

    const entries = noDiff ? [] : computeDiff(origVal, curVal);
    const allHunks = noDiff ? [] : groupHunks(entries);
    L(
      "rebuild: hunks",
      allHunks.map(
        (h) =>
          `${h.type}#${h.id.slice(0, 6)} d${h.delLines.length}a${h.addLines.length}`,
      ),
    );

    const applied = appliedIdsRef.current;
    const pending = allHunks.filter((h) => !applied.has(h.contentKey));
    setHasPending(pending.length > 0);

    // 3. 全量重建 view zones + decorations。
    //    无论是否还有 pending，都必须先移除上一次的旧 zone / 旧装饰 ——
    //    否则最后一个 Apply（pending=0）或 Reject（noDiff）后 UI 残留。
    const prevZones = overlayRef.current.consumeZoneIds();
    const addDecos: monaco.editor.IModelDeltaDecoration[] = [];

    ed.changeViewZones((accessor) => {
      // 移除上一次 rebuild 添加的所有 view zones
      for (const zid of prevZones) accessor.removeZone(zid);

      for (const hunk of pending) {
        // 计算 afterLine
        let afterLine = 0;
        for (let p = 0; p < hunk.startIdx; p++)
          if (entries[p].kind !== "del") afterLine++;

        // 删除行 ViewZone
        if (hunk.delLines.length > 0) {
          const zn = Math.max(0, Math.min(afterLine, model.getLineCount()));
          const zid = accessor.addZone({
            afterLineNumber: zn,
            heightInLines: hunk.delLines.length,
            domNode: buildDelZone(hunk),
          });
          overlayRef.current.addZoneId(zid);
        }

        // 新增行装饰器
        if (hunk.addLines.length > 0) {
          const hunkType: AddHunkType = hunk.type === "modify" ? "modify" : "add";
          buildAddOverlays(hunk, afterLine, ed, m, addDecos, hunkType);
        }

        // 按钮 ViewZone
        const btnAfter =
          hunk.addLines.length > 0
            ? Math.min(
                afterLine + hunk.addLines.length,
                model.getLineCount(),
              )
            : Math.max(0, Math.min(afterLine, model.getLineCount()));
        const bzid = accessor.addZone({
          afterLineNumber: btnAfter,
          heightInLines: 1,
          domNode: buildBtnZone(hunk, m, {
            onApply: (_hunkId, hunkKey) => {
              L("btn:Apply", {
                id: hunk.id.slice(0, 6),
                type: hunk.type,
              });
              appliedIdsRef.current.add(hunkKey);
              scheduleRebuild();
              const mv = editorRef.current?.getModel()?.getValue();
              if (mv != null) onPartialSaveRef.current?.(mv, hunkKey);
            },
            onReject: (rejectedHunk) => {
              L("btn:Reject", {
                id: rejectedHunk.id.slice(0, 6),
                type: rejectedHunk.type,
              });
              rejectHunk(
                rejectedHunk.type,
                rejectedHunk,
                originalRef.current,
                ed,
                m,
              );
              scheduleRebuild();
              const mv = editorRef.current?.getModel()?.getValue();
              if (mv != null) onPartialSaveRef.current?.(mv, "");
            },
          }),
        });
        overlayRef.current.addZoneId(bzid);
      }
    });

    // a11y：view zones 内有可交互按钮，Monaco 默认 aria-hidden="true" 会阻止焦点
    // 注意：FakeMonaco 没有 getDomNode，用可选链防御
    (ed.getDomNode?.()?.querySelector(".view-zones") as HTMLElement | null)
      ?.removeAttribute("aria-hidden");

    // 4. 批量应用装饰器
    if (decoColRef.current) {
      decoColRef.current.set(addDecos);
    }

    // 4b. 首 hunk 兜底可见性：Monaco 对「初始视口外」的 viewZone 不做布局
    //     （DOM 节点已创建但 top:0 / 0×0，等于没渲染）。CRLF 长文件打开时
    //     首 hunk 往往落在初始视口外，于是第一处改动看不到 Apply/Reject
    //     按钮与删除线，看起来「和未改过一样」。首次（或 original/current
    //     变化后）把首 hunk 滚进视口，强制 Monaco 完成该 zone 的布局。
    //     注意：必须等两帧 —— changeViewZones 后 zone 布局是异步的，立即
    //     reveal 时 Monaco 尚未更新滚动高度，会算出错误的滚动目标。
    if (!initialRevealDoneRef.current && pending.length > 0) {
      const first = pending[0];
      let firstAfterLine = 0;
      for (let p = 0; p < first.startIdx; p++)
        if (entries[p].kind !== "del") firstAfterLine++;
      // 仅真实 Monaco 具备该方法；FakeMonaco（单测）直接跳过整个块，
      // 避免在 fake-timer 环境下调度 rAF 干扰测试。
      if (typeof ed.revealLineInCenterIfOutsideViewport === "function") {
        const targetLine = firstAfterLine + 1;
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            const ed2 = editorRef.current;
            if (typeof ed2?.revealLineInCenterIfOutsideViewport === "function")
              ed2.revealLineInCenterIfOutsideViewport(targetLine);
          });
        });
        initialRevealDoneRef.current = true;
      }
    }

    // 5. 无 diff 或全部处理完毕 → 回调（此刻旧 zone / 装饰已清空，UI 无残留）
    if (noDiff || pending.length === 0) {
      // 去重：applyAll / rejectAll 已同步通知过，rebuild 不再重复
      if (resolvedRef.current !== curVal) {
        resolvedRef.current = curVal;
        L("rebuild: resolved", { len: curVal.length });
        onAllResolved?.(curVal);
      }
      return;
    }
  }, [onAllResolved]);

  /* ── 防重入调度 ──────────────────────────────────────────────────── */
  const scheduleRebuild = useCallback(() => {
    if (rebuildPendingRef.current) return;
    rebuildPendingRef.current = true;
    requestAnimationFrame(() => rebuild());
  }, [rebuild]);

  /* ── Apply All / Reject All ──────────────────────────────────────── */
  const applyAll = useCallback(() => {
    const ed = editorRef.current;
    if (!ed) return;
    const model = ed.getModel();
    if (!model) return;
    const curVal = model.getValue();
    L("applyAll", { len: curVal.length });
    const entries = computeDiff(originalRef.current, curVal);
    const hunks = groupHunks(entries);
    for (const h of hunks) appliedIdsRef.current.add(h.contentKey);
    resolvedRef.current = curVal;
    onAllResolved?.(curVal);
    scheduleRebuild();
  }, [onAllResolved]);

  const rejectAll = useCallback(() => {
    const ed = editorRef.current;
    if (!ed) return;
    const model = ed.getModel();
    if (!model) return;
    const m = monacoRef.current;
    if (!m) return;
    const origVal = originalRef.current;
    const curVal = model.getValue();
    if (origVal === curVal) return;

    const entries = computeDiff(origVal, curVal);
    const allHunks = groupHunks(entries);
    const applied = appliedIdsRef.current;
    const pendingHunks = allHunks.filter((h) => !applied.has(h.contentKey));

    if (pendingHunks.length === 0) {
      return;
    }

    L("rejectAll", {
      orig: origVal.length,
      cur: curVal.length,
      total: allHunks.length,
      pending: pendingHunks.length,
    });

    if (pendingHunks.length === allHunks.length) {
      ed.pushUndoStop();
      model.pushEditOperations(
        [],
        [{ range: model.getFullModelRange(), text: origVal }],
        () => null,
      );
      ed.pushUndoStop();
      resolvedRef.current = origVal;
      onAllResolved?.(origVal);
      scheduleRebuild();
      return;
    }

    for (const hunk of pendingHunks) {
      rejectHunk(hunk.type, hunk, origVal, ed, m);
    }
    scheduleRebuild();
  }, [onAllResolved]);

  /* ── Mount ────────────────────────────────────────────────────────── */
  const handleMount = useCallback(
    (editor: monaco.editor.IStandaloneCodeEditor, mi: typeof monaco) => {
      editorRef.current = editor;
      monacoRef.current = mi;
      decoColRef.current = editor.createDecorationsCollection();
      // 统一 model EOL 为 LF：空 model（如 current=""）的默认 EOL 可能是 CRLF，
      // Reject 恢复多行文本时 \n 会被 Monaco 按 model EOL 转成 \r\n，
      // 导致 model 与 original（LF 语义）不一致（diff normalizeEol 会掩盖 UI 差异，
      // 但 onAllResolved/保存内容会被 EOL 污染）。
      const m0 = editor.getModel();
      if (m0 && m0.getEOL() !== "\n") m0.setEOL(mi.editor.EndOfLineSequence.LF);
      L("mount", {
        modelLines: editor.getModel()?.getLineCount(),
        orig: originalRef.current.length,
        cur: current.length,
      });
      requestAnimationFrame(() => rebuild());
    },
    [rebuild, current.length],
  );

  /* ── Unmount cleanup ──────────────────────────────────────────────── */
  useEffect(
    () => () => {
      L("unmount");
      const ed = editorRef.current;
      if (ed) overlayRef.current.clearAll(ed);
      decoColRef.current = null;
      editorRef.current = null;
      monacoRef.current = null;
    },
    [],
  );

  /* ── Render ────────────────────────────────────────────────────────── */
  return (
    <div
      style={{
        flex: 1,
        overflow: "hidden",
        display: "flex",
        flexDirection: "column",
        position: "relative",
      }}
    >
      <Editor
        height="100%"
        defaultValue={current}
        language={language}
        theme="vs-dark"
        onMount={handleMount}
        options={{
          readOnly: true,
          minimap: { enabled: true, scale: 2, showSlider: "mouseover" },
          lineNumbers: "on",
          fontSize: 13,
          fontFamily:
            "'SF Mono','Cascadia Code','Fira Code',monospace",
          scrollBeyondLastLine: false,
          tabSize: 4,
          renderWhitespace: "selection",
          padding: { top: 8, bottom: 70 },
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
            {t("common.loadingEditor")}
          </div>
        }
      />
      {/* 底部全局操作栏 —— 仅在有未处理 hunk 时显示 */}
      {hasPending && (
        <div className="mid-float-bar">
          <span className="mid-float-label">{t("fileViewer.suggestionLabel")}</span>
          <div className="mid-float-actions">
            <button className="mid-btn-all mid-btn-reject-all" onClick={rejectAll}>
              {t("fileViewer.rejectAll")}
            </button>
            <button className="mid-btn-all mid-btn-apply-all" onClick={applyAll}>
              {t("fileViewer.acceptAll")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
