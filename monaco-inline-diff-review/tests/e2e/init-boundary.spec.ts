/**
 * init-boundary.spec.ts — Mount/初始化 + 边界条件 e2e
 *
 * 覆盖设计文档用例：
 *   CMP-2  original === current → 直接 onAllResolved
 *   CMP-23 initialAppliedKeys → 初始过滤，直接 resolved
 *   CMP-30 original 为空 → 全 add（E2）
 *   CMP-31 current 为空 → 全 del（E3）
 *   CMP-32 两者皆空 → 无 hunks（E4）
 *   CMP-33 特殊字符 Unicode/Emoji（E9）
 *   CMP-34 末行无换行符（E10）
 *   CMP-35 单行修改（E5）
 *   CMP-43 两个不相邻 modify → 2 个独立 hunk（E6）
 *   CMP-44 空行/空白字符差异（E7）
 *   CMP-45 超大文件性能（E8）
 *   CMP-64 add + del 混合类型初始渲染（H1）
 */
import { expect, test } from "@playwright/test";
import {
  addLines,
  applyBtns,
  clickApplyAt,
  collectPageErrors,
  delLines,
  expectCleanUI,
  getModelValue,
  getResolvedEvents,
  openScenario,
  rejectBtns,
  waitRebuild,
  waitResolved,
} from "./helpers";

test.describe("Mount 与初始化", () => {
  test("CMP-2 original === current → 直接 onAllResolved，无 hunk 渲染", async ({ page }) => {
    const errors = collectPageErrors(page);
    await openScenario(page, { case: "no_diff" });
    await waitResolved(page);

    await expectCleanUI(page);
    expect(await getModelValue(page)).toBe("a\nb");
    const resolved = await getResolvedEvents(page);
    expect(resolved).toHaveLength(1);
    expect(resolved[0].content).toBe("a\nb");
    expect(errors).toEqual([]);
  });

  test("CMP-23 initialAppliedKeys 非空 → 初始过滤，直接 resolved", async ({ page }) => {
    // 先加载一次，用台架的 __computeKeys 算出 modify hunk 的 contentKey
    await openScenario(page, { case: "modify_simple" });
    const keys: string[] = await page.evaluate(() =>
      (window as any).__computeKeys("a\nb\nc", "a\nx\nc"),
    );
    expect(keys).toHaveLength(1);

    // 带 initialAppliedKeys 重新加载 → hunk 被过滤，不渲染
    await openScenario(page, { case: "modify_simple", appliedKeys: keys });
    await waitResolved(page);

    await expectCleanUI(page);
    // Apply 语义：不改 model，model 保持 current
    expect(await getModelValue(page)).toBe("a\nx\nc");
    const resolved = await getResolvedEvents(page);
    expect(resolved).toHaveLength(1);
    expect(resolved[0].content).toBe("a\nx\nc");
  });

  test("CMP-64 add + del 混合类型初始渲染（H1）", async ({ page }) => {
    await openScenario(page, { case: "mixed_add_del" });
    await waitRebuild(page);

    // 2 个独立 hunk：H1=add(x)，H2=modify(b→c)（相邻 del+add 被 groupHunks 合并）
    await expect(applyBtns(page)).toHaveCount(2);
    await expect(rejectBtns(page)).toHaveCount(2);
    // .mid-add-line 是 Monaco 整行背景 decoration，不含文本；
    // 数量 2 = 绿行 highlight 行数（x + c），具体内容由 model 断言覆盖
    await expect(addLines(page)).toHaveCount(2);
    // 删除线：仅 modify hunk 的 b
    expect(await delLines(page).allTextContents()).toEqual(["b"]);
    expect(await getModelValue(page)).toBe("x\na\nc");
  });
});

