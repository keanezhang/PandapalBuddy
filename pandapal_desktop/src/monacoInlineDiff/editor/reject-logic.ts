import type monaco from "monaco-editor";
import type { Hunk } from "../engine/types";
import { computeDiff, groupHunks } from "../engine";

/* ── 日志 ─────────────────────────────────────────────────────────────── */

const L = (...args: unknown[]) => console.debug("[mid-reject]", ...args);

/* ── 辅助：基于当前 model 重新计算 diff，定位某个 hunk ────────────────── */

/**
 * 基于当前 model 内容重新计算 diff，查找与给定 hunkId 匹配的 hunk。
 * 因为 reject 时 model 可能已被之前的 reject 修改，需要重新定位。
 *
 * hunkId 的锚点段（原始行区间）不随 model 变化漂移，故 id 精确匹配即稳定可靠；
 * 仅在内容哈希段因故不一致时，才回退到「type + 原始行锚点」匹配。
 */
export function findFreshHunk(
  hunkId: string,
  original: string,
  model: monaco.editor.ITextModel,
): Hunk | undefined {
  const hunks = groupHunks(computeDiff(original, model.getValue()));

  // 优先用 id 精确匹配（锚点稳定 → 跨 model 变化仍精确命中）
  const byId = hunks.find((h) => h.id === hunkId);
  if (byId) return byId;

  // 兜底：仅按 type + 原始行锚点匹配（忽略内容哈希段）
  // id 格式: "{type}#{origStart}-{origEnd}#{hashDel}#{hashAdd}"
  const seg = hunkId.split("#");
  if (seg.length < 2) return undefined;
  const type = seg[0];
  const anchor = seg[1];
  return hunks.find(
    (h) => h.type === type && `${h.origStart}-${h.origEnd}` === anchor,
  );
}

/* ── 辅助：计算 entries[startIdx] 在 model 中的行号 ──────────────────── */

/**
 * 对于 add 条目，行号已包含当前行，返回当前行号；
 * 对于 del 条目，行号未递增（当前行在 model 中不存在），返回下一行号。
 */
export function lineNumberOfEntry(
  original: string,
  model: monaco.editor.ITextModel,
  startIdx: number,
): number {
  const entries = computeDiff(original, model.getValue());
  let ln = 0;
  for (let p = 0; p < entries.length; p++) {
    if (entries[p].kind !== "del") ln++;
    if (p === startIdx) return entries[p].kind === "del" ? ln + 1 : ln;
  }
  return 1; // fallback
}

/* ── 辅助：entries 在 startIdx 之前的非 del 行数 ──────────────────────── */

/**
 * 计算 diff entries 中 startIdx 之前所有非 del 行的数量，
 * 用于定位 del 类型 hunk 的插入位置。
 */
export function nonDelLinesBefore(
  original: string,
  model: monaco.editor.ITextModel,
  startIdx: number,
): number {
  const entries = computeDiff(original, model.getValue());
  let n = 0;
  for (let p = 0; p < startIdx; p++)
    if (entries[p].kind !== "del") n++;
  return n;
}

/* ── 核心：Reject 一个 hunk ──────────────────────────────────────────── */

/**
 * 根据 hunk 类型执行逆操作，修改 model 内容：
 *   - **add**：删除新增的行
 *   - **del**：恢复被删除的行（在正确位置插入）
 *   - **modify**：保留 del 行（恢复原始），去掉 add 行（整体 setValue 重建）
 *
 * @param type     hunk 类型
 * @param hunk     要 reject 的 hunk 对象（含 startIdx/endIdx）
 * @param original 原始文本（用于 modify 类型重建）
 * @param editor   Monaco 编辑器实例
 * @param monaco   Monaco 命名空间
 */
