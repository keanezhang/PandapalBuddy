"""pandaren/tools/write_file.py — 创建/覆盖文件工具。

对标 Claude Code FileWriteTool：
- validateInput 时效性检查（必须读过 + 未被外部修改）
- 临界区二次 stat 防并发改写
- 写后 populate 缓存（避免后续读 miss）
- 结构化输出：create/update + 行数统计

SDK 契约：写入后记录到 WorkingMemory[recent_file_reads]。
"""
from __future__ import annotations

import difflib
import logging
import os
from dataclasses import dataclass

from pandaren.tool.types import ToolTier, SensitivityLevel
from pandaren.tool.definition.context import ToolContext
from pandaren.tool.definition.tool_policy import ToolPolicy
from pandaren.tool.definition.tool_lifecycle import ToolLifecycle
from pandaren.tool.decorator import tool
from pandaren.utils import expand_path, format_file_size
from ._utils import (
    record_file_access,
    set_last_read_mtime,
    get_last_read_mtime,
    validate_write_input,
    populate_read_cache,
)

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────
#  结构化输出（HasLLMFormat 协议，与 grep.py 的 GrepResult 一致）
# ────────────────────────────────────────────

@dataclass
class WriteResult:
    """write_file 的结构化结果。

    实现 HasLLMFormat 协议，ToolExecutor Phase 3 自动调用 __tool_format_for_llm__()。
    格式化为 LLM 可读文本；raw 数据保留在 ToolResult.data 中供框架层使用。
    """
    type: str                # "create" | "update"
    file_path: str
    content: str
    added_lines: int
    removed_lines: int = 0
    diff: str = ""

    def __tool_format_for_llm__(self) -> str:
        if self.type == "create":
            return (
                f"✅ 已创建新文件：{self.file_path}\n"
                f"   大小：{format_file_size(len(self.content.encode('utf-8')))}\n"
                f"   行数：{self.added_lines}"
            )
        else:
            lines = [
                f"✅ 已更新文件：{self.file_path}",
                f"   变更：+{self.added_lines} 行  -{self.removed_lines} 行",
            ]
            if self.diff:
                lines.append(f"\n{self.diff}")
            return "\n".join(lines)


# ────────────────────────────────────────────
#  LLM Guide — 对标 Claude Code FileWriteTool/prompt.ts
# ────────────────────────────────────────────

_WRITE_FILE_LLM_GUIDE = """创建新文件或完全覆盖现有文件。

使用规则：
- 如果目标文件已存在，你必须**先使用 read_file 工具读取其内容**，否则本工具会失败
- 修改现有文件时优先使用 edit_file——它只发送差异。仅当创建新文件或完全重写时才使用本工具
- 用户可能传递以 . 开头的隐藏目录名（如 .pandapal/plans/xxx），转换为绝对路径时必须保留前导点，.pandapal 是一个完整目录名
- **绝不**主动创建文档文件（*.md、README 等），除非用户明确要求
- 仅当用户明确要求时才使用 emoji，避免在文件中添加 emoji
- 自动创建不存在的父目录"""


# ── 工具定义 ──

@tool.function(
    tier=ToolTier.ALWAYS,
    name="write_file",
    description="创建新文件或完全覆盖现有文件，自动创建父目录。修改现有文件请优先使用 edit_file。",
    when_to_use="创建新文件或需要完全重写现有文件时调用。仅修改部分内容请使用 edit_file。",
    policy=ToolPolicy(
        sensitivity=SensitivityLevel.LOW,
        # sensitive_permission=SensitivePermission.DATA_WRITE,
        is_reversible=True, audit_required=False,
        is_idempotent=False, max_calls_per_turn=10,
    ),
    lifecycle=ToolLifecycle(validate_input=validate_write_input),
    llm_guide=_WRITE_FILE_LLM_GUIDE,
    progress_label='写入文件「{file_path}」',
)
def write_file(
    ctx: ToolContext,
    file_path: str,
    content: str,
) -> str:
    """创建或覆盖本地文件。

    Args:
        ctx: 工具上下文。
        file_path: 目标文件路径（绝对路径）。
        content: 要写入的文件内容。

    Returns:
        WriteResult（HasLLMFormat 协议，ToolExecutor 自动格式化）。
    """
    full_path = expand_path(file_path)
    abs_path = str(full_path)
    existed = full_path.exists()
    original = ""

    # 临界区二次检查：文件在 validate 和 write 之间可能被修改
    if existed:
        last_mtime = get_last_read_mtime(abs_path)
        try:
            current_mtime = os.stat(abs_path).st_mtime_ns
        except OSError:
            return f"错误：无法访问文件：{file_path}"
        if last_mtime is not None and current_mtime != last_mtime:
            return (
                f"错误：文件 '{file_path}' 在验证后又被修改（可能由用户或格式化工具）。\n"
                f"请重新读取文件内容后再写入。"
            )
        try:
            original = full_path.read_text(encoding="utf-8")
        except Exception:
            # original 仅用于生成 diff 展示（展示类）；读失败 diff 会失真，留痕暴露。
            logger.debug("write_file: 读取原文用于 diff 失败: %s", file_path, exc_info=True)

    try:
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")

        # ── 写后：记录 WorkingMemory + 填充缓存 + 更新 read mtime ──
        record_file_access(ctx, file_path, op="write")
        new_mtime = os.stat(abs_path).st_mtime_ns
        populate_read_cache(abs_path, content, new_mtime)
        set_last_read_mtime(abs_path, new_mtime)

        # ── 结构化输出（返回对象，ToolExecutor Phase 3 自动格式化）──
        new_lines = len(content.splitlines())
        if existed:
            diff = _compute_diff(original, content, file_path)
            added, removed = _count_diff_changes(diff)
            return WriteResult(
                type="update", file_path=file_path, content=content,
                added_lines=added, removed_lines=removed, diff=diff,
            )
        else:
            return WriteResult(
                type="create", file_path=file_path, content=content,
                added_lines=new_lines,
            )

    except PermissionError:
        return f"错误：无权限写入：{file_path}"
    except Exception as e:
        return f"写入文件失败：{e}"


def _compute_diff(original: str, new_content: str, file_path: str) -> str:
    """计算 unified diff（对标 Claude Code 的 structuredPatch）。"""
    if not original:
        return ""
    a_lines = original.splitlines(keepends=True)
    b_lines = new_content.splitlines(keepends=True)
    difflines = list(difflib.unified_diff(
        a_lines, b_lines,
        fromfile=f"a/{file_path}", tofile=f"b/{file_path}",
    ))
    if not difflines:
        return ""
    if len(difflines) > 200:
        difflines = difflines[:200]
        difflines.append("... (diff 已截断)")
    return "".join(difflines)


def _count_diff_changes(diff: str) -> tuple[int, int]:
    """从 unified diff 统计实际增删行数（对标 Claude Code 的 countLinesChanged）。"""
    if not diff:
        return 0, 0
    added = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++ "))
    removed = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("--- "))
    return added, removed
