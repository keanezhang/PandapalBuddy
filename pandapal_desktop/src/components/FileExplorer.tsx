/**
 * src/components/FileExplorer.tsx
 *
 * 编码模式下的工作目录文件树。
 *
 * 数据来源：
 *   - 根目录  ← useWorkspaceStore.current（用户「打开的文件夹」）
 *   - 树数据  ← useFileStore.fileTree（loadFileTree 递归构建，已跳过 node_modules/.git 等）
 *   - 点击文件 ← loadAndOpenFile(path)：读入内容并在 FileViewerPanel 中打开（自动展开查看器）
 *
 * 说明：fileTree 已由 fileStore 一次性递归加载完成，本组件的展开/折叠纯属 UI 状态，
 *       不再触发磁盘 IO；因此默认所有目录折叠，只展示顶层条目（类 IDE 行为）。
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useWorkspaceStore } from "../store/workspaceStore";
import { useFileStore, type FileNode } from "../store/fileStore";
import { fileIcon } from "./fileRenderers/fileTypes";

/* ── 单个树节点（递归）──────────────────────────────────────────────── */
function FileTreeNode({ node, depth }: { node: FileNode; depth: number }) {
  const [expanded, setExpanded] = useState(false);
  const loadAndOpenFile = useFileStore((s) => s.loadAndOpenFile);
  const activeFileId = useFileStore((s) => s.activeFileId);

  const indent = 8 + depth * 12;

  if (node.isDirectory) {
    const hasChildren = !!node.children && node.children.length > 0;
    return (
      <>
        <div
          className="file-tree-row"
          style={{ paddingLeft: indent }}
          onClick={() => setExpanded((e) => !e)}
          title={node.name}
        >
          <span className="file-tree-caret">{hasChildren ? (expanded ? "▾" : "▸") : ""}</span>
          <span className="file-tree-icon">{expanded ? "📂" : "📁"}</span>
          <span className="file-tree-name">{node.name}</span>
        </div>
        {expanded && node.children?.map((c) => (
          <FileTreeNode key={c.path} node={c} depth={depth + 1} />
        ))}
      </>
    );
  }

  const isActive = activeFileId === node.path;
  return (
    <div
      className={`file-tree-row${isActive ? " active" : ""}`}
      style={{ paddingLeft: indent + 14 /* 对齐目录的 caret 宽度 */ }}
      onClick={() => void loadAndOpenFile(node.path)}
      title={node.name}
    >
      <span className="file-tree-icon">{fileIcon(node.name)}</span>
      <span className="file-tree-name">{node.name}</span>
    </div>
  );
}

/* ── 文件树入口 ─────────────────────────────────────────────────────── */
export function FileExplorer() {
  const { t } = useTranslation();
  const current = useWorkspaceStore((s) => s.current);
  const fileTree = useFileStore((s) => s.fileTree);
  const loading = useFileStore((s) => s.fileTreeLoading);
  const loadFileTree = useFileStore((s) => s.loadFileTree);

  // 工作目录变化时重新加载文件树
  useEffect(() => {
    if (current) void loadFileTree(current);
  }, [current, loadFileTree]);

  if (!current) {
    return <div className="file-tree-empty">{t("fileExplorer.noWorkspace")}</div>;
  }
  if (loading && fileTree.length === 0) {
    return <div className="file-tree-empty"><span className="skills-loading-dot" /> {t("fileExplorer.loading")}</div>;
  }
  if (fileTree.length === 0) {
    return <div className="file-tree-empty">{t("fileExplorer.empty")}</div>;
  }

  return (
    <div className="file-tree">
      {fileTree.map((node) => (
        <FileTreeNode key={node.path} node={node} depth={0} />
      ))}
    </div>
  );
}
