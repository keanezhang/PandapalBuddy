/**
 * props.spec.ts — props 变化 + unmount 生命周期 e2e
 *
 * 覆盖设计文档用例：
 *   CMP-20 original prop 变化 → appliedIdsRef 重置 → 已 Apply 的 hunk 复活（INV-7 + RSK-5）
 *   CMP-21 current prop 变化（≠ model）→ setValue + rebuild
 *   CMP-22 current prop 等值 → resolved 终态不被破坏（「仅 rebuild」分支由单测覆盖）
 *   CMP-24 original + current 同时变化 → 以新 props 为准（P6）
 *   CMP-51 Apply All 后切换文件（original/current 同时换）→ 状态完全重置（B10）
 *   CMP-52 Reject All 后经 props 变化回到无 diff → rebuild 检测 origVal===curVal（B11）
 *   CMP-42 unmount → OverlayManager.clearAll 调用，无泄漏无异常（RSK-7）
 */
import { expect, test } from "@playwright/test";
import {
  addLines,
  applyBtns,
  clickApplyAll,
  clickApplyAt,
  clickRejectAll,
  collectPageErrors,
  expectCleanUI,
  getModelValue,
  getResolvedEvents,
  openScenario,
  setProps,
  waitRebuild,
  waitResolved,
} from "./helpers";

test("CMP-20 original prop 变化 → appliedIdsRef 重置 → 已 Apply 的 hunk 复活", async ({ page }) => {
  await openScenario(page, { case: "add_simple" }); // a b → a x b
  await clickApplyAt(page, 0); // Apply 唯一的 add hunk → resolved、UI 清空
  await waitResolved(page);
  await expectCleanUI(page);

  // 父级换 original（current 不变，且 model === current → RSK-5 条件成立，触发 rebuild）
  await setProps(page, { original: "q\nb" });

  // appliedIdsRef 已重置 → 对新 original 的 diff 重新渲染（q → a,x 的 modify hunk）
  await expect(applyBtns(page)).toHaveCount(1);
  expect(await getModelValue(page)).toBe("a\nx\nb"); // model 不动
});

test("CMP-21 current prop 变化（≠ model）→ setValue + rebuild", async ({ page }) => {
  await openScenario(page, { case: "add_simple" }); // a b → a x b
  await expect(applyBtns(page)).toHaveCount(1);

  await setProps(page, { current: "a\ny\nb" });

  // model 被 setValue 为新 current，并以新 current 重新 diff
  expect(await getModelValue(page)).toBe("a\ny\nb");
  await expect(applyBtns(page)).toHaveCount(1);
  await expect(addLines(page)).toHaveCount(1);
});

test("CMP-22 current prop 等值传入 → resolved 终态不被破坏", async ({ page }) => {
  const errors = collectPageErrors(page);
  await openScenario(page, { case: "add_simple" });
  await clickApplyAt(page, 0);
  await waitResolved(page);
  await expectCleanUI(page);

  // 等值 current：React 依赖相同，effect 不触发（「仅 rebuild」分支由单测 CMP-22 覆盖）。
  // e2e 层面验证：等值 props 不破坏已 resolved 的终态。
  await setProps(page, { current: "a\nx\nb" });
  expect(await getModelValue(page)).toBe("a\nx\nb");
  await expectCleanUI(page);
  expect(await getResolvedEvents(page)).toHaveLength(1);
  expect(errors).toEqual([]);
});

test("CMP-24 original + current 同时变化 → 以新 props 为准（P6）", async ({ page }) => {
  await openScenario(page, { case: "add_simple" }); // a b → a x b

  // 一次 setState 同时换两个 props
  await setProps(page, { original: "p\nq", current: "p\nr\nq" });

  // 最终状态完全由新 props 决定：model = 新 current，diff = 新 original vs 新 current
  expect(await getModelValue(page)).toBe("p\nr\nq");
  await expect(applyBtns(page)).toHaveCount(1);
  await expect(addLines(page)).toHaveCount(1);
});

test("CMP-51 Apply All 后切换文件 → applied 状态完全重置（B10）", async ({ page }) => {
  await openScenario(page, { case: "add_simple" }); // a b → a x b
  await clickApplyAll(page);
  await waitResolved(page);

  // 模拟父级切换到另一个文件：original/current 同时更换
  await setProps(page, { original: "x\ny", current: "x\nz\ny" });

  // original effect 重置 appliedIdsRef → 新文件的 hunk 正常渲染，之前 Apply All 的状态不残留
  expect(await getModelValue(page)).toBe("x\nz\ny");
  await expect(applyBtns(page)).toHaveCount(1);
  await expect(addLines(page)).toHaveCount(1);
});

test("CMP-52 Reject All 后经 props 回到无 diff → rebuild 检测 origVal===curVal（B11 变体）", async ({
  page,
}) => {
  const errors = collectPageErrors(page);
  await openScenario(page, { case: "add_simple" }); // a b → a x b

  await clickRejectAll(page); // 快速路径：model → original，直接 resolved
  await waitResolved(page);
  expect(await getModelValue(page)).toBe("a\nb");

  // 父级先传入一个不同的 current（触发 setValue + rebuild，出现新 hunk）
  await setProps(page, { current: "a\ny\nb" });
  expect(await getModelValue(page)).toBe("a\ny\nb");
  await expect(applyBtns(page)).toHaveCount(1);

  // 再传回与 original 相同的 current → setValue 后 rebuild：origVal===curVal →
  // 直接 onAllResolved，无 hunk，不进入循环
  await setProps(page, { current: "a\nb" });
  expect(await getModelValue(page)).toBe("a\nb");
  await expectCleanUI(page);
  expect((await getResolvedEvents(page)).length).toBeGreaterThanOrEqual(2);
  expect(errors).toEqual([]);
});

test("CMP-42 unmount → clearAll 清理，无泄漏无异常；重新 mount 恢复初始状态", async ({ page }) => {
  const errors = collectPageErrors(page);
  await openScenario(page, { case: "three_funcs" });
  await expect(applyBtns(page)).toHaveCount(3);

  // 卸载组件
  await page.locator("#toggle-mount").click();
  await waitRebuild(page);
  await expect(page.locator(".view-lines")).toHaveCount(0);
  await expect(applyBtns(page)).toHaveCount(0);
  expect(errors).toEqual([]);

  // 重新挂载 → 初始状态完整恢复（3 个 hunk，无 applied 残留）
  await page.locator("#toggle-mount").click();
  await page.waitForSelector(".view-lines", { timeout: 30_000 });
  await expect(applyBtns(page)).toHaveCount(3);
  expect(errors).toEqual([]);
});
