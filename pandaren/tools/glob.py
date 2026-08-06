"""pandaren/tools/glob.py — 文件名搜索内置工具。

按通配符模式匹配文件路径，自动排除版本控制目录和常见忽略目录，按修改时间降序排序。
优先调用系统 ripgrep（若已安装），降级为纯 Python 实现。
"""

import fnmatch
import os
import pathlib
import re
import shutil
import stat as stat_mod
import subprocess
import time
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
    is_unc_path,
    suggest_similar_path,
)

# ────────────────────────────────────────────
#  目录跳过名单
# ────────────────────────────────────────────

# VCS 版本控制目录
_VCS_DIRS: frozenset[str] = frozenset({".git", ".svn", ".hg", ".bzr", ".jj", ".sl"})

# 常见需要跳过的目录（精确匹配）— Python 降级模式专用
_SKIP_DIRS_EXACT: frozenset[str] = frozenset({
    # 依赖/包管理
    "node_modules", ".venv", "venv", "env",
    # Python 缓存/构建
    "__pycache__", "dist", "build", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
    # IDE 配置
    ".idea", ".vscode",
})

# 常见需要跳过的目录（fnmatch 模式匹配）— Python 降级模式专用
_SKIP_DIR_PATTERNS: frozenset[str] = frozenset({
    "*.egg-info", "*.dist-info",
})

# 合并后的精确匹配集合（os.walk dirnames 过滤用）
_SKIP_DIRS: frozenset[str] = _VCS_DIRS | _SKIP_DIRS_EXACT

# ripgrep 可用性缓存（同 grep.py 模式）
_RG_AVAILABLE: Optional[bool] = None


def _has_ripgrep() -> bool:
    """检测系统是否安装了 ripgrep（结果缓存）。"""
    global _RG_AVAILABLE
    if _RG_AVAILABLE is None:
        _RG_AVAILABLE = shutil.which("rg") is not None
    return _RG_AVAILABLE


def _should_skip_dir(dirname: str) -> bool:
    """检查目录是否应该被跳过（VCS / 忽略目录 / 模式匹配）。"""
    if dirname in _SKIP_DIRS:
        return True
    return any(fnmatch.fnmatch(dirname, p) for p in _SKIP_DIR_PATTERNS)


# ── 以下函数已迁移到 pandaren.utils 作为共享基础设施 ──
#   set_search_root()       → pandaren.utils.set_search_root()
#   _resolve_project_root() → pandaren.utils.resolve_project_root()
#   _expand_path()          → pandaren.utils.expand_path()
#   _is_unc_path()          → pandaren.utils.is_unc_path()
#   _suggest_similar_path() → pandaren.utils.suggest_similar_path()
# glob.py 内部直接使用导入的共享工具。


# ────────────────────────────────────────────
#  Brace expansion（{a,b} → 多个 pattern）
# ────────────────────────────────────────────

_BRACE_RE = re.compile(r"\{([^{}]+)\}")


def _expand_braces(pattern: str) -> list[str]:
    """展开 {a,b,c} 大括号模式为多个独立 pattern。

    例如：
      '*.{py,tsx}'     → ['*.py', '*.tsx']
      'src/**/*.{ts}'  → ['src/**/*.ts']  （单选项原样保留）
      '**/*.py'        → ['**/*.py']       （无大括号原样返回）

    仅展开最外层大括号，不处理嵌套 {a,{b,c}}。
    """
    match = _BRACE_RE.search(pattern)
    if not match:
        return [pattern]

    brace_content = match.group(1)
    alternatives = [a.strip() for a in brace_content.split(",")]

    # 单选项 {ts} → 不展开，保持原样（ripgrep 自己支持单选项大括号）
    if len(alternatives) <= 1:
        return [pattern]

    prefix = pattern[:match.start()]
    suffix = pattern[match.end():]

    results = []
    for alt in alternatives:
        expanded = prefix + alt + suffix
        # 递归展开嵌套大括号
        results.extend(_expand_braces(expanded))
    return results


