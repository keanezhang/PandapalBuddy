/**
 * sequences.spec.ts — 多 hunk 顺序操作 e2e（跨帧，每步后等 rebuild 完成）
 *
 * 覆盖设计文档用例：
 *   CMP-11 Reject H1 → Reject H2（modify 位置漂移后仍正确，RSK-1 跨帧版）
 *   CMP-12 Apply H1(add) → Reject H2(del)（RSK-3）
 *   CMP-13 Reject 最后一个 hunk → 设计期望复活【已知差距，test.fail 标记】
 *   CMP-53 多个 add hunk 分别操作（A3）
 *   CMP-54 多个 del hunk 逐个 Reject（D5）
 *   CMP-55 add + del + modify 三种类型混合（H2，修正版数据）
 *   CMP-56 Apply 中间 hunk 后其他按钮位置不漂移（S3，真实像素断言）
 *   CMP-57 Reject add → Reject del（S6）
 *   CMP-58 Reject del → Reject add（S7，修正版数据）
 *   CMP-59 Reject H1 → Apply H2（S9）
 *   CMP-60 Reject → Apply → Reject 三步连续混合（S10）
 *   CMP-61 Apply H1 → Reject 相邻 H2（S11）
 *   CMP-62 H1 已 Apply + Reject H2（X3，modify setValue 保留已 Apply 修改）
 *   CMP-63 相邻 modify 共享 context（X5，修正：实际合并为 1 个 hunk）
 *
 * ⚠️ 设计文档数据修正说明：
 *   - CMP-55 原数据 "a\nb\nc\nd"→"x\na\ny\nd" 的 del(b) 与 del(c)+add(y) 相邻，会被
 *     groupHunks 合并成 1 个 modify，得不到「3 个独立 hunk」。改用 ctx 行隔开的数据。
 *   - CMP-58 原数据 "a\nb"→"a\nx" 相邻 del+add 同样合并为 modify，改用 "a\nb\nc"→"a\nc\nx"。
 *   - CMP-63 原前提「两个相邻 modify」在 groupHunks 下必然合并为 1 个 hunk，按真实行为断言。
 */
import { expect, test } from "@playwright/test";
import {
  addLines,
  applyBtns,
  btnTopY,
  clickApplyAt,
  clickRejectAt,
  delLines,
  modifyAddLines,
  expectCleanUI,
  getModelValue,
  openScenario,
  rejectBtns,
  waitRebuild,
  waitResolved,
} from "./helpers";

test("CMP-11 跨帧连续 Reject 两个 modify → 位置漂移后仍各自正确还原", async ({ page }) => {
  await openScenario(page, { case: "modify_multi" }); // a b c d e f → a x c y e z
  await expect(applyBtns(page)).toHaveCount(3);

  await clickRejectAt(page, 0); // b→x 还原
  expect(await getModelValue(page)).toBe("a\nb\nc\ny\ne\nz");
  await expect(applyBtns(page)).toHaveCount(2);

  await clickRejectAt(page, 0); // d→y 还原（findFreshHunk 在新 model 上重新定位）
  expect(await getModelValue(page)).toBe("a\nb\nc\nd\ne\nz");
  await expect(applyBtns(page)).toHaveCount(1);

  // H3 保留，最终 Reject 后回到 original
  await clickRejectAt(page, 0);
  await waitResolved(page);
  expect(await getModelValue(page)).toBe("a\nb\nc\nd\ne\nf");
  await expectCleanUI(page);
});

test("CMP-12 Apply H1(add) → Reject H2(del) → 两者都正确", async ({ page }) => {
  await openScenario(page, { case: "add_then_del" }); // a b c → x a c
  await expect(applyBtns(page)).toHaveCount(2);

  await clickApplyAt(page, 0); // Apply add x：model 不变
  expect(await getModelValue(page)).toBe("x\na\nc");
  await expect(applyBtns(page)).toHaveCount(1);

  await clickRejectAt(page, 0); // Reject del b：恢复 b
  await waitResolved(page);
  expect(await getModelValue(page)).toBe("x\na\nb\nc");
  await expectCleanUI(page);
});

// 设计 CMP-13/S12 期望：Reject 最后一个 hunk → rebuild 后 hunk 重新出现 → 可再 Apply。
// 实际实现：Reject 后 model==original → diff 无 hunk → onAllResolved，hunk 不复活。
// 单测 CMP-13 因此被弱化为只断言 onAllResolved。此处用 test.fail 记录差距：
// 行为若未来被修复，本测试会「意外通过」并报警。
test.fail(
  "CMP-13 Reject 最后一个 hunk 后设计期望 hunk 复活（当前实现不复活，已知差距）",
  async ({ page }) => {
    await openScenario(page, { case: "modify_simple" });
    await clickRejectAt(page, 0);
    expect(await getModelValue(page)).toBe("a\nb\nc");
    // 设计期望：hunk 重新出现
    await expect(applyBtns(page)).toHaveCount(1);
  },
);

