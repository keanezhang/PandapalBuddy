"""pandaren/utils/path_utils.py — 路径处理共享工具。

提供跨工具复用的路径操作：
  - expand_path: ~ / 相对 / 绝对路径统一展开
  - is_unc_path: UNC 网络路径检测（安全：防止 NTLM 凭据泄露）
  - suggest_similar_path: 拼写纠正 + 项目根调整建议
  - format_file_size: 人类可读的文件大小格式化
  - validate_sandbox_path: 沙箱路径校验（防止写入幻觉路径到系统目录）
"""

from __future__ import annotations

import difflib
import pathlib

from .project_root import resolve_project_root


def expand_path(path: str, base_dir: pathlib.Path | None = None) -> pathlib.Path:
    """展开路径：处理 ~ / 相对路径 / 绝对路径。

    类似 Claude Code 的 expandPath()，统一路径解析逻辑：
    - ~/xxx   → 用户主目录下
    - 相对路径  → 基于 base_dir（默认项目根）解析
    - 绝对路径  → 保持原样
    - 空白修剪   → 自动 strip()

    Args:
        path: 原始路径字符串。
        base_dir: 相对路径解析的基础目录，None 时使用项目根目录。

    Returns:
        展开后的绝对 pathlib.Path。
    """
    path = path.strip()
    if path.startswith("~"):
        return pathlib.Path(path).expanduser()
    if pathlib.Path(path).is_absolute():
        return pathlib.Path(path)
    base = base_dir or resolve_project_root()
    return (base / path).resolve()


def is_unc_path(path: str) -> bool:
    """检测是否为 UNC 路径（Windows 网络路径），防止 NTLM 凭据泄露。

    \\\\server\\share 或 //server/share 格式。

    Args:
        path: 待检测的路径字符串。

    Returns:
        True 如果是 UNC 路径。
    """
    return path.startswith("\\\\") or path.startswith("//")


def suggest_similar_path(target: str) -> str | None:
    """在项目根下找最接近的路径建议。

    两种策略：
      1. 拼写纠正：用 difflib 找项目根下最接近的子目录名
      2. 丢掉 repo 组件修正（Claude Code 的 suggestPathUnderCwd 思路）：
         当 LLM 编造 /Users/foo/src/bar（不存在）
         而实际项目根是 /Users/foo/src/myrepo，检测 /Users/foo/src/myrepo/bar 是否存在

    Args:
        target: 用户/LLM 输入的路径。

    Returns:
        建议的相对路径字符串，或 None（无建议）。
    """
    root = resolve_project_root()

    # ── 策略 1：拼写纠正 ──
    try:
        candidates = [
            str(p.relative_to(root))
            for p in root.iterdir()
            if p.is_dir()
        ][:50]
    except OSError:
        candidates = []

    matches = difflib.get_close_matches(target, candidates, n=1, cutoff=0.5)
    if matches:
        return matches[0]

    # ── 策略 2：丢掉 repo 组件修正 ──
    target_path = pathlib.Path(target).expanduser()
    if target_path.is_absolute():
        parent_of_root = root.parent
        try:
            rel_from_parent = target_path.relative_to(parent_of_root)
        except ValueError:
            rel_from_parent = None

        if rel_from_parent is not None and not str(rel_from_parent).startswith(".."):
            corrected = root / rel_from_parent
            if corrected.exists():
                return str(corrected.relative_to(root))

    return None


_BLOCKED_SYSTEM_DIRS: frozenset[str] = frozenset({
    "/", "/etc", "/bin", "/sbin", "/usr/bin", "/usr/sbin",
    "/boot", "/dev", "/sys", "/proc", "/lib", "/lib64",
    "/usr/lib", "/usr/lib64", "/var/log", "/var/run",
    "/System", "/Library/System",
    "C:\\Windows", "C:\\Windows\\System32",
    "C:\\Program Files", "C:\\Program Files (x86)",
})


def validate_sandbox_path(
    full_path: pathlib.Path,
    *,
    project_root: pathlib.Path | None = None,
) -> str | None:
    """沙箱路径校验：确保写入路径在项目根或用户主目录内，不落在系统目录。

    防止 LLM 幻觉路径（如 /home/nonexistent/output.txt）被 write_file 虚假成功写入。

    Args:
        full_path: 已展开的绝对路径。
        project_root: 项目根目录路径，None 时自动解析。

    Returns:
        None 表示通过校验；返回错误消息字符串表示校验失败。
    """
    resolved = full_path.resolve() if full_path.exists() else full_path
    resolved_str = str(resolved)

    # 1. 检查是否在系统禁止目录下
    for blocked in _BLOCKED_SYSTEM_DIRS:
        if resolved_str == blocked or resolved_str.startswith(blocked + "/") or resolved_str.startswith(blocked + "\\"):
            return (
                f"安全限制：禁止写入系统目录 '{blocked}' 下的文件。\n"
                f"请将文件写入项目目录或用户主目录下。\n"
                f"当前请求路径：{full_path}"
            )

    # 2. 检查是否在项目根或用户主目录内
    root = project_root or resolve_project_root()
    home = pathlib.Path.home()
    root_str = str(root.resolve())
    home_str = str(home.resolve())

    in_project = resolved_str == root_str or resolved_str.startswith(root_str + "/") or resolved_str.startswith(root_str + "\\")
    in_home = resolved_str == home_str or resolved_str.startswith(home_str + "/") or resolved_str.startswith(home_str + "\\")

    if not in_project and not in_home:
        return (
            f"安全限制：文件路径必须在项目目录或用户主目录内。\n"
            f"  项目根目录：{root_str}\n"
            f"  用户主目录：{home_str}\n"
            f"  请求路径：{full_path}\n"
            f"请检查路径是否正确，或使用项目目录/主目录下的合法路径。"
        )

    return None


def format_file_size(size_bytes: int) -> str:
    """将字节数格式化为人类可读字符串。

    Args:
        size_bytes: 字节数。

    Returns:
        格式化的字符串，如 "1.5 KB", "3.2 MB"。
    """
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
