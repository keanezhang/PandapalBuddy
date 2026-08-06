"""pandaren.utils — 工具层共享基础设施。

提供跨工具复用的通用能力：
  project_root  — 工作区根目录（唯一来源：用户显式指定，不做自动探测）
  path_utils    — 路径展开、UNC 检测、相似文件建议
  file_validators — 二进制/设备文件检测、大小校验
"""

from .project_root import (
    WorkspaceNotSelectedError,
    is_workspace_set,
    resolve_project_root,
    set_search_root,
)
from .path_utils import (
    expand_path,
    is_unc_path,
    suggest_similar_path,
    format_file_size,
    validate_sandbox_path,
)
from .file_validators import (
    is_blocked_device_path,
    is_binary_extension,
    has_binary_extension,
    BINARY_EXTENSIONS,
    BLOCKED_DEVICE_PATHS,
    BLOCKED_DEVICE_PREFIXES,
)

__all__ = [
    # project_root
    "WorkspaceNotSelectedError",
    "is_workspace_set",
    "resolve_project_root",
    "set_search_root",
    # path_utils
    "expand_path",
    "is_unc_path",
    "suggest_similar_path",
    "format_file_size",
    "validate_sandbox_path",
    # file_validators
    "is_blocked_device_path",
    "is_binary_extension",
    "has_binary_extension",
    "BINARY_EXTENSIONS",
    "BLOCKED_DEVICE_PATHS",
    "BLOCKED_DEVICE_PREFIXES",
]
