"""pandaren/tool/schema_inference.py — 统一的 JSON Schema 推导模块。

从函数签名 + type hints + docstring 推导 input_schema。
消除原 decorator.py 和 loader.py 中的重复实现。
"""

from __future__ import annotations

import inspect
import logging
import types
import typing
from typing import Any, Callable, get_type_hints, get_origin, get_args

from .definition.context import ToolContext

logger = logging.getLogger(__name__)

# Python 类型到 JSON Schema 类型映射
_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _unwrap_optional(hint: Any) -> Any:
    """解包 Optional[X] → X，非 Optional 原样返回。"""
    origin = get_origin(hint)
    if origin is typing.Union or origin is getattr(types, "UnionType", None):
        args = [a for a in get_args(hint) if a is not type(None)]  # noqa: E721
        if len(args) == 1:
            return args[0]
    return hint


def parse_docstring(func: Callable) -> tuple[str, dict[str, str]]:
    """解析函数 docstring，提取描述和参数说明。

    支持 Google / NumPy / Sphinx 三种风格（简化解析）。
    返回: (description, {param_name: param_description})
    """
    doc = inspect.getdoc(func) or ""
    lines = doc.strip().split("\n")

    # 第一行作为 description
    description = lines[0].strip() if lines else func.__name__

    # 简易解析 Google 风格 Args
    params: dict[str, str] = {}
    in_args = False
    for line in lines[1:]:
        stripped = line.strip()
        if stripped.lower().startswith(("args:", "arguments:", "parameters:")):
            in_args = True
            continue
        if stripped.lower().startswith(("returns:", "raises:", "examples:", "notes:", "yields:")):
            in_args = False
            continue
        if in_args and ":" in stripped:
            param_name, param_desc = stripped.split(":", 1)
            param_name = param_name.strip()
            # 跳过带类型注解的参数名（如 "path (str)"）
            if "(" in param_name:
                param_name = param_name.split("(")[0].strip()
            params[param_name] = param_desc.strip()

    return description, params


def infer_input_schema(func: Callable, param_docs: dict[str, str] | None = None) -> dict[str, Any]:
    """从函数签名 + type hints + docstring 推导 JSON Schema。

    统一入口，消除 decorator.py / loader.py 的重复逻辑。
    自动跳过第一个参数（ctx: ToolContext）。

    Args:
        func: 工具的 executor 函数。
        param_docs: 参数文档（来自 docstring 解析），None 时自动解析。

    Returns:
        JSON Schema dict。
    """
    if param_docs is None:
        _, param_docs = parse_docstring(func)

    try:
        hints = get_type_hints(func)
    except Exception:
        # 类型提示解析失败（未解析 forward-ref 等）→ schema 静默丢类型，留痕暴露根因。
        logger.warning(
            "schema_inference: get_type_hints(%s) 失败，schema 将缺类型信息",
            getattr(func, "__name__", func), exc_info=True,
        )
        hints = {}

    sig = inspect.signature(func)
    params = list(sig.parameters.values())

    # 跳过第一个参数（ToolContext）——优先基于类型注解判断，回退到参数名匹配
    if params:
        first_hint = hints.get(params[0].name)
        if first_hint is ToolContext or params[0].name in ("ctx", "context", "self"):
            params = params[1:]

    properties: dict[str, Any] = {}
    required: list[str] = []

    for param in params:
        prop: dict[str, Any] = {}
        hint = hints.get(param.name)

        # 类型映射：先解包 Optional[X] → X，再查 origin 后查表
        base_hint = _unwrap_optional(hint)
        origin = get_origin(base_hint)
        if origin is not None and origin in _TYPE_MAP:
            prop["type"] = _TYPE_MAP[origin]
        elif base_hint in _TYPE_MAP:
            prop["type"] = _TYPE_MAP[base_hint]
        else:
            prop["type"] = "string"  # 默认 string

        # 描述
        if param.name in param_docs:
            prop["description"] = param_docs[param.name]

        properties[param.name] = prop

        # 必填判断（无默认值 → 必填）
        if param.default is inspect.Parameter.empty:
            required.append(param.name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required

    return schema
