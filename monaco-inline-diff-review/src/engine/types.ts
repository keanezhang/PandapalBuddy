/** 单条 diff 条目：context（未变）/ del（原始有建议无）/ add（原始无建议有） */
export interface DiffEntry {
  kind: "ctx" | "del" | "add";
  text: string;
}

/** Hunk 类型 */
export type HunkType = "del" | "add" | "modify";

/**
 * 一个 diff 块（Hunk）。
 *
 * 由连续的同类型 DiffEntry 组成（del/add 相邻时会合并为 modify）。
 *
 * 身份坐标系：hunk 通过它在 **不可变的 original 文本** 中占据的行区间
 * `[origStart, origEnd)` 锚定。original 永不改变，因此该锚点：
 *   - **唯一**：不同 hunk 占据的原始行不相交，纯 add 的插入缝隙也互不相同；
 *   - **稳定**：Reject 其它 hunk 会改动 model，但绝不改动本 hunk 对应的原始行，
 *     故锚点（进而 contentKey）永不漂移。
 * 这取代了早期基于「diff entries 下标」的 key —— 后者随 model 变化而漂移。
 */
export interface Hunk {
  /** 唯一标识，等于 contentKey */
  id: string;
  /**
   * 稳定 key，用于 applied tracking。
   * 格式：`{type}#{origStart}-{origEnd}#{hashDel}#{hashAdd}`。
   * 锚点段保证跨 model 变化稳定 + 唯一；哈希段用于识别「同位置不同内容」
   * （AI 修订建议时同一原始位置换了新内容，应重新出现）。
   */
  contentKey: string;
  /** hunk 类型 */
  type: HunkType;
  /** 组成该 hunk 的所有 diff entries */
  entries: DiffEntry[];
  /**
   * 在当前 diff entries 数组中的起始索引。
   * ⚠️ 仅用于「本次 rebuild/reject 内」的渲染定位（计算行号），
   * 随 model 变化漂移，**不得用于跨操作的身份标识**——那是 contentKey 的职责。
   */
  startIdx: number;
  /** 在 entries 数组中的结束索引（同 startIdx，仅供本次渲染定位） */
  endIdx: number;
  /** 该 hunk 在 original 中占据的起始行索引（0-based，稳定锚点） */
  origStart: number;
  /** 该 hunk 在 original 中占据的结束行索引（不含，= origStart + delLines.length） */
  origEnd: number;
  /** 被删除的行（del/modify 类型有值） */
  delLines: string[];
  /** 新增的行（add/modify 类型有值） */
  addLines: string[];
}
