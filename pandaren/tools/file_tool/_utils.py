"""pandaren/tools/file_tool/_utils.py — 文件操作共享基础设施。

供 read_file / write_file / edit_file / delete_file / list_files 五个工具共用：
  - ReadCache: 文件读取去重缓存（LRU），write/edit/delete 后自动 invalidate
  - record_file_access: WorkingMemory 契约记录（read/write/edit/delete 操作）
  - 共享常量: 大小上限 / 行数上限 / 缓存上限 / 图片扩展名
  - validate_input 钩子: validate_read_input / validate_write_edit_input /
                         validate_delete_input / validate_list_input
"""
from __future__ import annotations

import os
import pathlib
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from pandaren.tool.definition.context import ToolContext
from pandaren.tool.definition.tool_result import ValidationResult
from pandaren.memory import RECENT_FILE_READS_WM_KEY
from pandaren.utils import (
    expand_path,
    is_unc_path,
    suggest_similar_path,
    format_file_size,
    resolve_project_root,
    is_blocked_device_path,
    is_binary_extension,
    validate_sandbox_path,
)

# ────────────────────────────────────────────
#  常量
# ────────────────────────────────────────────

# 文件大小上限（5MB），环境变量 PANDAREN_READ_MAX_BYTES 可覆盖
_DEFAULT_MAX_FILE_BYTES: int = 5 * 1024 * 1024

# 全量读取行数上限：触发"建议分段读"警告
MAX_LINES_FULL_READ: int = 2000

# 去重缓存最大条目
_DEDUP_CACHE_MAX_SIZE: int = 50

# 可读图片扩展名（排除在二进制检测之外）
IMAGE_EXTENSIONS: frozenset[str] = frozenset({"png", "jpg", "jpeg", "gif", "webp"})


def _resolve_max_file_bytes() -> int:
    """返回当前生效的文件大小上限（环境变量可覆盖）。"""
    env_val = os.getenv("PANDAREN_READ_MAX_BYTES")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            pass
    return _DEFAULT_MAX_FILE_BYTES


# ────────────────────────────────────────────
#  文件去重缓存
# ────────────────────────────────────────────

@dataclass
class _ReadCacheEntry:
    """单条缓存条目。"""
    content: str
    mtime_ns: int
    offset: int
    limit: int | None


class ReadCache:
    """read_file 的模块级去重缓存（LRU，上限 50）。

    策略：(path, offset, limit) + mtime 不变 → 命中 stub。
    Claude Code 数据显示 ~18% Read 调用可去重。
    """

    def __init__(self, max_size: int = _DEDUP_CACHE_MAX_SIZE):
        self._max_size = max_size
        self._store: OrderedDict[str, _ReadCacheEntry] = OrderedDict()

    @staticmethod
    def _make_key(abs_path: str, offset: int, limit: int | None) -> str:
        return f"{abs_path}\x00{offset}\x00{limit}"

    def get(self, abs_path: str, offset: int, limit: int | None) -> str | None:
        """命中且 mtime 未变 → 返回 content；否则 None。"""
        key = self._make_key(abs_path, offset, limit)
        entry = self._store.get(key)
        if entry is None:
            return None
        try:
            current_mtime = os.stat(abs_path).st_mtime_ns
        except OSError:
            self._store.pop(key, None)
            return None
        if entry.mtime_ns == current_mtime:
            self._store.move_to_end(key, last=True)
            return entry.content
        self._store.pop(key, None)
        return None

    def set(
        self, abs_path: str, offset: int, limit: int | None,
        content: str, mtime_ns: int,
    ) -> None:
        """写入缓存，LRU 淘汰最旧条目。"""
        key = self._make_key(abs_path, offset, limit)
        self._store[key] = _ReadCacheEntry(
            content=content, mtime_ns=mtime_ns, offset=offset, limit=limit,
        )
        self._store.move_to_end(key, last=True)
        while len(self._store) > self._max_size:
            self._store.popitem(last=False)

    def invalidate(self, abs_path: str) -> None:
        """写入/编辑后清除该文件所有缓存条目。"""
        prefix = f"{abs_path}\x00"
        for k in list(self._store):
            if k.startswith(prefix):
                del self._store[k]


