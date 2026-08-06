"""pandaren/plan/files.py — Plan Mode 文件操作（纯函数）

设计原则：
  - 所有文件 IO 操作集中于此，tools.py 只做委托
  - 路径安全校验：防止路径穿越
  - generate_word_slug: 生成安全的文件名 slug
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("pandaren.plan.files")


def generate_word_slug(name: str, max_words: int = 5) -> str:
    """从名称生成安全的文件 slug。

    Args:
        name: 原始名称（中文/英文混合）
        max_words: 最大单词数限制

    Returns:
        安全的 slug 字符串（不含路径分隔符）
    """
    import re
    # 移除特殊字符，保留字母、数字、中文和空格
    cleaned = re.sub(r'[^\w\s\u4e00-\u9fff-]', '', name, flags=re.UNICODE)
    # 替换空白为连字符
    slug = re.sub(r'[\s_]+', '-', cleaned.strip())
    # 截断到 max_words 个词
    parts = [p for p in slug.split('-') if p]
    slug = '-'.join(parts[:max_words]) if parts else "plan"
    return slug or "plan"


def generate_plan_file_path(
    plan_name: str,
    config_home: str | None = None,
    *,
    plan_dir: str | None = None,
) -> str:
    """生成计划文件路径。

    路径优先级: plan_dir > config_home/plans > {cwd}/.pandaren/plans

    Args:
        plan_name:  计划文件名（不含扩展名），由 LLM 指定
        config_home: 配置目录，默认使用当前工作目录下的 .pandaren
        plan_dir:    自定义计划文件目录（绝对路径），传入后忽略 config_home

    Returns:
        计划文件的绝对路径
    """
    if plan_dir is not None:
        plans_dir = Path(plan_dir)
    elif config_home is not None:
        plans_dir = Path(config_home) / "plans"
    else:
        plans_dir = Path.cwd() / ".pandaren" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)

    # 安全：只取文件名部分，去掉路径分隔符，防止路径穿越
    safe_name = plan_name.replace("/", "_").replace("\\", "_")
    return str(plans_dir / f"{safe_name}.md")


def validate_plan_file_path(file_path: str) -> str | None:
    """校验用户指定的计划文件路径是否合法。

    规则:
      - 必须是绝对路径
      - 必须以 .md 结尾
      - 父目录必须存在
      - 不能包含路径穿越（..）

    Args:
        file_path: 用户指定的路径

    Returns:
        合法路径 or None（不合法）
    """

    path = Path(file_path).resolve()

    # 检查路径穿越
    if ".." in file_path:
        logger.warning("[plan-files] path traversal detected: %s", file_path)
        return None

    # 检查后缀
    if path.suffix.lower() != ".md":
        logger.warning("[plan-files] not a .md file: %s", file_path)
        return None

    # 检查父目录存在
    if not path.parent.exists():
        logger.warning("[plan-files] parent dir not exists: %s", path.parent)
        return None

    return str(path)


def write_initial_plan_file(file_path: str, user_message: str) -> None:
    """在 enter_plan_mode 时写入计划文件的初始内容。

    将用户的原始需求消息以 Markdown 格式写入计划文件，
    作为计划的起点。后续 LLM 通过 write_plan 全量覆盖。

    Args:
        file_path:    计划文件绝对路径
        user_message: 用户原始需求消息

    Raises:
        OSError: 写入失败时抛出（由调用方处理）
    """
    content = f"# 用户原始需求\n\n{user_message}\n"
    plan_file = Path(file_path)
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text(content, encoding="utf-8")
    logger.debug(
        "[plan-files] wrote initial plan (%d chars) to %s",
        len(content), file_path,
    )


def write_plan_content(file_path: str, content: str) -> None:
    """将计划内容写入文件（全量覆盖）。

    Args:
        file_path: 计划文件绝对路径
        content:   完整的 Markdown 内容

    Raises:
        OSError: 写入失败时抛出（由调用方处理）
    """
    plan_file = Path(file_path)
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text(content, encoding="utf-8")
    logger.info("[write_plan_content-plan-files] wrote %d chars to %s", len(content), file_path)


def read_plan(file_path: str) -> str | None:
    """读取计划文件内容。

    Args:
        file_path: 计划文件绝对路径

    Returns:
        文件内容 or None（文件不存在时）
    """
    plan_file = Path(file_path)
    if not plan_file.exists():
        return None
    return plan_file.read_text(encoding="utf-8")


def plan_exists(file_path: str) -> bool:
    """检查计划文件是否存在且非空。

    Args:
        file_path: 计划文件绝对路径

    Returns:
        True 如果文件存在且内容非空
    """
    plan_file = Path(file_path)
    if not plan_file.exists():
        return False
    try:
        content = plan_file.read_text(encoding="utf-8")
        return bool(content and content.strip())
    except (OSError, UnicodeDecodeError):
        return False
