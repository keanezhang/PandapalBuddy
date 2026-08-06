"""pandaren/utils/file_validators.py — 文件安全性校验工具。

提供读/写操作前的轻量校验：
  - 设备文件拦截：防止读取会阻塞或无限输出的 /dev 文件
  - 二进制扩展名检测：在读之前通过扩展名判断是否为二进制文件
  - 大小阈值检测：在读之前检查文件是否过大
"""

from __future__ import annotations

import pathlib
import os

# ────────────────────────────────────────────
#  设备文件黑名单
# ────────────────────────────────────────────

# Device files that would hang the process: infinite output or blocking input.
# Checked by path only (no I/O). Safe devices like /dev/null are intentionally omitted.
BLOCKED_DEVICE_PATHS: frozenset[str] = frozenset({
    # 无限输出 — 永远不会到达 EOF
    "/dev/zero",
    "/dev/random",
    "/dev/urandom",
    "/dev/full",
    # 阻塞等待输入
    "/dev/stdin",
    "/dev/tty",
    "/dev/console",
    # 无意义读取
    "/dev/stdout",
    "/dev/stderr",
    # fd 别名
    "/dev/fd/0",
    "/dev/fd/1",
    "/dev/fd/2",
})

# /proc/self/fd/... 和 /proc/<pid>/fd/... 也是 stdio 别名
BLOCKED_DEVICE_PREFIXES: tuple[str, ...] = ("/proc/",)


def is_blocked_device_path(file_path: str) -> bool:
    """检查路径是否为应被阻止的设备文件（路径级别，无 I/O）。

    阻止原因：读取会导致永久阻塞或无限输出。

    Args:
        file_path: 绝对路径。

    Returns:
        True 如果路径对应一个应被阻止的设备文件。
    """
    if file_path in BLOCKED_DEVICE_PATHS:
        return True
    # /proc/self/fd/0-2 和 /proc/<pid>/fd/0-2 是 Linux stdio 别名
    if file_path.startswith(BLOCKED_DEVICE_PREFIXES):
        parts = file_path.split("/")
        if len(parts) >= 4 and parts[-2] == "fd":
            fd_part = parts[-1]
            if fd_part in ("0", "1", "2"):
                return True
    return False


# ────────────────────────────────────────────
#  二进制文件扩展名
# ────────────────────────────────────────────

BINARY_EXTENSIONS: frozenset[str] = frozenset({
    # 编译/链接产物
    "exe", "dll", "so", "dylib", "o", "a", "obj", "lib",
    # JVM
    "class", "jar", "war",
    # 归档
    "zip", "tar", "gz", "bz2", "xz", "zst", "7z", "rar",
    # 数据库/二进制数据
    "db", "sqlite", "sqlite3", "dat", "bin",
    # 富媒体文档
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    # 字体
    "ttf", "otf", "woff", "woff2", "eot",
    # 音视频
    "mp3", "mp4", "avi", "mkv", "mov", "wav", "flac", "ogg", "opus",
    "m4a", "m4v", "webm", "wmv",
    # 压缩图/图标
    "ico", "icns",
    # 其他二进制
    "pyc", "pyo", "npy", "npz", "pkl", "pickle",
    "wasm", "node", "elf",
})


def is_binary_extension(ext: str) -> bool:
    """通过扩展名判断是否为二进制文件（纯字符串检查，无 I/O）。

    Args:
        ext: 小写的文件扩展名，不含点号（如 'png', 'exe'）。

    Returns:
        True 如果扩展名属于已知二进制类型。
    """
    return ext in BINARY_EXTENSIONS


def has_binary_extension(file_path: str) -> bool:
    """通过路径判断是否为二进制文件（纯字符串检查，无 I/O）。

    Args:
        file_path: 文件路径。

    Returns:
        True 如果扩展名属于已知二进制类型。
    """
    ext = pathlib.Path(file_path).suffix.lstrip(".").lower()
    return ext in BINARY_EXTENSIONS


# ────────────────────────────────────────────
#  大小校验
# ────────────────────────────────────────────

def validate_file_size(
    file_path: str,
    max_bytes: int,
) -> str | None:
    """检查文件大小是否在允许范围内。

    使用 os.path.getsize() 做快速的元数据查询（不读文件内容）。

    Args:
        file_path: 文件路径（应已展开为绝对路径）。
        max_bytes: 允许的最大字节数。

    Returns:
        None = 通过；str = 错误消息（应返回给 LLM 或抛出）。
    """
    try:
        size = os.path.getsize(file_path)
    except OSError:
        return None  # 文件不存在等由 validate_input 处理

    if size == 0:
        return None  # 空文件可以读

    if size > max_bytes:
        from .path_utils import format_file_size
        return (
            f"文件过大（{format_file_size(size)}），超过读取上限（{format_file_size(max_bytes)}）。\n"
            f"请使用 offset 和 limit 参数分段读取：\n"
            f"  read_file(file_path=\"{file_path}\", offset=1, limit=500)"
        )
    return None