_read_cache = ReadCache()


# ────────────────────────────────────────────
#  缓存填充 helper（write_file / edit_file 写后使用）
# ────────────────────────────────────────────

def populate_read_cache(abs_path: str, raw_content: str, mtime_ns: int) -> None:
    """写/编辑后用与 read_file 一致的格式化内容填充缓存。

    避免 read_file 后续命中缓存时返回未格式化的裸内容。
    """
    lines = raw_content.splitlines(keepends=True)
    formatted = "".join(f"{i + 1}→{line.rstrip()}\n" for i, line in enumerate(lines)) if lines else ""
    header = f"# {abs_path}  [1-{len(lines)}/{len(lines)} 行]\n"
    _read_cache.set(abs_path, 1, None, header + formatted, mtime_ns)


# ────────────────────────────────────────────
#  最后一次读取的 mtime（写前时效性检查用）
# ────────────────────────────────────────────

# 记录每个文件最后一次被 read 时的 os.stat().st_mtime_ns。
# write_file 的 validate_input 检查此值：文件存在但未读过 → 拒绝写入。
# 写完后 write_file 更新此值，避免后续写误判为 stale。
_last_read_mtimes: dict[str, int] = {}


def set_last_read_mtime(abs_path: str, mtime_ns: int) -> None:
    """read_file 成功后调用，记录该文件的读取时刻。"""
    _last_read_mtimes[abs_path] = mtime_ns


def get_last_read_mtime(abs_path: str) -> int | None:
    """返回文件最后一次被 read 时的 mtime_ns，未读过返回 None。"""
    return _last_read_mtimes.get(abs_path)


# ────────────────────────────────────────────
#  WorkingMemory 记录
# ────────────────────────────────────────────

_RECENT_FILE_READS_MAX = 20


def record_file_access(ctx: ToolContext, file_path: str, op: str) -> None:
    """记录文件访问到 WorkingMemory[recent_file_reads]，供 PostCompact 使用。

    遵守 SDK / 应用层契约：
      - key = pandaren.memory.RECENT_FILE_READS_WM_KEY
      - value = list[{"path": str, "timestamp": float, "op": "read"|"write"|"edit"}]
      - 同 path 多次访问只保留最新（按 timestamp 取大）
      - 列表长度上限 _RECENT_FILE_READS_MAX_ENTRIES，按 timestamp 倒序裁剪


    失败静默——辅助记录，不影响主流程。
    """
    wm = getattr(ctx, "working_memory", None)
    if wm is None:
        if os.getenv("PANDAREN_RECENT_FILES_DEBUG"):
            print(f"[recent_files] working_memory is None, skip recording {file_path}")
        return
    try:
        try:
            abs_path = str(pathlib.Path(file_path).resolve())
        except Exception:
            abs_path = file_path
        existing = wm.get(RECENT_FILE_READS_WM_KEY)
        records: list[dict[str, Any]] = []
        if isinstance(existing, list):
            records = [r for r in existing if isinstance(r, dict)]
        records = [r for r in records if r.get("path") != abs_path]
        records.append({"path": abs_path, "timestamp": time.time(), "op": op})
        if len(records) > _RECENT_FILE_READS_MAX:
            records.sort(key=lambda r: float(r.get("timestamp", 0) or 0), reverse=True)
            records = records[:_RECENT_FILE_READS_MAX]
        wm.set(RECENT_FILE_READS_WM_KEY, records)
        if os.getenv("PANDAREN_RECENT_FILES_DEBUG"):
            print(f"[recent_files] recorded {op} {abs_path} (total={len(records)})")
    except Exception as exc:
        if os.getenv("PANDAREN_RECENT_FILES_DEBUG"):
            print(f"[recent_files] failed: {exc}")


# ────────────────────────────────────────────
#  validate_input 共享钩子
# ────────────────────────────────────────────

