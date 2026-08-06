/**
 * src/store/fileStore.ts
 *
 * 文件管理 Store。
 * - 文件树懒加载
 * - Tab 管理（最大 10 个）
 * - 内容缓存（最大 20 条，LRU 淘汰）
 * - 打开文件时自动展开查看器
 */

import { create } from "zustand";
import { readTextFile, writeTextFile, readDir, remove, stat } from "@tauri-apps/plugin-fs";
import { open, save } from "@tauri-apps/plugin-dialog";
import { usePreferenceStore } from "./preferenceStore";

export interface FileNode {
  path: string;
  name: string;
  isDirectory: boolean;
  children?: FileNode[];
}

export interface OpenFile {
  id: string;
  path: string;
  name: string;
  extension: string;
}

// 扩展名 → 查看器模式的映射（ViewerMode / VIEWER_MAP / resolveViewerMode）
// 已收敛至单一真相源 components/fileRenderers/fileTypes.ts，store 不再持有。

const MAX_TABS = 10;
const MAX_CACHE = 20;

// ── 文件打开策略（大小 / 类型闸门）────────────────────────────────────────
/** 文本/代码类预览上限：超过即拒绝（Monaco 大文件会卡顿） */
const MAX_TEXT_BYTES = 5 * 1024 * 1024;   // 5 MB
/** PDF 预览上限：超过即拒绝 */
const MAX_PDF_BYTES = 50 * 1024 * 1024;   // 50 MB
/** 图片预览上限 */
const MAX_IMAGE_BYTES = 50 * 1024 * 1024; // 50 MB

/** 图片扩展名：不读文本，由 ImageRenderer 读字节渲染 */
const IMAGE_EXTS = new Set([
  "png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico",
]);

/**
 * 明确的二进制类型：一律拒绝预览（可执行 / 压缩 / 媒体 / Office / 字体 / 数据库 等）。
 * 未列出的未知扩展名会当作文本尝试读取（带大小闸门兜底）。
 */
const BINARY_EXTS = new Set([
  "exe", "dll", "so", "dylib", "bin", "dat", "class", "pyc", "pyo", "o", "a", "obj", "lib", "wasm",
  "zip", "tar", "gz", "tgz", "bz2", "xz", "7z", "rar", "jar", "war", "ear",
  "db", "sqlite", "sqlite3", "mdb",
  "mp3", "wav", "flac", "ogg", "m4a", "aac", "mp4", "avi", "mov", "mkv", "webm", "flv", "wmv",
  "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp",
  "woff", "woff2", "ttf", "otf", "eot",
  "psd", "ai", "sketch", "fig",
  "iso", "dmg", "pkg", "deb", "rpm", "msi",
]);

