/**
 * editDiffReconstructor 单元测试（vitest，node 环境，零 mock）。
 *
 * 用例 diff 数据由 scripts/gen_diff_samples.py 用 Python difflib 真实生成
 * （与后端 pandaren/tools/file_tool/edit_file.py 的 _compute_diff 同源），
 * 保证 hunk 格式与真实事件一致（difflib 默认 n=3 上下文、count=1 省略、
 * 新增文件 oldStart=0、CRLF 行尾保留、无换行末尾连写等）。
 */
import { describe, expect, it } from "vitest";
import {
  extractUnifiedDiff,
  parseUnifiedDiff,
  reconstructOriginal,
  reconstructOriginalFromResult,
} from "../editDiffReconstructor";

// ── 真实 difflib 样例（gen_diff_samples.py 输出）──

/** CASE1：单行替换，n=3 上下文 */
const ORIGINAL_1 = [
  "line1",
  "line2",
  "line3",
  "你是一位资深架构师。用户想要设计的模块是：**$ARGUMENTS**",
  "line5",
  "line6",
  "line7",
].join("\n") + "\n";
const SUGGESTED_1 = ORIGINAL_1.replace("**$ARGUMENTS**", "**$ARGUMENTS**（diff 显示验证）");
const DIFF_1 =
  "--- a/C:/test/file.md\n" +
  "+++ b/C:/test/file.md\n" +
  "@@ -1,7 +1,7 @@\n" +
  " line1\n line2\n line3\n" +
  "-你是一位资深架构师。用户想要设计的模块是：**$ARGUMENTS**\n" +
  "+你是一位资深架构师。用户想要设计的模块是：**$ARGUMENTS**（diff 显示验证）\n" +
  " line5\n line6\n line7\n";

/** CASE2：多 hunk（两处独立修改，30 行文件） */
const ORIGINAL_2 = Array.from({ length: 30 }, (_, i) => `line${i + 1}`).join("\n") + "\n";
const SUGGESTED_2 = ORIGINAL_2.replace("line5\n", "line5x\n").replace("line25\n", "line25y\n");
const DIFF_2 =
  "--- a/tmp/f.txt\n" +
  "+++ b/tmp/f.txt\n" +
  "@@ -2,7 +2,7 @@\n" +
  " line2\n line3\n line4\n" +
  "-line5\n" +
  "+line5x\n" +
  " line6\n line7\n line8\n" +
  "@@ -22,7 +22,7 @@\n" +
  " line22\n line23\n line24\n" +
  "-line25\n" +
  "+line25y\n" +
  " line26\n line27\n line28\n";

/** CASE3：新增文件（oldStart=0，oldLines 为空） */
const SUGGESTED_3 = "hello\nworld\n";
const DIFF_3 = "--- a/new.txt\n+++ b/new.txt\n@@ -0,0 +1,2 @@\n+hello\n+world\n";

/** CASE4：CRLF 行尾（b→B 替换） */
const SUGGESTED_4 = "a\r\nB\r\nc\r\n";
const ORIGINAL_4 = "a\r\nb\r\nc\r\n";
const DIFF_4 =
  "--- a/crlf.txt\n+++ b/crlf.txt\n@@ -1,3 +1,3 @@\n a\r\n-b\r\n+B\r\n c\r\n";

/** CASE5：文件末尾无换行（difflib 连写 -bbb+BBB，parse 计数不符 → 明确限制为 null） */
const DIFF_5 = "--- a/nonl.txt\n+++ b/nonl.txt\n@@ -1,2 +1,2 @@\n aaa\n-bbb+BBB";

/** CASE6：删除 3 行 + 新增 1 行 */
const ORIGINAL_6 = Array.from({ length: 12 }, (_, i) => `l${i + 1}`).join("\n") + "\n";
const SUGGESTED_6 = ORIGINAL_6.replace("l4\nl5\nl6\n", "l4b\n");
const DIFF_6 =
  "--- a/del.txt\n+++ b/del.txt\n" +
  "@@ -1,9 +1,7 @@\n l1\n l2\n l3\n-l4\n-l5\n-l6\n+l4b\n l7\n l8\n l9\n";

