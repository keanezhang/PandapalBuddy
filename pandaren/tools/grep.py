"""pandaren/tools/grep.py — 文本搜索内置工具。

在文件或目录中搜索正则表达式模式。
优先调用系统 ripgrep（若已安装），降级为纯 Python 实现。

对标 Claude Code GrepTool，实现同等能力：
  - glob 参数：文件过滤（brace-aware 拆分）
  - type 参数：语言类型过滤（rg --type）
  - -A/-B/-C：独立控制前后上下文行数
  - -n：可控行号
  - -e 保护：pattern 以 - 开头时自动加 -e
  - 先截断再 relativize：性能优化
  - count 模式真实解析匹配数
  - 排序容错 + 文件名 tiebreaker
"""

from __future__ import annotations

import re
import subprocess
import pathlib
import shutil
from dataclasses import dataclass
from typing import Optional

from pandaren.tool.types import ToolTier, SensitivityLevel
from pandaren.tool import ToolContext
from pandaren.tool.definition.tool_policy import ToolPolicy
from pandaren.tool.definition.tool_lifecycle import ToolLifecycle
from pandaren.tool.definition.tool_result import ValidationResult
from pandaren.tool.decorator import tool
from pandaren.utils import (
    resolve_project_root,
    expand_path,
    suggest_similar_path,
)

# ★ 复用 glob.py 的大括号展开（{ts,tsx} → [ts, tsx]）
from pandaren.tools.glob import _expand_braces as _expand_braces

# ────────────────────────────────────────────
#  VCS 目录黑名单
# ────────────────────────────────────────────

_VCS_DIRS: frozenset[str] = frozenset({".git", ".svn", ".hg", ".bzr", ".jj", ".sl"})

# type 参数 → 文件扩展名映射（Python 降级模式使用，ripgrep 自身支持 --type）
_TYPE_EXT_MAP: dict[str, frozenset[str]] = {
    # ── 编程语言 ──
    "py": frozenset({".py", ".pyx", ".pxd", ".pyi"}),
    "js": frozenset({".js", ".mjs", ".cjs"}),
    "ts": frozenset({".ts", ".mts", ".cts"}),
    "tsx": frozenset({".tsx"}),
    "jsx": frozenset({".jsx"}),
    "rust": frozenset({".rs"}),
    "go": frozenset({".go"}),
    "java": frozenset({".java"}),
    "c": frozenset({".c", ".h"}),
    "cpp": frozenset({".cpp", ".cc", ".cxx", ".hpp", ".hxx"}),
    "sh": frozenset({".sh", ".bash", ".zsh"}),
    # ── 文档（个人助理 / 通用场景）──
    "md": frozenset({".md", ".markdown"}),
    "txt": frozenset({".txt", ".text", ".log"}),
    "json": frozenset({".json"}),
    "yaml": frozenset({".yaml", ".yml"}),
    "toml": frozenset({".toml"}),
    "xml": frozenset({".xml", ".svg"}),
    "html": frozenset({".html", ".htm"}),
    "css": frozenset({".css", ".scss", ".less"}),
    "rst": frozenset({".rst"}),
    "tex": frozenset({".tex", ".sty", ".cls"}),
    "csv": frozenset({".csv", ".tsv"}),
    "ini": frozenset({".ini", ".cfg", ".conf"}),
}

_RG_AVAILABLE: Optional[bool] = None


# ────────────────────────────────────────────
#  项目根目录探测 — 已迁移到 pandaren.utils
# ────────────────────────────────────────────
#   _resolve_project_root() → pandaren.utils.resolve_project_root()
#   _expand_path()          → pandaren.utils.expand_path()
# grep.py 内部直接使用导入的共享工具。


def _is_under_vcs(file_path: pathlib.Path) -> bool:
    """检查路径是否在 VCS 目录下。"""
    return any(part in _VCS_DIRS for part in file_path.parts)


def _has_ripgrep() -> bool:
    global _RG_AVAILABLE
    if _RG_AVAILABLE is None:
        _RG_AVAILABLE = shutil.which("rg") is not None
    return _RG_AVAILABLE


# ────────────────────────────────────────────
#  GrepResult — 结构化输出
# ────────────────────────────────────────────

