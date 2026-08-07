import { useState, useCallback } from "react";

/**
 * 单个 AI 建议的状态。
 */
export interface Suggestion {
  /** 原始文件内容 */
  original: string;
  /** AI 建议的文件内容 */
  suggested: string;
  /** 已 Apply 的 hunk contentKey 列表（用于页签切换后恢复状态） */
  appliedContentKeys: string[];
}

/**
 * useDiffSuggestions — 管理多文件 AI 代码建议的 Hook。
 *
 * 提供 show / update / markApplied / clear 四个原子操作，
 * 与 InlineDiffEditor 的 onPartialSave / onAllResolved 回调无缝对接。
 *
 * @example
 * ```tsx
 * const { suggestions, showSuggestion, updateSuggestion, markApplied, clearSuggestion } = useDiffSuggestions();
 *
 * // 收到 AI 建议
 * showSuggestion("/path/to/file.py", originalCode, suggestedCode);
 *
 * // 逐项 Apply 时
 * const handlePartialSave = (content: string, hunkKey: string) => {
 *   updateSuggestion(filePath, content);
 *   if (hunkKey) markApplied(filePath, hunkKey);
 * };
 *
 * // 全部处理完毕
 * const handleAllResolved = (final: string) => {
 *   clearSuggestion(filePath);
 * };
 * ```
 */
export function useDiffSuggestions() {
  const [suggestions, setSuggestions] = useState<
    Record<string, Suggestion>
  >({});

  /** 展示一条 AI 建议（覆盖同路径已有建议） */
  const showSuggestion = useCallback(
    (path: string, original: string, suggested: string) => {
      console.debug("[mid-hook] show", {
        path,
        o: original.length,
        s: suggested.length,
      });
      setSuggestions((prev) => ({
        ...prev,
        [path]: { original, suggested, appliedContentKeys: [] },
      }));
    },
    [],
  );

  /** 更新建议内容（Reject 后同步 suggested，避免切文件后状态丢失） */
  const updateSuggestion = useCallback(
    (path: string, suggested: string) => {
      console.debug("[mid-hook] update", path, suggested.length);
      setSuggestions((prev) => {
        const s = prev[path];
        if (!s) return prev;
        return { ...prev, [path]: { ...s, suggested } };
      });
    },
    [],
  );

  /** 逐项 Apply 时标记已处理的 hunk contentKey */
  const markApplied = useCallback((path: string, contentKey: string) => {
    console.debug("[mid-hook] mark", path, contentKey.slice(0, 6));
    setSuggestions((prev) => {
      const s = prev[path];
      if (!s) return prev;
      if (s.appliedContentKeys.includes(contentKey)) return prev;
      return {
        ...prev,
        [path]: {
          ...s,
          appliedContentKeys: [...s.appliedContentKeys, contentKey],
        },
      };
    });
  }, []);

  /** 清除一条建议（全部处理完毕后调用） */
  const clearSuggestion = useCallback((path: string) => {
    console.debug("[mid-hook] clear", path);
    setSuggestions((prev) => {
      const next = { ...prev };
      delete next[path];
      return next;
    });
  }, []);

  return {
    suggestions,
    showSuggestion,
    updateSuggestion,
    markApplied,
    clearSuggestion,
  } as const;
}
