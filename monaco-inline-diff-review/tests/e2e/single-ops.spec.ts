/**
 * single-ops.spec.ts — 单 hunk Apply/Reject + 回调契约 e2e
 *
 * 覆盖设计文档用例：
 *   CMP-3  Apply add hunk（INV-4 Apply 不改 model）
 *   CMP-5  Apply del hunk
 *   CMP-6  Reject add hunk → 行被删除
 *   CMP-7  Reject del hunk → 行被恢复
 *   CMP-26 Apply → onPartialSave(mv, hunkKey≠"")
 *   CMP-27 Reject → onPartialSave(mv, "")
 *   CMP-28 最后一个 hunk 处理后 → onAllResolved，不再触发 onPartialSave
 *   CMP-29 回调为 undefined → 不报错
 */
import { expect, test } from "@playwright/test";
import {
  applyBtns,
  clickApplyAt,
  clickRejectAt,
  collectPageErrors,
  expectCleanUI,
  getModelValue,
  getPartialSaves,
  getResolvedEvents,
  openScenario,
  waitResolved,
} from "./helpers";

test.describe("单 hunk Apply（INV-4：不改 model）", () => {
  test("CMP-3 Apply add hunk → hunk 消失，model 不变，回调契约正确", async ({ page }) => {
    await openScenario(page, { case: "add_simple" });
    await expect(applyBtns(page)).toHaveCount(1);

    await clickApplyAt(page, 0);
    await waitResolved(page);

    // INV-4：Apply 不改 model，仍为 current
    expect(await getModelValue(page)).toBe("a\nx\nb");
    await expectCleanUI(page);

    // CMP-26：partialSave 恰好 1 次，hunkKey 非空
    const saves = await getPartialSaves(page);
    expect(saves).toHaveLength(1);
    expect(saves[0].content).toBe("a\nx\nb");
    expect(saves[0].hunkKey).toBeTruthy();

    // CMP-28：pending=0 → onAllResolved，之后不再有 partialSave
    const resolved = await getResolvedEvents(page);
    expect(resolved).toHaveLength(1);
    expect(resolved[0].content).toBe("a\nx\nb");
    expect(resolved[0].at).toBeGreaterThanOrEqual(saves[0].at);
    expect(await getPartialSaves(page)).toHaveLength(1);
  });

  test("CMP-5 Apply del hunk → hunk 消失，model 不变", async ({ page }) => {
    await openScenario(page, { case: "del_simple" });
    await expect(applyBtns(page)).toHaveCount(1);

    await clickApplyAt(page, 0);
    await waitResolved(page);

    expect(await getModelValue(page)).toBe("a\nc");
    await expectCleanUI(page);
    expect((await getResolvedEvents(page))[0].content).toBe("a\nc");
  });
});

test.describe("单 hunk Reject（INV-3：回到 original）", () => {
  test("CMP-6 Reject add hunk → 新增行被删除", async ({ page }) => {
    await openScenario(page, { case: "add_simple" });

    await clickRejectAt(page, 0);
    await waitResolved(page);

    expect(await getModelValue(page)).toBe("a\nb");
    await expectCleanUI(page);

    // CMP-27：Reject 的 partialSave hunkKey 为空串
    const saves = await getPartialSaves(page);
    expect(saves).toHaveLength(1);
    expect(saves[0].content).toBe("a\nb");
    expect(saves[0].hunkKey).toBe("");

    // Reject 最后一个 → resolved 内容 == original
    expect((await getResolvedEvents(page))[0].content).toBe("a\nb");
  });

  test("CMP-7 Reject del hunk → 被删行恢复", async ({ page }) => {
    await openScenario(page, { case: "del_simple" });

    await clickRejectAt(page, 0);
    await waitResolved(page);

    expect(await getModelValue(page)).toBe("a\nb\nc");
    await expectCleanUI(page);
  });
});

test.describe("回调契约", () => {
  test("CMP-29 不传回调 → 操作正常完成，无异常", async ({ page }) => {
    const errors = collectPageErrors(page);
    await openScenario(page, { case: "add_simple", noCallbacks: true });

    await clickApplyAt(page, 0);
    await expectCleanUI(page);
    expect(await getModelValue(page)).toBe("a\nx\nb");
    expect(errors).toEqual([]);
  });
});