@dataclass
class GrepResult:
    """grep 搜索结果（结构化）。

    实现 HasLLMFormat 协议，ToolExecutor 自动调用 __tool_format_for_llm__。
    """
    mode: str                         # "files_with_matches" | "content" | "count"
    filenames: list[str]              # 匹配的文件列表
    num_files: int                    # 匹配文件总数
    content: str | None = None        # content 模式的文本
    num_matches: int | None = None    # count 模式：解析后的真实匹配总数
    num_lines: int | None = None      # content 模式：显示的行数
    applied_limit: int | None = None  # 实际截断数（仅在发生截断时设置）
    applied_offset: int = 0           # 实际偏移量
    truncated: bool = False           # 是否被截断
    total_count: int = 0              # 截断前的总数

    def __tool_format_for_llm__(self) -> str:
        """HasLLMFormat 协议：将结构化结果转为 LLM 可读文本。"""
        lines: list[str] = []

        # Header — 按模式差异化
        parts: list[str] = []
        if self.mode == "content":
            parts.append(f"# 显示 {self.num_lines or 0} 行")
        elif self.mode == "count":
            parts.append(
                f"# {self.num_files} 个文件中, 共 {self.num_matches or 0} 处匹配"
            )
        else:
            parts.append(f"# 找到 {self.num_files} 个文件")

        if self.applied_limit is not None:
            parts.append(f"limit: {self.applied_limit}")
        if self.applied_offset > 0:
            parts.append(f"offset: {self.applied_offset}")
        lines.append(", ".join(parts))

        # Body
        if self.mode == "content" and self.content:
            lines.append(self.content)
        elif self.mode == "count" and self.content:
            # count 模式也展示每文件的明细
            lines.append(self.content)
        elif self.filenames:
            lines.extend(self.filenames)

        # Truncation hint — content 模式用 applied_limit，files 模式用 shown
        if self.truncated:
            if self.mode == "content":
                lines.append(
                    f"\n[仅显示前 {self.applied_limit} 行，"
                    f"使用 offset={self.applied_limit} 查看更多]"
                )
            elif self.filenames:
                shown = len(self.filenames)
                remaining = self.total_count - self.applied_offset - shown
                if remaining > 0:
                    next_offset = self.applied_offset + shown
                    lines.append(
                        f"\n[已显示 {self.applied_offset + 1}-{self.applied_offset + shown}/{self.total_count}"
                        f" 条，使用 offset={next_offset} 查看更多]"
                    )

        return "\n".join(lines)


# ────────────────────────────────────────────
#  内部辅助
# ────────────────────────────────────────────

def _to_relative(p_str: str, base: pathlib.Path | None = None) -> str:
    """将绝对路径转为相对于 base 的路径（节省 token），默认相对于 CWD。

    自动处理 rg 标准输出格式：
      path:lineno:content  → relativize(path) + :lineno:content
      path:content         → relativize(path) + :content
      path:count           → relativize(path) + :count
      path                 → relativize(path)              (files_with_matches)
    """
    base = base or pathlib.Path.cwd()
    # 提取路径前缀（rg 输出中的第一个 : 之前是文件路径）
    colon_idx = p_str.find(":")
    if colon_idx > 0:
        filepath = p_str[:colon_idx]
        rest = p_str[colon_idx:]
        try:
            return str(pathlib.Path(filepath).relative_to(base)) + rest
        except ValueError:
            return p_str
    else:
        try:
            return str(pathlib.Path(p_str).relative_to(base))
        except ValueError:
            return p_str


# ── suggest_similar_path 已迁移到 pandaren.utils.suggest_similar_path ──


def _get_mtime_ms(p_str: str) -> float:
    """获取文件的修改时间（Unix timestamp），失败返回 0。"""
    try:
        return pathlib.Path(p_str).stat().st_mtime
    except OSError:
        return 0.0


def _split_glob_patterns(glob_str: str) -> list[str]:
    """拆分空格/逗号分隔的 glob pattern，保护大括号内的逗号。

    对齐 Claude Code GrepTool 的 brace-aware 拆分：
      '*.py *.ts'       → ['*.py', '*.ts']
      '*.{ts,tsx}'     → ['*.{ts,tsx}']       (大括号内逗号不拆分)
      '*.md, *.py'      → ['*.md', '*.py']
      '*.{ts,tsx} *.py' → ['*.{ts,tsx}', '*.py']
    """
    patterns: list[str] = []
    for chunk in glob_str.split():
        if "{" in chunk and "}" in chunk:
            patterns.append(chunk)
        else:
            for sub in chunk.split(","):
                sub = sub.strip()
                if sub:
                    patterns.append(sub)
    return [p for p in patterns if p]


