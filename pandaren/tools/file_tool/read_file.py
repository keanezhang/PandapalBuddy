"""pandaren/tools/read_file.py — 读取文件工具。

支持文本文件、图片、Jupyter Notebook (.ipynb) 和 PDF。

v2 特性：
- 去重：同一文件+offset+limit 在 mtime 未变时跳过，节省 token
- Token/大小保护：MAX_FILE_BYTES 硬上限 + MAX_LINES_FULL_READ 行数警告
- validate_input：二进制/设备文件拦截 + 智能路径建议
- 路径规范化：expand_path（~ 展开、相对路径→项目根、空白修剪）
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
from dataclasses import dataclass
from typing import Optional

from pandaren.tool.types import ToolTier, SensitivityLevel
from pandaren.tool.definition.context import ToolContext
from pandaren.tool.definition.tool_policy import ToolPolicy
from pandaren.tool.definition.tool_lifecycle import ToolLifecycle
from pandaren.tool.decorator import tool
from pandaren.utils import expand_path, format_file_size
from ._utils import (
    _read_cache,
    _resolve_max_file_bytes,
    MAX_LINES_FULL_READ,
    IMAGE_EXTENSIONS,
    record_file_access,
    validate_read_input,
    set_last_read_mtime,
)


def _try_record_mtime(abs_path: str) -> None:
    """尝试记录 read mtime，失败静默。"""
    try:
        set_last_read_mtime(abs_path, os.stat(abs_path).st_mtime_ns)
    except OSError:
        pass


# ────────────────────────────────────────────
#  LLM Guide — 对标 Claude Code FileReadTool/prompt.ts
# ────────────────────────────────────────────

_READ_FILE_LLM_GUIDE = f"""读取本地文件。你可以直接访问机器上的任何文件。如果用户提供了一个路径，假设它是有效的——读一个不存在的文件也没关系，会返回错误。

