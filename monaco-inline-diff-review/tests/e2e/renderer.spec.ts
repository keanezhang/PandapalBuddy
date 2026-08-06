/**
 * renderer.spec.ts — CodeRenderer 模式切换 e2e（case=renderer 台架）
 *
 * 覆盖设计文档用例：
 *   CR-1 readOnly=true + original 存在 → suggestion 模式（InlineDiffEditor）
 *   CR-2 readOnly=false → edit 模式（纯 Monaco，onChange 生效）
 *   CR-3 original=undefined → edit 模式（即使 readOnly=true）
 *   CR-4 fileId 作为 React key → 切换文件强制重挂载，丢弃组件内 applied 状态
 */
import { expect, test } from "@playwright/test";
import {
  applyBtns,
  clickApplyAt,
  collectPageErrors,
  expectCleanUI,
  floatBar,
  getEvents,
  openScenario,
  setRenderer,
  waitResolved,
} from "./helpers";

async function openRenderer(page: import("@playwright/test").Page) {
  await openScenario(page, { case: "renderer" });
  // suggestion 模式初始就绪：1 个 modify hunk（b→x）
  await expect(applyBtns(page)).toHaveCount(1);
}

test("CR-1 readOnly + original → suggestion 模式渲染 InlineDiffEditor", async ({ page }) => {
  await openRenderer(page);

  await expect(page.locator("#mode")).toHaveText("suggestion");
  await expect(floatBar(page)).toBeVisible();
  await expect(page.locator(".mid-del-line")).toHaveCount(1);
  await expect(page.locator(".mid-add-line")).toHaveCount(1);
});

test("CR-2 readOnly=false → edit 模式：无 diff UI，onChange 生效", async ({ page }) => {
  await openRenderer(page);

  await setRenderer(page, { readOnly: false });
  await expect(page.locator("#mode")).toHaveText("edit");
  await expect(applyBtns(page)).toHaveCount(0);
  await expect(floatBar(page)).toHaveCount(0);

  // 编辑模式 onChange 已接入：输入字符 → change 事件
  await page.locator(".monaco-editor .view-lines").click();
  await page.keyboard.type("z");
  await expect
    .poll(async () => (await getEvents(page)).filter((e) => e.type === "change").length)
    .toBeGreaterThanOrEqual(1);
});

test("CR-3 original=undefined → edit 模式（即使 readOnly=true）", async ({ page }) => {
  await openRenderer(page);

  await setRenderer(page, { original: null });
  await expect(page.locator("#mode")).toHaveText("edit");
  await expect(applyBtns(page)).toHaveCount(0);
  await expect(floatBar(page)).toHaveCount(0);
});

test("CR-4 fileId 切换 → key 变化强制重挂载，applied 状态丢弃", async ({ page }) => {
  const errors = collectPageErrors(page);
  await openRenderer(page);

  // f1：Apply 唯一 hunk → resolved、UI 清空
  await clickApplyAt(page, 0);
  await waitResolved(page);
  await expectCleanUI(page);

  // 切换到 f2：key={fileId} → 旧 InlineDiffEditor 卸载、新实例挂载
  await setRenderer(page, { fileId: "f2" });
  await expect(page.locator("#file-id")).toHaveText("f2");

  // 新实例不带 applied 状态 → hunk 重新渲染
  await expect(applyBtns(page)).toHaveCount(1);
  expect(errors).toEqual([]);
});