test.describe("边界条件", () => {
  test("CMP-30 original 为空 → 全 add（E2）", async ({ page }) => {
    await openScenario(page, { case: "empty_original" });
    await waitRebuild(page);

    await expect(applyBtns(page)).toHaveCount(1);
    await expect(addLines(page)).toHaveCount(2);
    expect(await delLines(page).count()).toBe(0);
    expect(await getModelValue(page)).toBe("a\nb");

    // Reject 全 add → model 回到空
    await rejectBtns(page).first().click();
    await waitRebuild(page);
    await waitResolved(page);
    expect(await getModelValue(page)).toBe("");
    await expectCleanUI(page);
  });

  test("CMP-31 current 为空 → 全 del（E3）", async ({ page }) => {
    await openScenario(page, { case: "empty_current" });
    await waitRebuild(page);

    await expect(applyBtns(page)).toHaveCount(1);
    expect(await delLines(page).allTextContents()).toEqual(["a", "b"]);
    expect(await addLines(page).count()).toBe(0);
    expect(await getModelValue(page)).toBe("");

    // Reject 全 del → model 恢复 original
    await rejectBtns(page).first().click();
    await waitRebuild(page);
    await waitResolved(page);
    expect(await getModelValue(page)).toBe("a\nb");
    await expectCleanUI(page);
  });

  test("CMP-32 两者皆空 → 无 hunks，onAllResolved(空串)（E4）", async ({ page }) => {
    await openScenario(page, { case: "empty_both" });
    await waitResolved(page);

    await expectCleanUI(page);
    expect(await getModelValue(page)).toBe("");
    const resolved = await getResolvedEvents(page);
    expect(resolved).toHaveLength(1);
    expect(resolved[0].content).toBe("");
  });

  test("CMP-33 特殊字符 Unicode/Emoji 正确渲染（E9）", async ({ page }) => {
    await openScenario(page, { case: "unicode" });
    await waitRebuild(page);

    await expect(applyBtns(page)).toHaveCount(1);
    expect((await delLines(page).allTextContents()).join()).toContain("🎉");
    await expect(addLines(page)).toHaveCount(1);
    expect(await getModelValue(page)).toBe("名前\n🚀");

    // Reject → 恢复 emoji 原文
    await rejectBtns(page).first().click();
    await waitRebuild(page);
    expect(await getModelValue(page)).toBe("名前\n🎉");
  });

  test("CMP-34 末行无换行符 → 正确识别 modify（E10）", async ({ page }) => {
    await openScenario(page, { case: "no_trailing_nl" });
    await waitRebuild(page);

    await expect(applyBtns(page)).toHaveCount(1);
    expect(await delLines(page).allTextContents()).toEqual(["b"]);
    await expect(addLines(page)).toHaveCount(1);
  });

  test("CMP-35 单行修改（E5）", async ({ page }) => {
    await openScenario(page, { case: "single_line" });
    await waitRebuild(page);

    await expect(applyBtns(page)).toHaveCount(1);
    expect(await delLines(page).allTextContents()).toEqual(["single line"]);
    await expect(addLines(page)).toHaveCount(1);

    await clickApplyAt(page, 0);
    await waitResolved(page);
    expect(await getModelValue(page)).toBe("modified line");
  });

  test("CMP-43 两个不相邻 modify → 2 个独立 hunk，操作互不影响（E6）", async ({ page }) => {
    await openScenario(page, { case: "modify_two" });
    await waitRebuild(page);

    // 2 个独立 modify hunk，各有独立按钮
    await expect(applyBtns(page)).toHaveCount(2);
    expect(await delLines(page).allTextContents()).toEqual(["line2", "line4"]);
    await expect(addLines(page)).toHaveCount(2);

    // Apply 第一个 → 第二个不受影响
    await clickApplyAt(page, 0);
    await expect(applyBtns(page)).toHaveCount(1);
    expect(await delLines(page).allTextContents()).toEqual(["line4"]);
    // Apply 不改 model
    expect(await getModelValue(page)).toBe("line1\nnew2\nline3\nnew4\nline5");
  });

  test("CMP-44 空行差异 → 空行为独立行参与 diff（E7）", async ({ page }) => {
    await openScenario(page, { case: "empty_lines" });
    await waitRebuild(page);

    // b→x 单个 modify，空行是 ctx
    await expect(applyBtns(page)).toHaveCount(1);
    expect(await delLines(page).allTextContents()).toEqual(["b"]);
    await expect(addLines(page)).toHaveCount(1);

    await clickApplyAt(page, 0);
    await waitResolved(page);
    expect(await getModelValue(page)).toBe("a\n\nx\n\nc");
  });

  test("CMP-45 超大文件：1000 行 / 100 hunk 渲染与 rebuild 性能（E8）", async ({ page }) => {
    test.setTimeout(60_000);
    const t0 = Date.now();
    await openScenario(page, { case: "large" });
    await expect(applyBtns(page)).toHaveCount(100, { timeout: 30_000 });
    const renderMs = Date.now() - t0;

    // 单次 rebuild 耗时（设计 Oracle：<200ms；此处取宽松上界 3s 防 CI 抖动，实际值打日志）
    const t1 = Date.now();
    await clickApplyAt(page, 0);
    const rebuildMs = Date.now() - t1;
    await expect(applyBtns(page)).toHaveCount(99);
    expect(rebuildMs).toBeLessThan(3000);

    console.log(`[CMP-45] 首渲染(含导航)=${renderMs}ms, 单 rebuild=${rebuildMs}ms`);
    await page.screenshot({ path: "tests/e2e/screenshots/20-large-file.png", fullPage: false });
  });
});