def validate_read_input(args: dict, ctx: ToolContext) -> ValidationResult | None:
    """read_file 执行前校验：安全基检 + 二进制拦截 + 文件存在 + Did you mean。"""
    result = _validate_path_safety(args, "file_path", action="读取")
    if result:
        return result

    full_path = expand_path(args["file_path"].strip())
    ext = full_path.suffix.lstrip(".").lower()
    if is_binary_extension(ext) and ext not in IMAGE_EXTENSIONS and ext != "pdf":
        return ValidationResult(
            valid=False,
            message=f"无法读取二进制文件：{args['file_path']}\n文件扩展名 .{ext} 表示这是二进制文件。请使用适当的工具分析。",
            error_code=4,
        )

    if not full_path.exists():
        root = resolve_project_root()
        suggestion = suggest_similar_path(str(full_path))
        msg = f"文件不存在：{args['file_path']}（项目根目录：{root}）"
        if suggestion:
            msg = f"文件不存在：{args['file_path']}。Did you mean `{suggestion}`?（项目根目录：{root}）"
        return ValidationResult(valid=False, message=msg, error_code=1)

    if not full_path.is_file():
        return ValidationResult(valid=False, message=f"路径不是文件：{args['file_path']}", error_code=2)

    return None


def validate_edit_input(args: dict, ctx: ToolContext) -> ValidationResult | None:
    """edit_file 执行前校验：安全基检 + 时效性检查（读过 + 未被改过）。

    对标 Claude Code FileEditTool.validateInput：
      1. 文件必须存在（编辑不能创建新文件，用 write_file）
      2. 必须被 read 过
      3. 读取后未被外部修改
    """
    result = _validate_path_safety(args, "file_path", action="编辑")
    if result:
        return result

    full_path = expand_path(args["file_path"].strip())
    abs_path = str(full_path)

    if not full_path.exists():
        root = resolve_project_root()
        suggestion = suggest_similar_path(abs_path)
        msg = f"文件不存在：{args['file_path']}（项目根目录：{root}）"
        if suggestion:
            msg = f"文件不存在：{args['file_path']}。Did you mean `{suggestion}`?（项目根目录：{root}）"
        return ValidationResult(valid=False, message=msg, error_code=1)

    # 大小上限（防 OOM）
    try:
        size = os.stat(abs_path).st_size
    except OSError:
        return ValidationResult(valid=False, message=f"无法访问文件：{args['file_path']}", error_code=3)
    if size > 10 * 1024 * 1024:  # 10MB
        return ValidationResult(
            valid=False,
            message=f"文件过大（{format_file_size(size)}），不适合编辑。请使用 write_file 完全重写，或用 bash sed 进行流式替换。",
            error_code=10,
        )

    # .ipynb 拒绝
    if full_path.suffix.lower() == ".ipynb":
        return ValidationResult(
            valid=False,
            message="无法编辑 .ipynb 文件。请使用 read_file 读取 notebook 结构后，通过 write_file 重写。",
            error_code=5,
        )

    last_mtime = get_last_read_mtime(abs_path)
    if last_mtime is None:
        return ValidationResult(
            valid=False,
            message=f"文件 '{args['file_path']}' 尚未被读取。请先使用 read_file 读取文件内容，然后再编辑。",
            error_code=2,
        )

    try:
        current_mtime = os.stat(abs_path).st_mtime_ns
    except OSError:
        return ValidationResult(valid=False, message=f"无法访问文件：{args['file_path']}", error_code=3)

    if current_mtime != last_mtime:
        return ValidationResult(
            valid=False,
            message=f"文件 '{args['file_path']}' 在读取后已被修改（可能由用户或格式化工具）。请重新读取文件内容后再编辑。",
            error_code=4,
        )

    return None


def validate_write_edit_input(args: dict, ctx: ToolContext) -> ValidationResult | None:
    """write_file / edit_file 执行前通用校验：安全基检 + 沙箱校验（不含 staleness）。"""
    result = _validate_path_safety(args, "file_path", action="写入")
    if result:
        return result

    # 沙箱校验：防止写入系统目录或项目/主目录外的路径
    full_path = expand_path(args["file_path"].strip())
    sandbox_err = validate_sandbox_path(full_path)
    if sandbox_err:
        return ValidationResult(valid=False, message=sandbox_err, error_code=12)

    return None