# ────────────────────────────────────────────
#  环境变量配置（同 Claude Code 的可配置模式）
# ────────────────────────────────────────────

def _env_bool(key: str, default: bool = True) -> bool:
    """读取环境变量布尔值，空字符串视为 unset（同 Claude Code 的 || 语义）。"""
    val = os.environ.get(key, "")
    if not val:
        return default
    return val.lower() in ("true", "1", "yes")


# ────────────────────────────────────────────
#  validate_input 钩子
# ────────────────────────────────────────────

def _validate_glob_input(args: dict, ctx: ToolContext) -> ValidationResult | None:
    """执行前校验：路径存在性 + UNC 安全 + Did you mean... 建议。"""
    path = args.get("path")
    if not path:
        return None

    # SECURITY: 跳过 UNC 路径的文件系统操作，防止 NTLM 凭据泄露
    if is_unc_path(path):
        return None  # 不做进一步检查，避免触发网络请求

    root = resolve_project_root()
    base = expand_path(path, root)

    if base.exists():
        if not base.is_dir():
            return ValidationResult(
                valid=False,
                message=f"路径不是目录：{path}",
                error_code=2,
            )
        return None

    suggestion = suggest_similar_path(path)
    message = f"目录不存在：{path}（项目根目录：{root}）"
    if suggestion:
        message = f"目录不存在：{path}。Did you mean `{suggestion}`?（项目根目录：{root}）"
    return ValidationResult(valid=False, message=message, error_code=1)


# ────────────────────────────────────────────
#  ripgrep 后端
# ────────────────────────────────────────────

def _extract_glob_base_directory(pattern: str) -> tuple[str, str]:
    """从 glob pattern 中提取静态基础目录和剩余相对 pattern。

    同 Claude Code 的 extractGlobBaseDirectory()：
    找到首个 glob 特殊字符（* ? [ {），将其之前的路径部分作为 baseDir。
    """
    # 找首个 glob 特殊字符
    glob_chars_re = re.compile(r"[*?[{]")
    match = glob_chars_re.search(pattern)

    if not match or match.start() == 0:
        return "", pattern

    static_prefix = pattern[:match.start()]

    # 找静态前缀中最后一个路径分隔符
    last_sep = max(static_prefix.rfind("/"), static_prefix.rfind(os.sep))

    if last_sep == -1:
        return "", pattern

    base_dir = static_prefix[:last_sep]
    relative_pattern = pattern[last_sep + 1:]

    # 处理根目录（如 /*.txt → baseDir="/"）
    if not base_dir and last_sep == 0:
        base_dir = "/"

    return base_dir, relative_pattern


def _run_ripgrep_glob(
    pattern: str,
    cwd: pathlib.Path,
    max_results: int,
    no_ignore: bool,
    hidden: bool,
) -> list[str]:
    """使用 ripgrep 执行文件名搜索。

    对标 Claude Code 的 utils/glob.ts：
    - rg --files --glob <pattern> --sort=modified
    - 支持 --no-ignore / --hidden 配置
    - 自动处理绝对路径 pattern（提取 baseDir）
    """
    search_dir = str(cwd)
    search_pattern = pattern

    # 处理绝对路径：ripgrep --glob 只接受相对 pattern
    if os.path.isabs(pattern):
        base_dir, relative_pattern = _extract_glob_base_directory(pattern)
        if base_dir:
            search_dir = base_dir
            search_pattern = relative_pattern

    cmd = ["rg", "--files", "--sort=modified"]

    # 大括号展开：rg 不支持 {a,b}，需要预展开
    expanded_patterns = _expand_braces(search_pattern)

    for p in expanded_patterns:
        cmd += ["--glob", p]

    # .gitignore 控制（默认遵守，同 Claude Code 的 CLAUDE_CODE_GLOB_NO_IGNORE）
    if no_ignore:
        cmd.append("--no-ignore")

    # 隐藏文件控制（默认包含，同 Claude Code 的 CLAUDE_CODE_GLOB_HIDDEN）
    if hidden:
        cmd.append("--hidden")

    # 排除 VCS 目录
    for vcs_dir in _VCS_DIRS:
        cmd += ["--glob", f"!{vcs_dir}"]

    cmd.append(search_dir)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
        )
        output = result.stdout.strip()

        if not output:
            if result.returncode not in (0, 1):
                error_msg = result.stderr.strip()
                raise RuntimeError(f"ripgrep 执行失败: {error_msg}")
            return []

        lines = output.splitlines()
        return lines

    except subprocess.TimeoutExpired:
        raise RuntimeError(f"搜索超时（30 秒）：{pattern}")
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"ripgrep 搜索异常：{e}") from e


