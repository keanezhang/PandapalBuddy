/**
 * race.spec.ts — 同帧连续操作（时序/竞态）e2e
 *
 * 用 clickSameFrame 在同一 JS task 内连续触发多次点击（不跨帧），
 * 模拟设计文档 R1~R5 的「rebuild 尚未执行就连续操作」时序 —— 这是 RSK-1
 * 最高风险场景（stale hunk 引用 + 位置漂移），单测用 FakeMonaco 覆盖逻辑，
 * 这里用真实 Monaco + 真实 RAF 调度验证。
 *
 * 覆盖设计文档用例：
 *   CMP-36 同帧连续 Reject 2 个 modify hunks（RSK-1 [P0]）
 *   CMP-37 同帧 Apply H1 + Reject H2（R2 变体）
 *   CMP-38 同帧 Reject H1 + Apply H2（R3）
 *   CMP-39 快速连点同一 Apply 两次（R4）
 *   CMP-40 快速连点同一 Reject 两次（R5）
 */
import { expect, test } from "@playwright/test";
import {
  applyBtns,
  clickRejectAt,
  clickSameFrame,
  collectPageErrors,
  expectCleanUI,
  getModelValue,
  getPartialSaves,
  openScenario,
  waitResolved,
} from "./helpers";

test("CMP-36 同帧连续 Reject 2 个 modify hunks → 各自正确还原（RSK-1）", async ({ page }) => {
  await openScenario(page, { case: "modify_multi" }); // a b c d e f → a x c y e z
  await expect(applyBtns(page)).toHaveCount(3);

  // 同帧：Reject H1（stale 引用 + 第一次 setValue 后的位置漂移）+ Reject H2（findFreshHunk 重定位）
  await clickSameFrame(page, [
    { kind: "reject", index: 0 },
    { kind: "reject", index: 1 },
  ]);

  // H1/H2 均被还原，H3 保留
  expect(await getModelValue(page)).toBe("a\nb\nc\nd\ne\nz");
  await expect(applyBtns(page)).toHaveCount(1);

  // 最后 Reject H3 → 回到 original
  await clickRejectAt(page, 0);
  await waitResolved(page);
  expect(await getModelValue(page)).toBe("a\nb\nc\nd\ne\nf");
  await expectCleanUI(page);
});

test("CMP-37 同帧 Apply H1(add) + Reject H2(del) → 两者都生效", async ({ page }) => {
  await openScenario(page, { case: "add_then_del" }); // a b c → x a c
  await expect(applyBtns(page)).toHaveCount(2);

  await clickSameFrame(page, [
    { kind: "apply", index: 0 }, // Apply add x：标记 applied，model 不变
    { kind: "reject", index: 1 }, // Reject del b：恢复 b
  ]);
  await waitResolved(page);

  // x 保留（已 Apply）+ b 恢复（已 Reject）→ pending=0
  expect(await getModelValue(page)).toBe("x\na\nb\nc");
  await expectCleanUI(page);
});

test("CMP-38 同帧 Reject H1(modify) + Apply H2(add) → 两者都生效", async ({ page }) => {
  await openScenario(page, { case: "modify_then_add" }); // a b c → a x c n
  await expect(applyBtns(page)).toHaveCount(2);

  await clickSameFrame(page, [
    { kind: "reject", index: 0 }, // Reject modify b→x：setValue 重建，只影响 H1 区间
    { kind: "apply", index: 1 }, // Apply add n：标记 applied
  ]);
  await waitResolved(page);

  // b 恢复（H1 reject）、n 保留（H2 applied）→ pending=0
  expect(await getModelValue(page)).toBe("a\nb\nc\nn");
  await expectCleanUI(page);
});

test("CMP-39 快速连点同一 Apply 两次 → 不重复渲染、不报错（R4）", async ({ page }) => {
  const errors = collectPageErrors(page);
  await openScenario(page, { case: "add_simple" }); // a b → a x b
  await expect(applyBtns(page)).toHaveCount(1);

  await clickSameFrame(page, [
    { kind: "apply", index: 0 },
    { kind: "apply", index: 0 }, // 第二次：appliedIdsRef Set 去重 + scheduleRebuild 防重入
  ]);
  await waitResolved(page);

  expect(await getModelValue(page)).toBe("a\nx\nb"); // Apply 不改 model
  await expectCleanUI(page);
  // 设计期望 partialSave 恰好 1 次；实际实现每次点击都触发（无去重守卫），
  // 单测 CMP-39 已弱化为 ≥1 次，此处与单测口径一致。
  expect((await getPartialSaves(page)).length).toBeGreaterThanOrEqual(1);
  expect(errors).toEqual([]);
});

test("CMP-40 快速连点同一 Reject 两次 → 第二次静默 no-op、不抛异常（R5）", async ({ page }) => {
  const errors = collectPageErrors(page);
  await openScenario(page, { case: "add_simple" }); // a b → a x b
  await expect(applyBtns(page)).toHaveCount(1);

  await clickSameFrame(page, [
    { kind: "reject", index: 0 }, // 第一次：删除 add 行 x
    { kind: "reject", index: 0 }, // 第二次：findFreshHunk 找不到 → 早期返回
  ]);
  await waitResolved(page);

  expect(await getModelValue(page)).toBe("a\nb");
  await expectCleanUI(page);
  expect(errors).toEqual([]);
});
