/**
 * crlf-repro.spec.ts — 临时复现：真实文件场景「首 hunk 未标记」
 * 诊断版：滚动前后各按钮可见性 + Monaco zone DOM 结构 + scrollTop
 */
import { expect, test } from "@playwright/test";
import {
  applyBtns,
  delLines,
  openScenario,
  waitRebuild,
} from "./helpers";

async function dumpState(page: any, label: string) {
  const btns = await page.locator(".mid-btn-apply").evaluateAll((els) =>
    els.map((el) => {
      const r = el.getBoundingClientRect();
      return { visible: r.width > 0 && r.height > 0 && r.top > -5 && r.top < 900, top: Math.round(r.top), h: Math.round(r.height) };
    }),
  );
  const dels = await page.locator(".mid-del-line").evaluateAll((els) =>
    els.map((el) => {
      const r = el.getBoundingClientRect();
      return { visible: r.width > 0 && r.height > 0 && r.top > -5 && r.top < 900, top: Math.round(r.top), h: Math.round(r.height) };
    }),
  );
  const scroll = await page.evaluate(() => {
    const ed = (window as any).monaco?.editor?.getEditors?.()?.[0];
    return ed ? { top: ed.getScrollTop(), max: ed.getScrollHeight() - ed.getLayoutInfo().height } : null;
  });
  const zones = await page.evaluate(() => {
    const vz = document.querySelector(".view-zones");
    if (!vz) return "no .view-zones";
    const kids = Array.from(vz.children).map((k) => {
      const el = k as HTMLElement;
      return {
        cls: el.className,
        display: getComputedStyle(el).display,
        top: el.style.top,
        h: el.style.height,
        rectTop: Math.round(el.getBoundingClientRect().top),
        innerBtns: el.querySelectorAll(".mid-btn-apply").length,
        innerDels: el.querySelectorAll(".mid-del-line").length,
      };
    });
    return { count: kids.length, kids };
  });
  console.log(`[${label}] scroll=${JSON.stringify(scroll)}`);
  console.log(`[${label}] applyBtns:`, JSON.stringify(btns));
  console.log(`[${label}] delLines:`, JSON.stringify(dels));
  console.log(`[${label}] zones:`, JSON.stringify(zones));
  return { btns, dels };
}

for (const [caseName, eol] of [
  ["crlf_first_hunk", "CRLF"],
  ["lf_first_hunk", "LF"],
] as const) {
  test(`[repro] ${eol} 首 hunk 视口外 + 3 hunk 渲染`, async ({ page }) => {
    await openScenario(page, { case: caseName });
    await waitRebuild(page);

    await expect(applyBtns(page)).toHaveCount(3, { timeout: 10_000 });
    await dumpState(page, `${eol} initial`);

    // 滚动到首 hunk（line 61）：首 hunk 渲染行 ~59-62，内容坐标 ~1121-1216px
    await page.evaluate(() => {
      const ed = (window as any).monaco?.editor?.getEditors?.()?.[0];
      if (ed) ed.setScrollTop(1050);
    });
    await page.waitForTimeout(500);
    await dumpState(page, `${eol} after scrollTop=1050`);

    // 核心断言：首 hunk 按钮滚动后可见（用户场景）
    await expect(applyBtns(page).nth(0)).toBeVisible({ timeout: 5_000 });
    await expect(delLines(page).nth(0)).toBeVisible({ timeout: 5_000 });
    await page.screenshot({ path: `tests/e2e/screenshots/repro-${caseName}.png` });
  });
}
