"""pandaren/llm/schema.py — Python 类型 → JSON Schema / response_format 转换器

支持 dataclass 和 Pydantic BaseModel，自动转换为 OpenAI 兼容的
response_format（json_schema 模式）。

推荐用法 — 直接在 .llm_settings() 中传类型，SDK 自动转换：
    from dataclasses import dataclass, field

    @dataclass
    class UserInfo:
        name: str = field(metadata={"description": "用户姓名"})
        age: int  = field(metadata={"description": "用户年龄"})

    agent = (
        AgentBuilder()
        .llm(client)
        .llm_settings(response_format=UserInfo)   # 传类型，SDK 自动转
        ...
    )

    # 也支持 Pydantic BaseModel
    from pydantic import BaseModel, Field

    class UserInfo(BaseModel):
        name: str = Field(description="用户姓名")
        age: int  = Field(description="用户年龄")

    agent = (
        AgentBuilder()
        .llm(client)
        .llm_settings(response_format=UserInfo)
        ...
    )

手动转换（低级 API，需要时使用）：
    from pandaren.llm import json_schema

    agent = (
        AgentBuilder()
        .llm(client)
        .llm_settings(response_format=json_schema(UserInfo))
        ...
    )
"""

from __future__ import annotations

import sys
import types
from dataclasses import MISSING, fields, is_dataclass
from typing import Any, Union, get_args, get_origin, get_type_hints


# ═══════════════════════════════════════════════════════════════
#  公开 API
# ═══════════════════════════════════════════════════════════════


