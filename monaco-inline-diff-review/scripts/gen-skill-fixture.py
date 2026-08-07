"""生成 SKILL.md 真实场景 fixture（tests/demo/fixtures/skill_md_data.json）。

场景对齐用户真实 bug：code-design/SKILL.md（1067 行）经 AI 修改后，
第一个 hunk（description 新增行，文件第 6 行）在 UI 上不显示 diff 交互提示。

构造方式：
  current  = 磁盘当前内容（原样，保留行尾）
  original = current 还原 3 处修改：
    1) 删除第 6 行（description 新增的续行）          → 第 1 个 hunk（add，文件顶部）
    2) 第 30 行去掉「有据可查的」                      → 第 2 个 hunk（modify）
    3) 第 100 行改回短句                               → 第 3 个 hunk（modify）

行尾保持 current 原样（与后端 showSuggestion 的 suggested 一致），
diff 引擎内部会做 CRLF 归一化。
"""
import json
import io

SRC = r"C:\Users\keanezhang\PycharmProjects\pandapal_buddy\.pandapal\skills\user\code-design\SKILL.md"
OUT = r"C:\Users\keanezhang\PycharmProjects\pandapal_buddy\monaco-inline-diff-review\tests\demo\fixtures\skill_md_data.json"

with io.open(SRC, "r", encoding="utf-8", newline="") as f:
    current = f.read()  # 保留原始行尾（CRLF 或 LF）

lines = current.splitlines(keepends=True)  # 保留行尾，逐行可逆
print("total lines:", len(lines), "eol sample:", repr(lines[0][-3:]))

orig = list(lines)

# 先做行内替换（不影响行号），最后再删除行（删除会让后续 index 偏移）

# 2) 第 30 行（index 29）：去掉「有据可查的」
assert "有据可查的" in orig[29], f"line30 mismatch: {orig[29]!r}"
orig[29] = orig[29].replace("有据可查的", "")

# 3) 第 100 行（index 99）：改回短句
assert "变更类型" in orig[99], f"line100 mismatch: {orig[99]!r}"
orig[99] = orig[99].replace("新增 / 修改 / 扩展", "新增/修改/扩展")

# 1) 删除第 6 行（index 5）：description 新增续行（最后执行，避免偏移）
assert "本技能只产出设计方案" in orig[5], f"line6 mismatch: {orig[5]!r}"
del orig[5]

original = "".join(orig)

data = {
    "name": "skill_md_repro",
    "language": "markdown",
    "original": original,
    "current": current,
}

with io.open(OUT, "w", encoding="utf-8", newline="\n") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print("fixture written:", OUT)
print("current lines:", len(current.splitlines()))
print("original lines:", len(original.splitlines()))
print("original line6 now:", repr(original.splitlines()[5]))
