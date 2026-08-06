import type monaco from "monaco-editor";

/**
 * OverlayManager —— 统一管理 Monaco Editor 的 contentWidget 和 viewZone 生命周期。
 *
 * 设计原则：
 *   - 每次 rebuild 全量清除 → 全量重建，不做增量
 *   - 装饰器由 createDecorationsCollection 自行管理，不在此处追踪
 *   - zoneIds 支持 consume → remove → re-add 模式
 *
 * @example
 * ```ts
 * const om = new OverlayManager();
 * om.registerWidget(widget);
 * om.addZoneId(zoneId);
 * // ... 下次 rebuild
 * const prevZones = om.consumeZoneIds();
 * om.clearAll(editor);
 * ```
 */
export class OverlayManager {
  private widgets: monaco.editor.IContentWidget[] = [];
  private zoneIds: string[] = [];

  /** 注册一个 contentWidget */
  registerWidget(w: monaco.editor.IContentWidget): void {
    this.widgets.push(w);
  }

  /** 记录一个 viewZone ID */
  addZoneId(id: string): void {
    this.zoneIds.push(id);
  }

  /**
   * 取出所有 viewZone ID 并清空内部记录。
   * 用于 changeViewZones 回调中先 remove 所有旧 zone，再重新 add 新 zone。
   */
  consumeZoneIds(): string[] {
    const ids = this.zoneIds;
    this.zoneIds = [];
    return ids;
  }

  /**
   * 清除所有注册的 contentWidget。
   * 不会自动清除 viewZone —— viewZone 只能在 changeViewZones 回调中移除。
   */
  clearAll(ed: monaco.editor.IStandaloneCodeEditor): void {
    for (const w of this.widgets) {
      try {
        ed.removeContentWidget(w);
      } catch {
        // 忽略已移除或无效的 widget
      }
    }
    this.widgets = [];
  }
}