export function rejectHunk(
  type: "del" | "add" | "modify",
  hunk: Hunk,
  original: string,
  editor: monaco.editor.IStandaloneCodeEditor,
  monaco: typeof import("monaco-editor"),
): void {
  const model = editor.getModel();
  if (!model) {
    L("reject: no model");
    return;
  }

  L("reject start", {
    type,
    id: hunk.id.slice(0, 6),
    del: hunk.delLines.length,
    add: hunk.addLines.length,
    modelLines: model.getLineCount(),
  });

  if (type === "add") {
    // 删除新增的行
    const fresh = findFreshHunk(hunk.id, original, model);
    if (!fresh) {
      L("reject add: hunk not found!", hunk.id.slice(0, 6));
      return;
    }
    const addStart = lineNumberOfEntry(original, model, fresh.startIdx);
    const count = fresh.addLines.length;
    const lineCount = model.getLineCount();
    L("reject add: delete lines", { addStart, count, lineCount });

    // 注意：Monaco 会把越界 position clamp 到 model 末尾。
    // 若删除块触及最后一行，Range(addStart,1,addStart+count,1) 的终点
    // (lineCount+1, 1) 会被 clamp 成 (lineCount, maxCol)，只删内容不删换行，
    // 留下一个空行（真实 Monaco 行为，FakeModel 单测掩盖了它）。
    let range: monaco.Range;
    if (addStart + count <= lineCount) {
      // 删除块之后还有行：删到下一行行首，连同换行符一起移除
      range = new monaco.Range(addStart, 1, addStart + count, 1);
    } else if (addStart > 1) {
      // 删除块直到文件末尾：从上一行行尾删到 model 末尾，连同前置换行符
      const prevCol = model.getLineMaxColumn(addStart - 1);
      const endCol = model.getLineMaxColumn(lineCount);
      range = new monaco.Range(addStart - 1, prevCol, lineCount, endCol);
    } else {
      // 整个 model 都在删除块内：全量清空
      const endCol = model.getLineMaxColumn(lineCount);
      range = new monaco.Range(1, 1, lineCount, endCol);
    }
    editor.pushUndoStop();
    model.pushEditOperations([], [{ range, text: "" }], () => null);
    editor.pushUndoStop();
  } else if (type === "del") {
    // 恢复被删除的行
    const fresh = findFreshHunk(hunk.id, original, model);
    if (!fresh) {
      L("reject del: hunk not found!", hunk.id.slice(0, 6));
      return;
    }
    const afterLine = nonDelLinesBefore(original, model, fresh.startIdx);
    const text = fresh.delLines.join("\n");
    L("reject del: restore", {
      afterLine,
      modelLines: model.getLineCount(),
      textLen: text.length,
    });

    editor.pushUndoStop();
    if (afterLine <= 0) {
      // 在第一行之前插入
      const isEmptyModel =
        model.getLineCount() === 1 && model.getLineMaxColumn(1) === 1;
      // model 为空时不附加换行（否则空行 diff 保留，永远无法 resolved）
      const insertText = isEmptyModel ? text : text + "\n";
      model.pushEditOperations(
        [],
        [{ range: new monaco.Range(1, 1, 1, 1), text: insertText }],
        () => null,
      );
    } else if (afterLine >= model.getLineCount()) {
      // 在末尾追加
      const col = model.getLineMaxColumn(model.getLineCount());
      model.pushEditOperations(
        [],
        [
          {
            range: new monaco.Range(
              model.getLineCount(),
              col,
              model.getLineCount(),
              col,
            ),
            text: "\n" + text,
          },
        ],
        () => null,
      );
    } else {
      // 在中间插入
      const col = model.getLineMaxColumn(afterLine);
      model.pushEditOperations(
        [],
        [
          {
            range: new monaco.Range(afterLine, col, afterLine, col),
            text: "\n" + text,
          },
        ],
        () => null,
      );
    }
    editor.pushUndoStop();
  } else {
    // modify: 直接从 diff entries 重建 model
    // 保留 del 行（恢复原始），去掉 add 行
    // 注意：必须用 findFreshHunk 重新定位，不能用旧 hunk.startIdx（连续 Reject 时会漂移）
    const fresh = findFreshHunk(hunk.id, original, model);
    if (!fresh) {
      L("reject modify: hunk not found!", hunk.id.slice(0, 6));
      return;
    }
    const entries = computeDiff(original, model.getValue());
    const hs = fresh.startIdx;
    const he = fresh.endIdx;
    let rebuilt = "";
    for (let i = 0; i < entries.length; i++) {
      const e = entries[i];
      if (i >= hs && i <= he) {
        // 当前 modify hunk 内部：只保留 del 行
        if (e.kind === "del") rebuilt += e.text + "\n";
      } else {
        if (e.kind !== "del") rebuilt += e.text + "\n";
      }
    }
    if (rebuilt.endsWith("\n")) rebuilt = rebuilt.slice(0, -1);
    L("reject modify: rebuild", {
      oldLines: model.getLineCount(),
      newLen: rebuilt.length,
    });
    editor.pushUndoStop();
    model.setValue(rebuilt);
    editor.pushUndoStop();
  }

  L("reject done", { lines: model.getLineCount() });
}