/** CASE7：count=1 省略形式（@@ -3 +3,2 @@：old 侧 hunk 恰好 1 行，省略 ,1） */
const ORIGINAL_7 = ["l1", "l2", "old3", "l4"].join("\n") + "\n";
const SUGGESTED_7 = ["l1", "l2", "new3", "new3b", "l4"].join("\n") + "\n";
const DIFF_7 =
  "--- a/omit.txt\n+++ b/omit.txt\n@@ -3 +3,2 @@\n-old3\n+new3\n+new3b\n";

describe("extractUnifiedDiff", () => {
  // 用例1 [P1]: 从 EditResult 格式化字符串提取纯 diff（✅ 前缀 + 统计行 + 空行在前）
  it("用例1: 从带 ✅ 已编辑前缀的完整 result 中提取 diff", () => {
    const full =
      "✅ 已编辑：C:/test/file.md（替换 1 处）\n" +
      "   变更：+1 行  -1 行\n\n" +
      DIFF_1;
    expect(extractUnifiedDiff(full)).toBe(DIFF_1);
  });
  // 用例2 [P1]: 空 / undefined / 无 --- 头 → null
  it("用例2: null/undefined/空串/无 --- 头 → null", () => {
    expect(extractUnifiedDiff(null)).toBeNull();
    expect(extractUnifiedDiff(undefined)).toBeNull();
    expect(extractUnifiedDiff("")).toBeNull();
    expect(extractUnifiedDiff("纯文本没有 diff 头")).toBeNull();
  });
  // 用例3 [P2]: result 直接就是 diff（无前缀）也可提取
  it("用例3: result 本身就是纯 diff 时原样返回", () => {
    expect(extractUnifiedDiff(DIFF_1)).toBe(DIFF_1);
  });
});

describe("parseUnifiedDiff", () => {
  // 用例4 [P1]: 单 hunk 解析（old/new 行数与 @@ 声明一致）
  it("用例4: 单 hunk 计数正确（CASE1）", () => {
    const hunks = parseUnifiedDiff(DIFF_1);
    expect(hunks).not.toBeNull();
    expect(hunks!.length).toBe(1);
    expect(hunks![0].oldStart).toBe(1);
    expect(hunks![0].newStart).toBe(1);
    expect(hunks![0].oldLines.length).toBe(7);
    expect(hunks![0].newLines.length).toBe(7);
  });
  // 用例5 [P1]: 多 hunk 顺序保持
  it("用例5: 多 hunk 全部解析且顺序保持（CASE2）", () => {
    const hunks = parseUnifiedDiff(DIFF_2);
    expect(hunks).not.toBeNull();
    expect(hunks!.length).toBe(2);
    expect(hunks![0].oldStart).toBe(2);
    expect(hunks![1].oldStart).toBe(22);
  });
  // 用例6 [P1]: count=1 省略形式（@@ -3 +3,2 @@）
  it("用例6: old 单行省略 ,1（CASE7）", () => {
    const hunks = parseUnifiedDiff(DIFF_7);
    expect(hunks).not.toBeNull();
    expect(hunks![0].oldLines.length).toBe(1);
    expect(hunks![0].newLines.length).toBe(2);
    expect(reconstructOriginal(SUGGESTED_7, DIFF_7)).toBe(ORIGINAL_7);
  });
  // 用例7 [P1]: 新增文件 oldStart=0、oldLines 为空
  it("用例7: 新增文件（oldStart=0）解析（CASE3）", () => {
    const hunks = parseUnifiedDiff(DIFF_3);
    expect(hunks).not.toBeNull();
    expect(hunks![0].oldStart).toBe(0);
    expect(hunks![0].oldLines.length).toBe(0);
    expect(hunks![0].newLines.length).toBe(2);
  });
  // 用例8 [P1]: hunk 头格式异常 → null
  it("用例8: hunk 头格式异常 → null", () => {
    expect(parseUnifiedDiff("@@ 垃圾内容 @@\n line1\n")).toBeNull();
  });
  // 用例9 [P1]: 截断标记 → null（difflib 200 行截断）
  it("用例9: 截断标记(...) → null", () => {
    const truncated = DIFF_1 + "... (diff 已截断)";
    expect(parseUnifiedDiff(truncated)).toBeNull();
  });
  // 用例10 [P2]: 无换行末尾连写（-bbb+BBB）计数不符 → null（文档化限制）
  it("用例10: 无换行末尾连写（CASE5）→ null（明确限制，fallback 兜底）", () => {
    expect(parseUnifiedDiff(DIFF_5)).toBeNull();
  });
});

