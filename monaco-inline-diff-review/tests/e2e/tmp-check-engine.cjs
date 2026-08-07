// 用 dist 的 engine 跑真实场景 fixture，检查 hunk 结构与行类型
const fs = require("fs");
const distEngine = require("C:/Users/keanezhang/PycharmProjects/pandapal_buddy/monaco-inline-diff-review/dist/engine/index.cjs");

const fixPath = "C:/Users/keanezhang/PycharmProjects/pandapal_buddy/monaco-inline-diff-review/tests/demo/fixtures/skill_md_real.json";
const fix = JSON.parse(fs.readFileSync(fixPath, "utf-8"));

const entries = distEngine.computeDiff(fix.original, fix.current);
const dels = entries.filter(e => e.kind === "del").length;
const adds = entries.filter(e => e.kind === "add").length;
const ctx = entries.filter(e => e.kind === "ctx").length;
console.log(`entries total=${entries.length} del=${dels} add=${adds} ctx=${ctx}`);

const hunks = distEngine.groupHunks(entries);
console.log("hunk count:", hunks.length);
hunks.forEach((h, i) => {
  const d = h.lines.filter(l => l.kind === "del").length;
  const a = h.lines.filter(l => l.kind === "add").length;
  console.log(`hunk[${i}]: startIdx=${h.startIdx} lines=${h.lines.length} del=${d} add=${a}`);
  if (d === 0 || a === 0) {
    console.log("   hunk.lines:", JSON.stringify(h.lines.slice(0, 4)));
  }
});
