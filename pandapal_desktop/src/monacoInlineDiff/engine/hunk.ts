import type { DiffEntry, Hunk, HunkType } from "./types";

/**
 * djb2 哈希算法，用于生成 contentKey 的内容指纹段。
 *
 * 选择 djb2 而非较短的 hash 算法的原因：
 *   - 需要足够低的碰撞概率（多文件 + 多 hunk 场景）
 *   - 32-bit 输出转为 base-36，最多 7 字符
 *
 * @param s 要哈希的字符串
 * @returns base-36 编码的哈希字符串
 */
export function hashStr(s: string): string {
  let h = 5381; // djb2 初始值，确保空字符串也有非零 hash
  for (let i = 0; i < s.length; i++)
    h = (((h << 5) + h) ^ s.charCodeAt(i)) | 0;
  return (h >>> 0).toString(36);
}

/**
 * 组装一个 Hunk，并派生其稳定 contentKey。
 *
 * contentKey = `{type}#{origStart}-{origEnd}#{hashDel}#{hashAdd}`：
 *   - **锚点段** `origStart-origEnd`：该 hunk 在 original 中占据的行区间，
 *     唯一且不随 model 变化漂移 —— 身份稳定性的来源；
 *   - **哈希段** `hashDel#hashAdd`：内容指纹，用于识别「同一原始位置、
 *     内容却被 AI 修订过」的情况（此时应视为新 hunk 重新出现）。
 */
function makeHunk(
  type: HunkType,
  entries: DiffEntry[],
  startIdx: number,
  endIdx: number,
  delLines: string[],
  addLines: string[],
  origStart: number,
): Hunk {
  const origEnd = origStart + delLines.length;
  const contentKey =
    `${type}#${origStart}-${origEnd}` +
    `#${hashStr(delLines.join("\n"))}#${hashStr(addLines.join("\n"))}`;
  return {
    id: contentKey,
    contentKey,
    type,
    entries,
    startIdx,
    endIdx,
    origStart,
    origEnd,
    delLines,
    addLines,
  };
}

/**
 * 将 diff entries 分组为 Hunk 数组。
 *
 * 规则：
 *   - 连续的 ctx 条目会被跳过
 *   - 连续的同类型条目（del 或 add）合并为一个 hunk
 *   - 紧邻的 del↔add 序列合并为一个 modify hunk
 *   - 每个 hunk 锚定到它在 original 中占据的行区间 [origStart, origEnd)，
 *     并据此派生稳定唯一的 contentKey（见 {@link makeHunk}）
 *
 * @param entries computeDiff() 的输出
 * @returns Hunk 数组
 */
export function groupHunks(entries: DiffEntry[]): Hunk[] {
  const n = entries.length;

  // 预计算 origBefore[p] = entries[0..p-1] 中「消费原始行」的条目数（ctx + del）。
  // 即位置 p 处、在 original 中已经历过的行数 —— hunk 的起始原始行锚点。
  // add 条目不消费原始行（它在 original 中不存在）。
  const origBefore = new Array<number>(n + 1);
  origBefore[0] = 0;
  for (let p = 0; p < n; p++)
    origBefore[p + 1] = origBefore[p] + (entries[p].kind === "add" ? 0 : 1);

  const hunks: Hunk[] = [];
  let i = 0;

  while (i < n) {
    // 跳过 ctx 条目
    if (entries[i].kind === "ctx") {
      i++;
      continue;
    }

    const start = i;
    const type = entries[i].kind as "del" | "add";

    // 收集连续同类型条目
    while (i < n && entries[i].kind === type) i++;
    const end = i - 1;

    // 同类型块直接后接另一种非 ctx 块（del↔add）→ 合并为 modify hunk
    if (i < n && entries[i].kind !== "ctx" && entries[i].kind !== type) {
      const nextType = entries[i].kind;
      while (i < n && entries[i].kind === nextType) i++;

      const combined = entries.slice(start, i);
      const delTexts = combined
        .filter((e) => e.kind === "del")
        .map((e) => e.text);
      const addTexts = combined
        .filter((e) => e.kind === "add")
        .map((e) => e.text);

      if (delTexts.length > 0 && addTexts.length > 0) {
        // origStart = origBefore[start]：无论 del 先还是 add 先，add 不消费
        // 原始行，故块起点的原始行索引恒为 origBefore[start]。
        hunks.push(
          makeHunk(
            "modify",
            combined,
            start,
            i - 1,
            delTexts,
            addTexts,
            origBefore[start],
          ),
        );
        continue;
      }
      // 回退：不合并，按原始块处理
      i = end + 1;
    }

    // 独立的 del 或 add hunk
    const slice = entries.slice(start, end + 1);
    const texts = slice.map((e) => e.text);
    hunks.push(
      makeHunk(
        type,
        slice,
        start,
        end,
        type === "del" ? texts : [],
        type === "add" ? texts : [],
        origBefore[start],
      ),
    );
  }

  return hunks;
}
