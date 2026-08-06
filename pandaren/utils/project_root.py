"""pandaren/utils/project_root.py — 工作区根目录（唯一来源：用户显式指定）。

设计约束（IDE 形态）：
  - 工作区根目录**只能由外部显式指定**（用户在客户端选择的文件夹）。
  - **不做任何自动探测**（不扫 .git / pyproject.toml，不回退 CWD）。
    打包成软件后，进程 CWD / exe 路径是安装目录，自动探测只会指向错误位置。
  - 未指定工作区时，任何依赖它的文件工具都不可用 —— 直接抛
    WorkspaceNotSelectedError，由上层保证「未选工作区不装配 Agent」。

设置入口：set_search_root(directory)（客户端选目录后调用一次）。
读取入口：resolve_project_root()（glob / grep / read / write / 环境块）。

注意区分「应用数据根」（App 自己的 .env / SQLite / .pandapal，见
pandapal/local/run_local.py 的 _get_project_root）—— 那是另一个概念，
与此处的「工作区根」无关，不受本模块影响。
"""

from __future__ import annotations

import pathlib


class WorkspaceNotSelectedError(RuntimeError):
    """工作区未指定。

    在用户通过客户端选择工作目录（→ set_search_root）之前，
    Agent 的文件工具（glob / grep / read / write）不可用。
    """


# 唯一的工作区根目录。None 表示「尚未指定」，此时文件工具禁用。
_workspace_root: pathlib.Path | None = None


def set_search_root(directory: str | pathlib.Path | None) -> None:
    """指定工作区根目录（唯一设置入口）。

    影响所有依赖 resolve_project_root() 的工具（glob、grep、file_ops、
    环境块注入等）。客户端在用户选择文件夹后调用一次。

    Args:
        directory: 用户选择的工作目录绝对路径。
                   传入 None 则清空为「未指定」状态（文件工具重新禁用）。

    Raises:
        NotADirectoryError: 传入的路径不存在或不是目录。
    """
    global _workspace_root
    if directory is None:
        _workspace_root = None
        return
    resolved = pathlib.Path(directory).expanduser().resolve()
    if not resolved.is_dir():
        raise NotADirectoryError(f"工作区路径不存在或不是目录：{resolved}")
    _workspace_root = resolved


def is_workspace_set() -> bool:
    """工作区是否已指定。

    上层据此决定是否装配 / 启动 Agent（未指定 → 停在「打开文件夹」状态）。
    """
    return _workspace_root is not None


def resolve_project_root() -> pathlib.Path:
    """返回当前工作区根目录。

    Returns:
        用户通过 set_search_root() 指定的工作区根目录绝对路径。

    Raises:
        WorkspaceNotSelectedError: 尚未指定工作区（未调用 set_search_root）。
    """
    if _workspace_root is None:
        raise WorkspaceNotSelectedError(
            "尚未选择工作目录。请先在客户端打开一个文件夹后再执行文件操作。"
        )
    return _workspace_root
