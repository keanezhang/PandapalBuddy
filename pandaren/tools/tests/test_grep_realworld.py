"""真实场景测试：模拟 Agent 日常使用 grep 的典型流程。

运行方式：
    cd 项目根目录 && PYTHONPATH=. python3 pandaren/tools/tests/test_grep_realworld.py 2>/dev/null
"""

import sys

import pytest

if "pytest" in sys.modules:
    # 手动探索脚本：模块级演示代码需要交互选择工作区，不参与 pytest 收集
    pytest.skip("手动探索脚本，不参与 pytest", allow_module_level=True)

from pandaren.tools.grep import grep as _grep_tool
from pandaren.tool import ToolContext

ctx = ToolContext(agent_id="test", step_n=0, run_id="realworld", session_id="realworld")
grep = _grep_tool.executor


def t(desc: str, **kwargs) -> None:
    """执行一次 grep 并打印结果。"""
    kwargs.setdefault("output_mode", "files_with_matches")
    kwargs.setdefault("head_limit", 250)
    print(f"\n{'─'*55}")
    print(f"  {desc}")
    print(f"{'─'*55}")
    result = grep(ctx, **kwargs)
    print(result.__tool_format_for_llm__())


# ════════════════════════════════════════════════════
#  场景：Agent 接手一个新项目，开始探索 pandaren 代码
# ════════════════════════════════════════════════════

print("=" * 55)
print("  真实场景：Agent 探索 pandaren 项目")
print("=" * 55)

# 1. 先看有哪些 Python 文件定义了类
t("1. 找到定义了类的 Python 文件",
  pattern="^class \\w+", path="pandaren", glob="*.py")

# 2. 快速看每个文件有多少个类（count）
t("2. 统计各类 Python 文件中类的数量",
  pattern="^class ", path="pandaren", glob="*.py", output_mode="count")

# 3. 找 @dataclass 定义（带大括号 glob）
t('3. 找到 dataclass 定义文件',
  pattern="@dataclass", path="pandaren",
  glob="*.{py,pyi}", output_mode="files_with_matches")

# 4. 查看名为 ToolPolicy 的 dataclass 周围内容
t("4. 查看 ToolPolicy 类定义（前后各3行）",
  pattern="class ToolPolicy", path="pandaren",
  output_mode="content", context_before=3, context_after=5)

# 5. 找所有异步函数定义
t("5. 找到所有 async def",
  pattern="async def ", path="pandaren", glob="*.py",
  output_mode="files_with_matches")

# 6. 搜 TODO / FIXME / HACK（三合一搜索，不区分大小写）
t("6. 搜索 TODO/FIXME/HACK",
  pattern="TODO|FIXME|HACK", path="pandaren",
  output_mode="content", case_insensitive=True,
  show_line_numbers=True)

# 7. 搜自定义异常定义
t("7. 搜索自定义异常类（继承 Exception）",
  pattern="class \\w+Error", path="pandaren",
  output_mode="content", context_after=1)

# 8. 找 Protocol 定义
t("8. 找 Protocol 接口定义",
  pattern="class \\w+.*Protocol", path="pandaren",
  output_mode="content", glob="*.py")

# 9. 分页浏览所有 def（第1页）
t("9a. 所有的 def（第1页，3条）",
  pattern="def ", path="pandaren", glob="*.py",
  output_mode="files_with_matches", head_limit=3, offset=0)

t("9b. 所有的 def（第2页，3条）",
  pattern="def ", path="pandaren", glob="*.py",
  output_mode="files_with_matches", head_limit=3, offset=3)

t("9c. 所有的 def（第3页，3条）",
  pattern="def ", path="pandaren", glob="*.py",
  output_mode="files_with_matches", head_limit=3, offset=6)

# 10. 搜索特定类型文件 —— 只看 markdown 文档（模拟个人助理搜笔记）
t("10. 搜索文档中的 '生命周期' 关键词",
  pattern="生命周期", path="pandaren",
  glob="*.md", output_mode="content")

# 11. 搜索跨平台路径分隔符（用 type 过滤）
t("11. 用 type=py 只搜 Python 文件中 import pathlib",
  pattern="import pathlib", path="pandaren",
  type="py", output_mode="files_with_matches")

# 12. Copilot 式代码搜索：在某目录下搜函数调用
t("12. 搜 ToolPolicy 的使用位置",
  pattern="ToolPolicy\\(", path="pandaren", glob="*.py",
  output_mode="content", context_before=1, context_after=1)

# 13. 搜 __init__.py 里的 re-export（跨行+无行号）
t("13. 搜 __init__.py 中的 __all__ 定义",
  pattern="__all__", path="pandaren",
  glob="__init__.py", output_mode="content",
  context_after=3, show_line_numbers=False)

# 14. count 模式 + type 过滤
t("14. 统计文档文件中的匹配",
  pattern="pandaren", path="docs", output_mode="count",
  glob="*.md", head_limit=20)

# 15. 正则搜中文注释
t("15. 搜中文注释（# 开头后跟中文字符）",
  pattern="# [\\u4e00-\\u9fff]", path="pandaren",
  output_mode="content", glob="*.py", head_limit=10)

print("\n" + "=" * 55)
print("  全部真实场景测试完成")
print("=" * 55)