def validate_write_input(args: dict, ctx: ToolContext) -> ValidationResult | None:
    """write_file 执行前校验：安全基检 + 时效性检查（读过 + 未被改过）。

    对标 Claude Code FileWriteTool.validateInput：
      1. 文件存在 → 必须被 read 过
      2. 文件存在 → 读取后未被外部修改（如 linter/用户手动改）
    """
    result = _validate_path_safety(args, "file_path", action="写入")
    if result:
        return result

    full_path = expand_path(args["file_path"].strip())
    abs_path = str(full_path)

    # 新文件，不需要 staleness check
    if not full_path.exists():
        return None

    last_mtime = get_last_read_mtime(abs_path)
    if last_mtime is None:
        return ValidationResult(
            valid=False,
            message=f"文件 '{args['file_path']}' 尚未被读取。请先使用 read_file 读取文件内容，然后再写入。",
            error_code=2,
        )

    try:
        current_mtime = os.stat(abs_path).st_mtime_ns
    except OSError:
        return ValidationResult(
            valid=False,
            message=f"无法访问文件：{args['file_path']}",
            error_code=3,
        )

    if current_mtime != last_mtime:
        return ValidationResult(
            valid=False,
            message=f"文件 '{args['file_path']}' 在读取后已被修改（可能由用户或格式化工具）。请重新读取文件内容后再写入。",
            error_code=4,
        )

    return None


def validate_delete_input(args: dict, ctx: ToolContext) -> ValidationResult | None:
    """delete_file 执行前校验：UNC + 设备文件拦截 + 沙箱 + 存在性。"""
    result = _validate_path_safety(args, "file_path", action="删除")
    if result:
        return result

    full_path = expand_path(args["file_path"].strip())

    # 沙箱校验：防止删除系统目录或项目/主目录外的文件
    sandbox_err = validate_sandbox_path(full_path)
    if sandbox_err:
        return ValidationResult(valid=False, message=sandbox_err, error_code=12)

    if not full_path.exists():
        root = resolve_project_root()
        return ValidationResult(
            valid=False,
            message=f"文件不存在：{args['file_path']}（项目根目录：{root}）",
            error_code=1,
        )
    return None


def validate_list_input(args: dict, ctx: ToolContext) -> ValidationResult | None:
    """list_files 执行前校验：UNC 安全 + 路径存在 + 目录检查。"""
    path = args.get("path", ".").strip()
    result = _validate_path_safety(args, "path", action="列出", param_name="path", default=".")
    if result:
        return result

    full_path = expand_path(path)
    if not full_path.exists():
        root = resolve_project_root()
        return ValidationResult(
            valid=False,
            message=f"目录不存在：{path}（项目根目录：{root}）",
            error_code=1,
        )
    if not full_path.is_dir():
        return ValidationResult(
            valid=False,
            message=f"路径不是目录：{path}",
            error_code=2,
        )
    return None


# ────────────────────────────────────────────
#  内部 base helper
# ────────────────────────────────────────────

def _validate_path_safety(
    args: dict,
    key: str,
    *,
    action: str = "操作",
    param_name: str = "file_path",
    default: str = "",
) -> ValidationResult | None:
    """所有文件工具的通用安全基校验：空值 + UNC 阻断 + 设备文件阻断。

    每个具体的 validate_*_input 调用此 helper 后，再追加自己的业务校验。
    成功返回 None，失败返回 ValidationResult。
    """
    raw = args.get(key, "").strip() or default
    if not raw:
        return ValidationResult(
            valid=False,
            message=f"参数错误：{param_name} 不能为空",
            error_code=10,
        )

    if is_unc_path(raw):
        return ValidationResult(
            valid=False,
            message=f"安全限制：不支持 UNC 网络路径：{raw}",
            error_code=11,
        )

    full_path = expand_path(raw)
    if is_blocked_device_path(str(full_path)):
        return ValidationResult(
            valid=False,
            message=f"无法{action} '{raw}'：目标路径是设备文件。",
            error_code=9,
        )

    return None
