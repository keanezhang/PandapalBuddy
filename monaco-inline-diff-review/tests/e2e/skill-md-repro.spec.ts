/**
 * skill-md-repro.spec.ts — 真实用户 bug 回归：code-design/SKILL.md 顶部新增行
 *
 * Bug：AI 修改 1067 行 CRLF 的 SKILL.md 后，第一个 hunk 是「文件第 6 行的新增行」
 * （add 类型，位于顶部，不在视口外），历史版本该 hunk 不渲染 diff 交互提示
 * （无 apply/reject 按钮、无绿色新增行）。
 *
 * 复现场景（tests/demo/app.tsx）：
 *   ?case=skill_md        suggestion 模式：original=LF 旧版(1066 行) vs current=CRLF 新版(1067 行)
 *   ?case=skill_md_edit   编辑模式：基线=LF 旧版，内容=CRLF 新版
 *
 * 断言（修复后应全部通过）：
 *   1. 3 个 hunk 全部渲染出 apply 按钮（add×1 + modify×2）
 *   2. 首 hunk（add）绿色新增行可见且按钮可交互
 *   3. apply 首 hunk 后该新增行消失、触发 partialSave
 */
import { expect, test } from "@playwright/test";
import {
  addLines,
  applyBtns,
  clickApplyAt,
  getPartialSaves,
  openScenario,
  waitRebuild,
} from "./helpers";

test("skill_md: 顶部 add hunk 渲染出交互提示（3 个按钮全量）", async ({ page }) => {
  await openScenario(page, { case: "skill_md" });
  await waitRebuild(page);

  // 3 个 hunk：add(第6行) + modify(第30行) + modify(第100行)
  await expect(applyBtns(page)).toHaveCount(3, { timeout: 10_000 });

  // 首 hunk 绿色新增行可见（核心 bug：历史版本这里什么都不显示）
  await expect(addLines(page).nth(0)).toBeVisible({ timeout: 5_000 });

  // 首个 apply 按钮可点击（在视口内，无需滚动）
  await expect(applyBtns(page).nth(0)).toBeVisible({ timeout: 5_000 });
});

test("skill_md: apply 首 hunk（add）后该行消失并触发 partialSave", async ({ page }) => {
  await openScenario(page, { case: "skill_md" });
  await waitRebuild(page);

  await expect(applyBtns(page)).toHaveCount(3, { timeout: 10_000 });

  await clickApplyAt(page, 0);
  await waitRebuild(page);

  // add 行已接受 → 绿色新增行减为 0，剩余 2 个 modify hunk
  await expect(applyBtns(page)).toHaveCount(2, { timeout: 5_000 });
  expect(await addLines(page).count()).toBe(0);

  // partialSave 事件携带对应 hunkKey
  const saves = await getPartialSaves(page);
  expect(saves.length).toBe(1);
  expect(saves[0].hunkKey).toBeTruthy();
  expect(saves[0].content).toContain("本技能只产出设计方案");
});

test("skill_md_edit: 编辑模式加载 AI 改动后渲染顶部 add 行（CRLF 基线对照）", async ({ page }) => {
  await openScenario(page, { case: "skill_md_edit" });
  await waitRebuild(page);

  // 真实时序第一步：先挂载旧版（LF）→ 快照 = 旧版，无修改标记
  await expect(addLines(page)).toHaveCount(0, { timeout: 5_000 });

  // 真实时序第二步：模拟「AI 修改完成」→ content 更新为 CRLF 新版
  // → content ≠ 快照 → 300ms 防抖 → diff(旧版LF, 新版CRLF) → 标记渲染
  await page.click("#load-target");
  await waitRebuild(page);

  // 编辑模式颜色渲染：首个即顶部新增行（绿色 mid-add-line）
  await expect(addLines(page).nth(0)).toBeVisible({ timeout: 10_000 });

  // 契约：编辑模式无 hunk 按钮（apply/reject 仅 suggestion 模式 InlineDiffEditor）
  await expect(applyBtns(page)).toHaveCount(0, { timeout: 5_000 });

  await page.screenshot({
    path: "tests/e2e/screenshots/skill-md-repro.png",
    fullPage: false,
  });
});
