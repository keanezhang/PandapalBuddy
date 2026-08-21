"""pandaren/utils/tests/test_path_utils.py — path_utils 测试。

覆盖 code review 修复点：
  R1: validate_sandbox_path 用 os.path.normcase 统一比较（Windows 大小写不敏感）
  R2: expand_path 空字符串/全空白 → ValueError（fail-fast）

风险映射：
  - 系统目录黑名单（_BLOCKED_SYSTEM_DIRS）不能被大小写变体绕过
  - 项目根/主目录内路径必须放行
  - 前缀匹配不能误伤相邻目录（root=/proj 时 /proj2/x 不得放行）
  - is_unc_path 识别 \\server\\share 与 //server/share
  - format_file_size 的 B/KB/MB/GB 边界
"""

from __future__ import annotations

import os
import pathlib

import pytest

from pandaren.utils.path_utils import (
    expand_path,
    format_file_size,
    is_unc_path,
    suggest_similar_path,
    validate_sandbox_path,
)


# ════════════════════════════════════════════════════════════════
# Group A — expand_path（R2：空值 fail-fast）
# ════════════════════════════════════════════════════════════════

class TestExpandPath:

    def test_empty_string_raises(self):
        """R2：空字符串抛 ValueError（防静默回落项目根写入）"""
        with pytest.raises(ValueError):
            expand_path("")

    def test_whitespace_only_raises(self):
        """R2：全空白抛 ValueError"""
        with pytest.raises(ValueError):
            expand_path("   \t  ")

    def test_strip_whitespace(self, tmp_path):
        """路径前后空白被修剪"""
        result = expand_path("  sub/file.txt  ", base_dir=tmp_path)
        assert result == (tmp_path / "sub/file.txt").resolve()

    def test_relative_resolves_against_base_dir(self, tmp_path):
        """相对路径基于 base_dir 解析"""
        result = expand_path("sub/file.txt", base_dir=tmp_path)
        assert result == (tmp_path / "sub/file.txt").resolve()

    def test_absolute_kept(self):
        """绝对路径保持原样（resolve 后）"""
        p = pathlib.Path("/") / "tmp" / "abs.txt"
        result = expand_path(str(p), base_dir=pathlib.Path("/"))
        assert result == p.resolve()

    def test_tilde_expands_to_home(self):
        """~/x 展开为用户主目录"""
        result = expand_path("~/pandaren_tilde_test.txt")
        assert result == pathlib.Path.home() / "pandaren_tilde_test.txt"


# ════════════════════════════════════════════════════════════════
# Group B — is_unc_path
# ════════════════════════════════════════════════════════════════

class TestIsUncPath:

    @pytest.mark.parametrize(
        "p",
        [
            "\\\\server\\share",
            "\\\\server\\share\\dir",
            "//server/share",
            "//server/share/dir",
        ],
    )
    def test_unc_recognized(self, p):
        assert is_unc_path(p) is True

    @pytest.mark.parametrize(
        "p",
        ["C:\\local\\path", "/local/path", "relative/path", "", "server/share"],
    )
    def test_non_unc(self, p):
        assert is_unc_path(p) is False


# ════════════════════════════════════════════════════════════════
# Group C — format_file_size（B/KB/MB/GB 边界）
# ════════════════════════════════════════════════════════════════

class TestFormatFileSize:

    @pytest.mark.parametrize(
        ("size", "expected"),
        [
            (0, "0 B"),
            (1, "1 B"),
            (1023, "1023 B"),
            (1024, "1.0 KB"),
            (1536, "1.5 KB"),
            (1024 * 1024, "1.0 MB"),
            (1024 * 1024 * 1024, "1.0 GB"),
        ],
    )
    def test_boundaries(self, size, expected):
        assert format_file_size(size) == expected


# ════════════════════════════════════════════════════════════════
# Group D — validate_sandbox_path（R1：normcase 大小写归一）
# ════════════════════════════════════════════════════════════════

