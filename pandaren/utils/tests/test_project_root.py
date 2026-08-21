"""pandaren/utils/tests/test_project_root.py — project_root 测试。

覆盖：
  - set_search_root 校验目录存在性（NotADirectoryError）
  - set_search_root(None) 清空状态
  - 未设置时 resolve_project_root 抛 WorkspaceNotSelectedError
  - 设置后 resolve 返回同一目录
"""

from __future__ import annotations

import pathlib

import pytest

from pandaren.utils.project_root import (
    WorkspaceNotSelectedError,
    is_workspace_set,
    resolve_project_root,
    set_search_root,
)


class TestProjectRoot:

    def test_unset_raises(self):
        """未设置工作区时 resolve 抛 WorkspaceNotSelectedError"""
        set_search_root(None)
        with pytest.raises(WorkspaceNotSelectedError):
            resolve_project_root()

    def test_is_workspace_set_false_by_default(self):
        set_search_root(None)
        assert is_workspace_set() is False

    def test_set_and_resolve(self, tmp_path):
        """设置后 resolve 返回同一目录（绝对化）"""
        set_search_root(tmp_path)
        try:
            assert is_workspace_set() is True
            assert resolve_project_root() == tmp_path.resolve()
        finally:
            set_search_root(None)

    def test_set_none_clears(self, tmp_path):
        """set_search_root(None) 清空为未指定状态"""
        set_search_root(tmp_path)
        set_search_root(None)
        with pytest.raises(WorkspaceNotSelectedError):
            resolve_project_root()

    def test_invalid_dir_raises(self, tmp_path):
        """不存在的目录抛 NotADirectoryError"""
        with pytest.raises(NotADirectoryError):
            set_search_root(tmp_path / "not_exist_dir")

    def test_file_as_root_raises(self, tmp_path):
        """文件路径（非目录）抛 NotADirectoryError"""
        f = tmp_path / "a.txt"
        f.write_text("x", encoding="utf-8")
        with pytest.raises(NotADirectoryError):
            set_search_root(f)

    def test_tilde_expansion(self, tmp_path, monkeypatch):
        """~ 路径展开后设置"""
        home = pathlib.Path.home()
        # 用 home 下真实存在的目录（home 本身）
        set_search_root("~")
        try:
            assert resolve_project_root() == home.resolve()
        finally:
            set_search_root(None)
