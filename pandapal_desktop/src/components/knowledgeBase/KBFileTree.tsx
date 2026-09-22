/**
 * src/components/knowledgeBase/KBFileTree.tsx
 *
 * 知识库文件树面板（对齐 docs/design/knowledge-base-ui-prototype.html 的 .tree-pane）。
 *
 * 职责：搜索过滤（F15）+ 递归渲染 + 展开/折叠 + 内联重命名 + 树内拖拽移动（F13）
 *      + 外部文件拖拽落点标记（F14，实际落盘由 KBDropZone 经 Tauri 事件触发）。
 *
 * 身份：以 `KBDocNode.path`（相对 documents_dir 的 POSIX 路径）为唯一 key。
 * 展开态是前端本地 state（不入后端，见设计 §10.2）。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import type { KBDocNode } from "../../types/api";
import { filterTree, countFiles, countFolders } from "./treeUtils";
import { fileIcon } from "../fileRenderers";

export interface KBUploadFeedback {
  uploaded: { name: string; suffix: string }[];
  rejected: { name: string; reason: string }[];
}

export interface KBFileTreeProps {
  tree: KBDocNode[];
  selectedPath: string | null;
  searchQuery: string;
  onSearchChange: (q: string) => void;
  onSelect: (node: KBDocNode) => void;
  onContextMenu: (node: KBDocNode, x: number, y: number) => void;
  /** 树内拖拽移动：sourcePath 移动到 targetDir（"" = 根） */
  onMoveNode: (sourcePath: string, targetDir: string) => void;
  /** 内联重命名提交（文件 / 文件夹） */
  onRename: (node: KBDocNode, newName: string) => void;
  /** 外部文件拖拽悬停目标（KBDropZone 提供），用于视觉高亮 */
  externalDropTarget?: string | null;
  /** 上传反馈（KB_DOCUMENTS_CHANGED） */
  uploadFeedback?: KBUploadFeedback | null;
  /** 外部请求进入重命名态（右键菜单 → 重命名），token 变化即触发 */
  renameRequest?: { path: string; token: number } | null;
}

const INDENT = 15;
const BASE_PAD = 6;

