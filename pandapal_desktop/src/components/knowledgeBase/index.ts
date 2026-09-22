/**
 * src/components/knowledgeBase/index.ts
 *
 * 知识库文件树 / 预览 / 右键菜单 / 确认弹窗 组件的统一出口。
 */
export { KBFileTree } from "./KBFileTree";
export type { KBFileTreeProps, KBUploadFeedback } from "./KBFileTree";

export { KBPreviewPanel, previewMode, textRendererKind, MAX_TEXT_BYTES } from "./KBPreviewPanel";
export type { KBPreviewPanelProps, KBPreviewDoc } from "./KBPreviewPanel";

export { KBContextMenu } from "./KBContextMenu";
export type { KBContextMenuProps, KbMenuAction } from "./KBContextMenu";

export { ConfirmDialog } from "./ConfirmDialog";
export type { ConfirmDialogProps } from "./ConfirmDialog";

export { KBDropZone, resolveDropDirAt } from "./KBDropZone";
export type { KBDropZoneProps } from "./KBDropZone";

export {
  sortNodes,
  countFiles,
  countFolders,
  findNode,
  findParentPath,
  subtreeHasMatch,
  filterTree,
} from "./treeUtils";