# ────────────────────────────────────────────
#  纯 Python 降级实现
# ────────────────────────────────────────────

def _parse_gitignore(gitignore_path: pathlib.Path) -> list[str]:
    """解析 .gitignore 文件，返回忽略模式列表。"""
    patterns = []
    try:
        with open(gitignore_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                # 跳过空行和注释
                if not line or line.startswith("#"):
                    continue
                # 去掉尾部空格（转义空格 \ 除外）
                if not line.endswith("\\"):
                    line = line.rstrip()
                # 目录标记 / 保持原样，后续在匹配时处理
                patterns.append(line)
    except OSError:
        pass
    return patterns


def _collect_gitignore_patterns(root: pathlib.Path) -> dict[pathlib.Path, list[str]]:
    """收集目录树中所有 .gitignore 的忽略模式。

    Returns:
        {目录路径: [忽略模式列表]} — 每个目录下 .gitignore 的模式
    """
    ignores: dict[pathlib.Path, list[str]] = {}
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in _VCS_DIRS]
        if ".gitignore" in filenames:
            gitignore_path = pathlib.Path(dirpath) / ".gitignore"
            patterns = _parse_gitignore(gitignore_path)
            if patterns:
                ignores[pathlib.Path(dirpath)] = patterns
    return ignores


def _matches_gitignore_pattern(path: str, pattern: str, is_dir: bool = False) -> bool:
    """检查路径是否匹配单个 .gitignore 模式。

    实现了 .gitignore 的核心语义：
    - foo/      → 仅匹配目录
    - /foo      → 相对于 .gitignore 所在目录
    - **/foo    → 任意层级
    - foo/**/bar → foo 下任意层级的 bar
    """
    # 处理取反模式（! 开头表示不忽略）
    if pattern.startswith("!"):
        return False  # 取反模式不在此处处理

    # 去掉尾部空格（已在上游处理）
    p = pattern

    # 目录专属模式（以 / 结尾）
    dir_only = p.endswith("/")
    if dir_only:
        p = p[:-1]
        if not is_dir:
            return False

    # 前导 / 表示相对于 .gitignore 所在目录
    if p.startswith("/"):
        p = p[1:]
        # 直接匹配
        return fnmatch.fnmatch(path, p) or fnmatch.fnmatch(os.path.basename(path), p)

    # **/ 表示任意层级
    if p.startswith("**/"):
        p = p[3:]
        return fnmatch.fnmatch(path, p) or fnmatch.fnmatch(os.path.basename(path), p)

    if "/**/" in p:
        # foo/**/bar → foo 下任意层级的 bar
        parts = p.split("/**/")
        if len(parts) == 2:
            prefix, suffix = parts
            return (path.startswith(prefix + "/") and
                    fnmatch.fnmatch(path.split("/")[-1], suffix))

    # 普通模式：匹配文件名或完整路径
    return fnmatch.fnmatch(path, p) or fnmatch.fnmatch(os.path.basename(path), p)


