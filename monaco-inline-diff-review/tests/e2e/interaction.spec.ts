/**
 * interaction.spec.ts — 交互效果 e2e：真实浏览器 + 真实 Monaco。
 *
 * 与其他 spec 互补：这里验证「用户实际看到和点到的东西」——
 * ViewZone / 按钮条 / 删除线 / 绿行 / gutter 是否真实渲染，按钮是否真的点得动
 * （Playwright actionability：被遮挡/不可点会直接失败），以及每步视觉变化截图。
 *
 * 覆盖设计文档用例：CMP-1、CMP-4、CMP-8、CMP-9、CMP-10、CMP-14、CMP-16、CMP-47、CMP-60。
 * 默认场景 three_funcs：3 个 modify hunk（return 1/2/3 → return 100/200/300）。
 *
 * 每步截图存至 tests/e2e/screenshots/，可直接打开核对视觉效果。
 */
import { expect, test, type Page } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";
import {
  addLines,
  applyBtns,
  clickApplyAt,
  delLines,
  expectCleanUI,
  getModelValue,
  getPartialSaves,
  getResolvedEvents,
  modifyAddLines,
  openScenario,
  rejectBtns,
} from "./helpers";

const SHOT_DIR = fileURLToPath(new URL("screenshots", import.meta.url));
mkdirSync(SHOT_DIR, { recursive: true });

/* ── 与 demo/app.tsx 的 three_funcs 场景一致 ──────────────────────────── */

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

const CURRENT = [
  "def alpha():",
  "    return 100",
  "",
  "def beta():",
  "    return 200",
  "",
  "def gamma():",
  "    return 300",
].join("\n");

/* ── 辅助 ─────────────────────────────────────────────────────────────── */

/** 编辑器可视区域文本。注意：Monaco 渲染时空格是 &nbsp;（U+00A0），断言前归一化。 */
async function editorText(page: Page) {
  const raw = await page.locator(".monaco-editor .view-lines").innerText();
  return raw.replace(/\u00A0/g, " ");
}

const btnBars = (page: Page) => page.locator(".mid-zone-btn-bar");
const gutters = (page: Page) => page.locator(".mid-modify-gutter");

async function shot(page: Page, name: string) {
  await page.screenshot({ path: resolve(SHOT_DIR, `${name}.png`) });
}

/** 打开 three_funcs 场景（3 个 modify hunk）并等待渲染完成 */
async function openDemo(page: Page) {
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(String(err)));
  await openScenario(page, { case: "three_funcs" });
  await expect(page.locator(".monaco-editor")).toBeVisible();
  await expect(btnBars(page)).toHaveCount(3); // auto-retry 覆盖 RAF→rebuild 时序
  return errors;
}

/* ── 1. CMP-1 初始渲染 ───────────────────────────────────────────────── */

test("CMP-1 初始渲染：3 个 hunk 的按钮条/删除线/绿行/浮动栏全部可见", async ({ page }) => {
  const errors = await openDemo(page);

  // 3 个 modify hunk → 3 组 Apply/Reject 按钮
  await expect(applyBtns(page)).toHaveCount(3);
  await expect(rejectBtns(page)).toHaveCount(3);

  // 每个 modify hunk 各 1 行删除线（被删的原始行）
  await expect(delLines(page)).toHaveCount(3);
  await expect(delLines(page).nth(0)).toContainText("return 1");
  await expect(delLines(page).nth(1)).toContainText("return 2");
  await expect(delLines(page).nth(2)).toContainText("return 3");

  // 每个 modify hunk 各 1 条绿行 decoration + gutter
  await expect(modifyAddLines(page)).toHaveCount(3);
  await expect(gutters(page)).toHaveCount(3);

  // 编辑器文本是 current 内容
  const text = await editorText(page);
  expect(text).toContain("return 100");
  expect(text).toContain("return 200");
  expect(text).toContain("return 300");

  // 底部浮动操作栏
  await expect(page.locator(".mid-float-bar")).toBeVisible();
  await expect(page.locator(".mid-btn-apply-all")).toBeVisible();
  await expect(page.locator(".mid-btn-reject-all")).toBeVisible();

  // 按钮真实可点（未被遮挡、pointer-events 生效）
  const firstApply = applyBtns(page).first();
  await expect(firstApply).toBeVisible();
  await firstApply.hover();

  await shot(page, "01-initial");
  expect(errors, "页面不应有未捕获异常").toEqual([]);
});