test("CMP-53 多个 add hunk：Apply H1 → Reject H2 互不影响（A3）", async ({ page }) => {
  await openScenario(page, { case: "add_multi" }); // a b → x a y b
  await expect(applyBtns(page)).toHaveCount(2);

  await clickApplyAt(page, 0); // Apply x：model 不变，H2 仍在
  expect(await getModelValue(page)).toBe("x\na\ny\nb");
  await expect(applyBtns(page)).toHaveCount(1);

  await clickRejectAt(page, 0); // Reject y：行 y 从 model 删除
  await waitResolved(page);
  expect(await getModelValue(page)).toBe("x\na\nb");
  await expectCleanUI(page);
});

test("CMP-54 多个 del hunk：逐个 Reject 正确恢复（D5）", async ({ page }) => {
  // 数据修正：设计原文 "a\nb\nc\nd"→"a\nd" 中 del b/del c 相邻，groupHunks 合并为 1 个
  // hunk。改用 ctx c 隔开的 "a\nb\nc\nd\ne"→"a\nc\ne"，得到 2 个独立 del hunk。
  await openScenario(page, { case: "del_multi" }); // a b c d e → a c e
  await expect(applyBtns(page)).toHaveCount(2);

  await clickRejectAt(page, 0); // 恢复 b
  expect(await getModelValue(page)).toBe("a\nb\nc\ne");
  await expect(applyBtns(page)).toHaveCount(1);

  await clickRejectAt(page, 0); // 恢复 d（findFreshHunk 在新 model 上重新定位）
  await waitResolved(page);
  expect(await getModelValue(page)).toBe("a\nb\nc\nd\ne");
  await expectCleanUI(page);
});

test("CMP-55 add + del + modify 三种类型混合渲染与操作（H2）", async ({ page }) => {
  await openScenario(page, { case: "mixed_three" }); // a b c d e f → x a b d y f
  await waitRebuild(page);

  // 3 个独立 hunk：add(x)、del(c)、modify(e→y)
  await expect(applyBtns(page)).toHaveCount(3);
  // 注意：.mid-add-line 是 Monaco 整行背景 decoration，不含文本；
  // 用 count 验证 highlight 行数：x 纯 add（绿），y 为 modify 新增行（黄）
  await expect(addLines(page)).toHaveCount(1);
  await expect(modifyAddLines(page)).toHaveCount(1);
  expect(await delLines(page).allTextContents()).toEqual(["c", "e"]);

  // Apply add → 只剩 del/modify
  await clickApplyAt(page, 0);
  await expect(applyBtns(page)).toHaveCount(2);
  expect(await getModelValue(page)).toBe("x\na\nb\nd\ny\nf");

  // Reject del(c) → c 恢复，modify 状态不变
  await clickRejectAt(page, 0);
  expect(await getModelValue(page)).toBe("x\na\nb\nc\nd\ny\nf");
  await expect(applyBtns(page)).toHaveCount(1);
  expect(await delLines(page).allTextContents()).toEqual(["e"]);
});

test("CMP-56 Apply 中间 hunk → 上方按钮不动，下方按钮因 UI 回收上移（S3）", async ({ page }) => {
  await openScenario(page, { case: "add_three" }); // a b c → x a y b z c
  await expect(applyBtns(page)).toHaveCount(3);

  // 记录 Apply 前的按钮位置 + H2 按钮条高度（Apply 后该 zone 移除）
  const y1Before = await btnTopY(applyBtns(page).nth(0));
  const y3Before = await btnTopY(applyBtns(page).nth(2));
  const h2Bar = page.locator(".mid-zone-btn-bar").nth(1);
  const h2Height = (await h2Bar.boundingBox())!.height;

  await clickApplyAt(page, 1); // Apply 中间的 y（Apply 不改 model）

  // H1 在上方，下方 UI 变化不影响它
  await expect(applyBtns(page)).toHaveCount(2);
  const y1After = await btnTopY(applyBtns(page).nth(0));
  const y3After = await btnTopY(applyBtns(page).nth(1));

  // 上方按钮（H1）位置不变（±2px）
  expect(Math.abs(y1After - y1Before)).toBeLessThanOrEqual(2);

  // H3：Apply 后 H2 的 view zone（按钮条）被移除，H3 位置等幅上移。
  // 这是 DOM 的正常回收行为；若位置不漂移才反常（hole/gap）。
  const shift = y3Before - y3After;
  expect(Math.abs(shift - h2Height)).toBeLessThanOrEqual(3);
});