# ────────────────────────────────────────────
#  ripgrep 实现
# ────────────────────────────────────────────

def _run_ripgrep(
    pattern: str,
    path: str,
    output_mode: str,
    context: int,
    context_before: int,
    context_after: int,
    case_insensitive: bool,
    multiline: bool,
    head_limit: int,
    offset: int,
    glob: str | None,
    type: str | None,
    show_line_numbers: bool,
    no_ignore: bool,
    search_root: pathlib.Path,
) -> GrepResult:
    cmd = ["rg", "--hidden"]
    # VCS 排除
    for vcs_dir in _VCS_DIRS:
        cmd += ["--glob", f"!{vcs_dir}"]
    # 限制单行长度（避免 minified 内容污染）
    cmd += ["--max-columns", "500"]

    # .gitignore 控制
    if no_ignore:
        cmd.append("--no-ignore")

    if case_insensitive:
        cmd.append("-i")
    if multiline:
        cmd += ["-U", "--multiline-dotall"]

    # 输出模式
    # 强制显示文件名（搜单文件时不丢失路径前缀）
    cmd.append("--with-filename")

    if output_mode == "files_with_matches":
        cmd.append("-l")
    elif output_mode == "count":
        cmd.append("-c")
    elif show_line_numbers:
        cmd.append("-n")

    # 上下文行（-C 优先于 -A/-B，对齐 Claude Code）
    if output_mode == "content":
        if context > 0:
            cmd += ["-C", str(context)]
        else:
            if context_before > 0:
                cmd += ["-B", str(context_before)]
            if context_after > 0:
                cmd += ["-A", str(context_after)]

    # type 过滤（rg --type）
    if type:
        cmd += ["--type", type]

    # glob 过滤（brace-aware 拆分 + 大括号展开）
    if glob:
        for g in _split_glob_patterns(glob):
            # ★ rg --glob 不支持 {a,b} 大括号语法，需预展开
            for expanded in _expand_braces(g):
                cmd += ["--glob", expanded]

    # pattern 以 - 开头时用 -e 保护
    if pattern.startswith("-"):
        cmd += ["-e", pattern]
    else:
        cmd.append(pattern)

    cmd.append(path)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        output = result.stdout.strip()

        if not output and result.returncode not in (0, 1):
            error_msg = result.stderr.strip()
            raise RuntimeError(f"ripgrep 执行失败: {error_msg}")

        if not output:
            return GrepResult(
                mode=output_mode, filenames=[], num_files=0,
            )

        lines = output.splitlines()
        total = len(lines)

        # ── 按模式分别处理 ──
        if output_mode == "files_with_matches":
            # 排序：mtime 降序 + 文件名 tiebreaker（对齐 Claude Code）
            lines.sort(key=lambda fp: (-_get_mtime_ms(fp), fp))

            # 先截断
            if offset > 0:
                lines = lines[offset:]
            truncated = head_limit > 0 and len(lines) > head_limit
            if truncated:
                lines = lines[:head_limit]

            # 后 relativize（先截断再转换，对齐 Claude Code 性能优化）
            lines = [_to_relative(l, search_root) for l in lines]

            return GrepResult(
                mode=output_mode,
                filenames=lines,
                num_files=total,
                applied_limit=head_limit if truncated else None,
                applied_offset=offset,
                truncated=truncated,
                total_count=total,
            )

        elif output_mode == "count":
            # 先截断
            if offset > 0:
                lines = lines[offset:]
            truncated = head_limit > 0 and len(lines) > head_limit
            if truncated:
                lines = lines[:head_limit]

            # 后 relativize
            lines = [_to_relative(l, search_root) for l in lines]

            # 解析 count 输出：每行格式 "filepath:count"
            total_matches = 0
            file_count = 0
            for line in lines:
                colon_idx = line.rfind(":")
                if colon_idx > 0:
                    try:
                        total_matches += int(line[colon_idx + 1:])
                    except ValueError:
                        pass
                    file_count += 1

            return GrepResult(
                mode=output_mode,
                filenames=[],
                num_files=file_count,
                content="\n".join(lines),
                num_matches=total_matches,
                applied_limit=head_limit if truncated else None,
                applied_offset=offset,
                truncated=truncated,
                total_count=total,
            )

        else:
            # content 模式：先截断再 relativize
            if offset > 0:
                lines = lines[offset:]
            truncated = head_limit > 0 and len(lines) > head_limit
            if truncated:
                lines = lines[:head_limit]

            lines = [_to_relative(l, search_root) for l in lines]
            return GrepResult(
                mode=output_mode,
                filenames=[],
                num_files=0,
                content="\n".join(lines),
                num_lines=len(lines),
                applied_limit=head_limit if truncated else None,
                applied_offset=offset,
                truncated=truncated,
                total_count=total,
            )

    except subprocess.TimeoutExpired:
        raise RuntimeError(f"搜索超时（30 秒）：{pattern}")
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"搜索异常：{e}") from e