class TestValidateSandboxPath:

    @pytest.fixture(autouse=True)
    def _workspace_root(self, tmp_path, monkeypatch):
        """validate_sandbox_path 未传 project_root 时依赖全局 workspace root"""
        import pandaren.utils.project_root as pr
        monkeypatch.setattr(pr, "_workspace_root", tmp_path)

    # ── 系统目录拦截 ──

    @pytest.mark.parametrize(
        "p",
        [
            "/etc/passwd",
            "/etc/",
            "/bin/ls",
            "/usr/bin/python3",
            "/var/log/syslog",
            "/System/Library/Fonts/x.ttf",
        ],
    )
    def test_posix_system_dir_blocked(self, p):
        """POSIX 系统目录（含子路径）必须被拦截"""
        err = validate_sandbox_path(pathlib.Path(p))
        assert err is not None

    @pytest.mark.parametrize(
        "p",
        [
            "C:\\Windows\\System32\\cmd.exe",
            "C:\\Windows\\system.ini",
            "C:\\Program Files\\app\\x.exe",
        ],
    )
    def test_windows_system_dir_blocked(self, p):
        """Windows 系统目录必须被拦截"""
        err = validate_sandbox_path(pathlib.Path(p))
        assert err is not None

    @pytest.mark.parametrize(
        "p",
        [
            "c:\\windows\\system32\\cmd.exe",      # 全小写盘符+路径
            "C:/Windows/System32/cmd.exe",          # 正斜杠变体
            "c:/windows/system32/",                 # 小写+正斜杠
        ],
    )
    def test_windows_system_dir_case_variant_blocked(self, p):
        """R1：Windows 大小写变体不可绕过系统目录拦截（normcase）"""
        if os.name != "nt":
            pytest.skip("Windows 专属用例")
        err = validate_sandbox_path(pathlib.Path(p))
        assert err is not None

    # ── home / 项目根放行 ──

    def test_home_dir_allowed(self):
        """主目录内路径放行"""
        p = pathlib.Path.home() / "sandbox_test_ok.txt"
        err = validate_sandbox_path(p)
        assert err is None

    def test_home_dir_case_variant_allowed(self):
        """R1：Windows 小写变体的主目录路径放行（不被误拒）"""
        if os.name != "nt":
            pytest.skip("Windows 专属用例")
        home = pathlib.Path.home()
        lc = str(home).lower() + "/sandbox_test_ok.txt"
        err = validate_sandbox_path(pathlib.Path(lc))
        assert err is None

    def test_project_root_allowed(self, tmp_path):
        """显式 project_root 内路径放行"""
        err = validate_sandbox_path(tmp_path / "x.txt", project_root=tmp_path)
        assert err is None

    def test_project_root_itself_allowed(self, tmp_path):
        """project_root 本身（根目录）放行"""
        err = validate_sandbox_path(tmp_path, project_root=tmp_path)
        assert err is None

    # ── 前缀误伤防护 ──

    def test_sibling_dir_not_allowed(self):
        """root=/proj 时 /proj2/x 不得放行（前缀须带分隔符）"""
        # 用不存在的虚构路径，避免 tmp_path 落在 home 内导致的放行干扰
        root = pathlib.Path("/proj_root_xyz")
        sibling = pathlib.Path("/proj_root_xyz2/file.txt")
        err = validate_sandbox_path(sibling, project_root=root)
        assert err is not None

    def test_deep_sibling_not_allowed(self):
        """root=/proj 时 /proj-extra/x 不得放行"""
        root = pathlib.Path("/proj_root_xyz")
        sibling = pathlib.Path("/proj_root_xyz-extra/file.txt")
        err = validate_sandbox_path(sibling, project_root=root)
        assert err is not None

    def test_outside_home_and_root_rejected(self):
        """既不在 home 也不在 project_root 的路径拒绝"""
        if os.name == "nt":
            outside = pathlib.Path("C:/proj_root_xyz_outside/file.txt")
            root = pathlib.Path("C:/proj_root_xyz")
        else:
            outside = pathlib.Path("/opt/xyz_outside/file.txt")
            root = pathlib.Path("/proj_root_xyz")
        err = validate_sandbox_path(outside, project_root=root)
        assert err is not None


# ════════════════════════════════════════════════════════════════
# Group E — suggest_similar_path（依赖全局 workspace root）
# ════════════════════════════════════════════════════════════════

class TestSuggestSimilarPath:

    def _set_root(self, monkeypatch, root: pathlib.Path):
        import pandaren.utils.project_root as pr
        monkeypatch.setattr(pr, "_workspace_root", root)

    def test_close_match_suggested(self, tmp_path, monkeypatch):
        """拼写接近的目录被建议"""
        (tmp_path / "documents").mkdir()
        self._set_root(monkeypatch, tmp_path)
        assert suggest_similar_path("documnts") == "documents"

    def test_no_match_returns_none(self, tmp_path, monkeypatch):
        """无接近匹配返回 None"""
        self._set_root(monkeypatch, tmp_path)
        assert suggest_similar_path("zzz_not_exist") is None

    def test_empty_candidates_returns_none(self, tmp_path, monkeypatch):
        """项目根为空目录时返回 None"""
        self._set_root(monkeypatch, tmp_path)
        assert suggest_similar_path("anything") is None