test("CMP-57 Reject add → Reject del：model 行数减少后 del 定位仍正确（S6）", async ({ page }) => {
  await openScenario(page, { case: "add_then_del" }); // a b c → x a c
  await expect(applyBtns(page)).toHaveCount(2);

  await clickRejectAt(page, 0); // Reject add x：3 行 → 2 行
  expect(await getModelValue(page)).toBe("a\nc");

  await clickRejectAt(page, 0); // Reject del b：afterLine 基于当前 model 计算
  await waitResolved(page);
  expect(await getModelValue(page)).toBe("a\nb\nc");
  await expectCleanUI(page);
});

test("CMP-58 Reject del → Reject add：model 行数增加后 add 定位仍正确（S7）", async ({ page }) => {
  await openScenario(page, { case: "del_then_add" }); // a b c → a c x
  await expect(applyBtns(page)).toHaveCount(2);

  await clickRejectAt(page, 0); // Reject del b：恢复 b（2 行 → 3 行）
  expect(await getModelValue(page)).toBe("a\nb\nc\nx");

  await clickRejectAt(page, 0); // Reject add x：删除末尾 x
  await waitResolved(page);
  expect(await getModelValue(page)).toBe("a\nb\nc");
  await expectCleanUI(page);
});

test("CMP-59 Reject modify → Apply add（S9 跨帧混合）", async ({ page }) => {
  await openScenario(page, { case: "modify_then_add" }); // a b c → a x c n
  await expect(applyBtns(page)).toHaveCount(2);

  await clickRejectAt(page, 0); // Reject modify b→x
  expect(await getModelValue(page)).toBe("a\nb\nc\nn");
  await expect(applyBtns(page)).toHaveCount(1); // add n 仍在

  await clickApplyAt(page, 0); // Apply add n
  await waitResolved(page);
  expect(await getModelValue(page)).toBe("a\nb\nc\nn");
  await expectCleanUI(page);
});

test("CMP-60 Reject → Apply → Reject 三步连续混合（S10）", async ({ page }) => {
  await openScenario(page, { case: "mixed_three" }); // a b c d e f → x a b d y f
  await expect(applyBtns(page)).toHaveCount(3);

  await clickRejectAt(page, 0); // Reject add x
  expect(await getModelValue(page)).toBe("a\nb\nd\ny\nf");
  await expect(applyBtns(page)).toHaveCount(2);

  await clickApplyAt(page, 0); // Apply del c（接受删除 c）
  expect(await getModelValue(page)).toBe("a\nb\nd\ny\nf");
  await expect(applyBtns(page)).toHaveCount(1);

  await clickRejectAt(page, 0); // Reject modify e→y
  await waitResolved(page);
  // 最终：x 被删、c 保持删除（已 Apply）、e 恢复
  expect(await getModelValue(page)).toBe("a\nb\nd\ne\nf");
  await expectCleanUI(page);
});

test("CMP-61 Apply H1 → Reject 相邻 H2：H1 区域不受影响（S11）", async ({ page }) => {
  await openScenario(page, { case: "modify_multi" }); // a b c d e f → a x c y e z

  await clickApplyAt(page, 0); // Apply H1(b→x)
  expect(await getModelValue(page)).toBe("a\nx\nc\ny\ne\nz");

  await clickRejectAt(page, 0); // Reject H2(d→y)
  expect(await getModelValue(page)).toBe("a\nx\nc\nd\ne\nz"); // H1 的 x 保留
  await expect(applyBtns(page)).toHaveCount(1); // H3 仍 pending
  expect(await delLines(page).allTextContents()).toEqual(["f"]);
});

test("CMP-62 H1 已 Apply + Reject H2：modify setValue 保留已 Apply 修改（X3）", async ({ page }) => {
  await openScenario(page, { case: "two_modify" }); // a b c d e → a x c y e

  await clickApplyAt(page, 0); // Apply H1(b→x)
  await clickRejectAt(page, 0); // Reject H2(d→y)：setValue 全量替换，H1 修改保留
  await waitResolved(page);

  expect(await getModelValue(page)).toBe("a\nx\nc\nd\ne");
  await expectCleanUI(page);
});

test("CMP-63 相邻 modify 合并为 1 个 hunk，Reject 后整体还原（X5 修正版）", async ({ page }) => {
  // 设计原前提是「两个相邻 modify」，但 groupHunks 对无 ctx 分隔的相邻修改必然合并。
  // 按真实行为断言：1 个 modify hunk，delLines=[b,c]，addLines=[x,y]。
  await openScenario(page, { case: "adjacent_modify" }); // a b c d → a x y d
  await expect(applyBtns(page)).toHaveCount(1);
  expect(await delLines(page).allTextContents()).toEqual(["b", "c"]);
  await expect(modifyAddLines(page)).toHaveCount(2);

  await clickRejectAt(page, 0);
  await waitResolved(page);
  expect(await getModelValue(page)).toBe("a\nb\nc\nd");
  await expectCleanUI(page);
});