describe("reconstructOriginal", () => {
  // 用例11 [P1]: 端到端单行替换反推 === 真实修改前内容
  it("用例11: 单行替换反推精确还原（CASE1）", () => {
    expect(reconstructOriginal(SUGGESTED_1, DIFF_1)).toBe(ORIGINAL_1);
  });
  // 用例12 [P1]: 多 hunk 从后往前逆应用，行号不漂移
  it("用例12: 多 hunk 反推精确还原（CASE2）", () => {
    expect(reconstructOriginal(SUGGESTED_2, DIFF_2)).toBe(ORIGINAL_2);
  });
  // 用例13 [P1]: 新增文件反推为空串（模块行为；上层对空 original 有跳过保护）
  it("用例13: 新增文件反推为空串（CASE3）", () => {
    expect(reconstructOriginal(SUGGESTED_3, DIFF_3)).toBe("");
  });
  // 用例14 [P1]: CRLF 行尾保留（行尾不漂移）
  it("用例14: CRLF 行尾反推保留 \\r\\n（CASE4）", () => {
    expect(reconstructOriginal(SUGGESTED_4, DIFF_4)).toBe(ORIGINAL_4);
  });
  // 用例15 [P1]: 删除+新增混合
  it("用例15: 删除 3 行 + 新增 1 行反推还原（CASE6）", () => {
    expect(reconstructOriginal(SUGGESTED_6, DIFF_6)).toBe(ORIGINAL_6);
  });
  // 用例16 [P1]: suggested 与 diff 不符（磁盘被外部修改）→ null
  it("用例16: suggested 内容与 diff 不符 → null（防误推）", () => {
    const tampered = SUGGESTED_1.replace("line5", "line5TAMPERED");
    expect(reconstructOriginal(tampered, DIFF_1)).toBeNull();
  });
  // 用例17 [P1]: 行号越界 → null
  it("用例17: newStart 越界 → null", () => {
    const diff = "--- a/x\n+++ b/x\n@@ -100,3 +100,3 @@\n a\n b\n c\n";
    expect(reconstructOriginal("a\nb\nc\n", diff)).toBeNull();
  });
  // 用例18 [P2]: 截断 diff → null
  it("用例18: 截断 diff 反推 → null", () => {
    expect(reconstructOriginal(SUGGESTED_1, DIFF_1 + "... (diff 已截断)")).toBeNull();
  });
});

describe("reconstructOriginalFromResult", () => {
  // 用例19 [P1]: 组合入口——带 ✅ 前缀 result → 精确还原
  it("用例19: 完整 result（✅ 前缀）→ 还原 original（CASE1）", () => {
    const full =
      "✅ 已编辑：C:/test/file.md（替换 1 处）\n" +
      "   变更：+1 行  -1 行\n\n" +
      DIFF_1;
    expect(reconstructOriginalFromResult(SUGGESTED_1, full)).toBe(ORIGINAL_1);
  });
  // 用例20 [P1]: result 为 null/无 diff → null
  it("用例20: result 无 diff → null", () => {
    expect(reconstructOriginalFromResult(SUGGESTED_1, null)).toBeNull();
    expect(reconstructOriginalFromResult(SUGGESTED_1, "没有 diff 的内容")).toBeNull();
  });
  // 用例21 [P2]: 组合入口同样防误推
  it("用例21: suggested 不符时组合入口 → null", () => {
    const full = "✅ 已编辑\n\n" + DIFF_1;
    expect(reconstructOriginalFromResult("tampered\ncontent\n", full)).toBeNull();
  });
});
