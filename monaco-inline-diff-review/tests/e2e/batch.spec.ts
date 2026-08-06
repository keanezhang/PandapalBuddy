/**
 * batch.spec.ts — Apply All / Reject All 批量操作 e2e
 *
 * 覆盖设计文档用例：
 *   CMP-15 Apply 部分 → Apply All
 *   CMP-17 Reject All 混合路径：部分已 Apply → 逐个 rejectHunk（RSK-4 + B5）
 *   CMP-18 Reject All 在全部已 Apply 时 → 无操作
 *   CMP-46 Reject 部分 → Apply All（B2）
 *   CMP-48 Reject 部分 → Reject All（B6：快速路径整体回滚）
 *   CMP-49 Reject All 后无 diff → 不进入无限循环（B7）
 *
 * ⚠️ CMP-47 实际行为：applyAll / rejectAll 快速路径都不触发 rebuild、不清理 hunk UI，
 *    由父级收到 onAllResolved 后卸载组件。相关断言如实记录该行为。
 */
import { expect, test } from "@playwright/test";
import {
  applyBtns,
  clickApplyAll,
  clickApplyAt,
  clickRejectAll,
  clickRejectAt,
  collectPageErrors,
  expectCleanUI,
  getModelValue,
  getPartialSaves,
  getResolvedEvents,
  openScenario,
  waitRebuild,
  waitResolved,
} from "./helpers";

const ORIGINAL = [
  "def alpha():",
  "    return 1",
  "",
  "def beta():",
  "    return 2",
  "",
  "def gamma():",
  "    return 3",
].join("\n");

const CURRENT = ORIGINAL.replace("return 1", "return 100")
  .replace("return 2", "return 200")
  .replace("return 3", "return 300");

/** H1 已 Apply、H2/H3 被 Reject 后的 model */
const AFTER_CMP17 = [
  "def alpha():",
  "    return 100",
  "",
  "def beta():",
  "    return 2",
  "",
  "def gamma():",
  "    return 3",
].join("\n");

test("CMP-15 Apply 部分 → Apply All → resolved，model 保持 current", async ({ page }) => {
  await openScenario(page, { case: "three_funcs" }); // three_funcs
  await expect(applyBtns(page)).toHaveCount(3);

  await clickApplyAt(page, 0); // 先单独 Apply H1
  await expect(applyBtns(page)).toHaveCount(2);

  await clickApplyAll(page);
  await waitResolved(page);

  // Apply All 不改 model；resolved 内容 === 当前 model
  expect(await getModelValue(page)).toBe(CURRENT);
  const resolved = await getResolvedEvents(page);
  expect(resolved).toHaveLength(1);
  expect(resolved[0].content).toBe(CURRENT);

  // scheduleRebuild → pending=0 → 清空所有 diff UI
  await expectCleanUI(page);
});

test("CMP-17 部分已 Apply → Reject All → 混合路径：仅未 Apply 的被 reject（RSK-4 + B5）", async ({
  page,
}) => {
  await openScenario(page, { case: "three_funcs" }); // three_funcs
  await clickApplyAt(page, 0); // Apply H1（return 1 → 100）

  await clickRejectAll(page);
  await waitResolved(page);

  // H2/H3 被 reject（恢复 return 2/3），H1 的修改保留（return 100）
  expect(await getModelValue(page)).toBe(AFTER_CMP17);

  // 混合路径经 scheduleRebuild → rebuild：pending=0 → UI 清空 + onAllResolved
  await expectCleanUI(page);
  const resolved = await getResolvedEvents(page);
  expect(resolved).toHaveLength(1);
  expect(resolved[0].content).toBe(AFTER_CMP17);
});

test("CMP-18 全部已 Apply → Reject All → 无操作（不回滚、无事件）", async ({ page }) => {
  const errors = collectPageErrors(page);
  await openScenario(page, { case: "three_funcs" }); // three_funcs

  await clickApplyAll(page);
  await waitResolved(page);
  expect(await getResolvedEvents(page)).toHaveLength(1);
  await expectCleanUI(page);

  // 操作栏已消失：Reject All 按钮不存在
  await expect(page.locator(".mid-btn-reject-all")).toBeHidden();
  expect(await getModelValue(page)).toBe(CURRENT);
  expect(await getResolvedEvents(page)).toHaveLength(1);
  expect(errors).toEqual([]);
});

test("CMP-46 Reject 部分 → Apply All → resolved 携带当前 model（B2）", async ({ page }) => {
  await openScenario(page, { case: "mixed_three" }); // a b c d e f → x a b d y f
  await expect(applyBtns(page)).toHaveCount(3);

  await clickRejectAt(page, 0); // Reject add x → model 失去 x
  expect(await getModelValue(page)).toBe("a\nb\nd\ny\nf");
  await expect(applyBtns(page)).toHaveCount(2);

  await clickApplyAll(page);
  await waitResolved(page);

  // Apply All 不改 model：del(c) 与 modify(e→y) 被标记 applied
  expect(await getModelValue(page)).toBe("a\nb\nd\ny\nf");
  const resolved = await getResolvedEvents(page);
  expect(resolved).toHaveLength(1);
  expect(resolved[0].content).toBe("a\nb\nd\ny\nf");
});

test("CMP-48 Reject 部分 → Reject All → 快速路径整体回滚 original（B6）", async ({ page }) => {
  await openScenario(page, { case: "three_funcs" }); // three_funcs

  await clickRejectAt(page, 0); // Reject H1：model 恢复 return 1，还剩 H2/H3
  await expect(applyBtns(page)).toHaveCount(2);

  await clickRejectAll(page);
  await waitResolved(page);

  // 剩余 hunk 全部未 Apply → 快速路径：model 整体替换为 original
  expect(await getModelValue(page)).toBe(ORIGINAL);
  const resolved = await getResolvedEvents(page);
  expect(resolved).toHaveLength(1);
  expect(resolved[0].content).toBe(ORIGINAL);

  // scheduleRebuild → noDiff → 清空所有 diff UI
  await expectCleanUI(page);
});

test("CMP-49 Reject All 后无 diff → 不进入无限循环（B7）", async ({ page }) => {
  const errors = collectPageErrors(page);
  await openScenario(page, { case: "add_simple" }); // a b → a x b
  await expect(applyBtns(page)).toHaveCount(1);

  await clickRejectAll(page);
  await waitResolved(page);
  expect(await getModelValue(page)).toBe("a\nb");
  await expectCleanUI(page);

  // 多等几帧：不得产生重复 onAllResolved，页面保持响应
  await waitRebuild(page);
  await waitRebuild(page);
  expect(await getResolvedEvents(page)).toHaveLength(1);
  expect(await getModelValue(page)).toBe("a\nb");
  expect(errors).toEqual([]);
});