def _is_ignored_by_gitignore(
    file_path: pathlib.Path,
    base: pathlib.Path,
    gitignores: dict[pathlib.Path, list[str]],
) -> bool:
    """检查文件是否被某个 .gitignore 规则忽略。"""
    # 从文件所在目录向上查找适用的 .gitignore
    current = file_path.parent
    while current >= base:
        if current in gitignores:
            try:
                rel_path = str(file_path.relative_to(current))
            except ValueError:
                continue
            for pattern in gitignores[current]:
                if _matches_gitignore_pattern(rel_path, pattern, False):
                    return True
        parent = current.parent
        if parent == current:
            break
        current = parent
    return False


def _walk_glob(
    base: pathlib.Path,
    pattern: str,
    max_collect: int = 10_000,
    hidden: bool = True,
    respect_gitignore: bool = True,
):
    """使用 os.walk 遍历目录树，跳过 VCS 和忽略目录，按模式匹配文件。

    支持的 pattern 格式：
      - '*.py'         → 非递归，仅 base 目录
      - '**/*.py'      → 递归搜索
      - 'src/**/*.ts'  → src/ 下递归搜索

    Args:
        base: 搜索根目录。
        pattern: 通配符模式。
        max_collect: 最大收集文件数，防止超大项目内存爆炸。默认 10000。
        hidden: 是否包含隐藏文件（. 开头）。默认 True。
        respect_gitignore: 是否遵守 .gitignore 规则。默认 True。

    Yields:
        (pathlib.Path, float) — (文件绝对路径, mtime)
    """
    # ── 预处理：大括号展开 ──
    expanded_patterns = _expand_braces(pattern)

    # ── 收集 .gitignore 规则 ──
    gitignores: dict[pathlib.Path, list[str]] = {}
    if respect_gitignore:
        gitignores = _collect_gitignore_patterns(base)

    seen: set[pathlib.Path] = set()
    count = 0

    for current_pattern in expanded_patterns:
        # ── 解析 pattern ──
        if "**" in current_pattern:
            recursive = True
            # ★ 循环处理多个 **，避免 **/dirname/** 这类模式
            #    只提取一次 prefix 导致 suffix 残留目录前缀
            #    例如 "**/pandapal_desktop/**" → prefix="" suffix="pandapal_desktop/**"
            #    fnmatch 不支持 **，匹配 filename 永远失败
            remaining = current_pattern
            prefix_parts: list[str] = []
            while "**" in remaining:
                idx = remaining.index("**")
                prefix_part = remaining[:idx].rstrip("/")
                suffix = remaining[idx + 2:].lstrip("/") or "*"
                if prefix_part:
                    prefix_parts.append(prefix_part)
                remaining = suffix
            prefix = "/".join(prefix_parts) if prefix_parts else ""
        else:
            recursive = False
            if "/" in current_pattern:
                idx = current_pattern.rindex("/")
                prefix = current_pattern[:idx]
                suffix = current_pattern[idx + 1:]
            else:
                prefix = ""
                suffix = current_pattern

        search_root = base / prefix if prefix else base
        if not search_root.is_dir():
            continue

        # ── 遍历：就地修改 dirnames 实现目录剪枝 ──
        for dirpath, dirnames, filenames in os.walk(search_root, topdown=True):
            # 跳过 VCS / 忽略目录
            dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]

            # 跳过隐藏目录（如果 hidden=False）
            if not hidden:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]

            # 遵守 .gitignore：跳过被忽略的目录
            if respect_gitignore and gitignores:
                filtered = []
                for d in dirnames:
                    dir_path = pathlib.Path(dirpath) / d
                    if not _is_ignored_by_gitignore(dir_path, base, gitignores):
                        filtered.append(d)
                dirnames[:] = filtered

            for filename in filenames:
                # 跳过隐藏文件（如果 hidden=False）
                if not hidden and filename.startswith("."):
                    continue

                # ★ 匹配目标：当 prefix 为空且 suffix 含目录前缀时
                #    需匹配完整相对路径（如 suffix="pandapal_desktop/src/.../router.ts"）
                #    否则匹配文件名即可（如 suffix="*.py" 或 prefix 非空已缩窄 search_root）
                if prefix:
                    match_target = filename
                elif "/" in suffix:
                    match_target = str(
                        (pathlib.Path(dirpath) / filename).relative_to(search_root)
                    ).replace("\\", "/")
                else:
                    match_target = filename
                if not fnmatch.fnmatch(match_target, suffix):
                    continue
                full_path = pathlib.Path(dirpath) / filename

                # 去重（多个 pattern 可能匹配同一文件）
                if full_path in seen:
                    continue
                seen.add(full_path)

                # 遵守 .gitignore：跳过被忽略的文件
                if respect_gitignore and gitignores:
                    if _is_ignored_by_gitignore(full_path, base, gitignores):
                        continue

                try:
                    st = os.stat(full_path)
                except OSError:
                    continue
                if not stat_mod.S_ISREG(st.st_mode):
                    continue
                yield full_path, st.st_mtime
                count += 1
                if count >= max_collect:
                    return

            if not recursive:
                break  # 非递归模式只处理顶层目录