# ────────────────────────────────────────────
#  Python 降级实现
# ────────────────────────────────────────────

def _run_python_grep(
    pattern: str,
    path: str,
    output_mode: str,
    context: int,
    context_before: int,
    context_after: int,
    case_insensitive: bool,
    multiline: bool,
    head_limit: int,
    offset: int,
    glob: str | None,
    type: str | None,
    show_line_numbers: bool,
    no_ignore: bool,
    search_root: pathlib.Path,
) -> GrepResult:
    flags = re.IGNORECASE if case_insensitive else 0
    if multiline:
        flags |= re.DOTALL

    try:
        compiled = re.compile(pattern, flags)
    except re.error as e:
        raise RuntimeError(f"正则表达式错误：{e}") from e

    # ── 文件收集（支持 glob 和 type 过滤）──
    base = pathlib.Path(path)
    if base.is_file():
        files = [base]
    elif base.is_dir():
        files = [
            p for p in base.glob("**/*")
            if p.is_file() and not _is_under_vcs(p)
        ]
    else:
        raise RuntimeError(f"路径不存在：{path}")

    # glob 过滤（brace-aware 拆分 + 大括号展开）
    if glob:
        import fnmatch
        raw_patterns = _split_glob_patterns(glob)
        # ★ fnmatch 不支持 {a,b} 大括号，需预展开
        glob_patterns: list[str] = []
        for rp in raw_patterns:
            if "{" in rp:
                glob_patterns.extend(_expand_braces(rp))
            else:
                glob_patterns.append(rp)
        filtered: list[pathlib.Path] = []
        for f in files:
            name = f.name
            for gp in glob_patterns:
                if fnmatch.fnmatch(name, gp):
                    filtered.append(f)
                    break
        files = filtered

    # type 过滤（按扩展名映射）
    if type:
        extensions = _TYPE_EXT_MAP.get(type.lower())
        if extensions:
            files = [f for f in files if f.suffix in extensions]

    matched_files: list[tuple[str, float]] = []  # (rel_path, mtime)
    file_counts: list[str] = []
    content_lines: list[str] = []

    # 上下文行数
    ctx_around = context
    ctx_before = context_before if ctx_around == 0 else ctx_around
    ctx_after = context_after if ctx_around == 0 else ctx_around

    for file in files:
        try:
            if multiline:
                text = file.read_text(encoding="utf-8", errors="replace")
            else:
                lines_text = file.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue

        if multiline:
            matches = list(compiled.finditer(text))
            if not matches:
                continue
            rel = _to_relative(str(file), search_root)
            if output_mode == "files_with_matches":
                mtime = _get_mtime_ms(str(file))
                matched_files.append((rel, mtime))
            elif output_mode == "count":
                file_counts.append(f"{rel}:{len(matches)}")
            else:
                line_prefix = f"{rel}:" if show_line_numbers else ""
                for m in matches:
                    line_num = text[:m.start()].count("\n") + 1
                    snippet = text[max(0, m.start() - 50):m.end() + 50].replace("\n", "\\n")
                    if show_line_numbers:
                        content_lines.append(f"{rel}:{line_num}:{snippet}")
                    else:
                        content_lines.append(f"{rel}:{snippet}")
        else:
            file_hits: list[tuple[int, str]] = []
            for i, line in enumerate(lines_text, start=1):
                if compiled.search(line):
                    file_hits.append((i, line))

            if not file_hits:
                continue

            rel = _to_relative(str(file), search_root)
            if output_mode == "files_with_matches":
                mtime = _get_mtime_ms(str(file))
                matched_files.append((rel, mtime))
            elif output_mode == "count":
                file_counts.append(f"{rel}:{len(file_hits)}")
            else:
                for lineno, linetext in file_hits:
                    if show_line_numbers:
                        content_lines.append(f"{rel}:{lineno}:{linetext}")
                    else:
                        content_lines.append(f"{rel}:{linetext}")

                    # 添加上下文行
                    if ctx_before > 0:
                        start_idx = max(0, lineno - 1 - ctx_before)
                        for bi in range(start_idx, lineno - 1):
                            if show_line_numbers:
                                content_lines.append(
                                    f"{rel}-{bi + 1}-{lines_text[bi]}"
                                )
                            else:
                                content_lines.append(f"{rel}-{lines_text[bi]}")
                    if ctx_after > 0:
                        end_idx = min(len(lines_text), lineno + ctx_after)
                        for ai in range(lineno, end_idx):
                            if show_line_numbers:
                                content_lines.append(
                                    f"{rel}-{ai + 1}-{lines_text[ai]}"
                                )
                            else:
                                content_lines.append(f"{rel}-{lines_text[ai]}")

    # ── 输出处理 ──
    if output_mode == "files_with_matches":
        matched_files.sort(key=lambda x: (-x[1], x[0]))  # mtime 降序 + 文件名 tiebreaker
        total = len(matched_files)
        if offset > 0:
            matched_files = matched_files[offset:]
        truncated = head_limit > 0 and len(matched_files) > head_limit
        if truncated:
            matched_files = matched_files[:head_limit]
        return GrepResult(
            mode=output_mode,
            filenames=[p for p, _ in matched_files],
            num_files=total,
            applied_limit=head_limit if truncated else None,
            applied_offset=offset,
            truncated=truncated,
            total_count=total,
        )
    elif output_mode == "count":
        out = file_counts
        total = len(out)
        if offset > 0:
            out = out[offset:]
        truncated = head_limit > 0 and len(out) > head_limit
        if truncated:
            out = out[:head_limit]

        # 解析 count
        total_matches = 0
        file_count = 0
        for line in out:
            colon_idx = line.rfind(":")
            if colon_idx > 0:
                try:
                    total_matches += int(line[colon_idx + 1:])
                except ValueError:
                    pass
                file_count += 1

        return GrepResult(
            mode=output_mode,
            filenames=[],
            num_files=file_count,
            content="\n".join(out),
            num_matches=total_matches,
            applied_limit=head_limit if truncated else None,
            applied_offset=offset,
            truncated=truncated,
            total_count=total,
        )
    else:
        out = content_lines
        total = len(out)
        if offset > 0:
            out = out[offset:]
        truncated = head_limit > 0 and len(out) > head_limit
        if truncated:
            out = out[:head_limit]
        return GrepResult(
            mode=output_mode,
            filenames=[],
            num_files=0,
            content="\n".join(out),
            num_lines=len(out),
            applied_limit=head_limit if truncated else None,
            applied_offset=offset,
            truncated=truncated,
            total_count=total,
        )


