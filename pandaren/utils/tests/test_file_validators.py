"""pandaren/utils/tests/test_file_validators.py — file_validators 测试。

覆盖 code review 修复点：
  R3: is_blocked_device_path 对尾部斜杠归一化（/dev/zero/ 应拦截）
  R4: validate_file_size 的 OSError 分支留痕（行为不变，返回 None）
"""

from __future__ import annotations

import os

import pytest

from pandaren.utils.file_validators import (
    has_binary_extension,
    is_binary_extension,
    is_blocked_device_path,
    validate_file_size,
)


# ════════════════════════════════════════════════════════════════
# Group A — is_blocked_device_path（R3）
# ════════════════════════════════════════════════════════════════

class TestIsBlockedDevicePath:

    @pytest.mark.parametrize(
        "p",
        [
            "/dev/zero",
            "/dev/random",
            "/dev/urandom",
            "/dev/full",
            "/dev/stdin",
            "/dev/tty",
            "/dev/console",
            "/dev/stdout",
            "/dev/stderr",
            "/dev/fd/0",
            "/dev/fd/1",
            "/dev/fd/2",
            "/proc/self/fd/0",
            "/proc/self/fd/2",
            "/proc/1234/fd/1",
        ],
    )
    def test_blocked_devices(self, p):
        assert is_blocked_device_path(p) is True

    @pytest.mark.parametrize(
        "p",
        [
            "/dev/zero/",          # R3：尾部斜杠归一化后仍拦截
            "/dev/random/",
            "/proc/self/fd/0/",
        ],
    )
    def test_trailing_slash_variants_blocked(self, p):
        """R3：尾部斜杠变体仍被拦截"""
        assert is_blocked_device_path(p) is True

    @pytest.mark.parametrize(
        "p",
        [
            "/dev/null",           # 安全设备有意放行
            "/dev/sda",
            "/etc/passwd",
            "/home/user/file.txt",
            "C:\\Windows\\notepad.exe",
            "/proc/1234/fd/99",    # fd 号非 0/1/2 放行
            "/proc/self/status",   # 非 fd 路径放行
            "",
        ],
    )
    def test_safe_paths(self, p):
        assert is_blocked_device_path(p) is False


# ════════════════════════════════════════════════════════════════
# Group B — 二进制扩展名
# ════════════════════════════════════════════════════════════════

class TestBinaryExtensions:

    @pytest.mark.parametrize("ext", ["exe", "dll", "so", "zip", "pdf", "mp4", "pyc", "db", "mp3", "ico"])
    def test_is_binary_extension_true(self, ext):
        assert is_binary_extension(ext) is True

    @pytest.mark.parametrize("ext", ["txt", "md", "py", "json", "csv", "log", ""])
    def test_is_binary_extension_false(self, ext):
        assert is_binary_extension(ext) is False

    def test_has_binary_extension_case_insensitive(self):
        """扩展名大小写不敏感（.ICO 与 .ico 等价）"""
        assert has_binary_extension("photo.ICO") is True
        assert has_binary_extension("photo.ico") is True
        assert has_binary_extension("notes.TXT") is False

    def test_has_binary_extension_no_ext(self):
        assert has_binary_extension("noext") is False


# ════════════════════════════════════════════════════════════════
# Group C — validate_file_size（R4）
# ════════════════════════════════════════════════════════════════

class TestValidateFileSize:

    def test_under_limit_passes(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("hello", encoding="utf-8")  # 5 bytes
        assert validate_file_size(str(f), max_bytes=100) is None

    def test_exact_limit_passes(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_bytes(b"x" * 100)
        assert validate_file_size(str(f), max_bytes=100) is None

    def test_over_limit_returns_message(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_bytes(b"x" * 101)
        err = validate_file_size(str(f), max_bytes=100)
        assert err is not None
        assert "文件过大" in err

    def test_empty_file_passes(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        assert validate_file_size(str(f), max_bytes=1) is None

    def test_missing_file_returns_none(self, tmp_path):
        """R4：文件不存在 → None（由上层 validate_input 处理，不抛异常）"""
        assert validate_file_size(str(tmp_path / "nope.txt"), max_bytes=100) is None

    def test_directory_returns_none_or_message(self, tmp_path):
        """目录作为输入不崩溃（OSError 分支或大小分支均安全）"""
        result = validate_file_size(str(tmp_path), max_bytes=100)
        # 目录大小不可靠：只要求不抛异常
        assert result is None or isinstance(result, str)
