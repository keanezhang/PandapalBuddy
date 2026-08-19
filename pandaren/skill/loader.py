"""pandaren/skill/loader.py — SkillLoader（从文件系统加载 Skill）

辅助工具，不是 SkillRegistry 的核心职责。
支持从 Markdown 文件加载 Skill 定义（YAML Frontmatter + Markdown body）。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from .models import Skill, SkillSource

logger = logging.getLogger("pandaren.skill.loader")


def load_skill_from_file(
    path: str | Path,
    source: SkillSource = SkillSource.PROJECT,
) -> Skill:
    """从 Markdown 文件加载 Skill。

    文件格式（YAML Frontmatter + Markdown body）：
        ---
        name: arch-design
        description: 资深架构师视角，设计模块/层/组件
        when_to_use: 当需要设计模块架构或组件分层时使用
        allowed_tools: read_file, grep_search, write_file
        allow_auto_trigger: true
        tags: design, architecture
        ---

        你是一位资深架构师...（正文 Markdown）

    Args:
        path: Skill 文件路径。
        source: Skill 来源（默认 PROJECT）。

    Returns:
        解析后的 Skill 对象。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 文件格式错误。
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Skill 文件不存在: {file_path}")

    text = file_path.read_text(encoding="utf-8")

    # 解析 YAML Frontmatter
    frontmatter, body = _parse_frontmatter(text)

    if not frontmatter.get("name"):
        # 用文件名作为 name
        frontmatter["name"] = file_path.stem

    if not body.strip():
        raise ValueError(f"Skill 文件 '{file_path}' 正文为空")

    # 解析 when_to_use（必填，缺失 → 抛 ValueError）
    when_to_use = _as_str(frontmatter.get("when_to_use")).strip()
    if not when_to_use:
        raise ValueError(
            f"Skill 文件 '{file_path}' 缺少 when_to_use 字段"
        )

    # 解析 allowed_tools
    allowed_tools = None
    if "allowed_tools" in frontmatter:
        raw = frontmatter["allowed_tools"]
        if isinstance(raw, str):
            allowed_tools = tuple(
                t.strip() for t in raw.split(",") if t.strip()
            )
        elif isinstance(raw, (list, tuple)):
            allowed_tools = tuple(str(t).strip() for t in raw if str(t).strip())

    # 解析 tags
    tags: tuple[str, ...] = ()
    if "tags" in frontmatter:
        raw_tags = frontmatter["tags"]
        if isinstance(raw_tags, str):
            tags = tuple(t.strip() for t in raw_tags.split(",") if t.strip())
        elif isinstance(raw_tags, (list, tuple)):
            tags = tuple(str(t).strip() for t in raw_tags if str(t).strip())

    # 解析 allow_auto_trigger
    allow_auto = True
    if "allow_auto_trigger" in frontmatter:
        val = frontmatter["allow_auto_trigger"]
        if isinstance(val, bool):
            allow_auto = val
        elif isinstance(val, str):
            allow_auto = val.lower() in ("true", "yes", "1")
        elif val is None:
            allow_auto = True
        else:
            allow_auto = bool(val)

    return Skill(
        name=_as_str(frontmatter["name"]),
        description=_as_str(frontmatter.get("description")),
        when_to_use=when_to_use,
        content=body.strip(),
        source=source,
        allowed_tools=allowed_tools,
        allow_auto_trigger=allow_auto,
        tags=tags,
    )


def load_skills_from_dir(
    directory: str | Path,
    source: SkillSource = SkillSource.PROJECT,
    pattern: str = "SKILL.md",
    recursive: bool = True,
) -> list[Skill]:
    """从目录批量加载 Skill。

    SK7 Fail-Safe：单个文件加载失败时跳过，不阻塞其他文件。

    Args:
        directory: 目录路径。
        source: Skill 来源。
        pattern: 文件匹配模式，默认 "SKILL.md"。
        recursive: 是否递归扫描子目录，默认 True。

    Returns:
        成功加载的 Skill 列表。
    """
    dir_path = Path(directory)
    if not dir_path.is_dir():
        logger.warning("Skill 目录不存在: %s", dir_path)
        return []

    glob_mode = f"**/{pattern}" if recursive else pattern
    skills: list[Skill] = []
    for file_path in sorted(dir_path.glob(glob_mode)):
        if file_path.is_file():
            try:
                skill = load_skill_from_file(file_path, source=source)
                skills.append(skill)
            except Exception as e:
                logger.warning(
                    "Skill 加载失败（跳过）: %s → %s", file_path, e,
                )

    # logger.info(
    #     "从 %s 加载了 %d 个 Skill（来源: %s, 递归: %s）",
    #     dir_path, len(skills), source.name, recursive,
    # )
    return skills


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """解析 YAML Frontmatter（使用 PyYAML，支持折叠块 `>`、保留块 `|`、列表、嵌套等完整语法）。

    Returns:
        (frontmatter_dict, body_text) 二元组。frontmatter 的 value 可能是
        str / list / dict / bool / int / None，调用方需用 ``_as_str()`` 等
        helper 规范化。
    """
    pattern = r"^---\s*\n(.*?)\n---\s*\n(.*)$"
    match = re.match(pattern, text, re.DOTALL)
    if not match:
        # 无 frontmatter，整个文本作为 body
        return {}, text

    fm_text = match.group(1)
    body = match.group(2)

    try:
        parsed = yaml.safe_load(fm_text)
    except yaml.YAMLError as e:
        logger.warning("YAML Frontmatter 解析失败（退回空 dict）: %s", e)
        return {}, body

    if parsed is None:
        return {}, body
    if not isinstance(parsed, dict):
        logger.warning(
            "YAML Frontmatter 顶层不是映射（type=%s），退回空 dict",
            type(parsed).__name__,
        )
        return {}, body

    # 键统一为 str（YAML 允许非字符串键，这里强制收敛以保持下游类型一致）
    frontmatter: dict[str, Any] = {str(k): v for k, v in parsed.items()}
    return frontmatter, body


def _as_str(value: Any) -> str:
    """把 YAML 还原的任意值规范化为 str（None → "", list/dict → 按 str(...) 兜底）。

    主要用于 frontmatter 字段：这些字段下游一律按字符串使用，但 YAML 会把
    空值 ``key:`` 还原为 ``None``，把引号包裹的数字/布尔还原为 int/bool，需要
    统一兜底以避免 ``AttributeError: 'NoneType' object has no attribute 'strip'``。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)