使用规则：
- file_path 必须使用绝对路径，不要使用相对路径
- 用户可能传递以 . 开头的隐藏目录名（如 .pandapal/plans/xxx），转换为绝对路径时必须保留前导点，.pandapal 是一个完整目录名，不是 .（当前目录）
- 默认从头读取最多 {MAX_LINES_FULL_READ} 行。超过此限制会提示你使用 offset/limit 分段读取
- 可通过 offset（起始行，1-based）和 limit（行数）指定读取范围。建议在不传这两个参数时读取整个文件
- 返回结果使用 cat -n 格式，行号从 1 开始
- 可以读取图片（PNG/JPG/GIF/WEBP），图片内容将以可视化形式呈现
- 可以读取 PDF 文件（.pdf）
- 可以读取 Jupyter Notebook（.ipynb），返回结构化的 cells 及其输出
- 只能读取文件，不能读取目录。要列出目录内容请使用 list_files 工具
- 如果用户提供了截图路径，始终使用此工具查看
- 如果文件存在但内容为空，会收到系统提醒"""


# ────────────────────────────────────────────
#  图片读取
# ────────────────────────────────────────────

@dataclass
class ImageReadResult:
    """图片读取结构化结果（HasLLMFormat 协议）。"""
    file_path: str
    base64_data: str
    media_type: str
    original_size: int
    original_width: int | None = None
    original_height: int | None = None

    def __tool_format_for_llm__(self) -> str:
        parts = [
            f"[图片] {self.file_path}",
            f"{format_file_size(self.original_size)}",
            self.media_type,
        ]
        if self.original_width and self.original_height:
            parts.append(f"{self.original_width}x{self.original_height}")
        return " | ".join(parts)


def _read_image(path: str) -> ImageReadResult:
    """读取图片，返回 ImageReadResult。"""
    max_bytes = _resolve_max_file_bytes()
    size = os.path.getsize(path)
    if size > max_bytes:
        raise ValueError(f"图片过大（{format_file_size(size)}），超过上限（{format_file_size(max_bytes)}）")
    if size == 0:
        raise ValueError("图片文件为空")

    ext = pathlib.Path(path).suffix.lstrip(".").lower()
    mime_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                 "gif": "image/gif", "webp": "image/webp"}
    media_type = mime_map.get(ext, f"image/{ext}")

    with open(path, "rb") as f:
        raw = f.read()

    width, height = None, None
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(raw))
        width, height = img.size
    except Exception:
        pass

    return ImageReadResult(
        file_path=path, base64_data=base64.b64encode(raw).decode("ascii"),
        media_type=media_type, original_size=size,
        original_width=width, original_height=height,
    )


# ────────────────────────────────────────────
#  Notebook 读取
# ────────────────────────────────────────────

def _read_notebook(path: str) -> str:
    """读取 .ipynb，返回精简 JSON。"""
    max_bytes = _resolve_max_file_bytes()
    size = os.path.getsize(path)
    if size > max_bytes:
        raise ValueError(f"Notebook 过大（{format_file_size(size)}），超过上限")

    with open(path, "r", encoding="utf-8") as f:
        nb = json.load(f)

    cells = nb.get("cells", [])
    simplified = [{"cell_type": c.get("cell_type"), "source": c.get("source"),
                   "execution_count": c.get("execution_count"), "outputs": c.get("outputs")}
                  for c in cells]
    return json.dumps(simplified, ensure_ascii=False, indent=2)


# ────────────────────────────────────────────
#  PDF 文本提取
# ────────────────────────────────────────────

def _read_pdf(path: str) -> str:
    """提取 PDF 文本（可选依赖 PyPDF2 / pdfplumber）。"""
    max_bytes = _resolve_max_file_bytes()
    size = os.path.getsize(path)
    if size > max_bytes:
        raise ValueError(f"PDF 过大（{format_file_size(size)}），超过上限")

    parts: list[str] = []
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            for i, page in enumerate(pdf.pages):
                text = page.extract_text()
                if text:
                    parts.append(f"--- 第 {i + 1} 页 ---\n{text}")
    except ImportError:
        try:
            from PyPDF2 import PdfReader
            reader = PdfReader(path)
            for i, page in enumerate(reader.pages):
                text = page.extract_text()
                if text:
                    parts.append(f"--- 第 {i + 1} 页 ---\n{text}")
        except ImportError:
            raise ImportError("PDF 读取需要安装 pdfplumber 或 PyPDF2")

    return "\n\n".join(parts) if parts else "(PDF 中未找到可提取的文本)"


# ────────────────────────────────────────────
#  read_file 工具
# ────────────────────────────────────────────

@tool.function(
    tier=ToolTier.ALWAYS,
    name="read_file",
    description="读取本地文件内容，支持文本、图片、Notebook 和 PDF。支持 offset/limit 分段读取大文件。",
    when_to_use="需要查看文件内容、读取配置、分析代码时调用。**始终使用此工具读取文件，不要用 bash cat 等命令。**",
    policy=ToolPolicy(
        sensitivity=SensitivityLevel.LOW,
        is_reversible=True, audit_required=False,
        is_idempotent=True, max_output_bytes=200_000,
        read_only=True,
    ),
    lifecycle=ToolLifecycle(validate_input=validate_read_input),
    llm_guide=_READ_FILE_LLM_GUIDE,
    progress_label='读取文件「{file_path}」',
)
def read_file(
    ctx: ToolContext,
    file_path: str,
    offset: int = 1,
    limit: Optional[int] = None,
) -> str:
    """读取本地文件内容。

    Args:
        ctx: 工具上下文。
        file_path: 文件的绝对路径。
        offset: 起始行号（1-based），默认为 1。
        limit: 最多读取行数，不指定则读取全部（受上限限制）。

    Returns:
        文件内容字符串（文本带行号，图片/Notebook/PDF 返回结构化信息）。
    """
    full_path = expand_path(file_path)
    abs_path = str(full_path)
    max_bytes = _resolve_max_file_bytes()

    # 防御性类型转换：LLM 可能传字符串 "20" 而非整数 20
    try:
        offset = int(offset)
    except (ValueError, TypeError):
        offset = 1
    if limit is not None:
        try:
            limit = int(limit)
        except (ValueError, TypeError):
            limit = None

    # 大小检查
    try:
        file_size = os.path.getsize(abs_path)
    except OSError:
        file_size = 0
    if file_size > max_bytes:
        return (
            f"错误：文件过大（{format_file_size(file_size)}），"
            f"超过读取上限（{format_file_size(max_bytes)}）。\n"
            f"请使用 offset 和 limit 分段读取：\n"
            f'  read_file(file_path="{file_path}", offset=1, limit=500)'
        )

    ext = pathlib.Path(abs_path).suffix.lstrip(".").lower()

    # ── 去重 ──
    dedup = _read_cache.get(abs_path, offset, limit)
    if dedup is not None:
        record_file_access(ctx, file_path, op="read")
        return dedup

    # ── 图片 ──
    if ext in IMAGE_EXTENSIONS:
        try:
            img = _read_image(abs_path)
            record_file_access(ctx, file_path, op="read")
            _try_record_mtime(abs_path)
            return img  # HasLLMFormat，Phase 3 自动格式化
        except FileNotFoundError:
            return f"错误：文件不存在：{file_path}"
        except PermissionError:
            return f"错误：无权限读取：{file_path}"
        except Exception as e:
            return f"读取图片失败：{e}"

    # ── Notebook ──
    if ext == "ipynb":
        try:
            content = _read_notebook(abs_path)
            record_file_access(ctx, file_path, op="read")
            _try_record_mtime(abs_path)
            return f"# {file_path}  [Notebook]\n\n{content}"
        except FileNotFoundError:
            return f"错误：文件不存在：{file_path}"
        except PermissionError:
            return f"错误：无权限读取：{file_path}"
        except Exception as e:
            return f"读取 Notebook 失败：{e}"

    # ── PDF ──
    if ext == "pdf":
        try:
            content = _read_pdf(abs_path)
            record_file_access(ctx, file_path, op="read")
            _try_record_mtime(abs_path)
            return f"# {file_path}  [PDF]\n\n{content}"
        except ImportError:
            return "错误：PDF 读取需安装可选依赖。运行：pip install PyPDF2"
        except FileNotFoundError:
            return f"错误：文件不存在：{file_path}"
        except PermissionError:
            return f"错误：无权限读取：{file_path}"
        except Exception as e:
            return f"读取 PDF 失败：{e}"

    # ── 文本 ──
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        # 记录 read mtime（空文件/偏移越界也记录，edit/write 的 staleness check 需要）
        try:
            set_last_read_mtime(abs_path, os.stat(abs_path).st_mtime_ns)
        except OSError:
            pass

        start = max(0, offset - 1)
        end = (start + limit) if limit is not None else len(lines)

        # 全量读取行数告警
        if limit is None and len(lines) > MAX_LINES_FULL_READ:
            return (
                f"提示：该文件共 {len(lines)} 行，超过全量读取建议上限（{MAX_LINES_FULL_READ} 行）。\n"
                f"为避免输出被截断，请使用 offset/limit 分段：\n"
                f'  read_file(file_path="{file_path}", offset=1, limit={MAX_LINES_FULL_READ})\n'
                f"如需继续全量读取，请显式指定 limit={len(lines)}"
            )

        selected = lines[start:end]

        if not selected:
            if len(lines) == 0:
                return "<system-reminder>警告：文件存在但内容为空。</system-reminder>"
            return f"<system-reminder>警告：文件存在，但偏移超出范围（起始行 {offset}，文件共 {len(lines)} 行）。</system-reminder>"

        result_lines = []
        for i, line in enumerate(selected, start=start + 1):
            result_lines.append(f"{i}→{line.rstrip()}")

        content = "\n".join(result_lines)
        header = f"# {file_path}  [{start + 1}-{start + len(selected)}/{len(lines)} 行]\n"
        full_content = header + content

        record_file_access(ctx, file_path, op="read")

        try:
            mtime_ns = os.stat(abs_path).st_mtime_ns
            _read_cache.set(abs_path, offset, limit, full_content, mtime_ns)
            set_last_read_mtime(abs_path, mtime_ns)
        except OSError:
            pass

        return full_content

    except FileNotFoundError:
        return f"错误：文件不存在：{file_path}"
    except PermissionError:
        return f"错误：无权限读取：{file_path}"
    except Exception as e:
        return f"读取文件失败：{e}"