/* ── 2. CMP-4 Apply 单个 modify hunk ─────────────────────────────────── */

test("CMP-4 Apply 单个 hunk：该 hunk UI 消失，model 不变，partialSave(hunkKey≠'')", async ({
  page,
}) => {
  await openDemo(page);

  await clickApplyAt(page, 0);

  // 该 hunk 的按钮条消失，剩余 2 个
  await expect(btnBars(page)).toHaveCount(2);

  // INV-4：Apply 不改 model
  expect(await getModelValue(page)).toBe(CURRENT);
  const text = await editorText(page);
  expect(text).toContain("return 100");

  // partialSave 携带非空 hunkKey；还有 2 个 pending，不应触发 allResolved
  const partials = await getPartialSaves(page);
  expect(partials).toHaveLength(1);
  expect(partials[0].hunkKey).toBeTruthy();
  expect(await getResolvedEvents(page)).toHaveLength(0);

  await shot(page, "02-after-apply");
});

/* ── 3. CMP-8 Reject 单个 modify hunk ────────────────────────────────── */

test("CMP-8 Reject modify hunk：建议行被原始行替换，partialSave('')", async ({ page }) => {
  await openDemo(page);

  // Reject 第 2 个 hunk（beta: return 2 → return 200）
  await rejectBtns(page).nth(1).click();

  // 等待 view 层渲染收敛（Monaco 渲染异步：model 同步回退，DOM 需 ~1 帧更新；
  // 点击前 DOM 含 "return 200"，poll 会等 DOM 真正更新后才通过）
  await expect.poll(async () => editorText(page)).not.toContain("return 200");
  const text = await editorText(page);
  expect(text).not.toContain("return 200");
  // H1/H3 的建议行仍在
  expect(text).toContain("return 100");
  expect(text).toContain("return 300");

  await expect(btnBars(page)).toHaveCount(2);

  const partials = await getPartialSaves(page);
  expect(partials).toHaveLength(1);
  expect(partials[0].hunkKey).toBe("");

  await shot(page, "03-after-reject");
});

/* ── 4. CMP-16 Reject All（快速路径）─────────────────────────────────── */

test("CMP-16 Reject All：model 整体回滚 original，allResolved(original)", async ({ page }) => {
  await openDemo(page);

  await page.locator(".mid-btn-reject-all").click();

  // 编辑器内容完全回滚
  await expect.poll(async () => getModelValue(page)).toBe(ORIGINAL);
  // Monaco view 渲染异步：等 DOM 同步收敛后再断言可见文本
  await expect.poll(async () => editorText(page)).not.toContain("return 100");
  const text = await editorText(page);
  expect(text).toContain("return 1");
  expect(text).not.toContain("return 100");

  // allResolved 恰好一次，内容 === original；不触发 partialSave
  const resolved = await getResolvedEvents(page);
  expect(resolved).toHaveLength(1);
  expect(resolved[0].content).toBe(ORIGINAL);
  expect(await getPartialSaves(page)).toHaveLength(0);

  // scheduleRebuild → noDiff → 清空所有 diff UI
  await expectCleanUI(page);

  await shot(page, "04-reject-all");
});

/* ── 5. CMP-14 Apply All ─────────────────────────────────────────────── */

