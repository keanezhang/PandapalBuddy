/**
 * src/store/kbStore.ts
 *
 * 知识库管理 Store。
 *
 * 消息流：
 *   前端发 KB_LIST → Python 回 KB_LIST_RESULT（知识库摘要列表，全量）
 *   前端发 KB_GET  → Python 回 KB_GET_RESULT（单库详情 + 文档列表）
 *   前端发 KB_BUILD → Python 持续回 KB_BUILD_PROGRESS + KB_BUILD_DONE/FAILED
 *   前端发 KB_SEARCH → Python 回 KB_SEARCH_RESULT（检索结果）
 *
 * 数据类型对齐后端 wire format：
 *   摘要字段来自 pandapal/knowledge_base/manager.py _summary()
 *   详情字段来自 manager.get_kb()
 */

import { create } from "zustand";
import type {
  KBDetail,
  KBDocNode,
  KBSearchResultItem,
  KBStatus,
  KBSummary,
  KbDocumentsChangedMsg,
} from "../types/api";

/** 建库进度（KB_BUILD_PROGRESS 增量） */
export interface KbBuildState {
  stage: string;
  percent: number;
  message: string;
  /** 增量建库（瞬态派生自 KB_BUILD_PROGRESS.payload.incremental，不持久化，见设计 R3） */
  incremental?: boolean;
}

/** 右栏 Tab（设计 §10.1 / 原型 rtabs） */
export type KbRightTab = "preview" | "settings" | "search";

/**
 * 上传反馈（KB_DOCUMENTS_CHANGED）。
 *
 * 字段从 wire 类型**派生**而非重写，避免与后端契约漂移
 * （真相源：`KbDocumentsChangedMsg`）。
 */
export interface KbUploadFeedback {
  uploaded: KbDocumentsChangedMsg["uploaded"];
  rejected: KbDocumentsChangedMsg["rejected"];
}

interface KbState {
  kbs: KBSummary[];
  loading: boolean;

  detail: KBDetail | null;
  detailLoading: boolean;

  /** name → 建库进度（运行中） */
  buildByName: Record<string, KbBuildState>;

  /** name → 文档树（KB_TREE_RESULT）。FS 即真相源，结构操作后由后端主动推送全量树 */
  treeByName: Record<string, KBDocNode[]>;
  /** 文档树请求进行中。全局单值——任一时刻只请求一个库的树 */
  treeLoading: boolean;

  /** name → 最近一次上传反馈（供编辑页 toast 展示） */
  uploadFeedbackByName: Record<string, KbUploadFeedback>;

  /** 当前选中项（文件/文件夹）相对 documents_dir 的 POSIX 路径；null = 未选中 */
  selectedPath: string | null;
  /** 右栏当前 Tab（预览 / 库设置 / 检索测试） */
  rightTab: KbRightTab;

  /** 独立检索测试结果 */
  searchResults: KBSearchResultItem[] | null;
  searchQuery: string;

  setLoading: (loading: boolean) => void;
  replaceAll: (kbs: KBSummary[]) => void;
  upsert: (kb: KBSummary) => void;
  remove: (name: string) => void;

  setDetail: (detail: KBDetail | null) => void;
  setDetailLoading: (loading: boolean) => void;

  setBuild: (name: string, state: KbBuildState) => void;
  clearBuild: (name: string) => void;

  /** 写入某库文档树（顺带复位 treeLoading —— 与 FileStore.setFileTree 同惯例） */
  setTree: (name: string, tree: KBDocNode[]) => void;
  setTreeLoading: (loading: boolean) => void;

  setUploadFeedback: (name: string, feedback: KbUploadFeedback) => void;

  /** 设置当前选中项（相对路径）；传 null 清空选中 */
  setSelectedPath: (path: string | null) => void;
  /** 切换右栏 Tab */
  setRightTab: (tab: KbRightTab) => void;

  setSearchResults: (results: KBSearchResultItem[] | null, query: string) => void;

  reset: () => void;
}

export const useKbStore = create<KbState>((set, get) => ({
  kbs: [],
  loading: false,
  detail: null,
  detailLoading: false,
  buildByName: {},
  treeByName: {},
  treeLoading: false,
  uploadFeedbackByName: {},
  selectedPath: null,
  rightTab: "preview",
  searchResults: null,
  searchQuery: "",

  setLoading: (loading) => set({ loading }),

  replaceAll: (kbs) => set({ kbs, loading: false }),

  upsert: (kb) => {
    const kbs = [...get().kbs];
    const idx = kbs.findIndex((k) => k.name === kb.name);
    if (idx >= 0) {
      kbs[idx] = kb;
    } else {
      kbs.push(kb);
    }
    set({ kbs });
  },

  remove: (name) =>
    set((state) => {
      // 删库时同步回收所有按 name 索引的派生状态，避免残留孤儿条目
      const buildByName = { ...state.buildByName };
      const treeByName = { ...state.treeByName };
      const uploadFeedbackByName = { ...state.uploadFeedbackByName };
      delete buildByName[name];
      delete treeByName[name];
      delete uploadFeedbackByName[name];
      return {
        kbs: state.kbs.filter((k) => k.name !== name),
        buildByName,
        treeByName,
        uploadFeedbackByName,
      };
    }),

  setDetail: (detail) => set({ detail, detailLoading: false }),
  setDetailLoading: (loading) => set({ detailLoading: loading }),

  setBuild: (name, state) =>
    set((s) => ({
      buildByName: { ...s.buildByName, [name]: state },
      // 建库中 → 列表状态乐观切 building
      kbs: s.kbs.map((k) => (k.name === name ? { ...k, status: "building" as KBStatus } : k)),
    })),

  clearBuild: (name) =>
    set((s) => {
      const buildByName = { ...s.buildByName };
      delete buildByName[name];
      return { buildByName };
    }),

  setTree: (name, tree) =>
    set((s) => ({ treeByName: { ...s.treeByName, [name]: tree }, treeLoading: false })),
  setTreeLoading: (loading) => set({ treeLoading: loading }),

  setUploadFeedback: (name, feedback) =>
    set((s) => ({ uploadFeedbackByName: { ...s.uploadFeedbackByName, [name]: feedback } })),

  setSelectedPath: (path) => set({ selectedPath: path }),
  setRightTab: (tab) => set({ rightTab: tab }),

  setSearchResults: (results, query) => set({ searchResults: results, searchQuery: query }),

  reset: () =>
    set({
      kbs: [],
      loading: false,
      detail: null,
      detailLoading: false,
      buildByName: {},
      treeByName: {},
      treeLoading: false,
      uploadFeedbackByName: {},
      selectedPath: null,
      rightTab: "preview",
      searchResults: null,
      searchQuery: "",
    }),
}));

// ── 状态徽章映射（返回 variant + labelKey）──────────────────────────

export function kbStatusMeta(
  status: KBStatus,
): { variant: "green" | "blue" | "yellow" | "red" | "default"; labelKey: string } {
  switch (status) {
    case "ready":
      return { variant: "green", labelKey: "kb.status.ready" };
    case "building":
      return { variant: "blue", labelKey: "kb.status.building" };
    case "pending":
      return { variant: "yellow", labelKey: "kb.status.pending" };
    case "failed":
      return { variant: "red", labelKey: "kb.status.failed" };
    case "empty":
    default:
      return { variant: "default", labelKey: "kb.status.empty" };
  }
}
