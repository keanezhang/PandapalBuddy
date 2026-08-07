/**
 * skill-md-real.spec.ts — 真实用户场景回归：CRLF 1067 行 SKILL.md 5 处修改
 *
 * 用户场景：AI 修改 pandaren/skills/code-design/SKILL.md（1067 行 CRLF），
 * 5 处全部为 modify 类型。历史版本首 hunk 在视口外导致 diff 交互提示不渲染。
 *
 * 渲染语义（src/editor/zone-builders.ts）：
 *   - del 行    → ViewZone（.mid-del-line，红底删除线）
 *   - add 行    → Monaco line decoration：
 *                 纯 add hunk  → .mid-add-line（绿）
 *                 modify hunk  → .mid-modify-line（黄）
 *   - 按钮      → ViewZone（.mid-btn-apply / .mid-btn-reject）
 *
 * 注意：modify hunk 的新增行断言必须用 modifyAddLines（.mid-modify-line），
 * 不能用 addLines（.mid-add-line）——两者是不同 hunk 类型的同类视觉标记。
 */
import { expect, test } from "@playwright/test";
import {
  applyAllBtn,
  applyBtns,
  collectPageErrors,
  delLines,
  expectCleanUI,
  modifyAddLines,
  openScenario,
  waitResolved,
} from "./helpers";

test("skill_md_real: CRLF 5处修改 首hunk 在视口内完整渲染（del+modify add+按钮）", async ({ page }) => {
  const errors = collectPageErrors(page);
  await openScenario(page, { case: "skill_md_real" });

  // 1) 5 个 hunk 全部渲染：
  //    - 5 个删除行 ViewZone（DOM 全量，与视口无关）
  //    - 5 个按钮 ViewZone
  //    - model 级 5 个 modify 新增行装饰（mid-modify-line）。
  //      注意：Monaco 对 line decoration 做虚拟渲染，视口外的装饰
  //      不生成 DOM（只有首 hunk L30 可见），因此必须用 __getDecoClasses()
  //      读 model 级装饰断言，而非 locator。
  await expect(applyBtns(page)).toHaveCount(5);
  await expect(delLines(page)).toHaveCount(5);
  const modifyDecoCount = await page.evaluate(() => {
    const classes = (window as any).__getDecoClasses?.() ?? [];
    return classes.filter((c: string) => c === "mid-modify-line").length;
  });
  expect(modifyDecoCount).toBe(5);

  // 2) 首 hunk（L30 区域 modify）在视口内且可见
  const firstDel = delLines(page).nth(0);
  await expect(firstDel).toBeVisible();
  const firstApply = applyBtns(page).nth(0);
  await expect(firstApply).toBeVisible();
  const box = await firstApply.boundingBox();
  expect(box!.y).toBeGreaterThan(0);

  // 3) 全量 accept → allResolved，UI 清空（含 modify 新增行）
  await applyAllBtn(page).click();
  await waitResolved(page);
  await expectCleanUI(page);

  // 4) 无 JS 异常
  expect(errors, `页面 JS 异常: ${errors.join("; ")}`).toEqual([]);
});
