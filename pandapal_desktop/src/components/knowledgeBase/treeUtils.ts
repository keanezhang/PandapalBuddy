/**
 * src/components/knowledgeBase/treeUtils.ts
 *
 * 知识库文件树的纯函数工具（不依赖 React / Tauri），便于独立单测。
 *
 * 身份口径：`KBDocNode.path` 为相对 `documents_dir` 的 POSIX 路径，库内唯一。
 * 排序口径与后端 `manager._scan_dir` 一致：目录优先 + 名称升序（localeCompare zh）。
 */

import type { KBDocNode } from "../../types/api";

/** 目录优先 + 名称升序，返回新数组（不改原树）。 */
export function sortNodes(nodes: KBDocNode[]): KBDocNode[] {
  return [...nodes].sort((a, b) => {
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
    return a.name.localeCompare(b.name, "zh");
  });
}

/** 递归统计文件数（文件夹不计入）。 */
export function countFiles(nodes: KBDocNode[] | null | undefined): number {
  let n = 0;
  for (const x of nodes ?? []) {
    if (x.is_dir) n += countFiles(x.children);
    else n += 1;
  }
  return n;
}

/** 递归统计文件夹数。 */
export function countFolders(nodes: KBDocNode[] | null | undefined): number {
  let n = 0;
  for (const x of nodes ?? []) {
    if (x.is_dir) {
      n += 1;
      n += countFolders(x.children);
    }
  }
  return n;
}

/** 按相对路径定位节点（DFS，命中即返回）。 */
export function findNode(nodes: KBDocNode[], path: string): KBDocNode | null {
  for (const n of nodes) {
    if (n.path === path) return n;
    if (n.is_dir && n.children) {
      const r = findNode(n.children, path);
      if (r) return r;
    }
  }
  return null;
}

/**
 * 返回某路径项所在父目录的相对路径（根为 ""）。找不到返回 null。
 * 用于「右键 → 新建文件夹 / 上传到此处 / 移到根目录」推导 target_dir。
 */
export function findParentPath(nodes: KBDocNode[], path: string, parent = ""): string | null {
  for (const n of nodes) {
    if (n.path === path) return parent;
    if (n.is_dir && n.children) {
      const r = findParentPath(n.children, path, n.path);
      if (r !== null) return r;
    }
  }
  return null;
}

/** 某节点自身或其后代是否命中查询（子串、大小写不敏感）。 */
export function subtreeHasMatch(node: KBDocNode, q: string): boolean {
  if (node.name.toLowerCase().includes(q)) return true;
  if (node.is_dir) return (node.children ?? []).some((c) => subtreeHasMatch(c, q));
  return false;
}

/**
 * 文件树搜索过滤（纯前端内存过滤，设计 F15 / R4）。
 *
 * 命中规则：保留「自身或后代命中」的节点（祖先链自然保留）；
 * `expandPaths` 返回命中目录路径集合，供前端自动展开。
 * 空查询返回原树（已排序），expandPaths 为空。
 */
export function filterTree(
  nodes: KBDocNode[],
  rawQuery: string,
): { nodes: KBDocNode[]; expandPaths: Set<string> } {
  const q = rawQuery.trim().toLowerCase();
  const expandPaths = new Set<string>();
  if (!q) return { nodes: sortNodes(nodes), expandPaths };

  const walk = (list: KBDocNode[]): KBDocNode[] => {
    const out: KBDocNode[] = [];
    for (const n of sortNodes(list)) {
      if (!subtreeHasMatch(n, q)) continue;
      if (n.is_dir) {
        expandPaths.add(n.path);
        out.push({ ...n, children: walk(n.children ?? []) });
      } else {
        out.push(n);
      }
    }
    return out;
  };

  return { nodes: walk(nodes), expandPaths };
}