test("CMP-14 Apply All：allResolved 一次、无 partialSave、model 保持 current", async ({
  page,
}) => {
  await openDemo(page);

  await page.locator(".mid-btn-apply-all").click();

  // Apply All 不改 model
  expect(await getModelValue(page)).toBe(CURRENT);
  const text = await editorText(page);
  expect(text).toContain("return 100");

  const resolved = await getResolvedEvents(page);
  expect(resolved).toHaveLength(1);
  expect(resolved[0].content).toBe(CURRENT);
  expect(await getPartialSaves(page)).toHaveLength(0);

  // scheduleRebuild → pending=0 → 清空所有 diff UI
  await expectCleanUI(page);

  await shot(page, "05-apply-all");
});

/* ── 6. CMP-60 混合交互序列 ──────────────────────────────────────────── */

test("CMP-60 混合序列：Apply H1 → Reject H2，UI 与 model 都正确", async ({ page }) => {
  await openDemo(page);

  // Apply 第 1 个 hunk（alpha: return 1 → 100）
  await clickApplyAt(page, 0);
  await expect(btnBars(page)).toHaveCount(2);

  // Reject 剩余的第 1 个（即原 H2，beta）
  await rejectBtns(page).first().click();
  await expect(btnBars(page)).toHaveCount(1);

  // model：return 100 保留（已 Apply）、return 2 恢复（已 Reject）、return 300 未处理
  const text = await editorText(page);
  expect(text).toContain("return 100");
  expect(text).toContain("return 2");
  expect(text).toContain("return 300");
  expect(text).not.toContain("return 200");

  await shot(page, "06-mixed-sequence");
});

/* ── 7. CMP-9 连续 Apply 所有 hunk（回归：最后一个 Apply 后 UI 残留）───── */

test("CMP-9 连续 Apply 全部 hunk：最后一个 Apply 后 UI 应完全清空", async ({ page }) => {
  await openDemo(page);

  await page.locator(".mid-btn-apply").first().click();
  await expect(btnBars(page)).toHaveCount(2);

  await page.locator(".mid-btn-apply").first().click();
  await expect(btnBars(page)).toHaveCount(1);

  await page.locator(".mid-btn-apply").first().click();

  // 最后一个 Apply 后：所有 diff UI 必须消失（按钮条/删除线/绿行/gutter）
  await expect(btnBars(page)).toHaveCount(0);
  await expectCleanUI(page);
  await expect(gutters(page)).toHaveCount(0);

  // model 保持 current
  expect(await getModelValue(page)).toBe(CURRENT);

  // 回调：partialSave ×3，allResolved ×1 携带 current
  expect(await getPartialSaves(page)).toHaveLength(3);
  const resolved = await getResolvedEvents(page);
  expect(resolved).toHaveLength(1);
  expect(resolved[0].content).toBe(CURRENT);

  await shot(page, "07-apply-all-one-by-one");
});

/* ── 8. CMP-10 连续 Reject 所有 hunk（回归：最后一个 Reject 后 UI 残留）── */

test("CMP-10 连续 Reject 全部 hunk：最后一个 Reject 后 UI 应完全清空", async ({ page }) => {
  await openDemo(page);

  await page.locator(".mid-btn-reject").first().click();
  await expect(btnBars(page)).toHaveCount(2);

  await page.locator(".mid-btn-reject").first().click();
  await expect(btnBars(page)).toHaveCount(1);

  await page.locator(".mid-btn-reject").first().click();

  // 最后一个 Reject 后：所有 diff UI 必须消失
  await expect(btnBars(page)).toHaveCount(0);
  await expectCleanUI(page);
  await expect(gutters(page)).toHaveCount(0);

  // INV-3：model 完全回滚 original
  expect(await getModelValue(page)).toBe(ORIGINAL);

  // 回调：partialSave ×3（Reject 传空 key），allResolved ×1 携带 original
  const partials = await getPartialSaves(page);
  expect(partials).toHaveLength(3);
  expect(partials.every((e) => e.hunkKey === "")).toBe(true);
  const resolved = await getResolvedEvents(page);
  expect(resolved).toHaveLength(1);
  expect(resolved[0].content).toBe(ORIGINAL);

  await shot(page, "08-reject-all-one-by-one");
});
