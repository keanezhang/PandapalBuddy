"""
Pandaren Agent SDK · ResponsesAPIClient Mock 测试

覆盖范围
--------
  ResponsesAPIClient（responses_client.py）
    - 构造参数校验（api_key/model_name/base_url 不可空）
    - 工厂方法：for_openai_responses / for_volcengine_responses / for_dashscope_responses
    - capabilities / model_name / response_id / last_messages_len 只读属性
    - _extract_instructions：str / list[text] / None
    - _convert_messages_to_input：user / assistant(含tool_calls) / tool / system
    - _convert_tools：function 类型扁平化 / 非 function 原样透传
    - _build_full_request：含 system / 无 system / tools / caching
    - _build_incremental_request：增量 input + previous_response_id
    - _detect_increment：messages[offset:]
    - _apply_model_settings：全字段写入 + max_tokens → max_output_tokens
    - _merge_settings：base / override 合并语义
    - _invalidate：清空 response_id + last_messages_len
    - _compute_tools_hash：None / 空列表 / 有内容
    - tools 变化检测 → 冷启动
    - messages 缩短 → 冷启动
    - _convert_output_to_response：纯文本 / function_call / reasoning
    - _status_to_finish_reason：completed→stop / incomplete→length / cancelled→stop / failed→stop
    - _build_usage_info：基础 / details / L4 caps 回填
    - _dig 工具函数
    - _parse_sse_line：data json / [DONE] / 非 data / JSON 错误
    - _classify_http_error：401 / 400 / 408 / 429 / 5xx / 其他
    - _is_response_id_expired_error：404 / 400+关键词 / 非400 / 非5xx-404
    - call()：MockTransport 全量/增量/HTTP 错误/超时/response_id 过期降级/JSON 解析失败
    - stream_response()：MockTransport 文本/tool_call/reasoning/response.completed/降级/超时
    - aclose() / async context manager

运行方式
--------
  cd pandaren/llm/tests && python test_responses_client_mock.py
  python test_responses_client_mock.py --section constructor
  python test_responses_client_mock.py --section factory
  python test_responses_client_mock.py --section request_build
  python test_responses_client_mock.py --section state
  python test_responses_client_mock.py --section response_convert
  python test_responses_client_mock.py --section sse_error
  python test_responses_client_mock.py --section call
  python test_responses_client_mock.py --section stream
  python test_responses_client_mock.py --section lifecycle
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

if sys.platform == "win32" and "pytest" not in sys.modules:  # pytest 下交给 capture，避免拆台
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

# ═══ SDK 导入 ═══
import httpx

from pandaren.llm import (
    LLMError,
    LLMAuthError,
    LLMRequestError,
    LLMRateLimitError,
    LLMServerError,
    LLMNetworkError,
    LLMTimeoutError,
    LLMResponseError,
    ModelSettings,
    UsageInfo,
    CompletionTokensDetails,
    PromptTokensDetails,
    LLMResponse,
    LLMStreamChunk,
    ToolCallDelta,
    ResponsesAPIClient,
)
from pandaren.llm.responses_client import _dig, _SseDone, _SSE_DONE
from pandaren.llm.capabilities import (
    EndpointCapabilities,
    OPENAI_RESPONSES,
    VOLCENGINE_RESPONSES,
    DASHSCOPE_RESPONSES,
)


# ════════════════════════════════════════════════════
#  轻量测试框架
# ════════════════════════════════════════════════════

class TestResult:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors: list[str] = []

    def ok(self, name: str):
        self.passed += 1
        print(f"   ✅ {name}")

    def fail(self, name: str, detail: str = ""):
        self.failed += 1
        msg = f"   ❌ {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        self.errors.append(msg)

    def summary(self, section: str = ""):
        total = self.passed + self.failed
        label = f" [{section}]" if section else ""
        print(f"\n📊{label} 通过={self.passed} / 失败={self.failed} / 总计={total}")
        if self.errors:
            print("   失败列表:")
            for e in self.errors:
                print(f"     {e}")
        return self.failed == 0


result = TestResult()


def assert_true(condition: bool, name: str, detail: str = ""):
    if condition:
        result.ok(name)
    else:
        result.fail(name, detail or "条件为 False")


def assert_raises(exc_type, name: str, detail: str = ""):
    def decorator(fn):
        try:
            fn()
            result.fail(name, f"未抛出 {exc_type.__name__}" + (f": {detail}" if detail else ""))
        except exc_type:
            result.ok(name)
        except Exception as e:
            result.fail(name, f"抛出了 {type(e).__name__}({e}) 而非 {exc_type.__name__}")
    return decorator


def async_run(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


# ════════════════════════════════════════════════════
#  工厂方法 — Responses API 响应构造
# ════════════════════════════════════════════════════

def _make_responses_api_response(
    content: str = "你好",
    status: str = "completed",
    model: str = "gpt-4o",
    resp_id: str = "resp_mock",
    tool_calls: list[dict] | None = None,
    reasoning_content: str | None = None,
    usage: dict | None = None,
) -> dict[str, Any]:
    """构造 Responses API 非流式响应 JSON。"""
    output: list[dict[str, Any]] = []

    # message output
    message_item: dict[str, Any] = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": content}],
    }
    output.append(message_item)

    # function_call output
    if tool_calls:
        for tc in tool_calls:
            output.append({
                "type": "function_call",
                "id": tc.get("id", "fc_mock"),
                "call_id": tc.get("call_id", tc.get("id", "call_mock")),
                "name": tc.get("name", ""),
                "arguments": tc.get("arguments", "{}"),
            })

    # reasoning output
    if reasoning_content:
        output.append({
            "type": "reasoning",
            "content": [{"type": "text", "text": reasoning_content}],
        })

    if usage is None:
        usage = {
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "input_tokens_details": {"cached_tokens": 80},
            "output_tokens_details": {"reasoning_tokens": 10},
        }

    return {
        "id": resp_id,
        "object": "response",
        "created_at": 1234567890,
        "model": model,
        "status": status,
        "output": output,
        "usage": usage,
    }


def _make_responses_sse_event(event_type: str, data: dict[str, Any] | None = None) -> str:
    """构造单个 Responses API SSE 事件行。"""
    payload = data or {}
    payload["type"] = event_type
    return f"data: {json.dumps(payload, ensure_ascii=False)}"


def _make_responses_sse_stream(
    text_parts: list[str] | None = None,
    tool_calls: list[dict] | None = None,
    reasoning_parts: list[str] | None = None,
    finish_status: str = "completed",
    usage: dict | None = None,
    resp_id: str = "resp_stream_mock",
) -> str:
    """构造 Responses API SSE 流式文本（多行 data: ... 格式）。"""
    lines = []

    # response.created
    lines.append(_make_responses_sse_event("response.created", {
        "response": {"id": resp_id, "status": "in_progress"},
    }))

    # 文本增量
    for part in (text_parts or []):
        lines.append(_make_responses_sse_event("response.output_text.delta", {"delta": part}))

    # reasoning 增量
    for part in (reasoning_parts or []):
        lines.append(_make_responses_sse_event("response.reasoning.delta", {"delta": part}))

    # tool_call 事件
    for i, tc in enumerate(tool_calls or []):
        # output_item.added
        lines.append(_make_responses_sse_event("response.output_item.added", {
            "item": {
                "type": "function_call",
                "id": tc.get("id", f"fc_{i}"),
                "call_id": tc.get("call_id", tc.get("id", f"call_{i}")),
                "name": tc.get("name", f"func_{i}"),
            },
            "output_index": i,
        }))
        # arguments delta
        for arg_part in tc.get("argument_parts", ["{}"]):
            lines.append(_make_responses_sse_event("response.function_call_arguments.delta", {
                "delta": arg_part,
                "item_id": tc.get("id", f"fc_{i}"),
                "output_index": i,
            }))
        # arguments done
        lines.append(_make_responses_sse_event("response.function_call_arguments.done", {
            "item_id": tc.get("id", f"fc_{i}"),
            "output_index": i,
        }))

    # response.completed
    completed_data: dict[str, Any] = {
        "response": {
            "id": resp_id,
            "status": finish_status,
            "model": "gpt-4o",
        },
    }
    if usage:
        completed_data["response"]["usage"] = usage
    lines.append(_make_responses_sse_event("response.completed", completed_data))

    lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


def _make_mock_client(
    response_json: dict | None = None,
    status_code: int = 200,
    sse_text: str | None = None,
    raise_on_request: Exception | None = None,
    **constructor_kwargs,
) -> ResponsesAPIClient:
    """构造一个注入了 MockTransport 的 ResponsesAPIClient。"""
    defaults = {
        "api_key": "mock-key",
        "model_name": "gpt-4o",
        "base_url": "https://mock.api/v1",
        "timeout": 5.0,
    }
    defaults.update(constructor_kwargs)
    client = ResponsesAPIClient(**defaults)

    if raise_on_request:
        def _handler(request: httpx.Request) -> httpx.Response:
            raise raise_on_request
        transport = httpx.MockTransport(_handler)
    elif sse_text is not None:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code,
                content=sse_text.encode("utf-8"),
                headers={"content-type": "text/event-stream"},
            )
        transport = httpx.MockTransport(_handler)
    elif response_json is not None:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, json=response_json)
        transport = httpx.MockTransport(_handler)
    else:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, json=_make_responses_api_response())
        transport = httpx.MockTransport(_handler)

    async_run(client._http_client.aclose())
    client._http_client = httpx.AsyncClient(transport=transport, timeout=5.0)
    return client


# ════════════════════════════════════════════════════
#  1. 构造 — 参数校验
# ════════════════════════════════════════════════════

def test_construction():
    """1. 构造 — 参数校验"""
    print("\n" + "═" * 60)
    print("1.  构造 — 参数校验")
    print("═" * 60)

    # 1.1 正常构造
    c = ResponsesAPIClient(
        api_key="sk-xxx", model_name="gpt-4o",
        base_url="https://api.openai.com/v1",
    )
    assert_true(c.model_name == "gpt-4o", "1.1 model_name 正确")
    assert_true(c._base_url == "https://api.openai.com/v1", "1.1 base_url 正确")
    async_run(c.aclose())

    # 1.2 api_key 为空 → ValueError
    @assert_raises(ValueError, "1.2 api_key 为空 → ValueError")
    def _():
        ResponsesAPIClient(api_key="", model_name="m", base_url="https://api/v1")

    # 1.3 model_name 为空 → ValueError
    @assert_raises(ValueError, "1.3 model_name 为空 → ValueError")
    def _():
        ResponsesAPIClient(api_key="k", model_name="", base_url="https://api/v1")

    # 1.4 base_url 为空 → ValueError
    @assert_raises(ValueError, "1.4 base_url 为空 → ValueError")
    def _():
        ResponsesAPIClient(api_key="k", model_name="m", base_url="")

    # 1.5 base_url 尾部 / 被去除
    c2 = ResponsesAPIClient(api_key="k", model_name="m", base_url="https://api/v1/")
    assert_true(c2._base_url == "https://api/v1", "1.5 base_url 尾部 / 被去除")
    async_run(c2.aclose())

    # 1.6 use_caching 默认 True
    c3 = ResponsesAPIClient(api_key="k", model_name="m", base_url="https://api/v1")
    assert_true(c3._use_caching is True, "1.6 默认 use_caching=True")
    async_run(c3.aclose())

    # 1.7 initial_response_id / initial_messages_len
    c4 = ResponsesAPIClient(
        api_key="k", model_name="m", base_url="https://api/v1",
        initial_response_id="resp_init", initial_messages_len=5,
    )
    assert_true(c4.response_id == "resp_init", "1.7 initial_response_id 存储")
    assert_true(c4.last_messages_len == 5, "1.7 initial_messages_len 存储")
    async_run(c4.aclose())

    # 1.8 model_name 只读
    c5 = ResponsesAPIClient(api_key="k", model_name="gpt-4o", base_url="https://api/v1")
    assert_true(c5.model_name == "gpt-4o", "1.8 model_name property 可读")
    @assert_raises(AttributeError, "1.8 model_name 不可写")
    def _():
        c5.model_name = "hacked"
    async_run(c5.aclose())

    # 1.9 response_id 只读
    c6 = ResponsesAPIClient(api_key="k", model_name="m", base_url="https://api/v1")
    @assert_raises(AttributeError, "1.9 response_id 不可写")
    def _():
        c6.response_id = "hacked"
    async_run(c6.aclose())

    # 1.10 last_messages_len 只读
    c7 = ResponsesAPIClient(api_key="k", model_name="m", base_url="https://api/v1")
    @assert_raises(AttributeError, "1.10 last_messages_len 不可写")
    def _():
        c7.last_messages_len = 999
    async_run(c7.aclose())


# ════════════════════════════════════════════════════
#  2. 工厂方法 & capabilities
# ════════════════════════════════════════════════════

def test_factory_and_capabilities():
    """2. 工厂方法 & capabilities"""
    print("\n" + "═" * 60)
    print("2.  工厂方法 & capabilities")
    print("═" * 60)

    # 2.1 for_openai_responses 绑定 OPENAI_RESPONSES
    c1 = ResponsesAPIClient.for_openai_responses(api_key="k")
    assert_true(c1.capabilities is OPENAI_RESPONSES, "2.1 for_openai_responses 绑定 OPENAI_RESPONSES")
    assert_true(c1.model_name == "gpt-4o", "2.1 默认 model_name=gpt-4o")
    assert_true("openai.com" in c1._base_url, "2.1 默认 base_url 含 openai.com")
    async_run(c1.aclose())

    # 2.2 for_openai_responses 自定义 model_name
    c2 = ResponsesAPIClient.for_openai_responses(api_key="k", model_name="o3-mini")
    assert_true(c2.model_name == "o3-mini", "2.2 自定义 model_name")
    async_run(c2.aclose())

    # 2.3 for_volcengine_responses 绑定 VOLCENGINE_RESPONSES
    c3 = ResponsesAPIClient.for_volcengine_responses(api_key="k", model_name="doubao-seed")
    assert_true(c3.capabilities is VOLCENGINE_RESPONSES, "2.3 for_volcengine_responses 绑定 VOLCENGINE_RESPONSES")
    assert_true("volces.com" in c3._base_url, "2.3 默认 base_url 含 volces.com")
    async_run(c3.aclose())

    # 2.4 for_dashscope_responses 绑定 DASHSCOPE_RESPONSES
    c4 = ResponsesAPIClient.for_dashscope_responses(api_key="k", model_name="qwen-plus")
    assert_true(c4.capabilities is DASHSCOPE_RESPONSES, "2.4 for_dashscope_responses 绑定 DASHSCOPE_RESPONSES")
    assert_true("dashscope" in c4._base_url, "2.4 默认 base_url 含 dashscope")
    async_run(c4.aclose())

    # 2.5 通用构造 capabilities=None
    c5 = ResponsesAPIClient(api_key="k", model_name="m", base_url="https://api/v1")
    assert_true(c5.capabilities is None, "2.5 未注入 capabilities → None")
    async_run(c5.aclose())

    # 2.6 capabilities 只读
    c6 = ResponsesAPIClient.for_openai_responses(api_key="k")
    @assert_raises(AttributeError, "2.6 capabilities 不可写")
    def _():
        c6.capabilities = None
    async_run(c6.aclose())


# ════════════════════════════════════════════════════
#  3. 请求构建（纯函数）
# ════════════════════════════════════════════════════

def test_request_build():
    """3. 请求构建"""
    print("\n" + "═" * 60)
    print("3.  请求构建（纯函数）")
    print("═" * 60)

    # 3.1 _extract_instructions — content: str
    msg_str = {"role": "system", "content": "你是助手"}
    assert_true(
        ResponsesAPIClient._extract_instructions(msg_str) == "你是助手",
        "3.1 _extract_instructions — content: str",
    )

    # 3.2 _extract_instructions — content: list[text]
    msg_list = {"role": "system", "content": [
        {"type": "text", "text": "第一段"},
        {"type": "text", "text": "第二段"},
    ]}
    result_text = ResponsesAPIClient._extract_instructions(msg_list)
    assert_true(
        result_text == "第一段\n第二段",
        "3.2 _extract_instructions — content: list[text]",
    )

    # 3.3 _extract_instructions — content: None
    msg_none = {"role": "system", "content": None}
    assert_true(
        ResponsesAPIClient._extract_instructions(msg_none) is None,
        "3.3 _extract_instructions — content: None",
    )

    # 3.4 _convert_messages_to_input — user
    user_msg = {"role": "user", "content": "hello"}
    converted = ResponsesAPIClient._convert_messages_to_input([user_msg])
    assert_true(converted[0] == {"role": "user", "content": "hello"}, "3.4 _convert user")

    # 3.5 _convert_messages_to_input — assistant with content
    asst_msg = {"role": "assistant", "content": "world"}
    converted = ResponsesAPIClient._convert_messages_to_input([asst_msg])
    assert_true(converted[0] == {"role": "assistant", "content": "world"}, "3.5 _convert assistant content")

    # 3.6 _convert_messages_to_input — assistant with tool_calls
    asst_tc_msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"BJ"}'}},
        ],
    }
    converted = ResponsesAPIClient._convert_messages_to_input([asst_tc_msg])
    assert_true(converted[0]["type"] == "function_call", "3.6 _convert assistant tool_calls → function_call")
    assert_true(converted[0]["call_id"] == "call_1", "3.6 call_id 正确")
    assert_true(converted[0]["name"] == "get_weather", "3.6 name 正确")

    # 3.7 _convert_messages_to_input — tool result
    tool_msg = {"role": "tool", "content": "晴", "tool_call_id": "call_1"}
    converted = ResponsesAPIClient._convert_messages_to_input([tool_msg])
    assert_true(converted[0]["type"] == "function_call_output", "3.7 _convert tool → function_call_output")
    assert_true(converted[0]["call_id"] == "call_1", "3.7 call_id 正确")
    assert_true(converted[0]["output"] == "晴", "3.7 output 正确")

    # 3.8 _convert_messages_to_input — system (非首条)
    sys_msg = {"role": "system", "content": "注意"}
    converted = ResponsesAPIClient._convert_messages_to_input([sys_msg])
    assert_true(converted[0]["role"] == "user", "3.8 _convert system → user role")
    assert_true("[System]" in converted[0]["content"], "3.8 content 含 [System] 前缀")

    # 3.9 _convert_tools — function 类型扁平化
    chat_tools = [
        {"type": "function", "function": {"name": "get_weather", "description": "天气", "parameters": {"type": "object"}}},
    ]
    converted_tools = ResponsesAPIClient._convert_tools(chat_tools)
    assert_true(converted_tools[0]["type"] == "function", "3.9 type=function 保留")
    assert_true(converted_tools[0]["name"] == "get_weather", "3.9 name 扁平化到顶层")
    assert_true("function" not in converted_tools[0], "3.9 function 嵌套被移除")

    # 3.10 _convert_tools — 非 function 原样透传
    custom_tool = {"type": "web_search", "query": "weather"}
    converted_tools = ResponsesAPIClient._convert_tools([custom_tool])
    assert_true(converted_tools[0] == custom_tool, "3.10 非 function 原样透传")

    # 3.11 _convert_tools — strict 字段
    strict_tools = [
        {"type": "function", "function": {"name": "f", "strict": True}},
    ]
    converted_tools = ResponsesAPIClient._convert_tools(strict_tools)
    assert_true(converted_tools[0].get("strict") is True, "3.11 strict 字段透传")

    # 3.12 _build_full_request — 完整体
    c = ResponsesAPIClient(api_key="k", model_name="gpt-4o", base_url="https://api/v1")
    messages = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "hello"},
    ]
    tools = [{"type": "function", "function": {"name": "f"}}]
    body = c._build_full_request(messages, tools, None)
    assert_true(body["model"] == "gpt-4o", "3.12 body 含 model")
    assert_true(body["instructions"] == "你是助手", "3.12 body 含 instructions")
    assert_true(len(body["input"]) == 1, "3.12 input 只有 user 消息")
    assert_true(body["input"][0]["role"] == "user", "3.12 input[0] 是 user")
    assert_true(len(body["tools"]) == 1, "3.12 tools 被转换")
    assert_true(body.get("caching") == {"type": "enabled"}, "3.12 caching 默认开启")
    async_run(c.aclose())

    # 3.13 _build_full_request — 只有 user msg（无 system）
    # _build_full_request 始终取 messages[0] 作为 instructions，
    # 且 len(messages)=1 时不生成 input
    c2 = ResponsesAPIClient(api_key="k", model_name="m", base_url="https://api/v1")
    messages_no_sys = [{"role": "user", "content": "hi"}]
    body2 = c2._build_full_request(messages_no_sys, None, None)
    assert_true(body2["instructions"] == "hi", "3.13 messages[0] content 作为 instructions")
    assert_true("input" not in body2, "3.13 只有 1 条消息 → 无 input")
    assert_true("tools" not in body2, "3.13 无 tools → 无 tools 字段")
    async_run(c2.aclose())

    # 3.14 _build_full_request — use_caching=False
    c3 = ResponsesAPIClient(api_key="k", model_name="m", base_url="https://api/v1", use_caching=False)
    body3 = c3._build_full_request([{"role": "user", "content": "hi"}], None, None)
    assert_true("caching" not in body3, "3.14 use_caching=False → 无 caching")
    async_run(c3.aclose())

    # 3.15 _build_incremental_request — 含 previous_response_id
    # initial_messages_len=2 表示上次 call 有 2 条 messages，
    # 所以增量 = messages[2:]
    c4 = ResponsesAPIClient(
        api_key="k", model_name="m", base_url="https://api/v1",
        initial_response_id="resp_prev", initial_messages_len=2,
    )
    messages_inc = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
        {"role": "user", "content": "world"},  # 增量
    ]
    body4 = c4._build_incremental_request(messages_inc, None, None)
    assert_true(body4["previous_response_id"] == "resp_prev", "3.15 含 previous_response_id")
    assert_true(len(body4["input"]) == 1, "3.15 增量 input 只有 1 条")
    assert_true(body4["input"][0]["content"] == "world", "3.15 增量内容正确")
    async_run(c4.aclose())

    # 3.16 _detect_increment
    c5 = ResponsesAPIClient(api_key="k", model_name="m", base_url="https://api/v1", initial_messages_len=2)
    msgs = [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}, {"role": "user", "content": "c"}]
    inc = c5._detect_increment(msgs)
    assert_true(len(inc) == 1, "3.16 _detect_increment 返回增量")
    assert_true(inc[0]["content"] == "c", "3.16 增量内容正确")
    async_run(c5.aclose())

    # 3.17 _apply_model_settings — 全字段
    body17: dict[str, Any] = {}
    settings = ModelSettings(
        temperature=0.7,
        max_tokens=100,
        top_p=0.9,
        reasoning={"effort": "high"},
        tool_choice="auto",
        parallel_tool_calls=True,
        extra_body={"custom_key": "custom_val"},
    )
    ResponsesAPIClient._apply_model_settings(body17, settings)
    assert_true(body17["temperature"] == 0.7, "3.17 temperature 写入")
    assert_true(body17["max_output_tokens"] == 100, "3.17 max_tokens → max_output_tokens")
    assert_true(body17["top_p"] == 0.9, "3.17 top_p 写入")
    assert_true(body17["reasoning"] == {"effort": "high"}, "3.17 reasoning 写入")
    assert_true(body17["tool_choice"] == "auto", "3.17 tool_choice 写入")
    assert_true(body17["parallel_tool_calls"] is True, "3.17 parallel_tool_calls 写入")
    assert_true(body17["custom_key"] == "custom_val", "3.17 extra_body 展开")

    # 3.18 _apply_model_settings — None
    body18: dict[str, Any] = {}
    ResponsesAPIClient._apply_model_settings(body18, None)
    assert_true(len(body18) == 0, "3.18 settings=None → body 不变")

    # 3.19 _merge_settings — base / override 合并
    base = ModelSettings(temperature=0.5, max_tokens=100)
    override = ModelSettings(max_tokens=200)
    merged = ResponsesAPIClient._merge_settings(base, override)
    assert_true(merged.temperature == 0.5, "3.19 temperature 保留 base")
    assert_true(merged.max_tokens == 200, "3.19 max_tokens 被 override 覆盖")

    # 3.20 _merge_settings — 两者都 None
    assert_true(ResponsesAPIClient._merge_settings(None, None) is None, "3.20 两者都 None → None")

    # 3.21 _merge_settings — base None
    ov = ModelSettings(temperature=0.3)
    assert_true(ResponsesAPIClient._merge_settings(None, ov) is ov, "3.21 base=None → override")

    # 3.22 _merge_settings — override None
    bs = ModelSettings(temperature=0.3)
    assert_true(ResponsesAPIClient._merge_settings(bs, None) is bs, "3.22 override=None → base")


# ════════════════════════════════════════════════════
#  4. 状态管理
# ════════════════════════════════════════════════════

def test_state_management():
    """4. 状态管理"""
    print("\n" + "═" * 60)
    print("4.  状态管理")
    print("═" * 60)

    # 4.1 _invalidate 清空 response_id 和 last_messages_len
    c = ResponsesAPIClient(
        api_key="k", model_name="m", base_url="https://api/v1",
        initial_response_id="resp_abc", initial_messages_len=5,
    )
    c._invalidate("test")
    assert_true(c._previous_response_id is None, "4.1 _invalidate 清空 response_id")
    assert_true(c._last_messages_len == 0, "4.1 _invalidate 清空 last_messages_len")
    async_run(c.aclose())

    # 4.2 _compute_tools_hash — None
    assert_true(ResponsesAPIClient._compute_tools_hash(None) is None, "4.2 None → None")

    # 4.3 _compute_tools_hash — 空列表
    assert_true(ResponsesAPIClient._compute_tools_hash([]) is None, "4.3 空列表 → None")

    # 4.4 _compute_tools_hash — 有内容 → 16 字符 hex
    tools = [{"type": "function", "function": {"name": "f"}}]
    h = ResponsesAPIClient._compute_tools_hash(tools)
    assert_true(h is not None and len(h) == 16, "4.4 返回 16 字符 hex")
    # 同样内容 → 同样 hash
    h2 = ResponsesAPIClient._compute_tools_hash(tools)
    assert_true(h == h2, "4.4 相同内容 → 相同 hash")

    # 4.5 _compute_tools_hash — 不同内容 → 不同 hash
    tools2 = [{"type": "function", "function": {"name": "g"}}]
    h3 = ResponsesAPIClient._compute_tools_hash(tools2)
    assert_true(h != h3, "4.5 不同内容 → 不同 hash")

    # 4.6 tools 变化检测 → 冷启动
    c2 = _make_mock_client()
    tools_a = [{"type": "function", "function": {"name": "f1"}}]
    tools_b = [{"type": "function", "function": {"name": "f2"}}]
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    # 第一次 call，设置 tools_hash
    async_run(c2.call(messages, tools=tools_a))
    assert_true(c2._previous_response_id is not None, "4.6 第一次 call 后有 response_id")
    # 第二次 call，tools 变化
    async_run(c2.call(messages, tools=tools_b))
    # 冷启动后 response_id 被更新（新的 response）
    assert_true(c2._previous_response_id is not None, "4.6 tools 变化后仍成功（冷启动重走全量）")
    async_run(c2.aclose())

    # 4.7 messages 缩短 → 冷启动
    c3 = _make_mock_client()
    long_messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]
    short_messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "a"},
    ]
    async_run(c3.call(long_messages))
    first_id = c3._previous_response_id
    assert_true(first_id is not None, "4.7 第一次 call 后有 response_id")
    # messages 缩短 → 冷启动
    async_run(c3.call(short_messages))
    assert_true(c3._previous_response_id is not None, "4.7 messages 缩短后仍成功（冷启动）")
    async_run(c3.aclose())


# ════════════════════════════════════════════════════
#  5. 响应转换 & Usage 构建
# ════════════════════════════════════════════════════

def test_response_convert():
    """5. 响应转换 & Usage 构建"""
    print("\n" + "═" * 60)
    print("5.  响应转换 & Usage 构建")
    print("═" * 60)

    c = ResponsesAPIClient(api_key="k", model_name="m", base_url="https://api/v1")

    # 5.1 _convert_output_to_response — 纯文本
    api_resp = _make_responses_api_response(content="你好世界")
    llm_resp = c._convert_output_to_response(api_resp)
    assert_true(llm_resp["content"] == "你好世界", "5.1 content 正确")
    assert_true(llm_resp["finish_reason"] == "stop", "5.1 finish_reason=stop")
    assert_true("tool_calls" not in llm_resp, "5.1 无 tool_calls")

    # 5.2 _convert_output_to_response — 含 function_call
    api_resp_tc = _make_responses_api_response(
        content=None,
        tool_calls=[{
            "id": "fc_1",
            "call_id": "call_1",
            "name": "get_weather",
            "arguments": '{"city":"BJ"}',
        }],
    )
    llm_resp_tc = c._convert_output_to_response(api_resp_tc)
    assert_true("tool_calls" in llm_resp_tc, "5.2 含 tool_calls")
    assert_true(llm_resp_tc["tool_calls"][0]["function"]["name"] == "get_weather", "5.2 tool_call name")
    assert_true(llm_resp_tc["tool_calls"][0]["function"]["arguments"] == '{"city":"BJ"}', "5.2 tool_call arguments")

    # 5.3 _convert_output_to_response — 含 reasoning
    api_resp_r = _make_responses_api_response(reasoning_content="推理过程")
    llm_resp_r = c._convert_output_to_response(api_resp_r)
    assert_true(llm_resp_r.get("reasoning_content") == "推理过程", "5.3 reasoning_content")

    # 5.4 _status_to_finish_reason 映射
    assert_true(ResponsesAPIClient._status_to_finish_reason("completed") == "stop", "5.4 completed→stop")
    assert_true(ResponsesAPIClient._status_to_finish_reason("incomplete") == "length", "5.4 incomplete→length")
    assert_true(ResponsesAPIClient._status_to_finish_reason("cancelled") == "stop", "5.4 cancelled→stop")
    assert_true(ResponsesAPIClient._status_to_finish_reason("failed") == "stop", "5.4 failed→stop")
    assert_true(ResponsesAPIClient._status_to_finish_reason("unknown") == "stop", "5.4 未知→stop")

    # 5.5 _build_usage_info — 基础
    usage_data = {
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 150,
    }
    usage = c._build_usage_info(usage_data)
    assert_true(usage["prompt_tokens"] == 100, "5.5 prompt_tokens=input_tokens")
    assert_true(usage["completion_tokens"] == 50, "5.5 completion_tokens=output_tokens")
    assert_true(usage["total_tokens"] == 150, "5.5 total_tokens")

    # 5.6 _build_usage_info — 含 details
    usage_data_d = {
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 150,
        "output_tokens_details": {"reasoning_tokens": 10, "output_tokens": 40},
        "input_tokens_details": {"cached_tokens": 80},
    }
    usage_d = c._build_usage_info(usage_data_d)
    assert_true(usage_d["completion_tokens_details"]["reasoning_tokens"] == 10, "5.6 reasoning_tokens")
    assert_true(usage_d["prompt_tokens_details"]["cached_tokens"] == 80, "5.6 cached_tokens")

    # 5.7 _build_usage_info — L4 caps 回填 cached_tokens
    c_caps = ResponsesAPIClient(
        api_key="k", model_name="m", base_url="https://api/v1",
        capabilities=OPENAI_RESPONSES,
    )
    usage_data_caps = {
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 150,
        "input_tokens_details": {"cached_tokens": 80},
    }
    usage_caps = c_caps._build_usage_info(usage_data_caps)
    assert_true(usage_caps["prompt_tokens_details"]["cached_tokens"] == 80, "5.7 caps 回填 cached_tokens")
    async_run(c_caps.aclose())

    # 5.8 _build_usage_info — L4 caps 回填（无 input_tokens_details，靠 caps 路径）
    usage_data_caps2 = {
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 150,
    }
    c_caps2 = ResponsesAPIClient(
        api_key="k", model_name="m", base_url="https://api/v1",
        capabilities=OPENAI_RESPONSES,
    )
    usage_caps2 = c_caps2._build_usage_info(usage_data_caps2)
    # 没有 input_tokens_details，也没有 cached_tokens_field 对应的数据，所以 prompt_tokens_details 不应有 cached_tokens
    assert_true(
        usage_caps2.get("prompt_tokens_details", {}).get("cached_tokens") is None,
        "5.8 无数据时 cached_tokens 为 None/不存在",
    )
    async_run(c_caps2.aclose())

    # 5.9 _dig 工具函数
    # _dig 传入的 data 是 usage dict 本身，路径含 usage. 前缀会被自动剥离
    usage_inner = {"input_tokens_details": {"cached_tokens": 42}}
    assert_true(_dig(usage_inner, "usage.input_tokens_details.cached_tokens") == 42, "5.9 _dig usage.前缀剥离后正常路径")
    assert_true(_dig(usage_inner, "input_tokens_details.cached_tokens") == 42, "5.9 _dig 无前缀直接路径")
    assert_true(_dig(usage_inner, "nonexistent") is None, "5.9 _dig 不存在 → None")

    # 5.10 _dig — usage. 前缀自动剥离
    inner = {"input_tokens_details": {"cached_tokens": 42}}
    assert_true(_dig(inner, "usage.input_tokens_details.cached_tokens") == 42, "5.10 usage. 前缀剥离")

    # 5.11 _dig — 非 dict 中间节点
    assert_true(_dig({"a": 1}, "a.b") is None, "5.11 非 dict 中间节点 → None")

    async_run(c.aclose())


# ════════════════════════════════════════════════════
#  6. SSE 解析 & 错误分类
# ════════════════════════════════════════════════════

def test_sse_and_error():
    """6. SSE 解析 & 错误分类"""
    print("\n" + "═" * 60)
    print("6.  SSE 解析 & 错误分类")
    print("═" * 60)

    # 6.1 _parse_sse_line — data: {json}
    line = 'data: {"type":"response.output_text.delta","delta":"Hello"}'
    parsed = ResponsesAPIClient._parse_sse_line(line)
    assert_true(isinstance(parsed, dict), "6.1 返回 dict")
    assert_true(parsed["delta"] == "Hello", "6.1 delta 正确")

    # 6.2 _parse_sse_line — data: [DONE]
    parsed_done = ResponsesAPIClient._parse_sse_line("data: [DONE]")
    assert_true(isinstance(parsed_done, _SseDone), "6.2 返回 _SseDone")

    # 6.3 _parse_sse_line — 非 data 行 → None
    assert_true(ResponsesAPIClient._parse_sse_line("event: xxx") is None, "6.3 非 data 行 → None")

    # 6.4 _parse_sse_line — 空行 → None
    assert_true(ResponsesAPIClient._parse_sse_line("") is None, "6.4 空行 → None")

    # 6.5 _parse_sse_line — JSON 格式错误 → None
    assert_true(ResponsesAPIClient._parse_sse_line("data: {invalid}") is None, "6.5 JSON 错误 → None")

    # 6.6 _classify_http_error — 401→LLMAuthError
    err = ResponsesAPIClient._classify_http_error(401, "Unauthorized", {})
    assert_true(isinstance(err, LLMAuthError), "6.6 401→LLMAuthError")
    assert_true(err.status_code == 401, "6.6 status_code=401")

    # 6.7 _classify_http_error — 403→LLMAuthError
    err2 = ResponsesAPIClient._classify_http_error(403, "Forbidden", {})
    assert_true(isinstance(err2, LLMAuthError), "6.7 403→LLMAuthError")

    # 6.8 _classify_http_error — 400→LLMRequestError
    err3 = ResponsesAPIClient._classify_http_error(400, "Bad Request", {})
    assert_true(isinstance(err3, LLMRequestError), "6.8 400→LLMRequestError")
    assert_true(err3.status_code == 400, "6.8 status_code=400")

    # 6.9 _classify_http_error — 404→LLMRequestError
    err4 = ResponsesAPIClient._classify_http_error(404, "Not Found", {})
    assert_true(isinstance(err4, LLMRequestError), "6.9 404→LLMRequestError")

    # 6.10 _classify_http_error — 408→LLMTimeoutError
    err5 = ResponsesAPIClient._classify_http_error(408, "Timeout", {})
    assert_true(isinstance(err5, LLMTimeoutError), "6.10 408→LLMTimeoutError")

    # 6.11 _classify_http_error — 429→LLMRateLimitError
    err6 = ResponsesAPIClient._classify_http_error(
        429, "Rate Limited", {"retry-after-ms": "2000"},
    )
    assert_true(isinstance(err6, LLMRateLimitError), "6.11 429→LLMRateLimitError")
    assert_true(err6.retry_after == 2.0, "6.11 retry_after 从 retry-after-ms 解析")

    # 6.12 _classify_http_error — 429 + retry-after (秒)
    err7 = ResponsesAPIClient._classify_http_error(
        429, "Rate Limited", {"retry-after": "5"},
    )
    assert_true(err7.retry_after == 5.0, "6.12 retry_after 从 retry-after 解析")

    # 6.13 _classify_http_error — 500→LLMServerError
    err8 = ResponsesAPIClient._classify_http_error(500, "Internal Server Error", {})
    assert_true(isinstance(err8, LLMServerError), "6.13 500→LLMServerError")

    # 6.14 _classify_http_error — 502→LLMServerError
    err9 = ResponsesAPIClient._classify_http_error(502, "Bad Gateway", {})
    assert_true(isinstance(err9, LLMServerError), "6.14 502→LLMServerError")

    # 6.15 _is_response_id_expired_error — 404
    exc_404 = LLMRequestError("HTTP 404: Not Found", status_code=404)
    assert_true(ResponsesAPIClient._is_response_id_expired_error(exc_404), "6.15 404 → True")

    # 6.16 _is_response_id_expired_error — 400 + "previous_response_id"
    exc_400_prev = LLMRequestError("previous_response_id not found", status_code=400)
    assert_true(ResponsesAPIClient._is_response_id_expired_error(exc_400_prev), "6.16 400+previous_response_id → True")

    # 6.17 _is_response_id_expired_error — 400 + "expired"
    exc_400_exp = LLMRequestError("response expired", status_code=400)
    assert_true(ResponsesAPIClient._is_response_id_expired_error(exc_400_exp), "6.17 400+expired → True")

    # 6.18 _is_response_id_expired_error — 400 + "invalid_response_id"
    exc_400_inv = LLMRequestError("invalid_response_id", status_code=400)
    assert_true(ResponsesAPIClient._is_response_id_expired_error(exc_400_inv), "6.18 400+invalid_response_id → True")

    # 6.19 _is_response_id_expired_error — 非 404 的 LLMServerError → False
    exc_500 = LLMServerError("HTTP 500: Internal Error", status_code=500)
    assert_true(not ResponsesAPIClient._is_response_id_expired_error(exc_500), "6.19 500 → False")

    # 6.20 _is_response_id_expired_error — LLMServerError 404
    exc_404s = LLMServerError("HTTP 404", status_code=404)
    assert_true(ResponsesAPIClient._is_response_id_expired_error(exc_404s), "6.20 LLMServerError 404 → True")

    # 6.21 _is_response_id_expired_error — 400 但不含过期关键词
    exc_400_other = LLMRequestError("context_length_exceeded", status_code=400)
    assert_true(not ResponsesAPIClient._is_response_id_expired_error(exc_400_other), "6.21 400+无关词 → False")

    # 6.22 _is_response_id_expired_error — LLMNetworkError 不是
    exc_net = LLMNetworkError("connection failed")
    assert_true(not ResponsesAPIClient._is_response_id_expired_error(exc_net), "6.22 LLMNetworkError → False")


# ════════════════════════════════════════════════════
#  7. call() MockTransport
# ════════════════════════════════════════════════════

def test_call():
    """7. call() MockTransport"""
    print("\n" + "═" * 60)
    print("7.  call() MockTransport")
    print("═" * 60)

    messages = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "你好"},
    ]

    # 7.1 call() — 全量路径成功
    c1 = _make_mock_client()
    resp = async_run(c1.call(messages))
    assert_true(resp["content"] == "你好", "7.1 全量路径 content 正确")
    assert_true(resp["finish_reason"] == "stop", "7.1 全量路径 finish_reason=stop")
    assert_true(resp["id"] == "resp_mock", "7.1 全量路径 id 正确")
    assert_true(c1.response_id == "resp_mock", "7.1 call 后 response_id 更新")
    assert_true(c1.last_messages_len == 2, "7.1 call 后 last_messages_len 更新")
    async_run(c1.aclose())

    # 7.2 call() — 增量路径成功
    c2 = _make_mock_client(
        initial_response_id="resp_prev", initial_messages_len=2,
    )
    messages_inc = messages + [{"role": "user", "content": "继续"}]
    # MockTransport 不区分请求体，直接返回成功
    resp2 = async_run(c2.call(messages_inc))
    assert_true(resp2["content"] == "你好", "7.2 增量路径成功")
    async_run(c2.aclose())

    # 7.3 call() — HTTP 500 → LLMServerError
    c3 = _make_mock_client(
        response_json={"error": {"message": "Internal Error"}},
        status_code=500,
    )
    try:
        async_run(c3.call(messages))
        result.fail("7.3 应抛出 LLMServerError")
    except LLMServerError:
        result.ok("7.3 HTTP 500 → LLMServerError")
    async_run(c3.aclose())

    # 7.4 call() — HTTP 401 → LLMAuthError
    c4 = _make_mock_client(
        response_json={"error": {"message": "Unauthorized"}},
        status_code=401,
    )
    try:
        async_run(c4.call(messages))
        result.fail("7.4 应抛出 LLMAuthError")
    except LLMAuthError:
        result.ok("7.4 HTTP 401 → LLMAuthError")
    async_run(c4.aclose())

    # 7.5 call() — 超时 → LLMTimeoutError
    c5 = _make_mock_client(raise_on_request=httpx.TimeoutException("timeout"))
    try:
        async_run(c5.call(messages))
        result.fail("7.5 应抛出 LLMTimeoutError")
    except LLMTimeoutError:
        result.ok("7.5 超时 → LLMTimeoutError")
    async_run(c5.aclose())

    # 7.6 call() — 连接失败 → LLMNetworkError
    c6 = _make_mock_client(raise_on_request=httpx.ConnectError("connection failed"))
    try:
        async_run(c6.call(messages))
        result.fail("7.6 应抛出 LLMNetworkError")
    except LLMNetworkError:
        result.ok("7.6 连接失败 → LLMNetworkError")
    async_run(c6.aclose())

    # 7.7 call() — response_id 过期 → 自动降级
    call_count = [0]

    def _expired_then_ok(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            # 第一次（增量）返回 400 + 过期关键词
            return httpx.Response(
                400,
                json={"error": {"message": "previous_response_id not found"}},
            )
        else:
            # 第二次（降级全量）成功
            return httpx.Response(200, json=_make_responses_api_response(
                resp_id="resp_fallback", content="降级成功",
            ))

    c7 = ResponsesAPIClient(
        api_key="k", model_name="m", base_url="https://api/v1",
        initial_response_id="resp_expired", initial_messages_len=1,
    )
    async_run(c7._http_client.aclose())
    c7._http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_expired_then_ok), timeout=5.0,
    )
    resp7 = async_run(c7.call(messages))
    assert_true(resp7["content"] == "降级成功", "7.7 response_id 过期降级成功")
    assert_true(call_count[0] == 2, "7.7 降级后重试了 2 次")
    assert_true(c7.response_id == "resp_fallback", "7.7 降级后 response_id 更新")
    async_run(c7.aclose())

    # 7.8 call() — JSON 解析失败 → LLMResponseError
    c8 = _make_mock_client(status_code=200)
    # 替换 transport 使其返回非 JSON
    def _bad_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all", headers={"content-type": "application/json"})
    async_run(c8._http_client.aclose())
    c8._http_client = httpx.AsyncClient(transport=httpx.MockTransport(_bad_json), timeout=5.0)
    try:
        async_run(c8.call(messages))
        result.fail("7.8 应抛出 LLMResponseError")
    except LLMResponseError:
        result.ok("7.8 JSON 解析失败 → LLMResponseError")
    async_run(c8.aclose())

    # 7.9 call() — 带 tools
    tools = [{"type": "function", "function": {"name": "f", "description": "d"}}]
    c9 = _make_mock_client()
    resp9 = async_run(c9.call(messages, tools=tools))
    assert_true(resp9["content"] == "你好", "7.9 带 tools 调用成功")
    async_run(c9.aclose())

    # 7.10 call() — 带 ModelSettings
    c10 = _make_mock_client()
    resp10 = async_run(c10.call(messages, settings=ModelSettings(temperature=0.5)))
    assert_true(resp10["content"] == "你好", "7.10 带 ModelSettings 调用成功")
    async_run(c10.aclose())

    # 7.11 call() — always_tools_count 参数（不报错）
    c11 = _make_mock_client()
    resp11 = async_run(c11.call(messages, always_tools_count=3))
    assert_true(resp11["content"] == "你好", "7.11 always_tools_count 参数不报错")
    async_run(c11.aclose())

    # 7.12 call() — HTTP 429 → LLMRateLimitError
    c12 = _make_mock_client(status_code=429)
    try:
        async_run(c12.call(messages))
        result.fail("7.12 应抛出 LLMRateLimitError")
    except LLMRateLimitError:
        result.ok("7.12 HTTP 429 → LLMRateLimitError")
    async_run(c12.aclose())


# ════════════════════════════════════════════════════
#  8. stream_response() MockTransport
# ════════════════════════════════════════════════════

def test_stream():
    """8. stream_response() MockTransport"""
    print("\n" + "═" * 60)
    print("8.  stream_response() MockTransport")
    print("═" * 60)

    messages = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "你好"},
    ]

    # 8.1 流式文本增量
    sse_text = _make_responses_sse_stream(
        text_parts=["你", "好", "世", "界"],
        usage={"input_tokens": 50, "output_tokens": 10, "total_tokens": 60},
    )
    c1 = _make_mock_client(sse_text=sse_text)
    chunks: list[LLMStreamChunk] = []
    async def _collect1():
        async for chunk in c1.stream_response(messages):
            chunks.append(chunk)
    async_run(_collect1())
    text_content = "".join(c.delta_content for c in chunks if c.delta_content)
    assert_true(text_content == "你好世界", "8.1 流式文本增量拼接正确")
    finish_chunks = [c for c in chunks if c.finish_reason]
    assert_true(len(finish_chunks) >= 1, "8.1 含 finish_reason chunk")
    assert_true(finish_chunks[-1].finish_reason == "stop", "8.1 finish_reason=stop")
    async_run(c1.aclose())

    # 8.2 流式 tool_call delta
    sse_tc = _make_responses_sse_stream(
        tool_calls=[{
            "id": "fc_0",
            "call_id": "call_0",
            "name": "get_weather",
            "argument_parts": ['{"ci', 'ty":"B', 'J"}'],
        }],
    )
    c2 = _make_mock_client(sse_text=sse_tc)
    tc_chunks: list[LLMStreamChunk] = []
    async def _collect2():
        async for chunk in c2.stream_response(messages):
            tc_chunks.append(chunk)
    async_run(_collect2())
    tc_deltas = [c for c in tc_chunks if c.tool_call_delta]
    assert_true(len(tc_deltas) >= 1, "8.2 含 tool_call_delta")
    # 第一个应该是 output_item.added（name），后续是 arguments delta
    first_tc = tc_deltas[0].tool_call_delta
    assert_true(first_tc["name"] == "get_weather", "8.2 第一个 delta 含 name")
    async_run(c2.aclose())

    # 8.3 流式 reasoning delta
    sse_reasoning = _make_responses_sse_stream(
        reasoning_parts=["让我想想", "..."],
    )
    c3 = _make_mock_client(sse_text=sse_reasoning)
    r_chunks: list[LLMStreamChunk] = []
    async def _collect3():
        async for chunk in c3.stream_response(messages):
            r_chunks.append(chunk)
    async_run(_collect3())
    r_text = "".join(c.delta_reasoning_content for c in r_chunks if c.delta_reasoning_content)
    assert_true(r_text == "让我想想...", "8.3 reasoning 增量拼接正确")
    async_run(c3.aclose())

    # 8.4 流式 response.completed → finish_reason + usage
    usage_data = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
    sse_usage = _make_responses_sse_stream(
        text_parts=["ok"],
        usage=usage_data,
    )
    c4 = _make_mock_client(sse_text=sse_usage)
    u_chunks: list[LLMStreamChunk] = []
    async def _collect4():
        async for chunk in c4.stream_response(messages):
            u_chunks.append(chunk)
    async_run(_collect4())
    usage_chunks = [c for c in u_chunks if c.usage is not None]
    assert_true(len(usage_chunks) >= 1, "8.4 含 usage chunk")
    assert_true(usage_chunks[-1].usage["prompt_tokens"] == 100, "8.4 usage.prompt_tokens 正确")
    assert_true(c4.response_id == "resp_stream_mock", "8.4 流式后 response_id 更新")
    async_run(c4.aclose())

    # 8.5 流式 response_id 过期 → 全量降级重试
    stream_call_count = [0]

    def _stream_expired_handler(request: httpx.Request) -> httpx.Response:
        stream_call_count[0] += 1
        if stream_call_count[0] == 1:
            # 第一次（增量）返回 400 过期
            return httpx.Response(
                400,
                json={"error": {"message": "previous_response_id not found"}},
                headers={"content-type": "application/json"},
            )
        else:
            # 第二次（降级全量）返回正常 SSE
            sse_ok = _make_responses_sse_stream(
                text_parts=["降级成功"],
                resp_id="resp_fallback_stream",
            )
            return httpx.Response(
                200,
                content=sse_ok.encode("utf-8"),
                headers={"content-type": "text/event-stream"},
            )

    c5 = ResponsesAPIClient(
        api_key="k", model_name="m", base_url="https://api/v1",
        initial_response_id="resp_expired_stream", initial_messages_len=1,
    )
    async_run(c5._http_client.aclose())
    c5._http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_stream_expired_handler), timeout=5.0,
    )
    fb_chunks: list[LLMStreamChunk] = []
    async def _collect5():
        async for chunk in c5.stream_response(messages):
            fb_chunks.append(chunk)
    async_run(_collect5())
    fb_text = "".join(c.delta_content for c in fb_chunks if c.delta_content)
    assert_true(fb_text == "降级成功", "8.5 流式降级成功")
    assert_true(c5.response_id == "resp_fallback_stream", "8.5 降级后 response_id 更新")
    async_run(c5.aclose())

    # 8.6 流式超时 → LLMTimeoutError
    c6 = _make_mock_client(raise_on_request=httpx.TimeoutException("stream timeout"))
    try:
        async def _try_stream6():
            async for _ in c6.stream_response(messages):
                pass
        async_run(_try_stream6())
        result.fail("8.6 应抛出 LLMTimeoutError")
    except LLMTimeoutError:
        result.ok("8.6 流式超时 → LLMTimeoutError")
    async_run(c6.aclose())

    # 8.7 流式连接失败 → LLMNetworkError
    c7 = _make_mock_client(raise_on_request=httpx.ConnectError("connection failed"))
    try:
        async def _try_stream7():
            async for _ in c7.stream_response(messages):
                pass
        async_run(_try_stream7())
        result.fail("8.7 应抛出 LLMNetworkError")
    except LLMNetworkError:
        result.ok("8.7 流式连接失败 → LLMNetworkError")
    async_run(c7.aclose())

    # 8.8 流式 HTTP 401 → LLMAuthError
    def _auth_err_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Unauthorized"}})
    c8 = ResponsesAPIClient(api_key="k", model_name="m", base_url="https://api/v1")
    async_run(c8._http_client.aclose())
    c8._http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_auth_err_handler), timeout=5.0,
    )
    try:
        async def _try_stream8():
            async for _ in c8.stream_response(messages):
                pass
        async_run(_try_stream8())
        result.fail("8.8 应抛出 LLMAuthError")
    except LLMAuthError:
        result.ok("8.8 流式 401 → LLMAuthError")
    async_run(c8.aclose())

    # 8.9 流式 — always_tools_count 参数（不报错）
    sse_simple = _make_responses_sse_stream(text_parts=["ok"])
    c9 = _make_mock_client(sse_text=sse_simple)
    async def _collect9():
        async for _ in c9.stream_response(messages, always_tools_count=2):
            pass
    async_run(_collect9())
    result.ok("8.9 流式 always_tools_count 不报错")
    async_run(c9.aclose())

    # 8.10 流式 — 带 ModelSettings
    sse_settings = _make_responses_sse_stream(text_parts=["ok"])
    c10 = _make_mock_client(sse_text=sse_settings)
    async def _collect10():
        async for _ in c10.stream_response(messages, settings=ModelSettings(temperature=0.5)):
            pass
    async_run(_collect10())
    result.ok("8.10 流式带 ModelSettings 成功")
    async_run(c10.aclose())


# ════════════════════════════════════════════════════
#  9. 生命周期
# ════════════════════════════════════════════════════

def test_lifecycle():
    """9. 生命周期"""
    print("\n" + "═" * 60)
    print("9.  生命周期")
    print("═" * 60)

    # 9.1 aclose() 释放资源
    c1 = ResponsesAPIClient(api_key="k", model_name="m", base_url="https://api/v1")
    async_run(c1.aclose())
    result.ok("9.1 aclose() 不抛异常")

    # 9.2 async context manager
    async def _ctx():
        async with ResponsesAPIClient(api_key="k", model_name="m", base_url="https://api/v1") as c:
            return c.model_name
    name = async_run(_ctx())
    assert_true(name == "m", "9.2 async context manager 正常")

    # 9.3 _build_url
    c3 = ResponsesAPIClient(api_key="k", model_name="m", base_url="https://api/v1")
    assert_true(c3._build_url(None) == "https://api/v1/responses", "9.3 _build_url 基础")
    async_run(c3.aclose())

    # 9.4 _build_url + extra_query
    c4 = ResponsesAPIClient(api_key="k", model_name="m", base_url="https://api/v1")
    url = c4._build_url(ModelSettings(extra_query={"api-version": "2024-02-01"}))
    assert_true("api-version=2024-02-01" in url, "9.4 _build_url 含 extra_query")
    async_run(c4.aclose())

    # 9.5 _build_headers
    c5 = ResponsesAPIClient(api_key="k", model_name="m", base_url="https://api/v1")
    headers = c5._build_headers(None)
    assert_true(headers["Authorization"] == "Bearer k", "9.5 Authorization header")
    assert_true(headers["Content-Type"] == "application/json", "9.5 Content-Type header")
    async_run(c5.aclose())

    # 9.6 _build_headers + extra_headers
    c6 = ResponsesAPIClient(api_key="k", model_name="m", base_url="https://api/v1")
    headers6 = c6._build_headers(ModelSettings(extra_headers={"X-Custom": "val"}))
    assert_true(headers6["X-Custom"] == "val", "9.6 extra_headers 合并")
    async_run(c6.aclose())

    # 9.7 _update_state_after_success
    c7 = ResponsesAPIClient(api_key="k", model_name="m", base_url="https://api/v1")
    c7._update_state_after_success(
        {"id": "resp_new"}, [{"role": "user", "content": "hi"}], "hash123",
    )
    assert_true(c7.response_id == "resp_new", "9.7 response_id 更新")
    assert_true(c7.last_messages_len == 1, "9.7 last_messages_len 更新")
    assert_true(c7._tools_hash == "hash123", "9.7 tools_hash 更新")
    async_run(c7.aclose())

    # 9.8 流式后 response_id 更新
    sse = _make_responses_sse_stream(text_parts=["ok"], resp_id="resp_stream_new")
    c8 = _make_mock_client(sse_text=sse)
    async def _collect8():
        async for _ in c8.stream_response([{"role": "user", "content": "hi"}]):
            pass
    async_run(_collect8())
    assert_true(c8.response_id == "resp_stream_new", "9.8 流式后 response_id 更新")
    async_run(c8.aclose())


# ════════════════════════════════════════════════════
#  Section 组织 & main 入口
# ════════════════════════════════════════════════════

SECTIONS: dict[str, list] = {
    "constructor": [test_construction],
    "factory": [test_factory_and_capabilities],
    "request_build": [test_request_build],
    "state": [test_state_management],
    "response_convert": [test_response_convert],
    "sse_error": [test_sse_and_error],
    "call": [test_call],
    "stream": [test_stream],
    "lifecycle": [test_lifecycle],
}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ResponsesAPIClient Mock 测试")
    parser.add_argument("--section", choices=list(SECTIONS.keys()), help="只运行指定 section")
    args = parser.parse_args()

    if args.section:
        sections_to_run = {args.section: SECTIONS[args.section]}
    else:
        sections_to_run = SECTIONS

    all_ok = True
    for section_name, tests in sections_to_run.items():
        for test_fn in tests:
            test_fn()
        if not result.summary(section_name):
            all_ok = False

    print("\n" + "═" * 60)
    result.summary("ResponsesAPIClient Mock 总计")
    sys.exit(0 if all_ok and result.failed == 0 else 1)
