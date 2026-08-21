"""pandaren/llm/tests/test_types.py — LLM 公共数据契约测试（依据 llm-types 测试设计文档）

覆盖：
  - ModelSettings：16 字段全默认 None / 字段构造往返 / == 比较 /
    include_usage 与 target_model 语义（纯承载，不参与 payload 构造）
  - LLMStreamChunk：全 None 默认 / 单增量语义（其余字段保持 None）/ 值相等
  - ToolCallDelta：必选字段集 / 文档化组装契约（id/name 取首次非空、arguments 串接）
  - TypedDict（UsageInfo / CompletionTokensDetails / PromptTokensDetails / LLMResponse）：
    必选/可选键集合 + 字段类型
  - FinishReason：Literal 取值穷举

运行：python -m pytest pandaren/llm/tests/test_types.py -q
"""

from __future__ import annotations

import dataclasses
from typing import get_args, get_type_hints

import pytest

from pandaren.llm.types import (
    CompletionTokensDetails,
    FinishReason,
    LLMResponse,
    LLMStreamChunk,
    ModelSettings,
    PromptTokensDetails,
    ToolCallDelta,
    UsageInfo,
)


# ════════════════════════════════════════════════════════════════
# ModelSettings
# ════════════════════════════════════════════════════════════════

# inv-1 全字段默认 None（不覆盖 provider 默认）
def test_model_settings_all_fields_default_none():
    s = ModelSettings()
    for f in dataclasses.fields(ModelSettings):
        assert getattr(s, f.name) is None


# inv-2 字段表锁定：设计文档 §2 声明的 16 个字段，不多不少
def test_model_settings_field_set():
    assert set(ModelSettings.__dataclass_fields__) == {
        "temperature", "max_tokens", "top_p", "frequency_penalty", "presence_penalty",
        "stop", "seed",
        "response_format", "tool_choice", "parallel_tool_calls",
        "include_usage",
        "reasoning",
        "target_model",
        "extra_body", "extra_headers", "extra_query",
    }


# 字段构造往返：每个字段按设计文档代表值构造后原样可读
@pytest.mark.parametrize("field, value", [
    ("temperature", 0.7),
    ("max_tokens", 512),
    ("top_p", 0.9),
    ("frequency_penalty", 0.5),
    ("presence_penalty", 0.2),
    ("stop", ["END", "STOP"]),
    ("seed", 42),
    ("response_format", {"type": "json_object"}),
    ("tool_choice", "auto"),
    ("tool_choice", {"type": "function", "function": {"name": "get_weather"}}),
    ("parallel_tool_calls", True),
    ("include_usage", True),
    ("reasoning", {"effort": "high"}),
    ("target_model", "dashscope"),
    ("extra_body", {"enable_thinking": True, "thinking_budget": 4096}),
    ("extra_headers", {"X-DashScope-Plugin": "p1"}),
    ("extra_query", {"api-version": "2024-02-01"}),
])
def test_model_settings_field_roundtrip(field, value):
    s = ModelSettings(**{field: value})
    assert getattr(s, field) == value


# inv-3 值相等（dataclass eq=True）：同值相等、异值不等
def test_model_settings_equality_same_values():
    a = ModelSettings(temperature=0.7, max_tokens=512)
    b = ModelSettings(temperature=0.7, max_tokens=512)
    assert a == b


def test_model_settings_inequality_diff_value():
    assert ModelSettings(temperature=0.7) != ModelSettings(temperature=0.9)


# include_usage 语义：None/False 等价（都不注入），True 注入
def test_include_usage_none_and_false_both_disabled():
    assert ModelSettings().include_usage is None
    assert ModelSettings(include_usage=False).include_usage is False
    assert not ModelSettings(include_usage=False).include_usage


def test_include_usage_true_enabled():
    assert ModelSettings(include_usage=True).include_usage is True


# inv-4 reasoning 仅承载 OpenAI 规范嵌套对象；None = 不传递
def test_reasoning_carries_nested_object_only():
    s = ModelSettings(reasoning={"effort": "low"})
    assert s.reasoning == {"effort": "low"}


# inv-5 target_model 仅承载路由键（由 LLMRouter 消费，不写入 HTTP 请求体）
def test_target_model_carries_router_key():
    assert ModelSettings(target_model="volcengine").target_model == "volcengine"


# ════════════════════════════════════════════════════════════════
# LLMStreamChunk
# ════════════════════════════════════════════════════════════════

def test_chunk_all_fields_default_none():
    c = LLMStreamChunk()
    for f in dataclasses.fields(LLMStreamChunk):
        assert getattr(c, f.name) is None


def test_chunk_field_set():
    assert set(LLMStreamChunk.__dataclass_fields__) == {
        "delta_content", "delta_reasoning_content", "refusal_delta",
        "tool_call_delta", "finish_reason", "usage",
    }