# ────────────────────────────────────────────
#  glob 工具
# ────────────────────────────────────────────

GlobLLMGuide = """使用通配符模式匹配文件名，按最近修改时间降序返回。严禁使用 bash find/ls 查找文件。

重要规则：
- 始终使用本工具搜索文件名。严禁在 bash 中调用 find/ls/rg --files，这会导致跳过安全检查。
- 非递归搜索：'*.py'（仅当前目录）
- 递归搜索：'**/*.py'（所有子目录，含嵌套）
- 指定子目录：'src/**/*.ts'（仅 src 下递归）
- 大括号展开：'*.{py,tsx}' 等同于 '*.py' + '*.tsx'
- 默认最多返回 100 个文件，用 max_results 调整
- 自动排除 .git/.svn 等版本控制目录
- 默认遵守 .gitignore 规则（可通过环境变量 PANDAREN_GLOB_NO_IGNORE=false 切换）
- 默认包含隐藏文件（可通过环境变量 PANDAREN_GLOB_HIDDEN=false 切换）
- 结果按修改时间排序，最近修改的文件排在最前
- 返回相对于搜索根目录的路径，更简洁易读
- 搜索文件内容用 grep 工具，搜索文件名用 glob 工具

搜索失败时的重试策略：
- 如果未找到文件，检查返回信息中的"搜索目录"是否正确
- 如果搜索目录不是项目根，使用 path 参数指定正确的搜索根目录重试
- 例如：glob(pattern='**/builder.py', path='/correct/project/root')
- 绝对不要在找不到文件时编造文件内容，必须先确认文件存在再读取
"""


