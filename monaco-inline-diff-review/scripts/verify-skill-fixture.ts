// 验证 skill_md_data.json 的 diff/hunk 结果
import { computeDiff } from "../src/engine/diff";
import { groupHunks } from "../src/engine/hunk";
import data from "../tests/demo/fixtures/skill_md_data.json";

const hunks = groupHunks(computeDiff(data.original, data.current));
console.log("hunk count:", hunks.length);
hunks.forEach((h, i) => {
  console.log(
    `hunk[${i}] type=${h.type} origStart=${h.origStart} origEnd=${h.origEnd} del=${h.delLines.length} add=${h.addLines.length}`,
  );
  console.log(`  del[0]:`, h.delLines[0]?.slice(0, 60));
  console.log(`  add[0]:`, h.addLines[0]?.slice(0, 60));
});
