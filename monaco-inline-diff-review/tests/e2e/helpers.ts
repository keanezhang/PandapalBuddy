/**
 * e2e 共享 helpers —— 所有 spec 通过这里与 demo 台架交互。
 *
 * 场景与断言数据对齐 tests/docs/test-design.md 的 Given/When/Then。
 */
import { expect, type Locator, type Page } from "@playwright/test";

export interface OpenOptions {
  /** 场景名（SCENARIOS key），默认 multi_modify */
  case?: string;
  /** 不传 onPartialSave/onAllResolved（CMP-29） */
  noCallbacks?: boolean;
  /** initialAppliedKeys，逗号分隔（CMP-23） */
  appliedKeys?: string[];
}

/** 打开 demo 并等待 Monaco 就绪 */
export async function openScenario(page: Page, opts: OpenOptions = {}) {
  const params = new URLSearchParams();
  if (opts.case) params.set("case", opts.case);
  if (opts.noCallbacks) params.set("noCallbacks", "1");
  if (opts.appliedKeys?.length) params.set("appliedKeys", opts.appliedKeys.join(","));
  const qs = params.toString();
  await page.goto(qs ? `/?${qs}` : "/");
  await page.waitForSelector(".view-lines", { timeout: 30_000 });
}

/* ── 读取 ── */

export const applyBtns = (page: Page) => page.locator(".mid-btn-apply");
export const rejectBtns = (page: Page) => page.locator(".mid-btn-reject");
export const delLines = (page: Page) => page.locator(".mid-del-line");
/** 纯 add hunk 的新增行（绿色 mid-add-line） */
export const addLines = (page: Page) => page.locator(".mid-add-line");
/** modify hunk 的新增行（黄色 mid-modify-line）——与 addLines 同属「新增行」语义，颜色不同 */
export const modifyAddLines = (page: Page) => page.locator(".mid-modify-line");
export const floatBar = (page: Page) => page.locator(".mid-float-bar");
export const applyAllBtn = (page: Page) => page.locator(".mid-btn-apply-all");
export const rejectAllBtn = (page: Page) => page.locator(".mid-btn-reject-all");

export async function getModelValue(page: Page): Promise<string> {
  return page.evaluate(() => (window as any).__getValue());
}

export interface DiffEvent {
  type: "partialSave" | "allResolved" | "change";
  content?: string;
  hunkKey?: string;
  at: number;
}

export async function getEvents(page: Page): Promise<DiffEvent[]> {
  return page.evaluate(() => (window as any).__diffEvents ?? []);
}

export async function getResolvedEvents(page: Page): Promise<DiffEvent[]> {
  return (await getEvents(page)).filter((e) => e.type === "allResolved");
}

export async function getPartialSaves(page: Page): Promise<DiffEvent[]> {
  return (await getEvents(page)).filter((e) => e.type === "partialSave");
}

/* ── 等待 ── */

/** 等待两帧 RAF + 一个宏任务，确保 scheduleRebuild 的 rebuild 已执行完 */
export function waitRebuild(page: Page): Promise<void> {
  return page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(resolve, 0))),
      ),
  );
}

/** 等待 allResolved 事件出现 */
export async function waitResolved(page: Page, timeout = 10_000) {
  await expect
    .poll(async () => (await getEvents(page)).some((e) => e.type === "allResolved"), { timeout })
    .toBe(true);
}

/* ── 操作 ── */

export async function clickApplyAt(page: Page, index: number) {
  await applyBtns(page).nth(index).click();
  await waitRebuild(page);
}

export async function clickRejectAt(page: Page, index: number) {
  await rejectBtns(page).nth(index).click();
  await waitRebuild(page);
}

export async function clickApplyAll(page: Page) {
  await applyAllBtn(page).click();
  await waitRebuild(page);
}

export async function clickRejectAll(page: Page) {
  await rejectAllBtn(page).click();
  await waitRebuild(page);
}

/**
 * 同一 JS task 内连续点击（不跨帧）——模拟设计文档「同帧连续操作」时序（R1~R5）。
 * 由于 rebuild 经 RAF 调度，同一 task 内的多次点击必定发生在同一帧。
 */
export async function clickSameFrame(
  page: Page,
  clicks: Array<{ kind: "apply" | "reject"; index: number }>,
) {
  await page.evaluate((cls) => {
    for (const c of cls) {
      const sel = c.kind === "apply" ? ".mid-btn-apply" : ".mid-btn-reject";
      const btn = document.querySelectorAll<HTMLElement>(sel)[c.index];
      if (!btn) throw new Error(`同帧点击失败：找不到 ${sel}[${c.index}]`);
      btn.click();
    }
  }, clicks);
  await waitRebuild(page);
}

/** 动态修改 props（CMP-20~24、CMP-51/52） */
export async function setProps(page: Page, p: { original?: string; current?: string }) {
  await page.evaluate((patch) => (window as any).__setProps(patch), p);
  await waitRebuild(page);
}

/** 动态修改 CodeRenderer 台架状态（CR-1~4） */
export async function setRenderer(
  page: Page,
  p: { content?: string; original?: string | null; readOnly?: boolean; fileId?: string },
) {
  await page.evaluate((patch) => (window as any).__setRenderer(patch), p);
  await waitRebuild(page);
}

/* ── 复合断言 ── */

/** 断言 UI 已清空：无 hunk 按钮、无删除线、无新增行（浮层条按设计保留）。
 *  注意：全部用 expect().toHaveCount()（自动重试）而非 count()+toBe(0)——
 *  allResolved 事件与 Monaco 清理 decoration/viewZone 的 DOM 更新不同步，
 *  立即断言会偶发读到残留 DOM（并行跑尤甚）。 */
export async function expectCleanUI(page: Page) {
  await expect(applyBtns(page)).toHaveCount(0);
  await expect(rejectBtns(page)).toHaveCount(0);
  await expect(delLines(page)).toHaveCount(0);
  await expect(addLines(page)).toHaveCount(0);
  await expect(modifyAddLines(page)).toHaveCount(0);
}

/** 收集页面 JS 异常（在测试开头调用，结尾断言为空） */
export function collectPageErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  return errors;
}

/** 按钮的视口 y 坐标（CMP-56 位置稳定性） */
export async function btnTopY(loc: Locator): Promise<number> {
  const box = await loc.boundingBox();
  if (!box) throw new Error("按钮不可见，无法取 boundingBox");
  return box.y;
}