export function KBFileTree({
  tree,
  selectedPath,
  searchQuery,
  onSearchChange,
  onSelect,
  onContextMenu,
  onMoveNode,
  onRename,
  externalDropTarget,
  uploadFeedback,
  renameRequest,
}: KBFileTreeProps) {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [renamingPath, setRenamingPath] = useState<string | null>(null);
  const [draggingPath, setDraggingPath] = useState<string | null>(null);
  const [dragOverPath, setDragOverPath] = useState<string | null>(null);
  const [hoverPath, setHoverPath] = useState<string | null>(null);
  const committedRef = useRef(false);

  // 外部（右键菜单）请求进入重命名态
  useEffect(() => {
    if (renameRequest) {
      committedRef.current = false;
      setRenamingPath(renameRequest.path);
    }
    // 仅 token 变化触发
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [renameRequest?.token]);

  const searching = searchQuery.trim().length > 0;
  const { nodes: viewNodes, expandPaths } = useMemo(
    () => filterTree(tree, searchQuery),
    [tree, searchQuery],
  );

  const isExpanded = (path: string) => (searching ? expandPaths.has(path) : expanded.has(path));

  const toggle = (path: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const commitRename = (node: KBDocNode, value: string) => {
    if (committedRef.current) return;
    committedRef.current = true;
    const v = (value || "").trim();
    setRenamingPath(null);
    if (!v || v === node.name) return;
    onRename(node, v);
  };

  const cancelRename = () => {
    committedRef.current = true;
    setRenamingPath(null);
  };

  const handleDrop = (targetDir: string) => {
    setDragOverPath(null);
    const src = draggingPath;
    setDraggingPath(null);
    if (!src) return;
    if (src === targetDir) return;
    onMoveNode(src, targetDir);
  };

  const renderRow = (node: KBDocNode, parentPath: string, depth: number): React.ReactNode => {
    const isFolder = node.is_dir;
    const expandedNow = isFolder && isExpanded(node.path);
    const selected = selectedPath === node.path;
    const renaming = renamingPath === node.path;
    const dropTargetDir = isFolder ? node.path : parentPath;
    const isDropHl =
      (dragOverPath !== null && dragOverPath === dropTargetDir) ||
      (externalDropTarget != null && externalDropTarget === dropTargetDir);

    return (
      <div key={node.path}>
        <div
          data-testid={`kb-node-${node.path}`}
          data-drop-dir={dropTargetDir}
          data-kind={isFolder ? "folder" : "file"}
          draggable
          onClick={() => {
            if (isFolder) {
              toggle(node.path);
              onSelect(node);
            } else {
              onSelect(node);
            }
          }}
          onDoubleClick={() => isFolder && toggle(node.path)}
          onContextMenu={(e) => {
            e.preventDefault();
            onContextMenu(node, e.clientX, e.clientY);
          }}
          onMouseEnter={() => setHoverPath(node.path)}
          onMouseLeave={() => setHoverPath((p) => (p === node.path ? null : p))}
          onDragStart={(e) => {
            setDraggingPath(node.path);
            try {
              e.dataTransfer.setData("text/kbnode", node.path);
            } catch {
              /* noop */
            }
            e.dataTransfer.effectAllowed = "move";
          }}
          onDragEnd={() => {
            setDraggingPath(null);
            setDragOverPath(null);
          }}
          onDragOver={(e) => {
            if (!draggingPath) return;
            // 阻止冒泡到容器（否则容器会把落点重置为根目录）
            e.preventDefault();
            e.stopPropagation();
            e.dataTransfer.dropEffect = "move";
            setDragOverPath(dropTargetDir);
          }}
          onDrop={(e) => {
            if (!draggingPath) return;
            // 阻止冒泡：命中行即落该行目标，绝不再由容器追加一次「移到根」的 handleDrop("")
            e.preventDefault();
            e.stopPropagation();
            handleDrop(dropTargetDir);
          }}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 5,
            padding: `4px 8px 4px ${BASE_PAD + depth * INDENT}px`,
            borderRadius: 6,
            cursor: "pointer",
            userSelect: "none",
            opacity: draggingPath === node.path ? 0.45 : 1,
            background: isDropHl
              ? "color-mix(in srgb, var(--accent) 24%, transparent)"
              : selected
                ? "color-mix(in srgb, var(--accent) 16%, transparent)"
                : hoverPath === node.path
                  ? "var(--bg-hover)"
                  : "transparent",
            outline: isDropHl ? "1px dashed var(--accent)" : "none",
            outlineOffset: -1,
          }}
        >
          <span
            onClick={(e) => {
              if (!isFolder) return;
              e.stopPropagation();
              toggle(node.path);
            }}
            style={{ width: 16, display: "inline-flex", justifyContent: "center", color: "var(--text-tertiary)" }}
          >
            {isFolder ? (expandedNow ? "▾" : "▸") : ""}
          </span>
          <span style={{ display: "inline-flex", color: isFolder ? "var(--accent)" : "var(--text-tertiary)" }}>
            {isFolder ? (expandedNow ? "📂" : "📁") : fileIcon(node.name)}
          </span>
          {renaming ? (
            <input
              data-testid={`kb-rename-input-${node.path}`}
              autoFocus
              defaultValue={node.name}
              onClick={(e) => e.stopPropagation()}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  commitRename(node, (e.target as HTMLInputElement).value);
                } else if (e.key === "Escape") {
                  e.preventDefault();
                  cancelRename();
                }
              }}
              onBlur={(e) => commitRename(node, e.target.value)}
              style={{
                flex: 1,
                minWidth: 0,
                background: "var(--bg-elevated)",
                border: "1px solid var(--accent)",
                borderRadius: 4,
                color: "var(--text-primary)",
                fontSize: "var(--text-sm)",
                padding: "1px 5px",
                outline: "none",
              }}
            />
          ) : (
            <span
              style={{
                flex: 1,
                minWidth: 0,
                fontSize: "var(--text-sm)",
                color: "var(--text-primary)",
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
              }}
              title={node.path}
            >
              {node.name}
            </span>
          )}
          <span
            role="button"
            title="更多操作"
            onClick={(e) => {
              e.stopPropagation();
              const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
              onContextMenu(node, r.left, r.bottom + 4);
            }}
            style={{
              opacity: hoverPath === node.path ? 1 : 0,
              color: "var(--text-tertiary)",
              padding: "0 2px",
              cursor: "pointer",
            }}
          >
            ⋯
          </span>
        </div>
        {isFolder && expandedNow && (
          <div>{sortList(node.children ?? []).map((c) => renderRow(c, node.path, depth + 1))}</div>
        )}
      </div>
    );
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", minHeight: 0, flex: 1 }}>
      {/* 搜索 */}
      <div style={{ padding: "10px 12px 8px", borderBottom: "1px solid var(--border-subtle)" }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 7,
            background: "var(--bg-elevated)",
            border: "1px solid var(--border-subtle)",
            borderRadius: "var(--radius-sm)",
            padding: "0 10px",
            color: "var(--text-tertiary)",
          }}
        >
          <span>🔍</span>
          <input
            data-testid="kb-tree-search"
            value={searchQuery}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder="搜索文件 / 文件夹"
            style={{
              flex: 1,
              background: "none",
              border: "none",
              outline: "none",
              color: "var(--text-primary)",
              fontSize: "var(--text-sm)",
              padding: "7px 0",
            }}
          />
        </div>
        {uploadFeedback && (uploadFeedback.uploaded.length > 0 || uploadFeedback.rejected.length > 0) && (
          <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 5 }}>
            {uploadFeedback.uploaded.map((f, i) => (
              <div key={`ok-${i}`} style={fbOk}>
                ✓ 已上传 {f.name}
              </div>
            ))}
            {uploadFeedback.rejected.map((f, i) => (
              <div key={`bad-${i}`} style={fbBad}>
                ⚠ 已跳过 {f.name}（{f.reason}）
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 树 */}
      <div
        data-testid="kb-tree-scroll"
        data-drop-dir=""
        onDragOver={(e) => {
          if (draggingPath) {
            e.preventDefault();
            setDragOverPath("");
          }
        }}
        onDrop={(e) => {
          if (draggingPath) {
            e.preventDefault();
            handleDrop("");
          }
        }}
        style={{ flex: 1, overflow: "auto", padding: "6px 6px 20px", minHeight: 0 }}
      >
        {viewNodes.length === 0 ? (
          <div style={{ padding: "26px 12px", color: "var(--text-tertiary)", fontSize: "var(--text-sm)", textAlign: "center", lineHeight: 1.7 }}>
            {searching ? "无匹配文件" : "还没有文件，拖拽或上传文档开始"}
          </div>
        ) : (
          sortList(viewNodes).map((n) => renderRow(n, "", 0))
        )}
      </div>

      {/* 底部统计 */}
      <div
        style={{
          padding: "8px 12px",
          borderTop: "1px solid var(--border-subtle)",
          color: "var(--text-tertiary)",
          fontSize: "var(--text-xs)",
          display: "flex",
          justifyContent: "space-between",
        }}
      >
        <span>{countFiles(tree)} 个文档</span>
        <span>{countFolders(tree)} 个文件夹</span>
      </div>
    </div>
  );
}

function sortList(nodes: KBDocNode[]): KBDocNode[] {
  return [...nodes].sort((a, b) => {
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
    return a.name.localeCompare(b.name, "zh");
  });
}

const fbOk: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  fontSize: "var(--text-xs)",
  padding: "6px 9px",
  borderRadius: "var(--radius-sm)",
  background: "color-mix(in srgb, var(--success, #22c55e) 12%, transparent)",
  color: "var(--success, #22c55e)",
};

const fbBad: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  fontSize: "var(--text-xs)",
  padding: "6px 9px",
  borderRadius: "var(--radius-sm)",
  background: "color-mix(in srgb, var(--danger, #ef4444) 12%, transparent)",
  color: "var(--danger, #ef4444)",
};

