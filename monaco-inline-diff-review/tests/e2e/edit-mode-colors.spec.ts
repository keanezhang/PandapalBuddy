/**
 * edit-mode-colors.spec.ts — 编辑模式（CodeRenderer）修改块颜色固化 e2e。
 *
 * 回归目标：
 *   CodeRenderer 曾用「单行 prevIsDel」判断 add 行是否属于修改块，
 *   导致 d3a3 修改块只有第 1 行黄、后 2 行被误标成纯新增绿——整篇看着全绿
 *   （model_prices.toml 字段重命名场景的根因）。
 *   修复后改为块级预扫：del 块后紧邻的 add 块整体标黄。
 *
 * 场景：?case=credential_form_edit（真实文件 CredentialForm.tsx）
 *   - 初始 content = 旧版（挂载基线），readOnly=false → 编辑模式
 *   - 点 HUD「模拟加载新版」按钮 → diff(旧, 新) → 渲染颜色
 *   - 期望：全部 add 行均为 mid-modify-line（黄），0 个 mid-add-line（绿）
 */
import { expect, test } from "@playwright/test";
import { collectPageErrors, openScenario } from "./helpers";

async function decoClasses(
  page: import("@playwright/test").Page,
): Promise<string[]> {
  return page.evaluate(() => (window as any).__getDecoClasses?.() ?? []);
}

/** 只保留本组件自己的 diff 颜色 class（过滤 Monaco 内置的 unicode-highlight 等） */
function midClasses(cls: string[]): string[] {
  return cls.filter((c) => c.startsWith("mid-"));
}

test("编辑模式：修改块整体黄，不误标绿（CredentialForm 真实文件）", async ({
  page,
}) => {
  const errors = collectPageErrors(page);
  await openScenario(page, { case: "credential_form_edit" });

  // 初始：编辑模式；基线 = 旧版，无 diff → 无 mid-* decoration
  // （注意：Monaco 内置会对中文注释加 unicode-highlight，需过滤掉）
  await expect(page.locator("#mode")).toHaveText("edit");
  expect(midClasses(await decoClasses(page))).toEqual([]);

  // 模拟加载新版 → 触发 diff(旧版, 新版)
  await page.locator("#load-target").click();

  // 1) 先等修改块 decoration 生成：29 个 add 行全部应为 modify（黄）。
  //    注意不能用「等绿=0」作为首个轮询——decoration 尚未生成时空数组也满足，
  //    会与 300ms debounce 产生竞态，导致后面的断言读到 0。
  await expect
    .poll(async () => {
      return midClasses(await decoClasses(page)).filter(
        (c) => c === "mid-modify-line",
      ).length;
    })
    .toBe(29);

  // 2) 核心回归断言：没有任何 add 行被误标成纯新增（绿）
  //    （修复前每个 d2a2/d3a3 块第 2 行起都会是 mid-add-line）
  const finalClasses = midClasses(await decoClasses(page));
  expect(finalClasses.filter((c) => c === "mid-add-line")).toHaveLength(0);

  expect(errors).toEqual([]);
});