@tool.function(
    tier=ToolTier.ALWAYS,
    name="glob",
    description="按通配符匹配文件名，返回匹配的文件路径列表（按最近修改时间降序）。",
    when_to_use=(
        "当需要按文件名模式查找文件时调用。例如搜索 'xxx.py'——直接使用本工具，不要用 bash find。"
        "当前目录：'*.py'；递归搜索：'**/*.py'、'src/**/*.ts'；大括号：'*.{py,tsx}'"
    ),
    policy=ToolPolicy(
        sensitivity=SensitivityLevel.LOW,
        is_reversible=True,
        is_idempotent=True,
        max_output_bytes=50_000,
        read_only=True,
    ),
    lifecycle=ToolLifecycle(
        validate_input=_validate_glob_input,
    ),
    llm_guide=GlobLLMGuide,
)
def glob(
    ctx: ToolContext,
    pattern: str,
    path: Optional[str] = None,
    max_results: int = 100,
) -> str:
    """使用通配符模式搜索文件，按修改时间降序排序。

    Args:
        pattern: 通配符模式：
            '*.py'         — 当前目录下的 .py 文件
            '**/*.py'      — 递归搜索所有子目录中的 .py 文件
            'src/**/*.ts'  — src 目录下递归搜索 .ts 文件
            '*.{py,tsx}'   — 大括号展开，同时匹配 .py 和 .tsx
        path: 搜索根目录（相对路径基于项目根目录解析），不指定使用项目根目录。
        max_results: 最多返回文件数，默认 100。传 0 取消限制。

    Returns:
        按修改时间降序的文件路径列表；无匹配时返回提示信息。
        包含搜索耗时信息（帮助 LLM 判断搜索范围是否过大）。
    """
    start_time = time.monotonic()

    # ── 路径解析 ──
    base = expand_path(path, resolve_project_root()) if path else resolve_project_root()

    # SECURITY: UNC 路径安全防护
    if path and is_unc_path(path):
        return f"错误：不支持 UNC 网络路径：{path}（安全限制）"

    if not base.exists():
        return f"错误：目录不存在：{base}"
    if not base.is_dir():
        return f"错误：路径不是目录：{base}"

    # ── 环境变量配置 ──
    no_ignore = _env_bool("PANDAREN_GLOB_NO_IGNORE", default=True)
    hidden = _env_bool("PANDAREN_GLOB_HIDDEN", default=True)

    try:
        # ── 选择搜索引擎 ──
        if _has_ripgrep():
            print("[glob] 使用 ripgrep 后端搜索")
            # ripgrep 后端：速度快，自带 .gitignore / 隐藏文件支持
            raw_files = _run_ripgrep_glob(
                pattern=pattern,
                cwd=base,
                max_results=max_results if max_results > 0 else 10_000,
                no_ignore=no_ignore,
                hidden=hidden,
            )

            if not raw_files:
                duration_ms = int((time.monotonic() - start_time) * 1000)
                root = resolve_project_root()
                hint = ""
                if base != root:
                    hint = f"\n提示：当前搜索目录不是项目根目录，可尝试 glob(pattern='{pattern}', path='{root}')"
                return f"未找到匹配 '{pattern}' 的文件（搜索目录：{base}，项目根目录：{root}，耗时：{duration_ms}ms）{hint}"

            # ripgrep --sort=modified 返回旧→新，需要反转为新→旧
            raw_files.reverse()

            total = len(raw_files)
            truncated = max_results > 0 and total > max_results
            if truncated:
                raw_files = raw_files[:max_results]

            # 转为相对路径
            lines = []
            for f in raw_files:
                p = pathlib.Path(f)
                try:
                    lines.append(str(p.relative_to(base)))
                except ValueError:
                    lines.append(str(p))

        else:
            # Python 降级后端
            results = list(_walk_glob(
                base, pattern,
                hidden=hidden,
                respect_gitignore=not no_ignore,
            ))

            if not results:
                duration_ms = int((time.monotonic() - start_time) * 1000)
                root = resolve_project_root()
                hint = ""
                if base != root:
                    hint = f"\n提示：当前搜索目录不是项目根目录，可尝试 glob(pattern='{pattern}', path='{root}')"
                return f"未找到匹配 '{pattern}' 的文件（搜索目录：{base}，项目根目录：{root}，耗时：{duration_ms}ms）{hint}"

            # 按 mtime 降序排序
            results.sort(key=lambda x: x[1], reverse=True)

            total = len(results)
            truncated = max_results > 0 and total > max_results
            if truncated:
                results = results[:max_results]

            lines = []
            for p, _ in results:
                try:
                    lines.append(str(p.relative_to(base)))
                except ValueError:
                    lines.append(str(p))

        # ── 格式化输出 ──
        duration_ms = int((time.monotonic() - start_time) * 1000)
        header = f"# 找到 {total} 个文件（模式：{pattern}，目录：{base}，耗时：{duration_ms}ms）"
        root = resolve_project_root()
        if base != root:
            header += f"\n# 项目根目录：{root}"
        result = header + "\n" + "\n".join(lines)

        if truncated:
            result += f"\n\n[已显示前 {max_results}/{total} 个文件，使用 max_results 参数查看更多]"

        return result

    except RuntimeError as e:
        return f"文件搜索失败：{e}"
    except Exception as e:
        return f"文件搜索异常：{e}"
