/**
 * kbErrorMessages.test.ts — 知识库错误码 → 用户可读文案映射（I 组用例 49）
 *
 * 设计 §7 原将本用例标为 Known-Gap G1（KB_ERROR_MESSAGES 未导出）；实现侧已改为
 * `export const KB_ERROR_MESSAGES`（BackendProvider.tsx:139）→ 本用例按设计期望正常断言，
 * 不做 it.fails 降级。
 */
import { describe, it, expect } from "vitest";
import { KB_ERROR_MESSAGES } from "../../../providers/BackendProvider";

const GOLDEN: Record<string, string> = {
  kb_not_found: "知识库不存在",
  kb_busy: "索引构建中，请稍后",
  invalid_path: "非法路径",
  path_not_found: "目标不存在，已刷新",
  invalid_name: "名称不合法",
  name_conflict: "同级已存在同名项",
  invalid_target: "不能移动到自身子目录",
  io_error: "文件操作失败",
};

describe("KB_ERROR_MESSAGES", () => {
  // inv-26 + R10 [P1]
  it("用例49 8 个错误码 → 8 条互异非空中文文案，且与 golden 一致", () => {
    const keys = Object.keys(KB_ERROR_MESSAGES);

    expect(keys).toHaveLength(8);
    expect(new Set(keys)).toEqual(new Set(Object.keys(GOLDEN)));

    const values = keys.map((k) => KB_ERROR_MESSAGES[k]);
    for (const v of values) {
      expect(typeof v).toBe("string");
      expect(v.length).toBeGreaterThan(0);
    }
    expect(new Set(values).size).toBe(8); // 两两互异，防串值

    for (const [code, msg] of Object.entries(GOLDEN)) {
      expect(KB_ERROR_MESSAGES[code]).toBe(msg);
    }
  });
});