# ────────────────────────────────────────────
#  validate_input 钩子
# ────────────────────────────────────────────

def _validate_grep_input(args: dict, ctx: ToolContext) -> ValidationResult | None:
    """执行前校验：路径存在性 + Did you mean... 建议（基于项目根解析路径）。"""
    path = args.get("path")
    # 对齐 glob：默认路径解析为项目根，相对路径基于项目根
    root = resolve_project_root()
    base = expand_path(path, root) if path else root

    if not base.exists():
        suggestion = suggest_similar_path(path or str(root))
        message = f"路径不存在：{path or str(root)}（项目根目录：{root}）"
        if suggestion:
            message = f"路径不存在：{path or str(root)}。Did you mean `{suggestion}`?（项目根目录：{root}）"
        return ValidationResult(valid=False, message=message, error_code=1)

    return None


# ────────────────────────────────────────────
#  grep 工具
# ────────────────────────────────────────────

GrepLLMGuide = """使用 ripgrep 进行文件内容搜索。严禁使用 bash grep/rg 搜索文件内容。

重要规则：
- 始终使用本工具搜索文件内容。严禁在 bash 中调用 grep/rg，这会导致跳过权限检查。
- 默认只返回文件名（output_mode="files_with_matches"），需要内容时指定 output_mode="content"
- 支持完整正则语法，但大括号等特殊字符需要转义：用 \\{\\} 匹配 {}
- 需要跨行搜索时使用 multiline=True（如搜索跨行 struct 定义）
- 优先用 glob 参数一步完成过滤，而非先调 glob 再调 grep：
  如 grep(pattern="def test", glob="*.py") 直接在 Python 文件中搜索
- type 参数按语言类型过滤：type="py" 只搜 Python 文件，type="js" 只搜 JS 文件
- context_before/context_after 独立控制匹配前后的上下文行数（仅 content 模式）
- head_limit 默认 250，用 offset 翻页查看更多结果
- 搜索文件名用 glob 工具，搜索文件内容用 grep 工具
- 排除目录用 path 参数定位到子目录，而非依赖过滤输出
"""