# inv-6 单增量语义：一个 chunk 承载一种语义，其余字段保持 None
@pytest.mark.parametrize("field, value", [
    ("delta_content", "你好"),
    ("delta_reasoning_content", "思考中"),
    ("refusal_delta", "抱歉，无法回答"),
    ("tool_call_delta", ToolCallDelta(
        index=0, id="call_1", name="get_weather", arguments_delta='{"city": "beijing"}'
    )),
    ("finish_reason", "stop"),
    ("usage", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
])
def test_chunk_single_semantics_others_none(field, value):
    c = LLMStreamChunk(**{field: value})
    assert getattr(c, field) == value
    for f in dataclasses.fields(LLMStreamChunk):
        if f.name != field:
            assert getattr(c, f.name) is None


def test_chunk_equality():
    assert LLMStreamChunk(delta_content="x") == LLMStreamChunk(delta_content="x")
    assert LLMStreamChunk(delta_content="x") != LLMStreamChunk(delta_content="y")


# ════════════════════════════════════════════════════════════════
# ToolCallDelta
# ════════════════════════════════════════════════════════════════

def test_tool_call_delta_required_keys():
    assert ToolCallDelta.__required_keys__ == frozenset({"index", "id", "name", "arguments_delta"})


def test_tool_call_delta_no_optional_keys():
    assert ToolCallDelta.__optional_keys__ == frozenset()


# Risk: 消费方拼错组装规则 → 用 types.py docstring 的文档化示例做契约测试
# （id/name 取首次非空，arguments_delta 按 index 串接）
def test_tool_call_delta_assembly_contract():
    deltas = [
        ToolCallDelta(index=0, id="call_1", name="get_weather", arguments_delta='{"city": "'),
        ToolCallDelta(index=0, id="", name="", arguments_delta='beijing"}'),
        ToolCallDelta(index=1, id="call_2", name="get_time", arguments_delta="{}"),
    ]
    acc: dict[int, dict] = {}
    for d in deltas:
        slot = acc.setdefault(d["index"], {"id": "", "name": "", "arguments": ""})
        if d["id"]:
            slot["id"] = d["id"]
        if d["name"]:
            slot["name"] = d["name"]
        slot["arguments"] += d["arguments_delta"]
    assert acc[0] == {"id": "call_1", "name": "get_weather", "arguments": '{"city": "beijing"}'}
    assert acc[1] == {"id": "call_2", "name": "get_time", "arguments": "{}"}


# ════════════════════════════════════════════════════════════════
# TypedDict 键集合与字段类型
# ════════════════════════════════════════════════════════════════

# [known-gap] 期望：计费三件套必选 + 两个 details 可选（NotRequired）
# 现状：types.py 顶部 `from __future__ import annotations` 把注解字符串化为 ForwardRef，
#       TypedDict 元类无法在类创建期识别 NotRequired，运行时 __required_keys__ 将全部 5 键
#       判为必选、__optional_keys__ 为空。静态类型检查器（mypy/pyright 直接解析源码）仍正确
#       识别 NotRequired，仅运行时内省元数据与设计契约不符。
@pytest.mark.xfail(
    strict=True,
    reason="NotRequired 被 future-annotations 字符串化，运行时键集合元数据视为必选",
)
def test_usage_info_key_split_contract():
    assert UsageInfo.__required_keys__ == frozenset(
        {"prompt_tokens", "completion_tokens", "total_tokens"}
    )
    assert UsageInfo.__optional_keys__ == frozenset(
        {"completion_tokens_details", "prompt_tokens_details"}
    )


def test_usage_info_field_types():
    hints = get_type_hints(UsageInfo)
    assert hints["prompt_tokens"] is int
    assert hints["completion_tokens"] is int
    assert hints["total_tokens"] is int
    assert hints["completion_tokens_details"] is CompletionTokensDetails
    assert hints["prompt_tokens_details"] is PromptTokensDetails


# [known-gap] 期望：6 个必选 + 3 个可选（NotRequired）；gap 原因同 test_usage_info_key_split_contract
@pytest.mark.xfail(
    strict=True,
    reason="NotRequired 被 future-annotations 字符串化，运行时键集合元数据视为必选",
)
def test_llm_response_key_split_contract():
    assert LLMResponse.__required_keys__ == frozenset(
        {"content", "finish_reason", "usage", "id", "model", "created"}
    )
    assert LLMResponse.__optional_keys__ == frozenset(
        {"tool_calls", "reasoning_content", "refusal"}
    )


def test_llm_response_field_types():
    hints = get_type_hints(LLMResponse)
    assert hints["content"] == str | None        # tool_calls 时可为 null
    assert hints["finish_reason"] == str | None
    assert hints["usage"] is UsageInfo
    assert hints["id"] is str
    assert hints["model"] is str
    assert hints["created"] is int


def test_completion_tokens_details_all_optional():
    assert CompletionTokensDetails.__required_keys__ == frozenset()
    assert CompletionTokensDetails.__optional_keys__ == frozenset(
        {"reasoning_tokens", "output_tokens", "text_tokens"}
    )


def test_prompt_tokens_details_all_optional():
    assert PromptTokensDetails.__required_keys__ == frozenset()
    assert PromptTokensDetails.__optional_keys__ == frozenset({
        "cached_tokens", "text_tokens", "cache_creation_input_tokens",
        "cache_type", "cache_creation",
    })


# ════════════════════════════════════════════════════════════════
# FinishReason
# ════════════════════════════════════════════════════════════════

def test_finish_reason_literal_values():
    assert get_args(FinishReason) == (
        "stop", "tool_calls", "length", "content_filter", "function_call"
    )
