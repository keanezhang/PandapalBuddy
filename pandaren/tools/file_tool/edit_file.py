"""pandaren/tools/file_tool/edit_file.py — 精确字符串替换工具。

对标 Claude Code FileEditTool：
- validateInput 时效性检查 + 大小上限 + .ipynb 拒绝
- 临界区二次 stat 防并发改写
- 写后 populate 缓存
- 结构化输出：diff 供 LLM 验证
- empty old_string → 空文件全量替换

SDK 契约：编辑后记录到 WorkingMemory[recent_file_reads]。
"""
from __future__ import annotations

import difflib
import os
from dataclasses import dataclass

from pandaren.tool.types import ToolTier, SensitivityLevel
from pandaren.tool.definition.context import ToolContext
from pandaren.tool.definition.tool_policy import ToolPolicy
from pandaren.tool.definition.tool_lifecycle import ToolLifecycle
from pandaren.tool.decorator import tool
from pandaren.utils import expand_path
from ._utils import (
    record_file_access,
    set_last_read_mtime,
    get_last_read_mtime,
    validate_edit_input,
    populate_read_cache,
)


# ────────────────────────────────────────────
#  结构化输出（HasLLMFormat 协议，与 write_file.py 一致）
# ────────────────────────────────────────────

@dataclass
class EditResult:
    """edit_file 的结构化结果。"""
    file_path: str
    old_string: str
    new_string: str
    original_file: str      # 编辑前全文
    updated_file: str       # 编辑后全文
    replaced: int           # 替换处数
    diff: str = ""

    def __tool_format_for_llm__(self) -> str:
        lines = [
            f"✅ 已编辑：{self.file_path}（替换 {self.replaced} 处）",
            f"   变更：+{self._count_diff_lines('+')} 行  -{self._count_diff_lines('-')} 行",
        ]
        if self.diff:
            lines.append(f"\n{self.diff}")
        return "\n".join(lines)

    def _count_diff_lines(self, prefix: str) -> int:
        if not self.diff:
            return 0
        return sum(
            1 for line in self.diff.splitlines()
            if line.startswith(prefix) and not line.startswith(f"{prefix * 3} ")
        )


# ────────────────────────────────────────────
#  LLM Guide — 对标 Claude Code FileEditTool/prompt.ts
# ────────────────────────────────────────────

_EDIT_FILE_LLM_GUIDE = """对文件执行精确字符串替换。

使用规则：
- **你必须在此次对话中至少使用过一次 read_file 工具，否则本工具会失败**
- 从 read_file 输出中复制 old_string 时，**只复制行号前缀后面的内容**。行号格式是 "数字→"，"→" 之后才是文件内容。**绝不**在 old_string 或 new_string 中包含行号前缀的任何部分
- 精确匹配：old_string 必须与文件中的内容完全一致（包括空白和缩进）
- 最小唯一性：**使用能唯一定位的最小字符串——通常 2-4 行足够了**。避免包含 10+ 行上下文
- 唯一性冲突：如果 old_string 在文件中出现多次，请提供更多上下文使其唯一，或将 replace_all 设为 True
- replace_all 用于批量重命名：当你需要替换整个文件中的某个变量名时使用
- 用户可能传递以 . 开头的隐藏目录名（如 .pandapal/plans/xxx），转换为绝对路径时必须保留前导点，.pandapal 是一个完整目录名
- 始终优先编辑已有文件，**绝不**新建文件（除非明确需要）
- 仅当用户明确要求时才使用 emoji"""


# ── 工具定义 ──

