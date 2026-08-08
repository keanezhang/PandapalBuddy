/**
 * src/components/ui/Modal.tsx
 *
 * 全局模态框：封装 .modal-overlay / .modal 系列类（SECTION 21）。
 * - 点击遮罩关闭（面板内 stopPropagation）
 * - width 为动态值，允许内联；颜色/圆角/层级全部走 token（--z-modal）
 * - className 可与 .modal 组合做尺寸覆写（如 .pet-store）
 */

import type { ReactNode } from "react";

interface ModalProps {
  title?: ReactNode;
  /** 标题右侧附加内容（计数、状态徽章等） */
  headerExtra?: ReactNode;
  footer?: ReactNode;
  onClose: () => void;
  /** 面板宽度（动态值，如 560 / "min(760px, 92vw)"） */
  width?: number | string;
  /** 附加到 .modal 面板上的修饰类 */
  className?: string;
  /** 免 .modal-body 包裹：调用方完全自控内部布局（如 PetStore 的工具栏/滚动区/分页） */
  bare?: boolean;
  children: ReactNode;
}

export function Modal({
  title,
  headerExtra,
  footer,
  onClose,
  width,
  className = "",
  bare = false,
  children,
}: ModalProps) {
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className={["modal", className].filter(Boolean).join(" ")}
        style={width !== undefined ? { width } : undefined}
        onClick={(e) => e.stopPropagation()}
      >
        {(title || headerExtra) && (
          <div className="modal-header">
            <span className="modal-title">{title}</span>
            {headerExtra}
            <div style={{ flex: 1 }} />
            <button className="modal-close" onClick={onClose} title="关闭">
              ✕
            </button>
          </div>
        )}
        {bare ? children : <div className="modal-body">{children}</div>}
        {footer && <div className="modal-footer">{footer}</div>}
      </div>
    </div>
  );
}
