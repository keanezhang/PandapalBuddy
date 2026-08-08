#!/usr/bin/env node
/**
 * scripts/lint-ui.mjs — TSX 内联样式约束检查（UI 一致性护栏）
 *
 * 规则（与 docs/ui-consistency-plan.md Phase 4 口径一致）：
 *   1. style={{ ... }} 对象内禁止颜色字面量：#hex / rgb() / rgba()
 *      → 必须引用 var(--*) token；透明度变体用 color-mix(in srgb, var(--x) P%, transparent)
 *      → var(--x, #fallback) 兜底形式合法（ErrorBoundary 等刻意保留的安全网）
 *   2. style={{ ... }} 对象内禁止 fontSize 数字/px 字面量 → 必须 var(--text-*)
 *   3. 合法例外：
 *      - CSS 变量桥接：style={{ "--row-color": c }}（键以 -- 开头）
 *      - 动态计算值：width: pct + "%" 等（非字面量，天然不命中）
 *      - 文件级白名单（见 WHITELIST）
 *      - 行级豁免：行尾注释 // ui-lint-ok（需注明原因）
 *
 * 实现：定位 style={{ 后做括号配对提取完整对象块（跨行），只对块内文本做字面量匹配，
 * 不会误伤相邻 JSX 行。
 *
 * 退出码：发现问题 → 1；全绿 → 0。
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const SRC = join(fileURLToPath(import.meta.url), "../../src");

/** 文件级白名单：相对 src/ 的路径 → 豁免原因 */
const WHITELIST = new Map([
  ["pages/dashboard/constants.ts", "图表分类色板 MODEL_PALETTE（语义分类色，非品牌色）"],
  ["components/fileRenderers/HtmlRenderer.tsx", "iframe sandbox 白底 + 预览/源码切换按钮"],
]);

const LINE_OK = /ui-lint-ok/;

/** 从 start 处（指向 "style={{" 的第一个 "{"）做花括号配对，返回块结束下标 */
function matchBlock(src, start) {
  let depth = 0;
  let inStr = null; // 当前字符串引号
  for (let i = start; i < src.length; i++) {
    const ch = src[i];
    if (inStr) {
      if (ch === "\\") i++; // 跳过转义
      else if (ch === inStr) inStr = null;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === "`") { inStr = ch; continue; }
    if (ch === "{") depth++;
    else if (ch === "}") {
      depth--;
      if (depth === 0) return i;
    }
  }
  return -1;
}

/** 块内违规检查，返回违规描述数组 */
function checkBlock(block) {
  const found = [];
  // hex 字面量：字符串值以 # 开头（"…#xxx…" 形式），排除 var(--x, #fb) 兜底
  const hexRe = /["'`]#([0-9a-fA-F]{3,8})["'`]/g;
  let m;
  while ((m = hexRe.exec(block))) {
    found.push(`hex 颜色 #${m[1]}`);
  }
  // rgb()/rgba() 字面量：字符串值以 rgb( 开头，排除 var()/color-mix( 包裹的合法用法
  const rgbaRe = /["'`](rgba?\(\d[^"'`]*\))["'`]/g;
  while ((m = rgbaRe.exec(block))) {
    found.push(`颜色 ${m[1]}`);
  }
  // fontSize 数字或尺寸字面量
  const fsRe = /fontSize:\s*([0-9.]+|["'`][0-9.]+(?:px|rem|em)["'`])/g;
  while ((m = fsRe.exec(block))) {
    found.push(`fontSize: ${m[1]}`);
  }
  return found;
}

/** 递归收集 .tsx 文件（跳过 monacoInlineDiff） */
function collect(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    if (name === "monacoInlineDiff" || name === "node_modules") continue;
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) out.push(...collect(p));
    else if (name.endsWith(".tsx")) out.push(p);
  }
  return out;
}

const reports = [];
let fileCount = 0;

for (const file of collect(SRC)) {
  const rel = relative(SRC, file);
  if (WHITELIST.has(rel)) continue;
  fileCount++;
  const src = readFileSync(file, "utf8");
  const styleRe = /style=\{\{/g;
  let m;
  while ((m = styleRe.exec(src))) {
    const blockStart = m.index + "style=".length; // 指向第一个 {
    const blockEnd = matchBlock(src, blockStart);
    if (blockEnd === -1) continue;
    const block = src.slice(blockStart, blockEnd + 1);
    const hits = checkBlock(block);
    if (hits.length === 0) continue;
    // 行级豁免：块起始所在行，或块内任意一行带 // ui-lint-ok 标记
    const lineNo = src.slice(0, m.index).split("\n").length;
    const lineText = src.split("\n")[lineNo - 1];
    if (LINE_OK.test(lineText) || LINE_OK.test(block)) continue;
    reports.push(`  ${rel}:${lineNo}  ${hits.join("；")}`);
  }
}

if (reports.length > 0) {
  console.error(`\n✗ lint:ui 发现 ${reports.length} 处内联样式违规：\n`);
  reports.forEach((r) => console.error(r));
  console.error(`
修复指引：
  · 颜色 → var(--*) token；透明度变体用 color-mix(in srgb, var(--x) P%, transparent)
  · fontSize → var(--text-*)
  · 确属例外（monaco 主题/色板等）→ 行尾加 // ui-lint-ok 并注明原因
`);
  process.exit(1);
} else {
  console.log(`✓ lint:ui 通过（${fileCount} 个 tsx 文件，无内联颜色/字号字面量）`);
}