@tool.function(
    tier=ToolTier.ALWAYS,
    name="edit_file",
    description="在文件中执行精确字符串替换。old_string 默认需唯一，使用 replace_all=True 可替换所有匹配。",
    when_to_use="需要修改文件的部分内容时调用。创建新文件或完全重写请使用 write_file。",
    policy=ToolPolicy(
        sensitivity=SensitivityLevel.LOW,
        # sensitive_permission=SensitivePermission.DATA_WRITE,
        is_reversible=True, audit_required=True,
        is_idempotent=False, max_calls_per_turn=30,
    ),
    lifecycle=ToolLifecycle(validate_input=validate_edit_input),
    llm_guide=_EDIT_FILE_LLM_GUIDE,
    progress_label='编辑文件「{file_path}」',
)
def edit_file(
    ctx: ToolContext,
    file_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """在文件中执行精确字符串替换。

    Args:
        ctx: 工具上下文。
        file_path: 要编辑的文件绝对路径。
        old_string: 要替换的精确字符串（须存在且唯一，除非 replace_all=True）。
        new_string: 替换后的新字符串。
        replace_all: 是否替换所有匹配。

    Returns:
        EditResult（HasLLMFormat，Phase 3 自动格式化）或错误字符串。
    """
    # no-op 拒绝
    if old_string == new_string:
        return (
            "错误：old_string 与 new_string 完全相同，这是一个无意义的 no-op 编辑。\n"
            "请确认你确实需要修改文件内容。如需读取文件请使用 read_file。"
        )

    full_path = expand_path(file_path)
    abs_path = str(full_path)

    # ── 临界区二次检查：validate 和 edit 之间可能被外部修改 ──
    last_mtime = get_last_read_mtime(abs_path)
    try:
        current_mtime = os.stat(abs_path).st_mtime_ns
    except OSError:
        return f"错误：无法访问文件：{file_path}"
    if last_mtime is not None and current_mtime != last_mtime:
        return (
            f"错误：文件 '{file_path}' 在验证后又被修改（可能由用户或格式化工具）。\n"
            f"请重新读取文件内容后再编辑。"
        )

    # 读取文件
    try:
        original = full_path.read_text(encoding="utf-8")
    except PermissionError:
        return f"错误：无权限读取：{file_path}"
    except Exception as e:
        return f"读取文件失败：{e}"

    # empty old_string 支持：空文件全量替换
    actual_old = old_string

    if actual_old == "":
        if original.strip() != "":
            return "错误：不能使用空 old_string 编辑已有内容的文件。请提供要替换的具体文本。"
        count = 1  # 空文件只有一次匹配
    else:
        count = original.count(actual_old)
        if count == 0:
            return (
                f"错误：在 {file_path} 中未找到要替换的字符串。\n"
                f"请确认 old_string 与文件内容完全一致（包括空白和缩进）。"
            )
        if not replace_all and count > 1:
            return (
                f"错误：old_string 在 {file_path} 中出现了 {count} 次（不唯一）。\n"
                f"请提供更多上下文使其唯一（通常 2-4 行即可），或将 replace_all 设为 True 替换所有匹配。"
            )

    nr = -1 if replace_all else 1
    updated = original.replace(actual_old, new_string, nr)

    # 写盘
    try:
        full_path.write_text(updated, encoding="utf-8")
    except PermissionError:
        return f"错误：无权限写入：{file_path}"
    except Exception as e:
        return f"编辑文件失败：{e}"

    # ── 写后：记录 + 缓存 + read mtime ──
    record_file_access(ctx, file_path, op="edit")
    new_mtime = os.stat(abs_path).st_mtime_ns
    populate_read_cache(abs_path, updated, new_mtime)
    set_last_read_mtime(abs_path, new_mtime)

    # ── 结构化输出 ──
    diff = _compute_diff(original, updated, file_path)
    replaced = count if replace_all else 1
    return EditResult(
        file_path=file_path,
        old_string=actual_old,
        new_string=new_string,
        original_file=original,
        updated_file=updated,
        replaced=replaced,
        diff=diff,
    )


def _compute_diff(original: str, updated: str, file_path: str) -> str:
    """计算 unified diff，超过 200 行截断。"""
    if not original:
        return ""
    a = original.splitlines(keepends=True)
    b = updated.splitlines(keepends=True)
    d = list(difflib.unified_diff(a, b, fromfile=f"a/{file_path}", tofile=f"b/{file_path}"))
    if len(d) > 200:
        d = d[:200]
        d.append("... (diff 已截断)")
    return "".join(d)
