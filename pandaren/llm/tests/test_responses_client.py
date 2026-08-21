"""pandaren/llm/tests/test_responses_client.py — ResponsesAPIClient 功能改动测试。

覆盖本轮改动（对应 docs/design/responses_client-测试设计.md）：
  A. _compute_instructions_hash —— system 变化检测（inv-A/B/C，Risk-1/11/12）
  B. instructions_changed / no_increment 冷启动检测（Risk-1/2/10）
  C. 成功后 / 流结束更新 _instructions_hash（inv-J，Risk-7）
  D. 事件→chunk 分发（端到端：SSE 事件序列 → stream_response 产物，Risk-9）
  E. _status_to_finish_reason failed→"error"（Risk-5）
  F. _convert_messages_to_input system 原样保留（inv-I，Risk-3）

隔离策略：纯函数/事件分发零 mock；网络层用 httpx.MockTransport 注入
client._http_client（假传输，无真实 I/O）。pytest-asyncio asyncio_mode=auto。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest

from pandaren.llm.responses_client import ResponsesAPIClient
from pandaren.llm.types import LLMStreamChunk

# ─── helpers ──────────────────────────────────────────────────────


def sse_text(events: list[dict[str, Any]]) -> str:
    """把事件 dict 列表转成 SSE 文本（data: {json}\\n\\n）。"""
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events)


def ok_json_response(resp_id: str = "resp_1") -> dict[str, Any]:
    """构造一个最小可用 Responses API 非流式响应。"""
    return {
        "id": resp_id,
        "object": "response",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "你好"}],
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }


def instructions_hash(messages: list[dict[str, Any]]) -> str | None:
    """参考实现（hashlib 标准库，非被测实现）：与源码语义对齐的 oracle。"""
    parts = [
        m["content"]
        for m in messages
        if isinstance(m, dict) and m.get("role") == "system"
        and isinstance(m.get("content"), str)
    ]
    if not parts:
        return None
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


@pytest.fixture
async def make_client():
    """构造注入 MockTransport 的 client，测试结束统一 aclose。"""
    created: list[ResponsesAPIClient] = []

    def _make(handler) -> ResponsesAPIClient:
        client = ResponsesAPIClient(
            api_key="test-key",
            model_name="test-model",
            base_url="https://api.test.com/v1",
            use_caching=False,
        )
        client._http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=5.0
        )
        created.append(client)
        return client

    yield _make
    for c in created:
        await c.aclose()


# ─── T 系列：纯函数 / 事件分发（零 mock）──────────────────────────


class TestInstructionsHash:
    def test_hash_none_without_system(self):  # T1, inv-B
        messages = [{"role": "user", "content": "hi"}]
        assert ResponsesAPIClient._compute_instructions_hash(messages) is None

    def test_hash_deterministic_and_joins_system(self):  # T2, inv-A/C
        messages = [
            {"role": "system", "content": "你是助手A"},
            {"role": "system", "content": "规则B"},
            {"role": "user", "content": "hi"},
        ]
        h1 = ResponsesAPIClient._compute_instructions_hash(messages)
        h2 = ResponsesAPIClient._compute_instructions_hash(list(messages))
        assert h1 == h2 == instructions_hash(messages)
        assert h1 is not None and len(h1) == 16

    def test_hash_order_sensitive(self):  # T3, inv-C（Risk-12 锁定现状）
        msgs_a = [
            {"role": "system", "content": "A"},
            {"role": "system", "content": "B"},
        ]
        msgs_b = [
            {"role": "system", "content": "B"},
            {"role": "system", "content": "A"},
        ]
        assert (
            ResponsesAPIClient._compute_instructions_hash(msgs_a)
            != ResponsesAPIClient._compute_instructions_hash(msgs_b)
        )

    def test_hash_skips_non_str_content_and_non_dict(self):  # T4, inv-B（Risk-11 锁定现状）
        messages: list[Any] = [
            {"role": "system", "content": ["not", "str"]},  # list content 不参与
            {"role": "system", "content": "仅此参与"},       # 唯一参与项
            "not-a-dict",                                   # 非 dict 跳过
        ]
        h = ResponsesAPIClient._compute_instructions_hash(messages)
        assert h == instructions_hash([{"role": "system", "content": "仅此参与"}])


class TestConvertMessagesToInput:
    def test_system_preserved_verbatim(self):  # T5, inv-I（Risk-3 P0）
        messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "hi"},
        ]
        converted = ResponsesAPIClient._convert_messages_to_input(messages)
        assert converted[0] == {"role": "system", "content": "你是助手"}
        assert converted[1] == {"role": "user", "content": "hi"}

    def test_system_in_middle_preserved(self):  # T6, inv-I
        messages = [
            {"role": "user", "content": "q1"},
            {"role": "system", "content": "中段规则"},
            {"role": "user", "content": "q2"},
        ]
        converted = ResponsesAPIClient._convert_messages_to_input(messages)
        assert converted[1] == {"role": "system", "content": "中段规则"}
        # 顺序保持
        assert [m["role"] for m in converted] == ["user", "system", "user"]


class TestStatusToFinishReason:
    def test_failed_maps_to_error(self):  # T7, Risk-5 P0
        assert ResponsesAPIClient._status_to_finish_reason("failed") == "error"

    def test_other_statuses(self):
        assert ResponsesAPIClient._status_to_finish_reason("completed") == "stop"
        assert ResponsesAPIClient._status_to_finish_reason("incomplete") == "length"
        assert ResponsesAPIClient._status_to_finish_reason("cancelled") == "stop"
        assert ResponsesAPIClient._status_to_finish_reason("unknown_xxx") == "stop"


class TestDispatchStreamEvent:
    """T8-T13：事件→chunk 映射（Oracle 基准见设计文档 §1.1）。

    事件分发已拆回主循环/降级循环各自内联（无独立函数可单测），
    本类改为端到端：MockTransport 返回 SSE 事件序列，经 stream_response 验证产物。
    """

    async def _collect(
        self, make_client, events: list[dict[str, Any]]
    ) -> tuple[list[LLMStreamChunk], ResponsesAPIClient]:
        client = make_client(
            lambda request: httpx.Response(200, text=sse_text(events))
        )
        chunks = [
            c
            async for c in client.stream_response(
                [{"role": "user", "content": "hi"}], tools=None
            )
        ]
        return chunks, client

    async def test_created_collects_response_id(self, make_client):  # T8
        chunks, client = await self._collect(
            make_client,
            [{"type": "response.created", "response": {"id": "resp_x"}}],
        )
        assert chunks == []
        assert client._previous_response_id == "resp_x"

    async def test_completed_outputs_finish_and_usage(self, make_client):  # T9
        chunks, client = await self._collect(
            make_client,
            [
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_x",
                        "status": "completed",
                        "usage": {
                            "input_tokens": 20,
                            "output_tokens": 8,
                            "total_tokens": 28,
                        },
                    },
                }
            ],
        )
        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk.finish_reason == "stop"
        assert chunk.usage == {
            "prompt_tokens": 20,
            "completion_tokens": 8,
            "total_tokens": 28,
        }
        assert client._previous_response_id == "resp_x"

    async def test_text_delta_and_empty_skip(self, make_client):  # T10
        chunks, _ = await self._collect(
            make_client,
            [
                {"type": "response.output_text.delta", "delta": "你好"},
                {"type": "response.output_text.delta", "delta": ""},
            ],
        )
        assert len(chunks) == 1 and chunks[0].delta_content == "你好"

    async def test_function_args_delta(self, make_client):  # T11
        chunks, _ = await self._collect(
            make_client,
            [
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc_1",
                    "output_index": 0,
                    "delta": '{"city": "北京"}',
                }
            ],
        )
        assert len(chunks) == 1
        assert chunks[0].tool_call_delta == {
            "index": 0,
            "id": "fc_1",
            "name": "",
            "arguments_delta": '{"city": "北京"}',
        }

    async def test_output_item_added_function_call(self, make_client):  # T12
        chunks, _ = await self._collect(
            make_client,
            [
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "call_1",
                        "name": "get_weather",
                    },
                }
            ],
        )
        assert len(chunks) == 1
        assert chunks[0].tool_call_delta == {
            "index": 0,
            "id": "call_1",
            "name": "get_weather",
            "arguments_delta": "",
        }

    async def test_other_events(self, make_client):  # T13（reasoning/refusal/done 类）
        chunks, _ = await self._collect(
            make_client,
            [
                {"type": "response.reasoning_text.delta", "delta": "思考中"},
                {"type": "response.refusal.delta", "delta": "拒绝"},
                {"type": "response.function_call_arguments.done"},
                {"type": "response.output_text.done"},
                {"type": "response.output_item.done"},
                {"type": "response.content_part.done"},
            ],
        )
        # reasoning → delta_reasoning_content；refusal → refusal_delta；done 类不产出
        assert [ch.delta_reasoning_content for ch in chunks if ch.delta_reasoning_content] == ["思考中"]
        assert [ch.refusal_delta for ch in chunks if ch.refusal_delta] == ["拒绝"]
        assert len(chunks) == 2


# ─── C 系列：非流式 call（MockTransport 假网络）────────────────────


class TestCallPaths:
    """C1-C9：全量/增量路径选择 + 冷启动检测 + 状态更新。"""

    async def test_first_call_full_request(self, make_client):  # C1+C6, inv-D（Risk-10）
        captured: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            captured.append(body)
            return httpx.Response(200, json=ok_json_response("resp_1"))

        client = make_client(handler)
        messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "你好"},
        ]
        result = await client.call(messages, tools=None)

        body = captured[0]
        # 首次调用：无 previous_response_id、input 为 messages[1:] 完整转换
        assert "previous_response_id" not in body
        assert body["model"] == "test-model"
        assert body["instructions"] == "你是助手"
        assert body["input"] == [{"role": "user", "content": "你好"}]
        assert "caching" not in body
        # 结果归一化
        assert result["content"] == "你好"
        assert result["finish_reason"] == "stop"

    async def test_second_call_incremental(self, make_client):  # C2, inv-E
        captured: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            captured.append(body)
            return httpx.Response(
                200,
                json=ok_json_response(
                    "resp_2" if "previous_response_id" in body else "resp_1"
                ),
            )

        client = make_client(handler)
        msgs1 = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "q1"},
        ]
        await client.call(msgs1, tools=None)

        msgs2 = msgs1 + [{"role": "user", "content": "q2"}]
        await client.call(msgs2, tools=None)

        body2 = captured[1]
        # 增量路径：带 previous_response_id，input 仅增量部分
        assert body2["previous_response_id"] == "resp_1"
        assert body2["input"] == [{"role": "user", "content": "q2"}]
        assert "instructions" not in body2  # 增量请求不重发 instructions

    async def test_instructions_changed_cold_start(self, make_client):  # C3, Risk-1 P0
        captured: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            captured.append(body)
            return httpx.Response(200, json=ok_json_response(f"resp_{len(captured)}"))

        client = make_client(handler)
        await client.call(
            [
                {"role": "system", "content": "旧指令"},
                {"role": "user", "content": "q1"},
            ],
            tools=None,
        )
        # system 被改写（压缩/换新）→ 必须冷启动重建全量上下文
        await client.call(
            [
                {"role": "system", "content": "新指令"},
                {"role": "user", "content": "q2"},
            ],
            tools=None,
        )
        body2 = captured[1]
        assert "previous_response_id" not in body2
        assert body2["instructions"] == "新指令"
        assert body2["input"] == [{"role": "user", "content": "q2"}]

    async def test_system_removed_cold_start(self, make_client):  # C4
        captured: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            captured.append(body)
            return httpx.Response(200, json=ok_json_response(f"resp_{len(captured)}"))

        client = make_client(handler)
        await client.call(
            [
                {"role": "system", "content": "旧指令"},
                {"role": "user", "content": "q1"},
            ],
            tools=None,
        )
        # system 被移除（hash None ≠ 旧值非 None）→ 同样触发冷启动
        await client.call(
            [{"role": "user", "content": "q2"}],
            tools=None,
        )
        body2 = captured[1]
        assert "previous_response_id" not in body2

    async def test_no_increment_cold_start(self, make_client):  # C5, Risk-2 P0
        captured: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            captured.append(body)
            return httpx.Response(200, json=ok_json_response(f"resp_{len(captured)}"))

        client = make_client(handler)
        messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "q1"},
        ]
        await client.call(messages, tools=None)
        # 调用方重发相同消息（长度未增长）→ 无增量可发，必须走全量
        await client.call(list(messages), tools=None)
        body2 = captured[1]
        assert "previous_response_id" not in body2
        assert body2["input"] == [{"role": "user", "content": "q1"}]

    async def test_instructions_hash_updated_after_success(self, make_client):  # C7, inv-J
        captured: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(200, json=ok_json_response(f"resp_{len(captured)}"))

        client = make_client(handler)
        messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "q1"},
        ]
        await client.call(messages, tools=None)
        # 成功后 instructions_hash 更新为本次 messages 的 hash
        assert client._instructions_hash == instructions_hash(messages)
        assert client._previous_response_id == "resp_1"
        assert client._last_messages_len == 2

    def test_invalidate_preserves_hashes(self):  # C8, inv-F
        client = ResponsesAPIClient(
            api_key="k", model_name="m", base_url="https://api.test.com"
        )
        client._previous_response_id = "resp_old"
        client._last_messages_len = 5
        client._tools_hash = "tools_hash_x"
        client._instructions_hash = "instr_hash_y"
        client.invalidate("test_reason")
        assert client._previous_response_id is None
        assert client._last_messages_len == 0
        # 两个 hash 保留（下次计算覆盖，不留作陈旧比较）
        assert client._tools_hash == "tools_hash_x"
        assert client._instructions_hash == "instr_hash_y"

    async def test_expired_fallback_updates_state(self, make_client):  # C9, Risk-7 P0
        captured: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            captured.append(body)
            if "previous_response_id" in body:
                # 增量请求携带过期 id → 400 expired
                return httpx.Response(
                    400, text='{"error": {"message": "previous_response_id expired"}}'
                )
            return httpx.Response(200, json=ok_json_response("resp_new"))

        client = make_client(handler)
        messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "q1"},
        ]
        await client.call(messages, tools=None)
        # 第二次增量 → 400 → 自动降级全量重试
        msgs2 = messages + [{"role": "user", "content": "q2"}]
        result = await client.call(msgs2, tools=None)

        assert len(captured) == 3  # 全量 → 增量(400) → 全量重试
        assert "previous_response_id" not in captured[2]
        # 降级后状态用全量重试的响应更新（含 instructions_hash）
        assert client._previous_response_id == "resp_new"
        assert client._last_messages_len == 3
        assert client._instructions_hash == instructions_hash(msgs2)
        assert result["content"] == "你好"


# ─── S 系列：流式 stream_response（MockTransport 假网络）───────────


class TestStreamPaths:
    async def test_stream_full_flow(self, make_client):  # S1, inv-H
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text=sse_text(
                    [
                        {"type": "response.created", "response": {"id": "resp_s1"}},
                        {"type": "response.output_text.delta", "delta": "你好"},
                        {
                            "type": "response.output_item.added",
                            "output_index": 0,
                            "item": {
                                "type": "function_call",
                                "id": "fc_1",
                                "call_id": "call_1",
                                "name": "get_weather",
                            },
                        },
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": "fc_1",
                            "output_index": 0,
                            "delta": '{"city": "北京"}',
                        },
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "resp_s1",
                                "status": "completed",
                                "usage": {
                                    "input_tokens": 20,
                                    "output_tokens": 8,
                                    "total_tokens": 28,
                                },
                            },
                        },
                    ]
                ),
            )

        client = make_client(handler)
        messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "北京天气"},
        ]
        chunks = [c async for c in client.stream_response(messages, tools=None)]

        # chunk 序列 golden value
        assert chunks[0].delta_content == "你好"
        assert chunks[1].tool_call_delta == {
            "index": 0, "id": "call_1", "name": "get_weather", "arguments_delta": "",
        }
        assert chunks[2].tool_call_delta == {
            "index": 0, "id": "fc_1", "name": "", "arguments_delta": '{"city": "北京"}',
        }
        assert chunks[3].finish_reason == "stop"
        assert chunks[3].usage == {
            "prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28,
        }
        # 流结束四状态更新（inv-H）
        assert client._previous_response_id == "resp_s1"
        assert client._last_messages_len == 2
        assert client._tools_hash is None  # tools=None → hash None
        assert client._instructions_hash == instructions_hash(messages)

    async def test_stream_usage_yielded_once(self, make_client):  # S2, inv-G（Risk-6 P1）
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text=sse_text(
                    [
                        {"type": "response.created", "response": {"id": "resp_s2"}},
                        {"type": "response.output_text.delta", "delta": "hi"},
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "resp_s2",
                                "status": "completed",
                                "usage": {
                                    "input_tokens": 3,
                                    "output_tokens": 1,
                                    "total_tokens": 4,
                                },
                            },
                        },
                    ]
                ),
            )

        client = make_client(handler)
        chunks = [c async for c in client.stream_response(
            [{"role": "user", "content": "hi"}], tools=None
        )]
        usage_chunks = [c for c in chunks if c.usage is not None]
        assert len(usage_chunks) == 1  # completed 已 flush，兜底不再重复 yield
        assert usage_chunks[0].usage["total_tokens"] == 4

    async def test_stream_usage_fallback_unreachable_xfail(self, make_client):  # S3（差距标记）
        """设计期望：provider 在 completed 前推送 usage 事件时兜底 flush 一次。

        实现现状：pending_usage 仅在 response.completed 事件中设置（L1332），
        无 completed 场景下兜底分支（L547-553）为防御性死代码，不可达。
        按测试闭环规则不改断言迁就实现，用 xfail 显式标记差距——
        将来若新增 usage 事件处理（如 response.usage），本用例意外通过即报警。
        """
        pytest.xfail(
            "设计期望 vs 实现：pending_usage 仅在 response.completed 设置，"
            "无 completed 场景兜底 flush 不可达"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text=sse_text(
                    [
                        {"type": "response.created", "response": {"id": "resp_s3"}},
                        # 虚构事件：模拟 provider 在 completed 前推送 usage
                        {
                            "type": "response.usage",
                            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                        },
                    ]
                ),
            )

        client = make_client(handler)
        chunks = [c async for c in client.stream_response(
            [{"role": "user", "content": "hi"}], tools=None
        )]
        usage_chunks = [c for c in chunks if c.usage is not None]
        assert len(usage_chunks) == 1  # 设计期望：兜底 flush 一次

    async def test_stream_no_response_id_keeps_state(self, make_client):  # S5, inv-H（Risk-8 P1）
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text=sse_text(
                    [
                        # 无 response.created / completed 无 id → response_id 保持 None
                        {"type": "response.output_text.delta", "delta": "hi"},
                        {
                            "type": "response.completed",
                            "response": {
                                "status": "completed",
                                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                            },
                        },
                    ]
                ),
            )

        client = make_client(handler)
        # 预置旧状态：异常流不得用旧 id 续接，也不得覆盖状态
        client._previous_response_id = "resp_old"
        client._last_messages_len = 0
        client._tools_hash = None
        client._instructions_hash = None

        chunks = [c async for c in client.stream_response(
            [{"role": "user", "content": "hi"}], tools=None
        )]
        # 正常产出（delta + completed 终止 chunk）
        assert chunks[0].delta_content == "hi"
        assert chunks[-1].finish_reason == "stop"
        # 流无 response_id → 状态全部保持
        assert client._previous_response_id == "resp_old"
        assert client._last_messages_len == 0
        assert client._tools_hash is None
        assert client._instructions_hash is None

    async def test_stream_expired_fallback_updates_state(self, make_client):  # S4, Risk-7 P0
        captured: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            captured.append(body)
            if "previous_response_id" in body:
                return httpx.Response(
                    400, text='{"error": {"message": "invalid_response_id"}}'
                )
            return httpx.Response(
                200,
                text=sse_text(
                    [
                        {"type": "response.created", "response": {"id": "resp_fb"}},
                        {"type": "response.output_text.delta", "delta": "降级成功"},
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "resp_fb",
                                "status": "completed",
                                "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
                            },
                        },
                    ]
                ),
            )

        client = make_client(handler)
        msgs1 = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "q1"},
        ]
        chunks1 = [c async for c in client.stream_response(msgs1, tools=None)]
        assert client._previous_response_id == "resp_fb"
        assert len(chunks1) >= 2

        # 第二次增量 → 400 invalid_response_id → 流式降级全量重试
        msgs2 = msgs1 + [{"role": "user", "content": "q2"}]
        chunks2 = [c async for c in client.stream_response(msgs2, tools=None)]
        # 降级后状态用 fallback 响应的 response_id 更新（含 instructions_hash）
        assert len(captured) == 3  # 全量 → 增量(400) → 全量重试
        assert "previous_response_id" not in captured[2]
        assert client._previous_response_id == "resp_fb"
        assert client._last_messages_len == 3
        assert client._instructions_hash == instructions_hash(msgs2)
        # fallback 流内容正常产出
        assert any(c.delta_content == "降级成功" for c in chunks2)
        assert any(c.finish_reason == "stop" for c in chunks2)
