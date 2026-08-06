"""
Pandaren Agent SDK · LLM 层 Mock 测试

覆盖范围
--------
  OpenAICompatibleClient（client.py）
    - 构造参数校验（api_key/model_name/base_url 不可空）
    - 工厂方法：for_openai / for_dashscope / for_volcengine（含 use_context_api）
    - capabilities 属性（只读，返回注入的 EndpointCapabilities 常量或 None）
    - cache / cache_depth 构造参数存储（_cache / _cache_depth）
    - _emit_capability_warnings：ProviderCapabilityWarning 触发条件
    - 生命周期通知：_on_history_compacted / _on_static_context_changed
    - always_tools_count 关键字参数（call / stream_response）
    - _build_payload：ModelSettings 全字段写入 + extra_body 展开
    - _build_headers：基础 + extra_headers 覆盖
    - _build_url：基础 + extra_query 拼接
    - _merge_settings：base / override 合并语义
    - _extract_response：标准响应 / choices 为空 / reasoning_content / refusal
    - _parse_sse_line：正常 / [DONE] / 心跳 / JSON 格式错误
    - _classify_http_error：401/400/408/429/5xx/其他
    - _build_usage_info：基础 / completion_tokens_details / prompt_tokens_details
    - _resolve_response_format：dict 原样 / type 自动转换
    - call()：MockTransport 非流式成功 / HTTP 错误 / 超时 / 网络异常 / JSON 解析失败
    - stream_response()：MockTransport 流式 / tool_call_delta / reasoning_content / refusal_delta
                       / include_usage / choices=[] usage-only chunk
    - aclose() / async context manager
    - model_name 只读属性

  LLMRouter（router.py）
    - register：精确 vs 前缀匹配
    - set_default
    - _resolve：精确 > 最长前缀 > default > 无匹配抛错
    - call / stream_response 路由委托
    - model_name 属性：default > primary > 空字符串
    - aclose 关闭所有 client

  schema.py
    - json_schema：dataclass → response_format
    - output_type_to_response_format：dataclass / pydantic
    - _validate_output_type：None / str / 非类型 / 非法类型

  exceptions.py
    - LLMError 层次：7 个子类继承关系
    - LLMRateLimitError.retry_after
    - LLMAuthError / LLMServerError status_code

  types.py
    - ModelSettings 默认全 None
    - LLMStreamChunk 各字段
    - LLMResponse 必选字段

运行方式
--------
  cd pandaren/llm/tests && python test_llm_mock.py
  python test_llm_mock.py --section client
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from dataclasses import dataclass, field
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
    LLMClient,
    OpenAICompatibleClient,
    LLMRouter,
    json_schema,
    output_type_to_response_format,
)
from pandaren.llm.client import (
    _SseDone,
    _SSE_DONE,
    ProviderCapabilityWarning,
)
from pandaren.llm.capabilities import (
    EndpointCapabilities,
    DASHSCOPE_CHAT,
    VOLCENGINE_CHAT,
    VOLCENGINE_CONTEXT_API,
    OPENAI_CHAT,
)
from pandaren.llm.cache_strategy import CacheDepth, CacheMode


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
#  工厂方法
# ════════════════════════════════════════════════════

def _make_openai_response(
    content: str = "你好",
    finish_reason: str = "stop",
    model: str = "mock-model",
    resp_id: str = "chatcmpl-mock",
    tool_calls: list[dict] | None = None,
    reasoning_content: str | None = None,
    refusal: str | None = None,
    usage: dict | None = None,
) -> dict[str, Any]:
    """构造 OpenAI 兼容的非流式响应 JSON。"""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if reasoning_content:
        message["reasoning_content"] = reasoning_content
    if refusal:
        message["refusal"] = refusal
    if usage is None:
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    return {
        "id": resp_id,
        "object": "chat.completion",
        "created": 1234567890,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage,
    }


def _make_sse_chunk(data: dict[str, Any]) -> str:
    """构造单个 SSE data 行。"""
    return f"data: {json.dumps(data, ensure_ascii=False)}"


def _make_sse_chunks(
    content_parts: list[str] | None = None,
    finish_reason: str = "stop",
    usage: dict | None = None,
    tool_call_deltas: list[dict] | None = None,
    reasoning_parts: list[str] | None = None,
    refusal_parts: list[str] | None = None,
    include_usage: bool = False,
) -> str:
    """构造 SSE 流式文本（多行 data: ... 格式）。"""
    lines = []

    # 首个 chunk：role
    lines.append(_make_sse_chunk({
        "id": "chatcmpl-s", "object": "chat.completion.chunk",
        "created": 0, "model": "m",
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }))

    # content chunks
    for part in (content_parts or []):
        lines.append(_make_sse_chunk({
            "id": "chatcmpl-s", "object": "chat.completion.chunk",
            "created": 0, "model": "m",
            "choices": [{"index": 0, "delta": {"content": part}, "finish_reason": None}],
        }))

    # reasoning_content chunks
    for part in (reasoning_parts or []):
        lines.append(_make_sse_chunk({
            "id": "chatcmpl-s", "object": "chat.completion.chunk",
            "created": 0, "model": "m",
            "choices": [{"index": 0, "delta": {"reasoning_content": part}, "finish_reason": None}],
        }))

    # refusal chunks
    for part in (refusal_parts or []):
        lines.append(_make_sse_chunk({
            "id": "chatcmpl-s", "object": "chat.completion.chunk",
            "created": 0, "model": "m",
            "choices": [{"index": 0, "delta": {"refusal": part}, "finish_reason": None}],
        }))

    # tool_call delta chunks
    for tc in (tool_call_deltas or []):
        lines.append(_make_sse_chunk({
            "id": "chatcmpl-s", "object": "chat.completion.chunk",
            "created": 0, "model": "m",
            "choices": [{"index": 0, "delta": {"tool_calls": [tc]}, "finish_reason": None}],
        }))

    # usage-only chunk (choices=[])
    if include_usage and usage:
        lines.append(_make_sse_chunk({
            "id": "chatcmpl-s", "object": "chat.completion.chunk",
            "created": 0, "model": "m", "choices": [], "usage": usage,
        }))

    # 终止 chunk
    finish_chunk: dict[str, Any] = {
        "id": "chatcmpl-s", "object": "chat.completion.chunk",
        "created": 0, "model": "m",
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
    }
    if include_usage and usage:
        finish_chunk["usage"] = usage
    lines.append(_make_sse_chunk(finish_chunk))

    lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


def _make_mock_client(
    response_json: dict | None = None,
    status_code: int = 200,
    sse_text: str | None = None,
    raise_on_post: Exception | None = None,
) -> OpenAICompatibleClient:
    """构造一个注入了 MockTransport 的 OpenAICompatibleClient。"""
    client = OpenAICompatibleClient(
        api_key="mock-key",
        model_name="mock-model",
        base_url="https://mock.api/v1",
        timeout=5.0,
    )

    if raise_on_post:
        def _handler(request: httpx.Request) -> httpx.Response:
            raise raise_on_post
        transport = httpx.MockTransport(_handler)
    elif sse_text is not None:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, content=sse_text.encode("utf-8"), headers={"content-type": "text/event-stream"})
        transport = httpx.MockTransport(_handler)
    elif response_json is not None:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, json=response_json)
        transport = httpx.MockTransport(_handler)
    else:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, json=_make_openai_response())
        transport = httpx.MockTransport(_handler)

    # 替换内部 http_client
    async_run(client._http_client.aclose())
    client._http_client = httpx.AsyncClient(transport=transport, timeout=5.0)
    return client


# ════════════════════════════════════════════════════
#  1. OpenAICompatibleClient — 构造
# ════════════════════════════════════════════════════

def test_client_construction():
    """1. OpenAICompatibleClient 构造"""
    print("\n" + "═" * 60)
    print("1.  构造 — 参数校验")
    print("═" * 60)

    # 1.1 正常构造
    c = OpenAICompatibleClient(api_key="k", model_name="gpt-4o", base_url="https://api.openai.com/v1")
    assert_true(c.model_name == "gpt-4o", "1.1 model_name 正确")
    async_run(c.aclose())

    # 1.2 api_key 为空抛 ValueError
    @assert_raises(ValueError, "1.2 api_key 为空 → ValueError")
    def _():
        OpenAICompatibleClient(api_key="", model_name="m", base_url="https://api.test/v1")

    # 1.3 model_name 为空抛 ValueError
    @assert_raises(ValueError, "1.3 model_name 为空 → ValueError")
    def _():
        OpenAICompatibleClient(api_key="k", model_name="", base_url="https://api.test/v1")

    # 1.4 base_url 尾部斜杠被去除
    c2 = OpenAICompatibleClient(api_key="k", model_name="m", base_url="https://api.test/v1/")
    assert_true(c2._base_url == "https://api.test/v1", "1.4 base_url 尾部 / 被去除")
    async_run(c2.aclose())

    # 1.5 默认参数（model_name 和 base_url 现在是必选参数，验证 timeout 默认值）
    c3 = OpenAICompatibleClient(api_key="k", model_name="gpt-4o", base_url="https://api.openai.com/v1")
    assert_true(c3.model_name == "gpt-4o", "1.5 model_name 正确")
    assert_true(c3._base_url == "https://api.openai.com/v1", "1.5 base_url 正确")
    assert_true(c3._timeout == 60.0, "1.5 默认 timeout=60s")
    async_run(c3.aclose())

    # 1.6 model_name 只读
    c4 = OpenAICompatibleClient(api_key="k", model_name="gpt-4o", base_url="https://api.openai.com/v1")
    assert_true(c4.model_name == "gpt-4o", "1.6 model_name property 可读")
    @assert_raises(AttributeError, "1.6 model_name 不可写")
    def _():
        c4.model_name = "hacked"
    async_run(c4.aclose())

    # 1.7 base_url 为空 → ValueError
    @assert_raises(ValueError, "1.7 base_url 为空 → ValueError")
    def _():
        OpenAICompatibleClient(api_key="k", model_name="m", base_url="")


# ════════════════════════════════════════════════════
#  1b. 工厂方法 / capabilities 属性 / 缓存参数 / 生命周期通知
# ════════════════════════════════════════════════════

def test_factory_and_capabilities():
    """1b. 工厂方法、capabilities 属性、cache 参数、生命周期通知"""
    print("\n" + "═" * 60)
    print("1b. 工厂方法 / capabilities / cache / 生命周期")
    print("═" * 60)

    # ── 工厂方法 ──

    # 1b.1 for_openai：默认 model_name / base_url / capabilities
    c = OpenAICompatibleClient.for_openai(api_key="k")
    assert_true(c.model_name == "gpt-4o", "1b.1 for_openai 默认 model_name=gpt-4o")
    assert_true(c._base_url == "https://api.openai.com/v1", "1b.1 for_openai 默认 base_url")
    assert_true(c.capabilities is OPENAI_CHAT, "1b.1 for_openai 绑定 OPENAI_CHAT")
    async_run(c.aclose())

    # 1b.2 for_openai：自定义 model_name
    c2 = OpenAICompatibleClient.for_openai(api_key="k", model_name="gpt-4-turbo")
    assert_true(c2.model_name == "gpt-4-turbo", "1b.2 for_openai 自定义 model_name")
    async_run(c2.aclose())

    # 1b.3 for_dashscope：默认 base_url / capabilities
    c3 = OpenAICompatibleClient.for_dashscope(api_key="k", model_name="qwen-plus")
    assert_true(
        c3._base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "1b.3 for_dashscope 默认 base_url",
    )
    assert_true(c3.capabilities is DASHSCOPE_CHAT, "1b.3 for_dashscope 绑定 DASHSCOPE_CHAT")
    async_run(c3.aclose())

    # 1b.4 for_volcengine：默认 base_url / capabilities（chat）
    c4 = OpenAICompatibleClient.for_volcengine(api_key="k", model_name="doubao-pro")
    assert_true(
        c4._base_url == "https://ark.cn-beijing.volces.com/api/v3",
        "1b.4 for_volcengine 默认 base_url",
    )
    assert_true(c4.capabilities is VOLCENGINE_CHAT, "1b.4 for_volcengine 默认绑定 VOLCENGINE_CHAT")
    async_run(c4.aclose())

    # 1b.5 for_volcengine(use_context_api=True) → VOLCENGINE_CONTEXT_API
    c5 = OpenAICompatibleClient.for_volcengine(
        api_key="k", model_name="ep-xxx", use_context_api=True
    )
    assert_true(
        c5.capabilities is VOLCENGINE_CONTEXT_API,
        "1b.5 for_volcengine(use_context_api=True) → VOLCENGINE_CONTEXT_API",
    )
    async_run(c5.aclose())

    # ── capabilities 属性 ──

    # 1b.6 直接注入 capabilities → property 返回相同对象
    c6 = OpenAICompatibleClient(
        api_key="k", model_name="m", base_url="https://api/v1",
        capabilities=DASHSCOPE_CHAT,
    )
    assert_true(c6.capabilities is DASHSCOPE_CHAT, "1b.6 capabilities 属性返回注入的常量")
    async_run(c6.aclose())

    # 1b.7 未注入 capabilities → property 返回 None
    c7 = OpenAICompatibleClient(api_key="k", model_name="m", base_url="https://api/v1")
    assert_true(c7.capabilities is None, "1b.7 未注入 capabilities → None")
    async_run(c7.aclose())

    # ── cache / cache_depth 构造参数 ──

    # 1b.8 默认 cache=True / cache_depth="history"
    c8 = OpenAICompatibleClient(api_key="k", model_name="m", base_url="https://api/v1")
    assert_true(c8._cache is True, "1b.8 默认 cache=True")
    assert_true(c8._cache_depth == "history", "1b.8 默认 cache_depth='history'")
    async_run(c8.aclose())

    # 1b.9 显式设置 cache=False / cache_depth="off"
    c9 = OpenAICompatibleClient(
        api_key="k", model_name="m", base_url="https://api/v1",
        cache=False, cache_depth="off",
    )
    assert_true(c9._cache is False, "1b.9 cache=False 被存储")
    assert_true(c9._cache_depth == "off", "1b.9 cache_depth='off' 被存储")
    async_run(c9.aclose())

    # 1b.10 cache="manual" / cache_depth="tools"
    c10 = OpenAICompatibleClient(
        api_key="k", model_name="m", base_url="https://api/v1",
        cache="manual", cache_depth="tools",
    )
    assert_true(c10._cache == "manual", "1b.10 cache='manual' 被存储")
    assert_true(c10._cache_depth == "tools", "1b.10 cache_depth='tools' 被存储")
    async_run(c10.aclose())

    # ── _emit_capability_warnings ──

    import warnings as _warnings

    # 1b.11 capabilities=None → 不发出告警
    c11 = OpenAICompatibleClient(api_key="k", model_name="m", base_url="https://api/v1")
    payload_cc = {
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}]}],
    }
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        OpenAICompatibleClient._emit_capability_warnings(payload_cc, None)
    assert_true(
        not any(issubclass(w.category, ProviderCapabilityWarning) for w in caught),
        "1b.11 capabilities=None 时不发 ProviderCapabilityWarning",
    )
    async_run(c11.aclose())

    # 1b.12 volcengine chat + messages 带 cache_control → 发出 ProviderCapabilityWarning
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        OpenAICompatibleClient._emit_capability_warnings(payload_cc, VOLCENGINE_CHAT)
    cap_warns = [w for w in caught if issubclass(w.category, ProviderCapabilityWarning)]
    assert_true(len(cap_warns) >= 1, "1b.12 volcengine chat + cache_control → ProviderCapabilityWarning")

    # 1b.13 dashscope chat（supports cache_control）→ 不发告警
    payload_ok = {
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}]}],
    }
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        OpenAICompatibleClient._emit_capability_warnings(payload_ok, DASHSCOPE_CHAT)
    cap_warns_ds = [w for w in caught if issubclass(w.category, ProviderCapabilityWarning)]
    assert_true(
        len(cap_warns_ds) == 0,
        "1b.13 dashscope chat + cache_control → 不发 ProviderCapabilityWarning",
    )

    # ── 生命周期通知 ──

    # 1b.14 _on_history_compacted → _cache_state.next_call_is_cold=True
    c14 = OpenAICompatibleClient(api_key="k", model_name="m", base_url="https://api/v1")
    assert_true(c14._cache_state.next_call_is_cold is False, "1b.14 初始 next_call_is_cold=False")
    c14._on_history_compacted()
    assert_true(c14._cache_state.next_call_is_cold is True, "1b.14 _on_history_compacted → cold=True")
    async_run(c14.aclose())

    # 1b.15 _on_static_context_changed → _cache_state.next_call_is_cold=True
    c15 = OpenAICompatibleClient(api_key="k", model_name="m", base_url="https://api/v1")
    c15._on_static_context_changed()
    assert_true(c15._cache_state.next_call_is_cold is True, "1b.15 _on_static_context_changed → cold=True")
    async_run(c15.aclose())

    # ── always_tools_count 参数（仅验证签名可接受，不发真实 HTTP 请求）──

    # 1b.16 call() 接受 always_tools_count 关键字参数
    c16 = _make_mock_client()
    msgs = [{"role": "user", "content": "hi"}]
    response = async_run(c16.call(msgs, always_tools_count=3))
    assert_true(isinstance(response, dict), "1b.16 call(always_tools_count=3) 可正常执行")
    async_run(c16.aclose())

    # 1b.17 stream_response() 接受 always_tools_count 关键字参数
    sse = _make_sse_chunks(content_parts=["ok"])
    c17 = _make_mock_client(sse_text=sse)

    async def _stream_test():
        chunks = []
        async for chunk in c17.stream_response(msgs, always_tools_count=2):
            chunks.append(chunk)
        return chunks

    chunks = async_run(_stream_test())
    assert_true(len(chunks) > 0, "1b.17 stream_response(always_tools_count=2) 可正常执行")
    async_run(c17.aclose())

def test_build_payload():
    """2. _build_payload — 请求体构建"""
    print("\n" + "═" * 60)
    print("2.  _build_payload — 请求体构建")
    print("═" * 60)

    c = OpenAICompatibleClient(api_key="k", model_name="test-model", base_url="https://api/v1")

    # 2.1 无 settings → 只有 model + messages
    payload = c._build_payload([{"role": "user", "content": "hi"}], None, None, stream=False)
    assert_true(payload["model"] == "test-model", "2.1 model 正确")
    assert_true(len(payload["messages"]) == 1, "2.1 messages 正确")
    assert_true("temperature" not in payload, "2.1 无 settings 时不写入 temperature")

    # 2.2 ModelSettings 全字段写入
    settings = ModelSettings(
        temperature=0.5,
        max_tokens=100,
        top_p=0.9,
        frequency_penalty=0.1,
        presence_penalty=0.2,
        stop=["END"],
        seed=42,
        response_format={"type": "json_object"},
        tool_choice="auto",
        parallel_tool_calls=True,
        reasoning={"effort": "low"},
    )
    payload2 = c._build_payload([{"role": "user", "content": "hi"}], None, settings, stream=False)
    assert_true(payload2["temperature"] == 0.5, "2.2 temperature 写入")
    assert_true(payload2["max_tokens"] == 100, "2.2 max_tokens 写入")
    assert_true(payload2["top_p"] == 0.9, "2.2 top_p 写入")
    assert_true(payload2["frequency_penalty"] == 0.1, "2.2 frequency_penalty 写入")
    assert_true(payload2["presence_penalty"] == 0.2, "2.2 presence_penalty 写入")
    assert_true(payload2["stop"] == ["END"], "2.2 stop 写入")
    assert_true(payload2["seed"] == 42, "2.2 seed 写入")
    assert_true(payload2["response_format"] == {"type": "json_object"}, "2.2 response_format 写入")
    assert_true(payload2["tool_choice"] == "auto", "2.2 tool_choice 写入")
    assert_true(payload2["parallel_tool_calls"] is True, "2.2 parallel_tool_calls 写入")
    assert_true(payload2["reasoning"] == {"effort": "low"}, "2.2 reasoning 写入")

    # 2.3 extra_body 展开到顶层
    settings3 = ModelSettings(extra_body={"enable_thinking": True, "thinking_budget": 4096})
    payload3 = c._build_payload([{"role": "user", "content": "hi"}], None, settings3, stream=False)
    assert_true(payload3["enable_thinking"] is True, "2.3 extra_body enable_thinking 展开")
    assert_true(payload3["thinking_budget"] == 4096, "2.3 extra_body thinking_budget 展开")

    # 2.4 tools 写入
    tools = [{"type": "function", "function": {"name": "f1"}}]
    payload4 = c._build_payload([{"role": "user", "content": "hi"}], tools, None, stream=False)
    assert_true(payload4["tools"] == tools, "2.4 tools 原样写入")

    # 2.5 stream=True + include_usage=True
    settings5 = ModelSettings(include_usage=True)
    payload5 = c._build_payload([{"role": "user", "content": "hi"}], None, settings5, stream=True)
    assert_true(payload5["stream"] is True, "2.5 stream=True")
    assert_true(payload5["stream_options"] == {"include_usage": True}, "2.5 stream_options 注入")

    # 2.6 stream=True + include_usage=None → 不注入 stream_options
    payload6 = c._build_payload([{"role": "user", "content": "hi"}], None, None, stream=True)
    assert_true(payload6["stream"] is True, "2.6 stream=True")
    assert_true("stream_options" not in payload6, "2.6 无 include_usage 时不注入 stream_options")

    # 2.7 ModelSettings None 字段不写入
    settings7 = ModelSettings(temperature=None, max_tokens=50)
    payload7 = c._build_payload([{"role": "user", "content": "hi"}], None, settings7, stream=False)
    assert_true("temperature" not in payload7, "2.7 temperature=None 不写入")
    assert_true(payload7["max_tokens"] == 50, "2.7 max_tokens=50 写入")

    async_run(c.aclose())


# ════════════════════════════════════════════════════
#  3. _build_headers / _build_url
# ════════════════════════════════════════════════════

def test_build_headers_url():
    """3. _build_headers / _build_url"""
    print("\n" + "═" * 60)
    print("3.  _build_headers / _build_url")
    print("═" * 60)

    c = OpenAICompatibleClient(api_key="my-key", model_name="m", base_url="https://api.test/v1")

    # 3.1 基础 headers
    headers = c._build_headers(None)
    assert_true(headers["Authorization"] == "Bearer my-key", "3.1 Authorization 基础格式")
    assert_true(headers["Content-Type"] == "application/json", "3.1 Content-Type")

    # 3.2 extra_headers 覆盖
    settings = ModelSettings(extra_headers={"X-Trace-Id": "123", "Authorization": "Bearer override"})
    headers2 = c._build_headers(settings)
    assert_true(headers2["X-Trace-Id"] == "123", "3.2 extra_headers 追加")
    assert_true(headers2["Authorization"] == "Bearer override", "3.2 extra_headers 覆盖 Authorization")

    # 3.3 基础 URL
    url = c._build_url(None)
    assert_true(url == "https://api.test/v1/chat/completions", "3.3 基础 URL 拼接")

    # 3.4 extra_query 拼接
    settings4 = ModelSettings(extra_query={"api-version": "2024-02-01", "debug": "1"})
    url4 = c._build_url(settings4)
    assert_true("api-version=2024-02-01" in url4, "3.4 extra_query 拼接 api-version")
    assert_true("debug=1" in url4, "3.4 extra_query 拼接 debug")

    async_run(c.aclose())


# ════════════════════════════════════════════════════
#  4. _merge_settings
# ════════════════════════════════════════════════════

def test_merge_settings():
    """4. _merge_settings — 合并语义"""
    print("\n" + "═" * 60)
    print("4.  _merge_settings — 合并语义")
    print("═" * 60)

    merge = OpenAICompatibleClient._merge_settings

    # 4.1 两者都 None → None
    assert_true(merge(None, None) is None, "4.1 双 None → None")

    # 4.2 base=None → 返回 override
    s = ModelSettings(temperature=0.5)
    result4 = merge(None, s)
    assert_true(result4.temperature == 0.5, "4.2 base=None 返回 override")

    # 4.3 override=None → 返回 base
    s3 = ModelSettings(temperature=0.7)
    result5 = merge(s3, None)
    assert_true(result5.temperature == 0.7, "4.3 override=None 返回 base")

    # 4.4 override 非 None 字段覆盖 base
    base = ModelSettings(temperature=0.5, max_tokens=100, top_p=0.9)
    override = ModelSettings(temperature=0.1, max_tokens=200)
    result6 = merge(base, override)
    assert_true(result6.temperature == 0.1, "4.4 override.temperature 覆盖")
    assert_true(result6.max_tokens == 200, "4.4 override.max_tokens 覆盖")
    assert_true(result6.top_p == 0.9, "4.4 base.top_p 保留")

    # 4.5 extra_body 合并
    base7 = ModelSettings(extra_body={"a": 1})
    override7 = ModelSettings(extra_body={"b": 2})
    result7 = merge(base7, override7)
    assert_true(result7.extra_body == {"b": 2}, "4.5 override extra_body 覆盖（不合并 dict）")


# ════════════════════════════════════════════════════
#  5. _extract_response
# ════════════════════════════════════════════════════

def test_extract_response():
    """5. _extract_response — 响应解析"""
    print("\n" + "═" * 60)
    print("5.  _extract_response — 响应解析")
    print("═" * 60)

    # _extract_response 已从 @staticmethod 改为实例方法（§15 L4 归一），
    # 需要通过实例调用。
    _client = OpenAICompatibleClient(
        api_key="test-key",
        model_name="test-model",
        base_url="http://localhost:8080/v1",
        cache=False,
    )
    _extract = _client._extract_response

    # 5.1 标准响应
    raw = _make_openai_response(content="你好世界", finish_reason="stop")
    resp = _extract(raw)
    assert_true(resp["content"] == "你好世界", "5.1 content 提取")
    assert_true(resp["finish_reason"] == "stop", "5.1 finish_reason")
    assert_true(resp["id"] == "chatcmpl-mock", "5.1 id")
    assert_true(resp["model"] == "mock-model", "5.1 model")
    assert_true(resp["usage"]["prompt_tokens"] == 10, "5.1 usage.prompt_tokens")

    # 5.2 tool_calls 提取
    tc = [{"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
    raw2 = _make_openai_response(tool_calls=tc, finish_reason="tool_calls", content=None)
    resp2 = _extract(raw2)
    assert_true(resp2.get("tool_calls") == tc, "5.2 tool_calls 提取")
    assert_true(resp2["finish_reason"] == "tool_calls", "5.2 finish_reason=tool_calls")

    # 5.3 reasoning_content 提取
    raw3 = _make_openai_response(reasoning_content="思考中...")
    resp3 = _extract(raw3)
    assert_true(resp3.get("reasoning_content") == "思考中...", "5.3 reasoning_content 提取")

    # 5.4 refusal 提取
    raw4 = _make_openai_response(refusal="我不能回答")
    resp4 = _extract(raw4)
    assert_true(resp4.get("refusal") == "我不能回答", "5.4 refusal 提取")

    # 5.5 choices 为空 → 优雅降级
    raw5 = {"id": "x", "object": "chat.completion", "created": 0, "model": "m", "choices": [], "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
    resp5 = _extract(raw5)
    assert_true(resp5["content"] is None, "5.5 choices=[] → content=None")
    assert_true(resp5["finish_reason"] is None, "5.5 choices=[] → finish_reason=None")

    # 5.6 usage 带 completion_tokens_details
    raw6 = _make_openai_response(usage={
        "prompt_tokens": 10,
        "completion_tokens": 500,
        "total_tokens": 510,
        "completion_tokens_details": {"reasoning_tokens": 400, "output_tokens": 100},
        "prompt_tokens_details": {"cached_tokens": 5},
    })
    resp6 = _extract(raw6)
    assert_true(resp6["usage"]["completion_tokens_details"]["reasoning_tokens"] == 400, "5.6 reasoning_tokens 提取")
    assert_true(resp6["usage"]["prompt_tokens_details"]["cached_tokens"] == 5, "5.6 cached_tokens 提取")

    # 5.7 无 usage → 默认 0
    raw7 = {"id": "x", "object": "chat.completion", "created": 0, "model": "m", "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}]}
    resp7 = _extract(raw7)
    assert_true(resp7["usage"]["prompt_tokens"] == 0, "5.7 无 usage → 默认 0")

    # 5.8 reasoning 字段兼容（部分第三方用 reasoning 而非 reasoning_content）
    raw8 = {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi", "reasoning": "think..."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
    resp8 = _extract(raw8)
    assert_true(resp8.get("reasoning_content") == "think...", "5.8 reasoning 字段兼容为 reasoning_content")


# ════════════════════════════════════════════════════
#  6. _parse_sse_line
# ════════════════════════════════════════════════════

def test_parse_sse_line():
    """6. _parse_sse_line — SSE 解析"""
    print("\n" + "═" * 60)
    print("6.  _parse_sse_line — SSE 解析")
    print("═" * 60)

    parse = OpenAICompatibleClient._parse_sse_line

    # 6.1 正常 JSON
    result1 = parse('data: {"id":"x","choices":[]}')
    assert_true(isinstance(result1, dict), "6.1 正常 JSON → dict")
    assert_true(result1["id"] == "x", "6.1 解析内容正确")

    # 6.2 [DONE] 信号
    result2 = parse("data: [DONE]")
    assert_true(isinstance(result2, _SseDone), "6.2 [DONE] → _SseDone 哨兵")

    # 6.3 心跳行
    result3 = parse(": ping")
    assert_true(result3 is None, "6.3 心跳行 → None")

    # 6.4 非 data 开头
    result4 = parse("event: message")
    assert_true(result4 is None, "6.4 非 data 行 → None")

    # 6.5 JSON 解析失败
    result5 = parse("data: {invalid json}")
    assert_true(result5 is None, "6.5 JSON 解析失败 → None")

    # 6.6 空行
    result6 = parse("")
    assert_true(result6 is None, "6.6 空行 → None")

    # 6.7 data 后带空格
    result7 = parse('data:  {"id":"y"}')
    assert_true(isinstance(result7, dict), "6.7 data 后多空格也能解析")


# ════════════════════════════════════════════════════
#  7. _classify_http_error
# ════════════════════════════════════════════════════

def test_classify_http_error():
    """7. _classify_http_error — HTTP 错误分类"""
    print("\n" + "═" * 60)
    print("7.  _classify_http_error — HTTP 错误分类")
    print("═" * 60)

    classify = OpenAICompatibleClient._classify_http_error

    # 7.1 401 → LLMAuthError
    err1 = classify(401, "Unauthorized", {})
    assert_true(isinstance(err1, LLMAuthError), "7.1 401 → LLMAuthError")
    assert_true(err1.status_code == 401, "7.1 status_code=401")

    # 7.2 403 → LLMAuthError
    err2 = classify(403, "Forbidden", {})
    assert_true(isinstance(err2, LLMAuthError), "7.2 403 → LLMAuthError")

    # 7.3 400 → LLMRequestError
    err3 = classify(400, "Bad Request", {})
    assert_true(isinstance(err3, LLMRequestError), "7.3 400 → LLMRequestError")

    # 7.4 408 → LLMTimeoutError
    err4 = classify(408, "Timeout", {})
    assert_true(isinstance(err4, LLMTimeoutError), "7.4 408 → LLMTimeoutError")

    # 7.5 429 → LLMRateLimitError + retry_after
    err5 = classify(429, "Rate Limited", {"retry-after": "5"})
    assert_true(isinstance(err5, LLMRateLimitError), "7.5 429 → LLMRateLimitError")
    assert_true(err5.retry_after == 5.0, "7.5 retry_after=5.0")

    # 7.6 429 + retry-after-ms
    err6 = classify(429, "Rate Limited", {"retry-after-ms": "3000"})
    assert_true(isinstance(err6, LLMRateLimitError), "7.6 429 → LLMRateLimitError")
    assert_true(err6.retry_after == 3.0, "7.6 retry-after-ms=3000 → 3.0s")

    # 7.7 429 无 Retry-After → retry_after=None
    err7 = classify(429, "Rate Limited", {})
    assert_true(err7.retry_after is None, "7.7 429 无 header → retry_after=None")

    # 7.8 500 → LLMServerError
    err8 = classify(500, "Internal Server Error", {})
    assert_true(isinstance(err8, LLMServerError), "7.8 500 → LLMServerError")

    # 7.9 502 → LLMServerError
    err9 = classify(502, "Bad Gateway", {})
    assert_true(isinstance(err9, LLMServerError), "7.9 502 → LLMServerError")

    # 7.10 404 → LLMServerError（兜底）
    err10 = classify(404, "Not Found", {})
    assert_true(isinstance(err10, LLMServerError), "7.10 404 → LLMServerError 兜底")

    # 7.11 所有错误都是 LLMError 子类
    for code in [400, 401, 403, 408, 429, 500, 502, 404]:
        err = classify(code, "err", {})
        assert_true(isinstance(err, LLMError), f"7.11 HTTP {code} → LLMError 子类")


# ════════════════════════════════════════════════════
#  8. _build_usage_info
# ════════════════════════════════════════════════════

def test_build_usage_info():
    """8. _build_usage_info — usage 构建"""
    print("\n" + "═" * 60)
    print("8.  _build_usage_info — usage 构建")
    print("═" * 60)

    # _build_usage_info 已从 @staticmethod 改为实例方法（§15 L4 归一），
    # 需要通过实例调用。创建一个无 capabilities 的实例来测试基础功能。
    _client = OpenAICompatibleClient(
        api_key="test-key",
        model_name="test-model",
        base_url="http://localhost:8080/v1",
        cache=False,
    )
    build = _client._build_usage_info

    # 8.1 基础 usage
    u1 = build({"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30})
    assert_true(u1["prompt_tokens"] == 10, "8.1 prompt_tokens")
    assert_true(u1["completion_tokens"] == 20, "8.1 completion_tokens")
    assert_true(u1["total_tokens"] == 30, "8.1 total_tokens")

    # 8.2 缺失字段默认 0
    u2 = build({})
    assert_true(u2["prompt_tokens"] == 0, "8.2 缺失 → 默认 0")

    # 8.3 completion_tokens_details
    u3 = build({
        "prompt_tokens": 1, "completion_tokens": 100, "total_tokens": 101,
        "completion_tokens_details": {"reasoning_tokens": 80, "output_tokens": 20},
    })
    assert_true(u3["completion_tokens_details"]["reasoning_tokens"] == 80, "8.3 reasoning_tokens")
    assert_true(u3["completion_tokens_details"]["output_tokens"] == 20, "8.3 output_tokens")

    # 8.4 prompt_tokens_details
    u4 = build({
        "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
        "prompt_tokens_details": {"cached_tokens": 8},
    })
    assert_true(u4["prompt_tokens_details"]["cached_tokens"] == 8, "8.4 cached_tokens")


# ════════════════════════════════════════════════════
#  9. call() — MockTransport 非流式
# ════════════════════════════════════════════════════

def test_call_mock():
    """9. call() — MockTransport 非流式调用"""
    print("\n" + "═" * 60)
    print("9.  call() — MockTransport 非流式")
    print("═" * 60)

    # 9.1 正常调用
    c = _make_mock_client(response_json=_make_openai_response(content="你好"))
    resp = async_run(c.call([{"role": "user", "content": "hi"}]))
    assert_true(resp["content"] == "你好", "9.1 正常调用 content 正确")
    assert_true(resp["finish_reason"] == "stop", "9.1 finish_reason=stop")
    async_run(c.aclose())

    # 9.2 带 tools
    tc = [{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"北京"}'}}]
    c2 = _make_mock_client(response_json=_make_openai_response(tool_calls=tc, finish_reason="tool_calls", content=None))
    resp2 = async_run(c2.call([{"role": "user", "content": "天气?"}], tools=[{"type": "function", "function": {"name": "get_weather"}}]))
    assert_true(resp2.get("tool_calls") is not None, "9.2 tool_calls 提取")
    assert_true(resp2["finish_reason"] == "tool_calls", "9.2 finish_reason=tool_calls")
    async_run(c2.aclose())

    # 9.3 HTTP 401 → LLMAuthError
    c3 = _make_mock_client(response_json={"error": "auth"}, status_code=401)
    try:
        async_run(c3.call([{"role": "user", "content": "hi"}]))
        result.fail("9.3 HTTP 401 → LLMAuthError", "未抛异常")
    except LLMAuthError:
        result.ok("9.3 HTTP 401 → LLMAuthError")
    async_run(c3.aclose())

    # 9.4 HTTP 429 → LLMRateLimitError
    c4 = _make_mock_client(response_json={"error": "rate"}, status_code=429)
    try:
        async_run(c4.call([{"role": "user", "content": "hi"}]))
        result.fail("9.4 HTTP 429 → LLMRateLimitError", "未抛异常")
    except LLMRateLimitError:
        result.ok("9.4 HTTP 429 → LLMRateLimitError")
    async_run(c4.aclose())

    # 9.5 HTTP 500 → LLMServerError
    c5 = _make_mock_client(response_json={"error": "server"}, status_code=500)
    try:
        async_run(c5.call([{"role": "user", "content": "hi"}]))
        result.fail("9.5 HTTP 500 → LLMServerError", "未抛异常")
    except LLMServerError:
        result.ok("9.5 HTTP 500 → LLMServerError")
    async_run(c5.aclose())

    # 9.6 超时 → LLMTimeoutError
    c6 = _make_mock_client(raise_on_post=httpx.TimeoutException("timeout"))
    try:
        async_run(c6.call([{"role": "user", "content": "hi"}]))
        result.fail("9.6 超时 → LLMTimeoutError", "未抛异常")
    except LLMTimeoutError:
        result.ok("9.6 超时 → LLMTimeoutError")
    async_run(c6.aclose())

    # 9.7 连接失败 → LLMNetworkError
    c7 = _make_mock_client(raise_on_post=httpx.ConnectError("connection failed"))
    try:
        async_run(c7.call([{"role": "user", "content": "hi"}]))
        result.fail("9.7 连接失败 → LLMNetworkError", "未抛异常")
    except LLMNetworkError:
        result.ok("9.7 连接失败 → LLMNetworkError")
    async_run(c7.aclose())

    # 9.8 JSON 解析失败 → LLMResponseError
    def _bad_json_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json", headers={"content-type": "application/json"})
    c8 = OpenAICompatibleClient(api_key="k", model_name="m", base_url="https://mock/v1")
    async_run(c8._http_client.aclose())
    c8._http_client = httpx.AsyncClient(transport=httpx.MockTransport(_bad_json_handler), timeout=5.0)
    try:
        async_run(c8.call([{"role": "user", "content": "hi"}]))
        result.fail("9.8 JSON 解析失败 → LLMResponseError", "未抛异常")
    except LLMResponseError:
        result.ok("9.8 JSON 解析失败 → LLMResponseError")
    async_run(c8.aclose())

    # 9.9 带 ModelSettings 透传
    c9 = _make_mock_client(response_json=_make_openai_response())
    resp9 = async_run(c9.call([{"role": "user", "content": "hi"}], settings=ModelSettings(temperature=0.0)))
    assert_true(resp9["content"] is not None, "9.9 带 settings 调用成功")
    async_run(c9.aclose())


# ════════════════════════════════════════════════════
#  10. stream_response() — MockTransport 流式
# ════════════════════════════════════════════════════

def test_stream_mock():
    """10. stream_response() — MockTransport 流式调用"""
    print("\n" + "═" * 60)
    print("10. stream_response() — MockTransport 流式")
    print("═" * 60)

    # 10.1 基础流式 content
    sse = _make_sse_chunks(content_parts=["你", "好", "世界"], finish_reason="stop")
    c = _make_mock_client(sse_text=sse)
    chunks: list[LLMStreamChunk] = []
    async def _collect():
        async for chunk in c.stream_response([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)
    async_run(_collect())

    content_parts = [ch.delta_content for ch in chunks if ch.delta_content]
    assert_true("".join(content_parts) == "你好世界", "10.1 流式 content 拼接正确")
    finish_chunks = [ch for ch in chunks if ch.finish_reason]
    assert_true(len(finish_chunks) == 1, "10.1 只有一个 finish_reason chunk")
    assert_true(finish_chunks[0].finish_reason == "stop", "10.1 finish_reason=stop")
    async_run(c.aclose())

    # 10.2 流式 tool_call_delta
    tc_deltas = [
        {"index": 0, "id": "call_1", "function": {"name": "get_weather", "arguments": '{"ci'}},
        {"index": 0, "id": "", "function": {"name": "", "arguments": 'ty":"北京"}'}},
    ]
    sse2 = _make_sse_chunks(tool_call_deltas=tc_deltas, finish_reason="tool_calls")
    c2 = _make_mock_client(sse_text=sse2)
    chunks2: list[LLMStreamChunk] = []
    async def _collect2():
        async for chunk in c2.stream_response([{"role": "user", "content": "天气?"}]):
            chunks2.append(chunk)
    async_run(_collect2())

    tc_chunks = [ch for ch in chunks2 if ch.tool_call_delta is not None]
    assert_true(len(tc_chunks) == 2, "10.2 收到 2 个 tool_call_delta")
    assert_true(tc_chunks[0].tool_call_delta["id"] == "call_1", "10.2 首帧 id 正确")
    assert_true(tc_chunks[0].tool_call_delta["name"] == "get_weather", "10.2 首帧 name 正确")
    assert_true(tc_chunks[1].tool_call_delta["arguments_delta"] == 'ty":"北京"}', "10.2 增量 arguments")
    async_run(c2.aclose())

    # 10.3 流式 reasoning_content
    sse3 = _make_sse_chunks(reasoning_parts=["思考", "中..."], content_parts=["答案"])
    c3 = _make_mock_client(sse_text=sse3)
    chunks3: list[LLMStreamChunk] = []
    async def _collect3():
        async for chunk in c3.stream_response([{"role": "user", "content": "hi"}]):
            chunks3.append(chunk)
    async_run(_collect3())

    reasoning = "".join(ch.delta_reasoning_content for ch in chunks3 if ch.delta_reasoning_content)
    assert_true(reasoning == "思考中...", "10.3 reasoning_content 拼接正确")
    async_run(c3.aclose())

    # 10.4 流式 refusal_delta
    sse4 = _make_sse_chunks(refusal_parts=["我不能回答"], content_parts=[""])
    c4 = _make_mock_client(sse_text=sse4)
    chunks4: list[LLMStreamChunk] = []
    async def _collect4():
        async for chunk in c4.stream_response([{"role": "user", "content": "hi"}]):
            chunks4.append(chunk)
    async_run(_collect4())

    refusal_text = "".join(ch.refusal_delta for ch in chunks4 if ch.refusal_delta)
    assert_true("我不能回答" in refusal_text, "10.4 refusal_delta 正确")
    async_run(c4.aclose())

    # 10.5 include_usage — usage 随终止 chunk 发出
    usage_data = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    sse5 = _make_sse_chunks(content_parts=["hi"], include_usage=True, usage=usage_data)
    c5 = _make_mock_client(sse_text=sse5)
    chunks5: list[LLMStreamChunk] = []
    async def _collect5():
        async for chunk in c5.stream_response([{"role": "user", "content": "hi"}], settings=ModelSettings(include_usage=True)):
            chunks5.append(chunk)
    async_run(_collect5())

    usage_chunks = [ch for ch in chunks5 if ch.usage is not None]
    assert_true(len(usage_chunks) >= 1, "10.5 至少 1 个 usage chunk")
    assert_true(usage_chunks[0].usage["total_tokens"] == 15, "10.5 usage.total_tokens 正确")
    async_run(c5.aclose())

    # 10.5b 回归：finish_reason chunk 在前、usage-only chunk 在后（DashScope/Qwen 的真实顺序）。
    #   历史 bug：finish chunk 先到时 pending_usage 尚为 None，若此刻就置 usage_flushed，
    #   末尾兜底 flush 会被跳过，导致后到的 usage 永远吐不出去（token 全 0）。
    #   现有 _make_sse_chunks 把 usage 放在 finish 之前/内联，抓不到本 bug，故手工构造。
    usage5b = {"prompt_tokens": 23, "completion_tokens": 403, "total_tokens": 426,
               "prompt_tokens_details": {"cached_tokens": 0}}
    sse5b = "\n\n".join([
        _make_sse_chunk({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}),
        _make_sse_chunk({"choices": [{"index": 0, "delta": {"content": "杭州很美"}, "finish_reason": None}]}),
        # 终止 chunk 在前，且不带 usage（usage=null）
        _make_sse_chunk({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": None}),
        # usage-only chunk 在后（choices=[]）
        _make_sse_chunk({"choices": [], "usage": usage5b}),
        "data: [DONE]",
    ]) + "\n\n"
    c5b = _make_mock_client(sse_text=sse5b)
    chunks5b: list[LLMStreamChunk] = []
    async def _collect5b():
        async for chunk in c5b.stream_response([{"role": "user", "content": "hi"}], settings=ModelSettings(include_usage=True)):
            chunks5b.append(chunk)
    async_run(_collect5b())
    usage5b_chunks = [ch for ch in chunks5b if ch.usage is not None]
    assert_true(len(usage5b_chunks) == 1, "10.5b 尾随 usage-only chunk 被兜底发出（恰好 1 次，不丢不重）")
    assert_true(usage5b_chunks[0].usage["prompt_tokens"] == 23, "10.5b usage.prompt_tokens 穿透正确")
    assert_true(any(ch.finish_reason == "stop" for ch in chunks5b), "10.5b finish_reason 仍正常发出")
    async_run(c5b.aclose())

    # 10.6 流式 HTTP 错误
    def _error_stream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})
    c6 = OpenAICompatibleClient(api_key="k", model_name="m", base_url="https://mock/v1")
    async_run(c6._http_client.aclose())
    c6._http_client = httpx.AsyncClient(transport=httpx.MockTransport(_error_stream_handler), timeout=5.0)
    try:
        async def _try_stream():
            async for _ in c6.stream_response([{"role": "user", "content": "hi"}]):
                pass
        async_run(_try_stream())
        result.fail("10.6 流式 HTTP 401 → LLMAuthError", "未抛异常")
    except LLMAuthError:
        result.ok("10.6 流式 HTTP 401 → LLMAuthError")
    async_run(c6.aclose())

    # 10.7 流式超时
    c7 = _make_mock_client(raise_on_post=httpx.TimeoutException("timeout"))
    try:
        async def _try_stream_timeout():
            async for _ in c7.stream_response([{"role": "user", "content": "hi"}]):
                pass
        async_run(_try_stream_timeout())
        result.fail("10.7 流式超时 → LLMTimeoutError", "未抛异常")
    except LLMTimeoutError:
        result.ok("10.7 流式超时 → LLMTimeoutError")
    async_run(c7.aclose())


# ════════════════════════════════════════════════════
#  11. aclose / context manager
# ════════════════════════════════════════════════════

def test_lifecycle():
    """11. aclose / async context manager"""
    print("\n" + "═" * 60)
    print("11. aclose / async context manager")
    print("═" * 60)

    # 11.1 aclose 关闭连接
    c = OpenAICompatibleClient(api_key="k", model_name="m", base_url="https://mock/v1")
    async_run(c.aclose())
    assert_true(c._http_client.is_closed, "11.1 aclose 后 http_client 已关闭")

    # 11.2 async context manager
    async def _use_ctx():
        async with OpenAICompatibleClient(api_key="k", model_name="m", base_url="https://mock/v1") as c2:
            assert_true(c2.model_name == "m", "11.2 __aenter__ 返回 self")
        assert_true(c2._http_client.is_closed, "11.2 __aexit__ 后已关闭")
    async_run(_use_ctx())


# ════════════════════════════════════════════════════
#  12. _resolve_response_format
# ════════════════════════════════════════════════════

def test_resolve_response_format():
    """12. _resolve_response_format"""
    print("\n" + "═" * 60)
    print("12. _resolve_response_format — 格式解析")
    print("═" * 60)

    resolve = OpenAICompatibleClient._resolve_response_format

    # 12.1 dict 原样返回
    d = {"type": "json_object"}
    assert_true(resolve(d) is d, "12.1 dict 原样返回")

    # 12.2 dataclass 类型自动转换
    @dataclass
    class UserInfo:
        name: str = field(metadata={"description": "姓名"})
        age: int = field(metadata={"description": "年龄"})

    result12 = resolve(UserInfo)
    assert_true(result12["type"] == "json_schema", "12.2 type=json_schema")
    assert_true(result12["json_schema"]["name"] == "UserInfo", "12.2 name=UserInfo")
    assert_true("name" in result12["json_schema"]["schema"]["properties"], "12.2 properties 含 name")


# ════════════════════════════════════════════════════
#  13. LLMRouter
# ════════════════════════════════════════════════════

def test_router():
    """13. LLMRouter — 路由器"""
    print("\n" + "═" * 60)
    print("13. LLMRouter — 路由器")
    print("═" * 60)

    # 构造 MockTransport client 工厂
    def _make_mock_client(tag: str) -> OpenAICompatibleClient:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "id": f"id-{tag}", "object": "chat.completion", "created": 0, "model": tag,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": f"from-{tag}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })
        c = OpenAICompatibleClient(api_key="k", model_name=tag, base_url="https://mock/v1")
        async_run(c._http_client.aclose())
        c._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
        return c

    # 13.1 精确匹配 > 前缀匹配
    c_default = _make_mock_client("default-model")
    c_prefix = _make_mock_client("gpt-family")
    c_exact = _make_mock_client("qwen-max")

    router = (
        LLMRouter()
        .register("gpt-", c_prefix)
        .register("qwen-max", c_exact)
        .set_default(c_default)
    )

    resp1 = async_run(router.call([{"role": "user", "content": "x"}], settings=ModelSettings(target_model="qwen-max")))
    assert_true(resp1["content"] == "from-qwen-max", "13.1 精确匹配 > 前缀")

    # 13.2 前缀匹配
    resp2 = async_run(router.call([{"role": "user", "content": "x"}], settings=ModelSettings(target_model="gpt-4o")))
    assert_true(resp2["content"] == "from-gpt-family", "13.2 前缀匹配 gpt-4o → gpt-family")

    # 13.3 无匹配 → default
    resp3 = async_run(router.call([{"role": "user", "content": "x"}], settings=ModelSettings(target_model="deepseek-v3")))
    assert_true(resp3["content"] == "from-default-model", "13.3 无匹配 → default")

    # 13.4 未指定 target_model → default
    resp4 = async_run(router.call([{"role": "user", "content": "x"}]))
    assert_true(resp4["content"] == "from-default-model", "13.4 无 target_model → default")

    # 13.5 model_name 属性
    assert_true(router.model_name == "default-model", "13.5 model_name 取 default.model_name")

    # 13.6 无 default + 无匹配 → LLMRequestError
    router2 = LLMRouter().register("only-*", c_prefix)
    try:
        async_run(router2.call([{"role": "user", "content": "x"}], settings=ModelSettings(target_model="other-x")))
        result.fail("13.6 无匹配 + 无 default → LLMRequestError", "未抛异常")
    except LLMRequestError:
        result.ok("13.6 无匹配 + 无 default → LLMRequestError")

    # 13.7 空路由键 → default/primary
    resp7 = async_run(router.call([{"role": "user", "content": "x"}], settings=ModelSettings()))
    assert_true(resp7["content"] == "from-default-model", "13.7 target_model=None → default")

    # 13.8 register key 为空 → ValueError
    @assert_raises(ValueError, "13.8 register key 为空 → ValueError")
    def _():
        LLMRouter().register("", c_default)

    # 13.9 register client 为 None → ValueError
    @assert_raises(ValueError, "13.9 register client=None → ValueError")
    def _():
        LLMRouter().register("key", None)  # type: ignore

    # 13.10 set_default client=None → ValueError
    @assert_raises(ValueError, "13.10 set_default client=None → ValueError")
    def _():
        LLMRouter().set_default(None)  # type: ignore

    # 13.11 流式路由
    sse = _make_sse_chunks(content_parts=["hi"], finish_reason="stop")

    def _stream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse.encode("utf-8"), headers={"content-type": "text/event-stream"})

    c_stream = OpenAICompatibleClient(api_key="k", model_name="stream-model", base_url="https://mock/v1")
    async_run(c_stream._http_client.aclose())
    c_stream._http_client = httpx.AsyncClient(transport=httpx.MockTransport(_stream_handler), timeout=5.0)

    router3 = LLMRouter().register("stream-", c_stream).set_default(c_stream)

    stream_chunks: list[LLMStreamChunk] = []
    async def _stream():
        async for chunk in router3.stream_response([{"role": "user", "content": "x"}], settings=ModelSettings(target_model="stream-test")):
            stream_chunks.append(chunk)
    async_run(_stream())
    content_text = "".join(ch.delta_content for ch in stream_chunks if ch.delta_content)
    assert_true(content_text == "hi", "13.11 流式路由正确")

    # 13.12 aclose 关闭所有 client
    router4 = LLMRouter().register("a", c_default).register("b-", c_prefix).set_default(c_exact)
    async_run(router4.aclose())
    # 不抛异常即通过
    assert_true(True, "13.12 aclose 关闭所有 client 不抛异常")

    # 13.13 最长前缀匹配
    c_short = _make_mock_client("gpt-short")
    c_long = _make_mock_client("gpt-4o-turbo")

    # 需要新建 clients（之前的已 close）
    c_short2 = _make_mock_client("gpt-short")
    c_long2 = _make_mock_client("gpt-4o-turbo")

    router5 = (
        LLMRouter()
        .register("gpt-", c_short2)
        .register("gpt-4o-", c_long2)
    )
    resp_long = async_run(router5.call(
        [{"role": "user", "content": "x"}],
        settings=ModelSettings(target_model="gpt-4o-turbo-preview"),
    ))
    assert_true(resp_long["content"] == "from-gpt-4o-turbo", "13.13 最长前缀 gpt-4o- 匹配")
    async_run(router5.aclose())

    # 13.14 LLMClient Protocol 兼容
    assert_true(isinstance(router, LLMClient), "13.14 LLMRouter 满足 LLMClient Protocol")


# ════════════════════════════════════════════════════
#  14. schema.py
# ════════════════════════════════════════════════════

# 嵌套 dataclass 用例（14.13）— 必须定义在模块顶层：
# typing.get_type_hints 只能解析模块 globals 中的类型名，
# 函数内部定义的 dataclass 若字段引用了仅存在于局部作用域的其他类型，
# PEP 563 模式下（`from __future__ import annotations`）无法还原。
@dataclass
class _NestedAddress:
    city: str = field(metadata={"description": "城市"})


@dataclass
class _NestedPerson:
    name: str
    address: _NestedAddress


def test_schema():
    """14. schema.py — JSON Schema 转换"""
    print("\n" + "═" * 60)
    print("14. schema.py — JSON Schema 转换")
    print("═" * 60)

    @dataclass
    class UserInfo:
        name: str = field(metadata={"description": "用户姓名"})
        age: int = field(metadata={"description": "用户年龄"})
        email: str | None = None

    # 14.1 json_schema 基础
    fmt = json_schema(UserInfo)
    assert_true(fmt["type"] == "json_schema", "14.1 type=json_schema")
    assert_true(fmt["json_schema"]["name"] == "UserInfo", "14.1 name=UserInfo")
    schema = fmt["json_schema"]["schema"]
    assert_true(schema["type"] == "object", "14.1 schema type=object")
    assert_true("name" in schema["properties"], "14.1 properties 含 name")
    assert_true("age" in schema["properties"], "14.1 properties 含 age")

    # 14.2 required 字段
    assert_true("name" in schema.get("required", []), "14.2 name 是 required")
    assert_true("age" in schema.get("required", []), "14.2 age 是 required")
    assert_true("email" not in schema.get("required", []), "14.2 email 非 required")

    # 14.3 description 注入
    assert_true(schema["properties"]["name"].get("description") == "用户姓名", "14.3 name description 正确")

    # 14.4 类型映射
    name_prop = schema["properties"]["name"]
    age_prop = schema["properties"]["age"]
    assert_true(name_prop.get("type") == "string", f"14.4 str → string (got {name_prop})")
    assert_true(age_prop.get("type") == "integer", f"14.4 int → integer (got {age_prop})")

    # 14.5 Optional 字段 nullable
    assert_true(schema["properties"]["email"].get("nullable") is True, "14.5 Optional 字段 nullable=True")

    # 14.6 strict 模式
    fmt_strict = json_schema(UserInfo, strict=True)
    strict_schema = fmt_strict["json_schema"]["schema"]
    assert_true(strict_schema.get("additionalProperties") is False, "14.6 strict → additionalProperties=False")
    assert_true("email" in strict_schema.get("required", []), "14.6 strict → 全部 required")

    # 14.7 自定义 name
    fmt_name = json_schema(UserInfo, name="CustomName")
    assert_true(fmt_name["json_schema"]["name"] == "CustomName", "14.7 自定义 name")

    # 14.8 非 dataclass → TypeError
    @assert_raises(TypeError, "14.8 非 dataclass → TypeError")
    def _():
        json_schema(str)

    # 14.9 output_type_to_response_format — dataclass
    fmt_dc = output_type_to_response_format(UserInfo)
    assert_true(fmt_dc["type"] == "json_schema", "14.9 output_type dataclass → json_schema")

    # 14.10 _validate_output_type — None → TypeError
    @assert_raises(TypeError, "14.10 output_type=None → TypeError")
    def _():
        output_type_to_response_format(None)  # type: ignore

    # 14.11 _validate_output_type — str → TypeError
    @assert_raises(TypeError, "14.11 output_type=str → TypeError")
    def _():
        output_type_to_response_format(str)

    # 14.12 _validate_output_type — 非类型 → TypeError
    @assert_raises(TypeError, "14.12 output_type=实例 → TypeError")
    def _():
        output_type_to_response_format("not a type")  # type: ignore

    # 14.13 嵌套 dataclass（类型定义在模块顶层，见文件上方 _NestedAddress / _NestedPerson）
    fmt_nested = json_schema(_NestedPerson)
    nested_props = fmt_nested["json_schema"]["schema"]["properties"]
    assert_true("address" in nested_props, "14.13 嵌套 dataclass 包含 address 字段")
    assert_true(nested_props["address"]["type"] == "object", "14.13 嵌套字段 type=object")

    # 14.14 list 类型
    @dataclass
    class Team:
        members: list[str]

    fmt_list = json_schema(Team)
    list_props = fmt_list["json_schema"]["schema"]["properties"]
    assert_true(list_props["members"]["type"] == "array", "14.14 list → array")
    assert_true(list_props["members"]["items"]["type"] == "string", "14.14 list[str] items.type=string")

    # 14.15 Pydantic 支持（如果安装了 pydantic）
    try:
        from pydantic import BaseModel, Field

        class PydanticUser(BaseModel):
            name: str = Field(description="姓名")
            age: int = Field(description="年龄")

        fmt_py = output_type_to_response_format(PydanticUser)
        assert_true(fmt_py["type"] == "json_schema", "14.15 Pydantic → json_schema")
        assert_true(fmt_py["json_schema"]["name"] == "PydanticUser", "14.15 name=PydanticUser")
    except ImportError:
        result.ok("14.15 Pydantic 未安装，跳过")


# ════════════════════════════════════════════════════
#  15. exceptions.py
# ════════════════════════════════════════════════════

def test_exceptions():
    """15. exceptions.py — 异常层次"""
    print("\n" + "═" * 60)
    print("15. exceptions.py — 异常层次")
    print("═" * 60)

    # 15.1 继承关系
    assert_true(issubclass(LLMAuthError, LLMError), "15.1 LLMAuthError < LLMError")
    assert_true(issubclass(LLMRequestError, LLMError), "15.1 LLMRequestError < LLMError")
    assert_true(issubclass(LLMRateLimitError, LLMError), "15.1 LLMRateLimitError < LLMError")
    assert_true(issubclass(LLMServerError, LLMError), "15.1 LLMServerError < LLMError")
    assert_true(issubclass(LLMNetworkError, LLMError), "15.1 LLMNetworkError < LLMError")
    assert_true(issubclass(LLMTimeoutError, LLMNetworkError), "15.1 LLMTimeoutError < LLMNetworkError")
    assert_true(issubclass(LLMResponseError, LLMError), "15.1 LLMResponseError < LLMError")

    # 15.2 LLMRateLimitError retry_after
    err = LLMRateLimitError("rate limited", retry_after=5.0)
    assert_true(err.retry_after == 5.0, "15.2 retry_after 正确")

    err_none = LLMRateLimitError("rate limited")
    assert_true(err_none.retry_after is None, "15.2 默认 retry_after=None")

    # 15.3 LLMAuthError status_code
    auth_err = LLMAuthError("forbidden", status_code=403)
    assert_true(auth_err.status_code == 403, "15.3 status_code 正确")

    # 15.4 LLMServerError status_code
    server_err = LLMServerError("internal", status_code=500)
    assert_true(server_err.status_code == 500, "15.4 status_code 正确")

    # 15.5 LLMTimeoutError 可被 LLMNetworkError 捕获
    try:
        raise LLMTimeoutError("timeout")
    except LLMNetworkError:
        assert_true(True, "15.5 LLMTimeoutError 被 LLMNetworkError 捕获")
    except Exception:
        result.fail("15.5 LLMTimeoutError 应被 LLMNetworkError 捕获")


# ════════════════════════════════════════════════════
#  16. types.py
# ════════════════════════════════════════════════════

def test_types():
    """16. types.py — 数据类型"""
    print("\n" + "═" * 60)
    print("16. types.py — 数据类型")
    print("═" * 60)

    # 16.1 ModelSettings 默认全 None
    ms = ModelSettings()
    assert_true(ms.temperature is None, "16.1 temperature 默认 None")
    assert_true(ms.max_tokens is None, "16.1 max_tokens 默认 None")
    assert_true(ms.top_p is None, "16.1 top_p 默认 None")
    assert_true(ms.frequency_penalty is None, "16.1 frequency_penalty 默认 None")
    assert_true(ms.presence_penalty is None, "16.1 presence_penalty 默认 None")
    assert_true(ms.stop is None, "16.1 stop 默认 None")
    assert_true(ms.seed is None, "16.1 seed 默认 None")
    assert_true(ms.response_format is None, "16.1 response_format 默认 None")
    assert_true(ms.tool_choice is None, "16.1 tool_choice 默认 None")
    assert_true(ms.parallel_tool_calls is None, "16.1 parallel_tool_calls 默认 None")
    assert_true(ms.include_usage is None, "16.1 include_usage 默认 None")
    assert_true(ms.reasoning is None, "16.1 reasoning 默认 None")
    assert_true(ms.target_model is None, "16.1 target_model 默认 None")
    assert_true(ms.extra_body is None, "16.1 extra_body 默认 None")
    assert_true(ms.extra_headers is None, "16.1 extra_headers 默认 None")
    assert_true(ms.extra_query is None, "16.1 extra_query 默认 None")

    # 16.2 LLMStreamChunk
    chunk = LLMStreamChunk(delta_content="hello")
    assert_true(chunk.delta_content == "hello", "16.2 delta_content 正确")
    assert_true(chunk.delta_reasoning_content is None, "16.2 delta_reasoning_content 默认 None")
    assert_true(chunk.refusal_delta is None, "16.2 refusal_delta 默认 None")
    assert_true(chunk.tool_call_delta is None, "16.2 tool_call_delta 默认 None")
    assert_true(chunk.finish_reason is None, "16.2 finish_reason 默认 None")
    assert_true(chunk.usage is None, "16.2 usage 默认 None")

    # 16.3 LLMStreamChunk tool_call_delta
    tc: ToolCallDelta = {"index": 0, "id": "call_1", "name": "f", "arguments_delta": "{}"}
    chunk2 = LLMStreamChunk(tool_call_delta=tc)
    assert_true(chunk2.tool_call_delta["index"] == 0, "16.3 tool_call_delta index")
    assert_true(chunk2.tool_call_delta["id"] == "call_1", "16.3 tool_call_delta id")

    # 16.4 LLMResponse 必选字段
    resp: LLMResponse = {
        "content": "hi",
        "finish_reason": "stop",
        "usage": UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        "id": "id-1",
        "model": "m",
        "created": 0,
    }
    assert_true(resp["content"] == "hi", "16.4 LLMResponse content")
    assert_true(resp["usage"]["total_tokens"] == 2, "16.4 LLMResponse usage")

    # 16.5 UsageInfo 带 details
    u = UsageInfo(
        prompt_tokens=10, completion_tokens=100, total_tokens=110,
        completion_tokens_details=CompletionTokensDetails(reasoning_tokens=80, output_tokens=20),
        prompt_tokens_details=PromptTokensDetails(cached_tokens=5),
    )
    assert_true(u["completion_tokens_details"]["reasoning_tokens"] == 80, "16.5 reasoning_tokens")
    assert_true(u["prompt_tokens_details"]["cached_tokens"] == 5, "16.5 cached_tokens")

    # 16.6 ModelSettings 全参数构造
    ms_full = ModelSettings(
        temperature=0.5,
        max_tokens=100,
        top_p=0.9,
        frequency_penalty=0.1,
        presence_penalty=0.2,
        stop=["END"],
        seed=42,
        response_format={"type": "json_object"},
        tool_choice="auto",
        parallel_tool_calls=True,
        include_usage=True,
        reasoning={"effort": "low"},
        target_model="qwen-max",
        extra_body={"enable_thinking": True},
        extra_headers={"X-Custom": "value"},
        extra_query={"api-version": "2024-01"},
    )
    assert_true(ms_full.temperature == 0.5, "16.6 全参数构造 temperature")
    assert_true(ms_full.target_model == "qwen-max", "16.6 全参数构造 target_model")
    assert_true(ms_full.extra_body == {"enable_thinking": True}, "16.6 全参数构造 extra_body")


# ════════════════════════════════════════════════════
#  17. 默认 settings 合并 + call 全链路
# ════════════════════════════════════════════════════

def test_default_settings_merge():
    """17. default_settings + call 时 settings 合并"""
    print("\n" + "═" * 60)
    print("17. default_settings + call 合并")
    print("═" * 60)

    # 17.1 构造时指定 default_settings，call 时 override
    c = OpenAICompatibleClient(
        api_key="k",
        model_name="m",
        base_url="https://mock/v1",
        default_settings=ModelSettings(temperature=0.5, max_tokens=100),
    )
    merged = c._merge_settings(c._default_settings, ModelSettings(temperature=0.1))
    assert_true(merged.temperature == 0.1, "17.1 override temperature 生效")
    assert_true(merged.max_tokens == 100, "17.1 base max_tokens 保留")
    async_run(c.aclose())

    # 17.2 default_settings 不传 → None
    c2 = OpenAICompatibleClient(api_key="k", model_name="m", base_url="https://mock/v1")
    assert_true(c2._default_settings is None, "17.2 默认 default_settings=None")
    async_run(c2.aclose())


# ════════════════════════════════════════════════════
#  18. LLMClient Protocol 验证
# ════════════════════════════════════════════════════

def test_protocol():
    """18. LLMClient Protocol 验证"""
    print("\n" + "═" * 60)
    print("18. LLMClient Protocol 验证")
    print("═" * 60)

    # 18.1 OpenAICompatibleClient 满足 LLMClient
    c = OpenAICompatibleClient(api_key="k", model_name="m", base_url="https://mock/v1")
    assert_true(isinstance(c, LLMClient), "18.1 OpenAICompatibleClient 满足 LLMClient Protocol")
    async_run(c.aclose())

    # 18.2 LLMRouter 满足 LLMClient
    router = LLMRouter().set_default(c)
    assert_true(isinstance(router, LLMClient), "18.2 LLMRouter 满足 LLMClient Protocol")
    # router 已持有 c，aclose 会关掉 c
    # c 已经 close 了，但 router.aclose 会 catch 异常
    async_run(router.aclose())


# ════════════════════════════════════════════════════
#  测试分区表 & 主入口
# ════════════════════════════════════════════════════

SECTIONS: dict[str, list] = {
    "client": [
        test_client_construction,
        test_factory_and_capabilities,
        test_build_payload,
        test_build_headers_url,
        test_merge_settings,
        test_extract_response,
        test_parse_sse_line,
        test_classify_http_error,
        test_build_usage_info,
        test_call_mock,
        test_stream_mock,
        test_lifecycle,
        test_resolve_response_format,
        test_default_settings_merge,
    ],
    "router": [
        test_router,
    ],
    "schema": [
        test_schema,
    ],
    "exceptions": [
        test_exceptions,
    ],
    "types": [
        test_types,
    ],
    "protocol": [
        test_protocol,
    ],
}

ALL_TESTS = [fn for fns in SECTIONS.values() for fn in fns]


def main():
    import argparse
    import logging

    parser = argparse.ArgumentParser(description="pandaren/llm 模块 Mock 测试")
    parser.add_argument(
        "--section",
        choices=list(SECTIONS.keys()),
        default="",
        help="仅运行指定测试分区 (client/router/schema/exceptions/types/protocol)",
    )
    args = parser.parse_args()

    print("🐼 Pandaren Agent SDK — LLM 层 Mock 测试")
    print("   目标模块: pandaren/llm/ (client, router, schema, exceptions, types)")
    print("   测试方式: httpx.MockTransport + unittest.mock")
    print()

    logging.getLogger("pandaren.llm").setLevel(logging.ERROR)

    to_run = SECTIONS[args.section] if args.section else ALL_TESTS
    for fn in to_run:
        fn()

    result.summary(args.section or "全部")

    if result.failed > 0:
        print("\n⚠️  存在失败的测试用例，请检查上方输出")
        sys.exit(1)
    else:
        total = result.passed + result.failed
        print(f"\n🎉 所有 {total} 个测试通过！LLM 层 Mock 测试验证完毕")
        sys.exit(0)


if __name__ == "__main__":
    main()