/** 字节数 → 人类可读（用于拒绝提示） */
function fmtSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${bytes} B`;
}

interface FileState {
  fileTree: FileNode[];
  fileTreeLoading: boolean;
  openFiles: OpenFile[];
  activeFileId: string | null;
  fileContents: Record<string, string>;
  /** LRU 顺序数组：记录 cache key 的插入/访问顺序，用于精确淘汰 */
  _cacheOrder: string[];

  /** AI 代码建议 diff（原始 vs 建议），支持多文件同时持有 */
  suggestions: Record<string, { original: string; suggested: string; appliedContentKeys: string[] }>;

  /** 递归遍历时跳过的目录名 */
  SKIP_DIRS: Set<string>;
  /** 将插件 DirEntry 递归转换为 FileNode[] */
  _buildFileTree: (dirPath: string, depth?: number) => Promise<FileNode[]>;
  setFileTree: (tree: FileNode[]) => void;
  setFileTreeLoading: (loading: boolean) => void;
  openFile: (path: string, content: string) => void;
  closeFile: (fileId: string) => void;
  switchActiveFile: (fileId: string) => void;

  // 异步操作方法（调用 Tauri 官方插件）
  loadFileTree: (rootPath: string) => Promise<void>;
  loadAndOpenFile: (path: string) => Promise<void>;
  saveCurrentFile: (content: string) => Promise<void>;
  deleteFile: (path: string) => Promise<void>;
  pickAndOpenFile: () => Promise<void>;
  pickAndSaveAs: (content: string) => Promise<void>;
  pickFolderAndLoad: () => Promise<void>;
  showSuggestion: (path: string, original: string, suggested: string) => void;
  /** 更新建议内容（逐项 Reject 后同步 suggested，避免切文件后状态丢失） */
  updateSuggestion: (path: string, suggested: string) => void;
  /** 逐项 Apply 时标记已处理的 hunk contentKey（防止切文件后丢失） */
  markHunkApplied: (path: string, contentKey: string) => void;
  clearSuggestion: (path: string) => void;
  acceptSuggestion: (path: string) => Promise<void>;
  rejectSuggestion: (path: string) => Promise<void>;
}

export const useFileStore = create<FileState>((set, get) => ({
  fileTree: [],
  fileTreeLoading: false,
  openFiles: [],
  activeFileId: null,
  fileContents: {},
  _cacheOrder: [],
  suggestions: {},

  setFileTree: (tree) => set({ fileTree: tree, fileTreeLoading: false }),
  setFileTreeLoading: (loading) => set({ fileTreeLoading: loading }),

  openFile: (path, content) => {
    const { openFiles, fileContents, _cacheOrder } = get();

    // 已在 Tab 列表中 → 仅切换，同时将 path 移到 LRU 尾部（表示最近访问）
    const existing = openFiles.find((f) => f.path === path);
    if (existing) {
      const newOrder = _cacheOrder.filter((p) => p !== path).concat(path);
      set({ activeFileId: existing.id, _cacheOrder: newOrder });
      return;
    }

    // LRU 缓存淘汰：用 _cacheOrder 精确追踪最旧条目
    let newContents = { ...fileContents, [path]: content };
    let newOrder = _cacheOrder.filter((p) => p !== path).concat(path);
    if (newOrder.length > MAX_CACHE) {
      const oldest = newOrder.shift()!;
      delete newContents[oldest];
    }

    const name = path.split("/").pop() || path;
    const ext = path.split(".").pop()?.toLowerCase() || "";
    const newFile: OpenFile = { id: path, path, name, extension: ext };

    // Tab 数量限制
    let newOpenFiles = [...openFiles, newFile];
    if (newOpenFiles.length > MAX_TABS) {
      const removed = newOpenFiles.shift()!;
      delete newContents[removed.path];
      newOrder = newOrder.filter((p) => p !== removed.path);
      // ★ 同步清除被挤出 Tab 的 suggestion，防止 auto-open effect 反复拉回
      if (get().suggestions[removed.path]) {
        const clean = { ...get().suggestions };
        delete clean[removed.path];
        set({
          openFiles: newOpenFiles,
          activeFileId: path,
          fileContents: newContents,
          _cacheOrder: newOrder,
          suggestions: clean,
        });
        // 自动展开查看器
        const pref = usePreferenceStore.getState();
        if (pref.splitRatio >= 0.99) {
          pref.setSplitRatio(0.6);
        }
        console.debug("[file] open:", path);
        return;
      }
    }

    set({
      openFiles: newOpenFiles,
      activeFileId: path,
      fileContents: newContents,
      _cacheOrder: newOrder,
    });

    // 自动展开查看器
    const pref = usePreferenceStore.getState();
    if (pref.splitRatio >= 0.99) {
      pref.setSplitRatio(0.6);
    }

    console.debug("[file] open:", path);
  },

  closeFile: (fileId) => {
    const { openFiles, activeFileId, fileContents, _cacheOrder, suggestions } = get();
    const idx = openFiles.findIndex((f) => f.id === fileId);
    if (idx === -1) return;

    const newOpenFiles = [...openFiles];
    newOpenFiles.splice(idx, 1);

    const newContents = { ...fileContents };
    delete newContents[fileId];

    const newOrder = _cacheOrder.filter((p) => p !== fileId);

    // 如果关闭的是当前活动 Tab，切换到相邻
    let newActiveId = activeFileId;
    if (activeFileId === fileId) {
      newActiveId = newOpenFiles[Math.min(idx, newOpenFiles.length - 1)]?.id ?? null;
    }

    // ★ 同步清除关闭 Tab 的 suggestion，防止 auto-open effect 反复拉回
    const cleanSuggestions = suggestions[fileId]
      ? (() => { const n = { ...suggestions }; delete n[fileId]; return n; })()
      : undefined;

    set({
      openFiles: newOpenFiles,
      activeFileId: newActiveId,
      fileContents: newContents,
      _cacheOrder: newOrder,
      ...(cleanSuggestions ? { suggestions: cleanSuggestions } : {}),
    });

    // 所有 Tab 关闭 → 收起查看器
    if (newOpenFiles.length === 0) {
      usePreferenceStore.getState().setSplitRatio(1.0);
    }

    console.debug("[file] close:", fileId);
  },

  switchActiveFile: (fileId) => {
    // 访问 Tab 时更新 LRU 顺序
    set((s) => ({
      activeFileId: fileId,
      _cacheOrder: s._cacheOrder.filter((p) => p !== fileId).concat(fileId),
    }));
  },

  // ── 工具函数 ──────────────────────────────────────────────────────────

  /**
   * 递归遍历时跳过的目录名。
   * 只跳过二进制/缓存/依赖目录，不跳过 IDE 配置（.idea/.vscode 中的 settings.json 用户可能需要查看）。
   */
  SKIP_DIRS: new Set(["venv", ".venv", "node_modules", "__pycache__", ".git"]),

  /** 将插件 DirEntry 递归转换为 FileNode[] */
  _buildFileTree: async (dirPath: string, depth: number = 0): Promise<FileNode[]> => {
    const MAX_DEPTH = 10;
    if (depth >= MAX_DEPTH) return [];

    try {
      const entries = await readDir(dirPath);
      const nodes: FileNode[] = [];
      const skipSet = get().SKIP_DIRS;

      for (const entry of entries) {
        const name = entry.name;
        if (!name || name.startsWith(".") || skipSet.has(name)) continue;

        const fullPath = `${dirPath}/${name}`;
        const node: FileNode = {
          path: fullPath,
          name,
          isDirectory: !!entry.isDirectory,
        };

        if (entry.isDirectory) {
          node.children = await get()._buildFileTree(fullPath, depth + 1);
        }

        nodes.push(node);
      }

      // 目录优先，字母序排序
      nodes.sort((a, b) =>
        Number(b.isDirectory) - Number(a.isDirectory) || a.name.localeCompare(b.name)
      );
      return nodes;
    } catch {
      return [];
    }
  },

  // ── 异步操作 ──────────────────────────────────────────────────────────

  loadFileTree: async (rootPath) => {
    set({ fileTreeLoading: true });
    try {
      const tree = await get()._buildFileTree(rootPath);
      set({ fileTree: tree, fileTreeLoading: false });
      console.debug("[file] tree loaded:", tree.length, "entries");
    } catch (e) {
      console.error("[file] load tree failed:", e);
      set({ fileTreeLoading: false });
    }
  },

  loadAndOpenFile: async (path) => {
    const ext = path.split(".").pop()?.toLowerCase() || "";
    try {
      // 文件大小（stat 失败则按 0 处理，交给后续读取兜底）
      let size = 0;
      try { size = (await stat(path)).size ?? 0; } catch { /* ignore */ }

      // 1. 明确的二进制类型 → 拒绝
      if (BINARY_EXTS.has(ext)) {
        get().openFile(path, `__ERROR__: 暂不支持预览二进制文件（.${ext}）`);
        return;
      }

      // 2. PDF → 50MB 以内交给 PdfRenderer（读字节渲染），超限拒绝
      if (ext === "pdf") {
        if (size > MAX_PDF_BYTES) {
          get().openFile(path, `__ERROR__: PDF 过大（${fmtSize(size)}），超过 50MB 预览上限`);
        } else {
          get().openFile(path, ""); // 内容留空，PdfRenderer 依据 path 自行读取
        }
        return;
      }

      // 3. 图片 → 由 ImageRenderer 读字节渲染，超限拒绝
      if (IMAGE_EXTS.has(ext)) {
        if (size > MAX_IMAGE_BYTES) {
          get().openFile(path, `__ERROR__: 图片过大（${fmtSize(size)}）`);
        } else {
          get().openFile(path, ""); // 内容留空，ImageRenderer 依据 path 自行读取
        }
        return;
      }

      // 4. 文本/代码类（含未知扩展名）→ 大小闸门后按文本读取
      if (size > MAX_TEXT_BYTES) {
        get().openFile(path, `__ERROR__: 文件过大（${fmtSize(size)}），超过 5MB 预览上限`);
        return;
      }
      const content = await readTextFile(path);
      get().openFile(path, content);
    } catch (e) {
      console.error("[file] read failed:", path, e);
      get().openFile(path, `__ERROR__: ${e}`);
    }
  },

  saveCurrentFile: async (content) => {
    const { activeFileId } = get();
    if (!activeFileId) return;
    try {
      await writeTextFile(activeFileId, content);
      set((s) => ({
        fileContents: { ...s.fileContents, [activeFileId]: content },
      }));
      console.debug("[file] saved:", activeFileId);
    } catch (e) {
      console.error("[file] save failed:", activeFileId, e);
    }
  },

  deleteFile: async (path) => {
    try {
      await remove(path);
      get().closeFile(path);
      console.debug("[file] deleted:", path);
    } catch (e) {
      console.error("[file] delete failed:", path, e);
    }
  },

  /** 打开文件选择对话框，读取文件并在查看器中打开 */
  pickAndOpenFile: async () => {
    try {
      // 不传 directory 参数，默认即为文件选择模式（非目录）
      const selected = await open({
        title: "选择文件",
        multiple: false,
      });
      console.debug("[file] file dialog result:", typeof selected, selected);
      if (selected && typeof selected === "string") {
        await get().loadAndOpenFile(selected);
      }
    } catch (e) {
      console.error("[file] pick failed:", e);
    }
  },

  /** 打开保存对话框，将当前内容保存为新文件 */
  pickAndSaveAs: async (content: string) => {
    try {
      const dest = await save({
        filters: [{ name: "所有文件", extensions: ["*"] }],
      });
      if (dest) {
        await writeTextFile(dest, content);
        set((s) => ({
          fileContents: { ...s.fileContents, [dest]: content },
        }));
        console.debug("[file] saved as:", dest);
      }
    } catch (e) {
      console.error("[file] save as failed:", e);
    }
  },

  /** 打开文件夹选择对话框，加载所选目录的文件树 */
  pickFolderAndLoad: async () => {
    try {
      const dir = await open({ directory: true, multiple: false });
      if (dir) {
        await get().loadFileTree(dir as string);
      }
    } catch (e) {
      console.error("[file] pick folder failed:", e);
    }
  },

  // ── AI 建议 diff ───────────────────────────────────────────────────────

  showSuggestion: (path, original, suggested) => {
    const existing = get().suggestions[path];
    console.debug("[file-s] show", { path, o: original.length, s: suggested.length, overwrite: existing != null, totalPending: Object.keys(get().suggestions).length + (existing ? 0 : 1) });
    set((st) => ({
      suggestions: { ...st.suggestions, [path]: { original, suggested, appliedContentKeys: [] } },
    }));
  },

  updateSuggestion: (path, suggested) => {
    console.debug("[file-s] update-suggested", path, suggested.length);
    set((st) => {
      const s = st.suggestions[path];
      if (!s) return {};
      return { suggestions: { ...st.suggestions, [path]: { ...s, suggested } } };
    });
  },

  markHunkApplied: (path, contentKey) => {
    console.debug("[file-s] mark-applied", path, contentKey.slice(0, 6));
    set((st) => {
      const s = st.suggestions[path];
      if (!s) return {};
      const keys = s.appliedContentKeys.includes(contentKey)
        ? s.appliedContentKeys
        : [...s.appliedContentKeys, contentKey];
      return { suggestions: { ...st.suggestions, [path]: { ...s, appliedContentKeys: keys } } };
    });
  },

  clearSuggestion: (path) => {
    console.debug("[file-s] clear", path);
    set((st) => {
      const next = { ...st.suggestions };
      delete next[path];
      return { suggestions: next };
    });
  },

  acceptSuggestion: async (path) => {
    const s = get().suggestions[path];
    if (!s) { console.debug("[file-s] accept: NOT FOUND", path); return; }
    console.debug("[file-s] accept", { path, o: s.original.length, s: s.suggested.length });
    try {
      await writeTextFile(path, s.suggested);
      set((st) => {
        const next = { ...st.suggestions };
        delete next[path];
        return {
          fileContents: { ...st.fileContents, [path]: s.suggested },
          suggestions: next,
        };
      });
    } catch (e) {
      console.debug("[file-s] accept FAIL", path, e);
    }
  },

  rejectSuggestion: async (path) => {
    const s = get().suggestions[path];
    if (!s) { console.debug("[file-s] reject: NOT FOUND", path); return; }
    console.debug("[file-s] reject", { path, o: s.original.length, s: s.suggested.length });
    try {
      await writeTextFile(path, s.original);
      set((st) => {
        const next = { ...st.suggestions };
        delete next[path];
        return {
          fileContents: { ...st.fileContents, [path]: s.original },
          suggestions: next,
        };
      });
    } catch (e) {
      console.debug("[file-s] reject FAIL", path, e);
      set((st) => {
        const next = { ...st.suggestions };
        delete next[path];
        return { suggestions: next };
      });
    }
  },
}));