def json_schema(
    cls: type,
    *,
    name: str | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    """将 dataclass 转换为 OpenAI 兼容的 response_format dict。

    Args:
        cls:    dataclass 类型
        name:   schema 名称（默认取类名）
        strict: 是否启用 strict 模式（OpenAI 推荐，但部分 provider 不支持）

    Returns:
        可直接传给 response_format 的 dict，如：
        {
            "type": "json_schema",
            "json_schema": {
                "name": "UserInfo",
                "strict": false,
                "schema": { ... }
            }
        }
    """
    if not is_dataclass(cls):
        raise TypeError(f"json_schema() 需要 dataclass 类型，收到 {cls!r}")

    schema_name = name or cls.__name__
    schema = _dataclass_to_schema(cls, strict=strict)

    result: dict[str, Any] = {
        "type": "json_schema",
        "json_schema": {
            "name": schema_name,
            "strict": strict,
            "schema": schema,
        },
    }

    return result


def output_type_to_response_format(
    output_type: type,
    *,
    strict: bool = False,
) -> dict[str, Any]:
    """将 dataclass / Pydantic BaseModel 转为 response_format dict。

    这是 LLMClient._resolve_response_format() 的底层实现，SDK 内部使用。
    当用户在 .llm_settings(response_format=SomeType) 中传入类型时，
    _resolve_response_format() 自动调用此函数完成转换。

    用户通常不需要直接调用，直接在 .llm_settings() 中传类型即可。

    Args:
        output_type: dataclass 类型 或 Pydantic BaseModel 子类
        strict: 是否启用 strict 模式

    Returns:
        OpenAI 兼容的 response_format dict
    """
    _validate_output_type(output_type)

    if _is_pydantic_model(output_type):
        return _pydantic_to_response_format(output_type, strict=strict)
    else:
        return json_schema(output_type, strict=strict)


def _validate_output_type(output_type: type) -> None:
    """校验 output_type 是否为合法的结构化输出类型。"""
    if output_type is None or output_type is str:
        raise TypeError(
            f"output_type 不支持 {output_type!r}（纯文本输出无需设置 output_type）"
        )

    if not isinstance(output_type, type):
        raise TypeError(
            f"output_type 需要一个类型（dataclass 或 Pydantic BaseModel），收到 {output_type!r}"
        )

    if is_dataclass(output_type):
        return

    if _is_pydantic_model(output_type):
        return

    raise TypeError(
        f"output_type 需要 dataclass 或 Pydantic BaseModel 类型，收到 {output_type!r}"
    )


def _is_pydantic_model(tp: type) -> bool:
    """检查类型是否为 Pydantic BaseModel 子类。"""
    try:
        from pydantic import BaseModel
        return isinstance(tp, type) and issubclass(tp, BaseModel)
    except ImportError:
        return False


def _pydantic_to_response_format(
    model_type: type,
    *,
    strict: bool = False,
) -> dict[str, Any]:
    """将 Pydantic BaseModel 转为 response_format dict。"""
    from pydantic import TypeAdapter

    adapter = TypeAdapter(model_type)
    schema = adapter.json_schema()

    result: dict[str, Any] = {
        "type": "json_schema",
        "json_schema": {
            "name": model_type.__name__,
            "strict": strict,
            "schema": schema,
        },
    }
    return result


# ═══════════════════════════════════════════════════════════════
#  内部转换逻辑
# ═══════════════════════════════════════════════════════════════


def _resolve_annotations(cls: type) -> dict[str, Any]:
    """把 dataclass 的注解字符串解析成真实类型对象。

    背景：开启 PEP 563（`from __future__ import annotations`）后，
    dataclass 字段的注解在 ``fields(cls)`` 里以**字符串**形式保存，
    必须先还原为真实类型对象，否则 ``_type_to_schema`` 无法识别。

    实现：仅依赖标准库 ``typing.get_type_hints``，只解析定义在**模块顶层**
    的类型名（这是 dataclass 的推荐用法）。若解析失败（例如 dataclass
    被定义在函数内部、嵌套类型只存在于局部作用域），保留原始字符串注解，
    由 ``_type_to_schema`` 走兜底返回 ``{}``，不会中断调用。

    注意：不支持「函数内部定义的 dataclass + 引用仅存在于局部作用域的类型」
    这种写法 —— 请把 dataclass 提到模块顶层定义。
    """
    module_globals = getattr(sys.modules.get(cls.__module__, None), "__dict__", {})
    try:
        return get_type_hints(cls, globalns=module_globals, localns=None)
    except Exception:
        # 保留字符串注解，交给 _type_to_schema 兜底；不抛错以保证健壮性。
        return {}


def _dataclass_to_schema(cls: type, *, strict: bool = False) -> dict[str, Any]:
    """将 dataclass 转换为 JSON Schema object。"""
    properties: dict[str, Any] = {}
    required: list[str] = []

    # 解析字符串形式的类型注解（PEP 563 / `from __future__ import annotations`）。
    # fields(cls) 返回的 f.type 在推迟求值模式下是字符串，需要先还原为真实类型对象，
    # 否则 _type_to_schema 无法识别 str/int/Optional/嵌套 dataclass 等。
    resolved_hints = _resolve_annotations(cls)

    for f in fields(cls):
        raw_type = resolved_hints.get(f.name, f.type)
        prop = _type_to_schema(raw_type, strict=strict)

        # 从 metadata 中提取 description
        desc = f.metadata.get("description") if f.metadata else None
        if desc:
            prop["description"] = desc

        properties[f.name] = prop

        # 判断是否必填：有 default / default_factory 的字段不必填
        if f.default is MISSING and f.default_factory is MISSING:
            required.append(f.name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }

    if required:
        schema["required"] = required

    # strict 模式要求 additionalProperties: False
    if strict:
        schema["additionalProperties"] = False
        # strict 模式下所有字段必须 required，可选字段用 nullable 表示
        schema["required"] = list(properties.keys())

    return schema


def _is_union(origin: Any) -> bool:
    """检查 origin 是否为 Union 类型（兼容 typing.Union 和 types.UnionType）。"""
    return origin is Union or origin is types.UnionType


def _type_to_schema(tp: Any, *, strict: bool = False) -> dict[str, Any]:
    """将 Python 类型注解转换为 JSON Schema。"""
    origin = get_origin(tp)
    args = get_args(tp)

    # ── Optional[X] / X | None → Union[X, None] ──
    if _is_union(origin):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1 and type(None) in args:
            # Optional[X]
            inner = _type_to_schema(non_none[0], strict=strict)
            if strict:
                # strict 模式：用 type 数组
                base_type = inner.get("type")
                if isinstance(base_type, str):
                    inner["type"] = [base_type, "null"]
                else:
                    inner["nullable"] = True
            else:
                inner["nullable"] = True
            return inner
        # 非 Optional 的 Union → anyOf
        return {"anyOf": [_type_to_schema(a, strict=strict) for a in non_none]}

    # ── list[X] ──
    if origin is list:
        item_type = args[0] if args else Any
        if item_type is Any:
            return {"type": "array"}
        return {
            "type": "array",
            "items": _type_to_schema(item_type, strict=strict),
        }

    # ── dict[str, X] ──
    if origin is dict:
        val_type = args[1] if len(args) > 1 else Any
        result: dict[str, Any] = {"type": "object"}
        if val_type is not Any:
            result["additionalProperties"] = _type_to_schema(val_type, strict=strict)
        return result

    # ── 基本标量类型 ──
    _SCALAR_MAP: dict[type, str] = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
    }
    if tp in _SCALAR_MAP:
        return {"type": _SCALAR_MAP[tp]}

    # ── 嵌套 dataclass ──
    if isinstance(tp, type) and is_dataclass(tp):
        return _dataclass_to_schema(tp, strict=strict)

    # ── Any / 未知类型 → 空-schema（不约束）──
    if tp is Any:
        return {}

    # ── 兜底 ──
    return {}
