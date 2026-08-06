"""pandaren/tools/file_tool/delete_file.py — 文件删除工具。

提供安全的文件删除能力：默认移入回收站，支持永久删除。
作为独立内置工具（非 bash rm），获得更清晰的审计日志和权限控制。
"""
from __future__ import annotations

import logging
import platform
import subprocess

from pandaren.identity.models import SensitivePermission
from pandaren.tool.types import ToolTier, SensitivityLevel
from pandaren.tool.definition.context import ToolContext
from pandaren.tool.definition.tool_policy import ToolPolicy
from pandaren.tool.definition.tool_lifecycle import ToolLifecycle
from pandaren.tool.decorator import tool
from pandaren.utils import expand_path
from ._utils import validate_delete_input

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────
#  LLM Guide — 对标 Claude Code BashTool 的 rm 安全策略
# ────────────────────────────────────────────

_DELETE_FILE_LLM_GUIDE = """删除文件或空目录。

使用规则：
- **始终使用本工具删除文件，不要用 bash rm**。本工具提供审计日志、回收站保护和权限控制
- 默认移入回收站（安全模式），而非永久删除——这是最重要的安全特性
- 设置 permanently=True 执行永久删除（需谨慎，不可逆）
- 仅删除空目录；要删除非空目录请用 bash rm -rf（会有二次确认警告）
- 删除前确认路径正确，避免误删重要文件"""


# ── 工具定义 ──

@tool.function(
    tier=ToolTier.ALWAYS,
    name="delete_file",
    description=(
        "删除文件或空目录。默认启用安全模式（移入回收站/Trash），"
        "设置 permanently=True 执行永久删除。"
    ),
    when_to_use=(
        "需要删除文件时调用——始终使用本工具，不要用 bash rm。"
        "默认进回收站可恢复；permanently=True 永久删除需谨慎。"
        "仅删除空目录；非空目录请用 bash rm -rf"
    ),
    policy=ToolPolicy(
        sensitivity=SensitivityLevel.HIGH,
        sensitive_permission=SensitivePermission.DATA_DELETE,
        is_reversible=False, audit_required=True,
        is_idempotent=False, max_calls_per_turn=10,
    ),
    lifecycle=ToolLifecycle(validate_input=validate_delete_input),
    llm_guide=_DELETE_FILE_LLM_GUIDE,
)
def delete_file(
    ctx: ToolContext,
    file_path: str,
    permanently: bool = False,
) -> str:
    """删除文件或空目录。

    默认行为（permanently=False）：
        - macOS: 移入 Trash (~/.Trash)，支持 osascript Finder delete
        - Linux: 尝试 send2trash，不可用时回退到永久删除
    永久删除（permanently=True）：直接 os.unlink / os.rmdir。

    Args:
        ctx: 工具上下文。
        file_path: 要删除的文件或空目录路径。
        permanently: 是否永久删除（跳过回收站），默认 False。

    Returns:
        删除成功确认；失败时返回错误信息。
    """
    path = expand_path(file_path)

    if path.is_dir():
        try:
            items = list(path.iterdir())
        except PermissionError:
            return f"错误：无权限访问目录：{file_path}"
        if items:
            count = len(items)
            sample = [e.name for e in list(items)[:5]]
            extra = " ... 等" if count > 5 else ""
            return (
                f"错误：目录非空，包含 {count} 个条目（{', '.join(sample)}{extra}）。\n"
                f"此工具仅删除空目录。请使用 bash rm -rf 删除非空目录，或先清空目录内容。"
            )

    if not permanently:
        # 尝试 send2trash
        try:
            import send2trash
            send2trash.send2trash(str(path))
            return f"✅ 已移至回收站：{file_path}（可用 send2trash 恢复）"
        except ImportError:
            pass  # send2trash 未安装（可选依赖）：预期路径，回落下一种方式
        except Exception:
            # send2trash 失败 → 将穿透到永久删除。用户要的是「移回收站」，这是语义降级，
            # 绝不静默（§九降级留痕）——留痕暴露「本该进回收站却走了永久删除」。
            logger.warning(
                "send2trash 失败，将回落到下一种回收站方式或永久删除：%s",
                file_path, exc_info=True,
            )

        # macOS Trash 手动实现
        if platform.system() == "Darwin":
            try:
                subprocess.run(
                    ["osascript", "-e",
                     f'tell app "Finder" to delete POSIX file "{path}"'],
                    capture_output=True, timeout=10, check=True,
                )
                return f"✅ 已移至回收站：{file_path}"
            except Exception:
                # macOS osascript 移回收站失败 → 穿透永久删除，同属语义降级，留痕。
                logger.warning(
                    "osascript 移回收站失败，将回落永久删除：%s",
                    file_path, exc_info=True,
                )

    # 永久删除
    try:
        entry_type = "目录" if path.is_dir() else "文件"
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()
        return f"✅ 已{'永久' if permanently else ''}删除{entry_type}：{file_path}"
    except PermissionError:
        return f"错误：无权限删除：{file_path}"
    except OSError as e:
        return f"删除失败：{e}"
