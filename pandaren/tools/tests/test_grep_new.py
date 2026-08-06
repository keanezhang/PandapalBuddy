"""手动测试 grep 新功能（对齐 Claude Code 后的能力）。

运行方式：在项目根目录执行
    python3 pandaren/tools/tests/test_grep_new.py

需要 ripgrep 已安装（否则用纯 Python 降级）。
"""

import sys

import pytest

if "pytest" in sys.modules:
    # 手动探索脚本：模块级演示代码需要交互选择工作区，不参与 pytest 收集
    pytest.skip("手动探索脚本，不参与 pytest", allow_module_level=True)

from pandaren.tools.grep import grep as _grep_tool, _split_glob_patterns, _TYPE_EXT_MAP, _has_ripgrep

# 模拟 ToolContext（grep 函数签名的第一个参数）
from pandaren.tool import ToolContext
ctx = ToolContext(agent_id="test", step_n=0, run_id="manual_test", session_id="manual_test")

# grep 被 @tool.function 装饰后变成 Tool 对象，真实函数在 .executor 上
grep = _grep_tool.executor


def header(msg: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {msg}")
    print(f"{'='*60}")


def show(result) -> None:
    """打印 GrepResult 的 LLM 格式化输出 + 结构化字段。"""
    print(result.__tool_format_for_llm__())
    print()
    print("  ┌─ 结构化字段 ─────────────────────┐")
    print(f"  │ mode:             {result.mode}")
    print(f"  │ num_files:        {result.num_files}")
    print(f"  │ num_matches:      {result.num_matches}")
    print(f"  │ num_lines:        {result.num_lines}")
    print(f"  │ applied_limit:    {result.applied_limit}")
    print(f"  │ applied_offset:   {result.applied_offset}")
    print(f"  │ truncated:        {result.truncated}")
    print(f"  │ total_count:      {result.total_count}")
    print(f"  │ filenames[:3]:    {result.filenames[:3] if result.filenames else '[]'}")
    print("  └────────────────────────────────────┘")


# ════════════════════════════════════════════════
#  案例 0：工具函数测试
# ════════════════════════════════════════════════
header("案例 0：_split_glob_patterns 测试")
print("'*.py *.ts'       →", _split_glob_patterns("*.py *.ts"))
print("'*.{ts,tsx}'     →", _split_glob_patterns("*.{ts,tsx}"))
print("'*.md, *.py'      →", _split_glob_patterns("*.md, *.py"))
print("'*.{ts,tsx} *.py' →", _split_glob_patterns("*.{ts,tsx} *.py"))
print("''               →", _split_glob_patterns(""))

header("案例 0b：_TYPE_EXT_MAP 文档类型")
for t in ("txt", "md", "yaml", "json", "toml", "csv", "html", "xml", "ini"):
    print(f"  type={t:6s} → {_TYPE_EXT_MAP.get(t, 'N/A')}")

header("案例 0c：ripgrep 可用性")
print(f"  ripgrep: {'✅ 已安装' if _has_ripgrep() else '❌ 未安装（将走 Python 降级）'}")


# ════════════════════════════════════════════════
#  案例 1：files_with_matches（默认）
# ════════════════════════════════════════════════
header("案例 1：搜索 Python 文件中的 'def grep'（默认 mode）")
result = grep(ctx, pattern="def grep", path="pandaren/tools", output_mode="files_with_matches")
show(result)


# ════════════════════════════════════════════════
#  案例 2：content 模式 + 行号
# ════════════════════════════════════════════════
header("案例 2：content 模式，显示 'class GrepResult' 定义行")
result = grep(
    ctx, pattern="class GrepResult", path="pandaren/tools/grep.py",
    output_mode="content", context_after=2,
)
show(result)


# ════════════════════════════════════════════════
#  案例 3：count 模式
# ════════════════════════════════════════════════
header("案例 3：count 模式，统计 def 在各文件中的数量")
result = grep(
    ctx, pattern="def ", path="pandaren/tools",
    output_mode="count", head_limit=10,
)
show(result)


# ════════════════════════════════════════════════
#  案例 4：glob 参数过滤
# ════════════════════════════════════════════════
header("案例 4：glob 过滤，只在 .py 文件中搜索 'import'")
result = grep(
    ctx, pattern="import pandaren",
    path="pandaren/tools",
    output_mode="files_with_matches",
    glob="*.py",
)
show(result)


# ════════════════════════════════════════════════
#  案例 5：type 参数过滤
# ════════════════════════════════════════════════
header("案例 5：type 过滤，只在 Python 文件中搜索 'Tool'")
result = grep(
    ctx, pattern="Tool",
    path="pandaren/tools",
    output_mode="count",
    type="py",
    head_limit=10,
)
show(result)


# ════════════════════════════════════════════════
#  案例 6：分页 — offset + head_limit
# ════════════════════════════════════════════════
header("案例 6：分页测试 — 第 1-3 条")
result = grep(
    ctx, pattern="def ", path="pandaren/tools",
    output_mode="files_with_matches",
    head_limit=3, offset=0,
)
show(result)

header("案例 6b：分页测试 — 第 4-6 条（offset=3）")
result = grep(
    ctx, pattern="def ", path="pandaren/tools",
    output_mode="files_with_matches",
    head_limit=3, offset=3,
)
show(result)


# ════════════════════════════════════════════════
#  案例 7：大小写不敏感
# ════════════════════════════════════════════════
header("案例 7：case_insensitive，搜索 'vcs'")
result = grep(
    ctx, pattern="VCS", path="pandaren/tools/grep.py",
    output_mode="content", case_insensitive=True,
)
show(result)


# ════════════════════════════════════════════════
#  案例 8：没有行号的内容搜索
# ════════════════════════════════════════════════
header("案例 8：content 模式但不显示行号")
result = grep(
    ctx, pattern="from __future__", path="pandaren/tools/grep.py",
    output_mode="content", show_line_numbers=False,
)
show(result)


# ════════════════════════════════════════════════
#  案例 9：brace glob 模式
# ════════════════════════════════════════════════
header("案例 9：glob 大括号模式 '*.{py,md}'")
result = grep(
    ctx, pattern="grep",
    path=".",
    output_mode="files_with_matches",
    glob="*.{py,md}",
)
show(result)


# ════════════════════════════════════════════════
#  案例 10：无结果
# ════════════════════════════════════════════════
header("案例 10：搜索不存在的模式")
result = grep(
    ctx, pattern="xyzzy_nonexistent_12345",
    path="pandaren/tools",
    output_mode="files_with_matches",
)
show(result)


# ════════════════════════════════════════════════
#  案例 11：context_before + context_after
# ════════════════════════════════════════════════
header("案例 11：context_before=1 + context_after=2")
result = grep(
    ctx, pattern="validate_input",
    path="pandaren/tools",
    output_mode="content",
    context_before=1, context_after=2,
    glob="*.py",
)
show(result)


print("\n✅ 全部测试案例执行完毕")
