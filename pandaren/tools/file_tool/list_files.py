"""pandaren/tools/file_tool/list_files.py — 目录列表工具。

对标 Claude Code 中通过 BashTool ls/find 实现的目录浏览能力，
封装为独立工具以提升 LLM 使用效率。

v2 改进：
- validate_input → 共用 _utils.validate_list_input（UNC + 安全基检）
- expand_path：~ 展开、相对路径→项目根
"""
from __future__ import annotations

import os
import pathlib
import stat
from typing import Optional

from pandaren.tool.types import ToolTier, SensitivityLevel
from pandaren.tool.definition.context import ToolContext
from pandaren.tool.definition.tool_policy import ToolPolicy
from pandaren.tool.definition.tool_lifecycle import ToolLifecycle
from pandaren.tool.decorator import tool
from pandaren.utils import expand_path, format_file_size
from ._utils import validate_list_input

# ── VCS 目录黑名单 ──
_VCS_DIRS: frozenset[str] = frozenset({".git", ".svn", ".hg", ".bzr", ".jj", ".sl"})


def _is_under_vcs(file_path: pathlib.Path) -> bool:
    return any(part in _VCS_DIRS for part in file_path.parts)


# ── 文件类型标记 ──

def _format_entry(entry: pathlib.Path, base: pathlib.Path) -> str:
    """格式化单个目录条目。"""
    try:
        st = entry.lstat()
    except OSError:
        return f"??? {entry.name}"

    rel = entry.relative_to(base) if entry != base else pathlib.Path(entry.name)

    if stat.S_ISLNK(st.st_mode):
        try:
            target = os.readlink(str(entry))
            return f"🔗 {rel} -> {target}"
        except OSError:
            return f"🔗 {rel}"

    if stat.S_ISDIR(st.st_mode):
        return f"📁 {rel}/"

    return f"📄 {rel}  ({format_file_size(st.st_size)})"


# ────────────────────────────────────────────
#  LLM Guide — 对标 Claude Code GlobTool + BashTool prompt
# ────────────────────────────────────────────

_LIST_FILES_LLM_GUIDE = """列出目录下的文件和子目录，了解项目结构。

使用规则：
- **浏览未知目录结构时始终使用本工具，不要用 bash ls/find**。本工具提供统一的格式化输出和安全检查
- 默认只列出一级内容（不递归）；需要深度遍历时设置 recursive=True
- 默认不显示隐藏文件（. 开头）；需要时设置 include_hidden=True
- 精确搜索已知文件名模式请用 glob 工具；搜索文件内容请用 grep 工具
- 自动排除 .git/.svn 等版本控制目录
- 最多返回 200 个条目（可通过 max_entries 调整上限）
- 本工具只能列出目录，不能读取文件内容——读取文件内容请用 read_file"""


# ── 工具定义 ──

@tool.function(
    tier=ToolTier.ALWAYS,
    name="list_files",
    description=(
        "列出指定目录下的文件和子目录。支持 glob 模式过滤和递归搜索。"
        "默认只列出当前目录的一级内容，不递归。"
    ),
    when_to_use=(
        "需要查看目录内容、了解项目结构时调用——始终使用本工具，不要用 bash ls/find。"
        "浏览未知目录用本工具；精确搜索文件名用 glob；搜索文件内容用 grep"
    ),
    policy=ToolPolicy(
        sensitivity=SensitivityLevel.LOW,
        is_reversible=True, audit_required=False,
        is_idempotent=True, max_output_bytes=50_000,
        read_only=True,
    ),
    lifecycle=ToolLifecycle(validate_input=validate_list_input),
    llm_guide=_LIST_FILES_LLM_GUIDE,
    progress_label='列出目录「{path}」',
)
def list_files(
    ctx: ToolContext,
    path: str = ".",
    glob_pattern: Optional[str] = None,
    recursive: bool = False,
    include_hidden: bool = False,
    max_entries: int = 200,
) -> str:
    """列出目录下的文件和子目录。

    Args:
        ctx: 工具上下文。
        path: 目标目录路径，默认为当前工作目录。
        glob_pattern: 可选 glob 过滤，如 "*.py", "*.{py,md}"。
        recursive: 是否递归列出所有子目录。
        include_hidden: 是否包含隐藏文件/目录（以 . 开头）。
        max_entries: 最多返回条目数。

    Returns:
        格式化的目录列表；错误时返回说明。
    """
    base = expand_path(path)

    try:
        if recursive:
            entries = list(base.rglob("*" if glob_pattern is None else glob_pattern))
        elif glob_pattern is not None:
            entries = list(base.glob(glob_pattern))
        else:
            entries = list(base.iterdir())

        entries = [e for e in entries if not _is_under_vcs(e)]

        if not include_hidden:
            entries = [
                e for e in entries
                if not any(part.startswith(".") for part in e.relative_to(base).parts)
            ]

        if not entries:
            filter_msg = f"（模式：{glob_pattern}）" if glob_pattern else ""
            return f"目录为空{filter_msg}：{base}"

        dirs = sorted([e for e in entries if e.is_dir()], key=lambda p: p.name.lower())
        files = sorted([e for e in entries if e.is_file()], key=lambda p: p.name.lower())
        symlinks = sorted([e for e in entries if e.is_symlink()], key=lambda p: p.name.lower())
        others = sorted(
            [e for e in entries if not e.is_dir() and not e.is_file() and not e.is_symlink()],
            key=lambda p: p.name.lower(),
        )

        total = len(entries)
        all_entries = dirs + files + symlinks + others
        is_truncated = total > max_entries
        if is_truncated:
            all_entries = all_entries[:max_entries]

        lines = [
            f"# {base}",
            f"# {'递归 ' if recursive else ''}共 {total} 个条目（目录 {len(dirs)}，文件 {len(files)}，链接 {len(symlinks)}）",
            "",
        ]
        for entry in all_entries:
            lines.append(_format_entry(entry, base))

        if is_truncated:
            lines.append(f"\n... 已截断（显示前 {max_entries}/{total} 条），使用 glob_pattern 过滤或缩小 path 范围")

        return "\n".join(lines)

    except PermissionError:
        return f"错误：无权限访问目录：{path}"
    except Exception as e:
        return f"列出目录失败：{e}"
