/**
 * pickOriginalCandidate 单元测试（vitest，node 环境，零 mock）。
 * 用例编号与设计文档 pickOriginalCandidate.design.md §8 一一对应。
 * 关键决议：R1「全部有效候选 == suggested」返回 valid[0]（= suggested）而非 null（见设计 §0/§8 用例6）。
 */
import { describe, expect, it } from "vitest";
import { pickOriginalCandidate } from "../editFileOriginalPicker";

/** 固定 seed 的 LCG（mulberry32），保证用例13 确定性来源（设计 §9.5 允许的 fast-check 替代方案）。 */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

describe("pickOriginalCandidate", () => {
  // 用例1: Risk-1 空候选 [P1] + inv-2/inv-4 + D5
  it("用例1: 空数组返回 null", () => {
    expect(pickOriginalCandidate([], "const a = 1;\n")).toBeNull();
  });

  // 用例2: Risk-1 空 original 误报 [P1] + inv-2/inv-4 + D2/D5
  it("用例2: 全无效候选(null/undefined/空串)返回 null，绝不返回空串", () => {
    expect(pickOriginalCandidate([null, undefined, ""], "const a = 1;\n")).toBeNull();
  });

  // 用例3: Risk-2 竞态下正确选「修改前」[P1] + inv-2/inv-3 + D1/D3
  it("用例3: 单个有效候选且 !== suggested 时返回该候选", () => {
    expect(pickOriginalCandidate(["const a = 0;\n"], "const a = 1;\n")).toBe("const a = 0;\n");
  });

  // 用例4: Risk-2 混选正确性 [P1] + inv-3 + D1/D3（find 跳过相等元素）
  it("用例4: 首候选 == suggested、后续存在 !== 时，返回第一个 !== 的候选", () => {
    expect(pickOriginalCandidate(["const a = 1;\n", "const a = 0;\n"], "const a = 1;\n")).toBe(
      "const a = 0;\n",
    );
  });

  // 用例5: Risk-4 连续编辑基线 [P2] + inv-3 顺序语义 + D1/D3
  it("用例5: 多个 !== suggested 的候选时，返回数组序第一个", () => {
    expect(
      pickOriginalCandidate(
        ["const a = 0;\n", "const b = 2;\n", "const a = 1;\n"],
        "const a = 1;\n",
      ),
    ).toBe("const a = 0;\n");
  });

  // 用例6: Risk-3 竞态全败语义 [P1] + inv-4 + D4 + 决议 R1
  it("用例6: 全部有效候选 == suggested 时返回第一个有效候选(= suggested)，而非 null（R1）", () => {
    expect(pickOriginalCandidate(["const a = 1;\n", "const a = 1;\n"], "const a = 1;\n")).toBe(
      "const a = 1;\n",
    );
  });

  // 用例7: Risk-1 + Risk-2 [P1] + inv-2/inv-3 + D2/D3（无效元素透明过滤）
  it("用例7: 无效元素(null/空串/undefined)前置被过滤，返回第一个有效 !== 候选", () => {
    expect(
      pickOriginalCandidate(
        [null, "", undefined, "const a = 0;\n", "const a = 1;\n"],
        "const a = 1;\n",
      ),
    ).toBe("const a = 0;\n");
  });

  // 用例8: Risk-2 混选 + Risk-4 顺序 [P1/P2] + inv-1/inv-3 + D2/D3
  it("用例8: 混合大杂烩返回第一个 !== suggested 的有效候选，且确定性成立", () => {
    const candidates = [
      undefined,
      "const a = 1;\n",
      "const a = 0;\n",
      "",
      "const a = 1;\n",
      "const c = 3;\n",
    ];
    const r = pickOriginalCandidate(candidates, "const a = 1;\n");
    expect(r).toBe("const a = 0;\n");
    // inv-1 确定性：同参数再调一次，返回值不变
    expect(pickOriginalCandidate(candidates, "const a = 1;\n")).toBe(r);
  });

  // 用例9: Risk-5 边界 [P3] + inv-2/inv-3/inv-4 + D1/D4 + 决议 R3（空白串视为有效候选）
  it("用例9: 仅空白串是有效候选（R3）——与 suggested 不同时被选中", () => {
    expect(pickOriginalCandidate(["   ", "const a = 0;\n"], "const a = 1;\n")).toBe("   ");
  });
  it("用例9: 全空白串 == suggested 时走 D4 兜底返回 valid[0]", () => {
    expect(pickOriginalCandidate(["   ", "   "], "   ")).toBe("   ");
  });

  // 用例10: Risk-5 超长 [P3] + inv-1/inv-3 + D1/D3
  it("用例10: MB 级超长串仅差一个字符即判为不同并选中，且确定性成立", () => {
    const suggested = "A".repeat(1_000_000) + "Z";
    const before = "A".repeat(1_000_000) + "Y";
    const r = pickOriginalCandidate([suggested, before], suggested);
    expect(r).toBe(before);
    // inv-1 确定性
    expect(pickOriginalCandidate([suggested, before], suggested)).toBe(r);
  });

  // 用例11: Risk-5 边界 [P3] + inv-3 + 决议 R4（相等判定为严格 ===，行尾/大小写差异视为不同串）
  it("用例11: CRLF 与 LF 视为不同串，CRLF 候选被选中（R4）", () => {
    expect(pickOriginalCandidate(["const a = 1;\r\n", "const a = 1;\n"], "const a = 1;\n")).toBe(
      "const a = 1;\r\n",
    );
  });
  it("用例11: 大小写不同视为不同串，大小写变体被选中（R4）", () => {
    expect(pickOriginalCandidate(["const A = 1;\n"], "const a = 1;\n")).toBe("const A = 1;\n");
  });

  // 用例12: Risk-5 退化 [P3] + inv-1/2/3/4 + D1–D5（suggested='' 不崩溃）
  it("用例12: suggested 为空串时，候选含非空串则命中非空串", () => {
    expect(pickOriginalCandidate(["const a = 0;\n", ""], "")).toBe("const a = 0;\n");
  });
  it("用例12: suggested 为空串且候选全无效时返回 null（不崩溃）", () => {
    expect(pickOriginalCandidate(["", null, undefined], "")).toBeNull();
  });

  // 用例13: [property] inv-1~inv-5 随机输入断言（固定 seed 伪随机 100 次，确定性来源为 mulberry32(20260101)）
  it("用例13: 属性测试 inv-1 确定性 / inv-2 值域 / inv-3 首选 / inv-4 兜底 / inv-5 无副作用", () => {
    const ALPHA = ["const a = 0;\n", "const a = 1;\n", "const b = 2;\n", ""];
    const rnd = mulberry32(20260101);
    for (let i = 0; i < 100; i++) {
      const suggested = ALPHA[Math.floor(rnd() * 3)]; // 非空串
      const len = Math.floor(rnd() * 7); // 长度 0–6
      const candidates: Array<string | null | undefined> = [];
      for (let j = 0; j < len; j++) {
        const pick = Math.floor(rnd() * 5);
        if (pick === 0) candidates.push(null);
        else if (pick === 1) candidates.push(undefined);
        else if (pick === 2) candidates.push("");
        else if (pick === 3) candidates.push(suggested);
        else candidates.push(ALPHA[Math.floor(rnd() * 4)]);
      }

      const before = JSON.stringify(candidates);
      const r = pickOriginalCandidate(candidates, suggested);
      const r2 = pickOriginalCandidate(candidates, suggested);

      const nonEmpty = candidates.filter((c): c is string => typeof c === "string" && c.length > 0);
      const firstDiff = nonEmpty.find((c) => c !== suggested);

      // inv-1 确定性
      expect(r).toBe(r2);
      // inv-2 值域：null ⟺ 无非空串；非 null 必为候选内非空串
      if (nonEmpty.length === 0) {
        expect(r).toBeNull();
      } else {
        expect(r).not.toBeNull();
        expect(typeof r).toBe("string");
        expect(r!.length).toBeGreaterThan(0);
        expect(candidates).toContain(r);
      }
      // inv-3 首选：存在 !== suggested 的有效候选 ⟹ 取数组序第一个
      if (firstDiff !== undefined) {
        expect(r).toBe(firstDiff);
      }
      // inv-4 兜底：无 !== suggested 但有非空串 ⟹ valid[0]；无非空串则 null（已被 inv-2 覆盖）
      if (firstDiff === undefined && nonEmpty.length > 0) {
        expect(r).toBe(nonEmpty[0]);
      }
      // inv-5 无副作用：入参数组未被修改
      expect(JSON.stringify(candidates)).toBe(before);
    }
  });
});