@tool.function(
    tier=ToolTier.ALWAYS,
    name="grep",
    description="使用正则表达式搜索文件内容。默认仅返回匹配的文件路径列表。",
    when_to_use=(
        "当需要按文件内容搜索时调用：搜函数定义、类名、TODO、字符串、配置项等。"
        "搜文件名请用 glob（不要用 bash find），搜内容请用 grep（不要用 bash grep/rg）"
    ),
    policy=ToolPolicy(
        sensitivity=SensitivityLevel.LOW,
        is_reversible=True,
        is_idempotent=True,
        max_output_bytes=100_000,
        read_only=True,
        supports_offset_pagination=True,
    ),
    lifecycle=ToolLifecycle(
        validate_input=_validate_grep_input,
    ),
    llm_guide=GrepLLMGuide,
)
def grep(
    ctx: ToolContext,
    pattern: str,
    path: str = ".",
    # ── 新增参数（对齐 Claude Code）──
    glob: str | None = None,
    type: str | None = None,
    context_before: int = 0,
    context_after: int = 0,
    show_line_numbers: bool = True,
    no_ignore: bool = False,
    # ── 已有参数 ──
    output_mode: str = "files_with_matches",
    context: int = 0,
    case_insensitive: bool = False,
    head_limit: int = 250,
    offset: int = 0,
    multiline: bool = False,
) -> GrepResult:
    """在文件或目录中使用正则表达式搜索内容。

    Args:
        pattern: 正则表达式模式，如 'def\\s+\\w+'、'class Foo'、'TODO'。
                 ripgrep 语法（非 Python re），大括号需转义：`\\{\\}`。
        path: 搜索路径（文件或目录），默认为当前目录。
        glob: Glob 模式过滤文件，如 "*.py"、"*.{ts,tsx}"。空格/逗号分隔多个模式。
              映射到 rg --glob，在文件遍历层面过滤，比后筛选更高效。
        type: 文件类型过滤（rg --type），如 "py"、"js"、"rust"、"go"。
        context_before: 匹配行前显示 N 行（rg -B），仅 content 模式。
        context_after: 匹配行后显示 N 行（rg -A），仅 content 模式。
        show_line_numbers: 是否显示行号（rg -n），仅 content 模式，默认 True。
        no_ignore: 是否忽略 .gitignore 规则，默认 False（遵循 .gitignore）。
        output_mode: 输出模式：
            'files_with_matches' — 仅返回匹配的文件路径列表（默认）
            'content'            — 返回匹配行内容（含行号和文件路径）
            'count'              — 返回每个文件的匹配数量
        context: 每个匹配行前后显示的上下文行数（rg -C），仅 content 模式。
                 当与 context_before/context_after 同时指定时，-C 优先。
        case_insensitive: 是否忽略大小写。
        head_limit: 限制输出行数，默认 250。传 0 取消限制。
        offset: 翻页偏移量，配合 head_limit 实现分页。
        multiline: 是否启用跨行匹配（. 匹配换行符），用于搜索跨行模式。

    Returns:
        GrepResult 结构化结果（自动转为 LLM 可读文本）。
    """
    # 对齐 glob：默认路径解析为项目根，相对路径基于项目根展开
    root = resolve_project_root()
    resolved_path = str(expand_path(path, root) if path else root)

    try:
        if _has_ripgrep():
            return _run_ripgrep(
                pattern=pattern, path=resolved_path, search_root=root,
                output_mode=output_mode,
                context=context, context_before=context_before, context_after=context_after,
                case_insensitive=case_insensitive, multiline=multiline,
                head_limit=head_limit, offset=offset,
                glob=glob, type=type,
                show_line_numbers=show_line_numbers, no_ignore=no_ignore,
            )
        else:
            return _run_python_grep(
                pattern=pattern, path=resolved_path, search_root=root,
                output_mode=output_mode,
                context=context, context_before=context_before, context_after=context_after,
                case_insensitive=case_insensitive, multiline=multiline,
                head_limit=head_limit, offset=offset,
                glob=glob, type=type,
                show_line_numbers=show_line_numbers, no_ignore=no_ignore,
            )
    except RuntimeError as e:
        return GrepResult(
            mode=output_mode, filenames=[], num_files=0,
            content=str(e),
        )
