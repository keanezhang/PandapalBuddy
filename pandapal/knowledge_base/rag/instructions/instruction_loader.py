#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAG 内部轻量指令加载器（替代 agent.instructions.instruction_manager）。

仅依赖标准库 + re，不引入外部模块。Jinja2 为可选依赖，启用后支持模板渲染。

功能：从 .md 文件读取指令内容（支持 YAML Front Matter 分离 / 提取），
     并对 {VAR_NAME} 占位符做变量替换或 Jinja2 模板渲染。

.. note:: 此模块为 SDK **内部** API，供 QueryIntentClassifier、Reranker 等组件使用。
          外部用户通常不需要直接调用。
"""

import re
from pathlib import Path
from typing import Union, Any, Dict, Optional, Tuple, overload, Literal

import logging

from ..exceptions import RAGComponentError

logger = logging.getLogger(__name__)

# Jinja2 可选依赖
try:
    from jinja2 import Environment as _Jinja2Env  # noqa: F401

    _JINJA2_AVAILABLE = True
except ImportError:  # pragma: no cover
    _JINJA2_AVAILABLE = False

# YAML Front Matter 正则：兼容 Windows \r\n
_FRONT_MATTER_RE = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n", re.DOTALL)


def _parse_front_matter(raw: str) -> Tuple[str, Dict[str, str]]:
    """
    解析 YAML Front Matter 并返回 (正文, 元数据字典)。

    元数据采用简单 ``key: value`` 逐行解析（不依赖 PyYAML），
    仅支持单行字符串值。

    Args:
        raw: 原始文件内容。

    Returns:
        (content, metadata) 元组。若无 Front Matter 则 metadata 为空字典。
    """
    fm_match = _FRONT_MATTER_RE.match(raw)
    if not fm_match:
        return raw, {}

    content = raw[fm_match.end():]
    metadata: Dict[str, str] = {}
    for line in fm_match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            metadata[key.strip()] = value.strip()
    return content, metadata


def _render_jinja2(content: str, variables: Dict[str, Any]) -> str:
    """
    使用 Jinja2 渲染模板内容。

    Args:
        content: 模板字符串。
        variables: 变量字典。

    Returns:
        渲染后的字符串。

    Raises:
        RAGComponentError: Jinja2 未安装。
    """
    if not _JINJA2_AVAILABLE:
        raise RAGComponentError(
            "使用 Jinja2 模板引擎需要安装 jinja2: pip install jinja2"
        )
    env = _Jinja2Env(
        keep_trailing_newline=True,
        undefined=__import__("jinja2").StrictUndefined,
    )
    template = env.from_string(content)
    return template.render(**variables)


def _render_simple(content: str, variables: Dict[str, Any]) -> str:
    """
    简单 ``{KEY}`` 占位符替换。

    只替换 variables 中已声明的键，其余 ``{xxx}`` 原样保留。

    Args:
        content: 模板字符串。
        variables: 变量字典。

    Returns:
        替换后的字符串。
    """
    for key, value in variables.items():
        pattern = r"\{" + re.escape(key) + r"\}"
        content = re.sub(pattern, str(value), content)
    return content


@overload
def load_instruction(
    file_path: Union[str, Path],
    variables: Optional[Dict[str, Any]] = ...,
    *,
    use_jinja2: bool = ...,
    return_metadata: Literal[False] = ...,
) -> str: ...


@overload
def load_instruction(
    file_path: Union[str, Path],
    variables: Optional[Dict[str, Any]] = ...,
    *,
    use_jinja2: bool = ...,
    return_metadata: Literal[True],
) -> Tuple[str, Dict[str, str]]: ...


def load_instruction(
    file_path: Union[str, Path],
    variables: Optional[Dict[str, Any]] = None,
    *,
    use_jinja2: bool = False,
    return_metadata: bool = False,
) -> Union[str, Tuple[str, Dict[str, str]]]:
    """
    加载一个 .md 指令文件并做变量替换。

    .. note:: 此函数为 SDK **内部** API，供 QueryIntentClassifier、Reranker 等
              组件使用。外部用户通常不需要直接调用。

    1. 读取文件全文。
    2. 如果文件以 ``---`` YAML Front Matter 开头，则自动剥离 Front Matter，
       只保留正文（与 agent.instructions 的行为一致）。
       当 ``return_metadata=True`` 时同时返回解析后的元数据字典。
    3. 对正文中的 ``{KEY}`` 占位符做安全替换（只替换 variables 中存在的键）。
       当 ``use_jinja2=True`` 时使用 Jinja2 模板引擎渲染（需安装 jinja2）。

    Args:
        file_path: 指令 .md 文件的绝对或相对路径。
        variables: 变量字典，键为占位符名（不带花括号），值将被 str() 转换后替换。
        use_jinja2: 是否使用 Jinja2 模板引擎。默认 False（使用简单 ``{KEY}`` 替换）。
        return_metadata: 是否同时返回 YAML Front Matter 元数据。
            为 True 时返回 ``(content, metadata)`` 元组。

    Returns:
        渲染后的指令字符串；或当 ``return_metadata=True`` 时返回
        ``(content, metadata)`` 元组。

    Raises:
        RAGComponentError: 文件不存在或 Jinja2 未安装。
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise RAGComponentError(f"指令文件不存在: {file_path}")

    raw = file_path.read_text(encoding="utf-8")

    # 解析 YAML Front Matter
    content, metadata = _parse_front_matter(raw)
    content = content.strip()

    # 变量替换
    if variables:
        if use_jinja2:
            content = _render_jinja2(content, variables)
        else:
            content = _render_simple(content, variables)

    if return_metadata:
        return content, metadata
    return content
