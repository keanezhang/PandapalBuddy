"""pandaren/llm/tests/test_capabilities.py — 端点能力矩阵测试（依据 llm-capabilities 测试设计文档）

覆盖：
  - L2 声明哲学：8 个「provider × endpoint」常量逐字段 Golden 值锁定（防"表与实现脱节"）
  - provider × endpoint 维度：同平台不同端点能力完全不同（防按 provider 聚合塌掉）
  - 全局不变式：frozen / provider×endpoint 唯一 / 平台命名 / 枚举字段在 Literal 类型内 /
    cache_control_type 一致性 / reasoning_control_values 与 budget 字段一致性

运行：python -m pytest pandaren/llm/tests/test_capabilities.py -q
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import get_args

import pytest

from pandaren.llm.capabilities import (
    DASHSCOPE_CHAT,
    DASHSCOPE_RESPONSES,
    DEEPSEEK_CHAT,
    OPENAI_CHAT,
    OPENAI_RESPONSES,
    VOLCENGINE_CHAT,
    VOLCENGINE_CONTEXT_API,
    VOLCENGINE_RESPONSES,
    EndpointCapabilities,
    EndpointKind,
    ExplicitCacheMode,
    ReasoningControlField,
)


ALL_ENDPOINTS = [
    DASHSCOPE_CHAT,
    VOLCENGINE_CHAT,
    VOLCENGINE_CONTEXT_API,
    OPENAI_CHAT,
    DEEPSEEK_CHAT,
    OPENAI_RESPONSES,
    VOLCENGINE_RESPONSES,
    DASHSCOPE_RESPONSES,
]

_ENDPOINT_IDS = [
    "dashscope_chat",
    "volcengine_chat",
    "volcengine_context_api",
    "openai_chat",
    "deepseek_chat",
    "openai_responses",
    "volcengine_responses",
    "dashscope_responses",
]


# ════════════════════════════════════════════════════════════════
# L2 声明哲学：常量表 Golden 值锁定（设计文档 §9 逐常量值表）
# ════════════════════════════════════════════════════════════════

_ENDPOINT_GOLDEN = [
    (DASHSCOPE_CHAT, {
        "provider": "dashscope",
        "endpoint": "chat_completions",
        "explicit_cache": "cache_control",
        "implicit_cache": True,
        "cached_tokens_field": "usage.prompt_tokens_details.cached_tokens",
        "cache_creation_field": "usage.prompt_tokens_details.cache_creation_input_tokens",
        "max_cache_breakpoints": 4,
        "min_cache_tokens": 1024,
        "cache_ttl_seconds": 300,
        "cache_control_type": "ephemeral",
        "cache_write_surcharge_percent": 125,
        "reasoning_control": "enable_thinking",
        "reasoning_control_values": ("true", "false"),
        "reasoning_budget_field": "thinking_budget",
        "returns_reasoning_content": True,
        "supports_parallel_tool_calls": True,
        "supports_tool_choice_required": True,
        "supports_tool_cache_control": False,
    }),
    (VOLCENGINE_CHAT, {
        "provider": "volcengine",
        "endpoint": "chat_completions",
        "explicit_cache": "none",
        "implicit_cache": True,
        "cached_tokens_field": "usage.prompt_tokens_details.cached_tokens",
        "cache_creation_field": None,
        "max_cache_breakpoints": 0,
        "min_cache_tokens": None,
        "cache_ttl_seconds": None,
        "cache_control_type": None,
        "cache_write_surcharge_percent": None,
        "reasoning_control": "thinking",
        "reasoning_control_values": ("disabled", "enabled", "auto"),
        "reasoning_budget_field": None,
        "returns_reasoning_content": True,
        "supports_parallel_tool_calls": True,
        "supports_tool_choice_required": True,
        "supports_tool_cache_control": False,
    }),
    (VOLCENGINE_CONTEXT_API, {
        "provider": "volcengine",
        "endpoint": "context_api",
        "explicit_cache": "context_id",
        "implicit_cache": False,
        "cached_tokens_field": "usage.prompt_tokens_details.cached_tokens",
        "cache_creation_field": "usage.prompt_tokens",
        "max_cache_breakpoints": 0,
        "min_cache_tokens": None,
        "cache_ttl_seconds": None,
        "cache_control_type": None,
        "cache_write_surcharge_percent": 100,
        "reasoning_control": "thinking",
        "reasoning_control_values": ("disabled", "enabled", "auto"),
        "reasoning_budget_field": None,
        "returns_reasoning_content": True,
        "supports_parallel_tool_calls": True,
        "supports_tool_choice_required": True,
        "supports_tool_cache_control": False,
    }),
    (OPENAI_CHAT, {
        "provider": "openai",
        "endpoint": "chat_completions",
        "explicit_cache": "none",
        "implicit_cache": True,
        "cached_tokens_field": "usage.prompt_tokens_details.cached_tokens",
        "cache_creation_field": None,
        "max_cache_breakpoints": 0,
        "min_cache_tokens": 1024,
        "cache_ttl_seconds": None,
        "cache_control_type": None,
        "cache_write_surcharge_percent": None,
        "reasoning_control": "reasoning",
        "reasoning_control_values": ("low", "medium", "high"),
        "reasoning_budget_field": None,
        "returns_reasoning_content": False,
        "supports_parallel_tool_calls": True,
        "supports_tool_choice_required": True,
        "supports_tool_cache_control": False,
    }),
    (DEEPSEEK_CHAT, {
        "provider": "deepseek",
        "endpoint": "chat_completions",
        "explicit_cache": "none",
        "implicit_cache": True,
        "cached_tokens_field": "usage.prompt_cache_hit_tokens",
        "cache_creation_field": "usage.prompt_cache_miss_tokens",
        "max_cache_breakpoints": 0,
        "min_cache_tokens": None,
        "cache_ttl_seconds": None,
        "cache_control_type": None,
        "cache_write_surcharge_percent": None,
        "reasoning_control": "none",
        "reasoning_control_values": (),
        "reasoning_budget_field": None,
        "returns_reasoning_content": True,
        "supports_parallel_tool_calls": True,
        "supports_tool_choice_required": True,
        "supports_tool_cache_control": False,
    }),
    (OPENAI_RESPONSES, {
        "provider": "openai",
        "endpoint": "responses_api",
        "explicit_cache": "responses_api",
        "implicit_cache": True,
        "cached_tokens_field": "usage.input_tokens_details.cached_tokens",
        "cache_creation_field": None,
        "max_cache_breakpoints": 0,
        "min_cache_tokens": 1024,
        "cache_ttl_seconds": None,
        "cache_control_type": None,
        "cache_write_surcharge_percent": None,
        "reasoning_control": "reasoning",
        "reasoning_control_values": ("low", "medium", "high"),
        "reasoning_budget_field": None,
        "returns_reasoning_content": False,
        "supports_parallel_tool_calls": True,
        "supports_tool_choice_required": True,
        "supports_tool_cache_control": False,
    }),
    (VOLCENGINE_RESPONSES, {
        "provider": "volcengine",
        "endpoint": "responses_api",
        "explicit_cache": "responses_api",
        "implicit_cache": True,
        "cached_tokens_field": "usage.input_tokens_details.cached_tokens",
        "cache_creation_field": None,
        "max_cache_breakpoints": 0,
        "min_cache_tokens": None,
        "cache_ttl_seconds": None,
        "cache_control_type": None,
        "cache_write_surcharge_percent": None,
        "reasoning_control": "thinking",
        "reasoning_control_values": ("disabled", "enabled", "auto"),
        "reasoning_budget_field": None,
        "returns_reasoning_content": True,
        "supports_parallel_tool_calls": True,
        "supports_tool_choice_required": True,
        "supports_tool_cache_control": False,
    }),
    (DASHSCOPE_RESPONSES, {
        "provider": "dashscope",
        "endpoint": "responses_api",
        "explicit_cache": "responses_api",
        "implicit_cache": True,
        "cached_tokens_field": "usage.input_tokens_details.cached_tokens",
        "cache_creation_field": "usage.input_tokens_details.cache_creation_input_tokens",
        "max_cache_breakpoints": 0,
        "min_cache_tokens": 1024,
        "cache_ttl_seconds": None,
        "cache_control_type": None,
        "cache_write_surcharge_percent": 125,
        "reasoning_control": "enable_thinking",
        "reasoning_control_values": ("true", "false"),
        "reasoning_budget_field": "thinking_budget",
        "returns_reasoning_content": True,
        "supports_parallel_tool_calls": True,
        "supports_tool_choice_required": True,
        "supports_tool_cache_control": False,
    }),
]


@pytest.mark.parametrize("caps, expected", _ENDPOINT_GOLDEN, ids=_ENDPOINT_IDS)
def test_endpoint_capability_golden(caps, expected):
    for field, value in expected.items():
        assert getattr(caps, field) == value, f"{caps.provider}/{caps.endpoint} 字段 {field} 与设计不符"


# ════════════════════════════════════════════════════════════════
# 全局不变式
# ════════════════════════════════════════════════════════════════

# inv-1 表规模：设计文档声明的 8 个「provider × endpoint」常量
def test_table_has_eight_constants():
    assert len(ALL_ENDPOINTS) == 8


# inv-2 全表为 EndpointCapabilities 实例（frozen dataclass）
def test_all_constants_are_endpoint_capabilities():
    for caps in ALL_ENDPOINTS:
        assert isinstance(caps, EndpointCapabilities)


# inv-3 provider × endpoint 唯一：常量身份不重复
def test_table_identity_unique():
    identities = [(c.provider, c.endpoint) for c in ALL_ENDPOINTS]
    assert len(identities) == len(set(identities))


# inv-4 命名约定：provider 一律用平台名（dashscope / volcengine / openai / deepseek）
def test_provider_names_use_platform():
    for caps in ALL_ENDPOINTS:
        assert caps.provider in {"dashscope", "volcengine", "openai", "deepseek"}


# inv-5 frozen：能力矩阵是静态协议事实，构造后不可改
@pytest.mark.parametrize("caps", ALL_ENDPOINTS, ids=_ENDPOINT_IDS)
def test_capabilities_frozen(caps):
    with pytest.raises(FrozenInstanceError):
        caps.provider = "mutated"


# inv-6 枚举字段值在各自 Literal 类型内（IDE 补全可见的全集）
@pytest.mark.parametrize("caps", ALL_ENDPOINTS, ids=_ENDPOINT_IDS)
def test_enum_fields_within_literal_types(caps):
    assert caps.endpoint in get_args(EndpointKind)
    assert caps.explicit_cache in get_args(ExplicitCacheMode)
    assert caps.reasoning_control in get_args(ReasoningControlField)


# inv-7 cache_control_type 仅对 cache_control 模式生效；其余模式必须为 None
def test_cache_control_type_only_for_cache_control_mode():
    for caps in ALL_ENDPOINTS:
        if caps.explicit_cache == "cache_control":
            assert caps.cache_control_type == "ephemeral"
        else:
            assert caps.cache_control_type is None


# inv-8 reasoning_control="none" 时取值表为空；否则非空
def test_reasoning_control_values_nonempty_when_controllable():
    for caps in ALL_ENDPOINTS:
        if caps.reasoning_control == "none":
            assert caps.reasoning_control_values == ()
        else:
            assert len(caps.reasoning_control_values) >= 2


# inv-9 reasoning_budget_field 仅 enable_thinking 模式有值（thinking_budget）
def test_reasoning_budget_field_only_for_enable_thinking():
    for caps in ALL_ENDPOINTS:
        if caps.reasoning_control == "enable_thinking":
            assert caps.reasoning_budget_field == "thinking_budget"
        else:
            assert caps.reasoning_budget_field is None


# Risk: 按 provider 聚合会塌掉 —— 同平台不同端点显式缓存机制互不相同
def test_volcengine_endpoints_differ_by_endpoint():
    assert VOLCENGINE_CHAT.explicit_cache == "none"
    assert VOLCENGINE_CONTEXT_API.explicit_cache == "context_id"
    assert VOLCENGINE_RESPONSES.explicit_cache == "responses_api"


def test_dashscope_endpoints_differ_by_endpoint():
    assert DASHSCOPE_CHAT.explicit_cache == "cache_control"
    assert DASHSCOPE_RESPONSES.explicit_cache == "responses_api"


# 端点覆盖：chat_completions / context_api / responses_api 三态齐备；
# messages 为 Anthropic 预留（EndpointKind 已声明但暂无常量落地）
def test_table_covers_three_endpoint_kinds():
    kinds = {c.endpoint for c in ALL_ENDPOINTS}
    assert kinds == {"chat_completions", "context_api", "responses_api"}
