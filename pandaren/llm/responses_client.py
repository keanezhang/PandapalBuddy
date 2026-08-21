"""pandaren/llm/responses_client.py — Responses API 调用路径实现

实现 LLMClient Protocol 的第二条调用路径：通过 /v1/responses 端点
（OpenAI / 火山方舟 Seed 2.0 / 阿里百炼）发起 LLM 调用。

与 OpenAICompatibleClient（走 /v1/chat/completions）完全独立，
但对外实现同一个 LLMClient Protocol，AgentLoop（run_core.py）零改动。

设计承接文档：docs/工程化设计文档/框架设计/16_responses_api.md

核心设计决策：
  - 接口统一，实现分离（两个独立类实现同一 Protocol）
  - 增量透明：调用方始终传全量 messages，Client 内部自动 diff
  - 自动降级：response_id 过期时内部自动重建全量请求
  - 状态最小持有：不缓存 messages 副本，只持有长度指纹

设计原则：
  RP1 Protocol 兼容性：满足 LLMClient Protocol 完整契约
  RP2 增量透明：调用方始终传全量，Client 内部自动 diff
  RP3 自动降级：response_id 过期时内部自动重建全量请求
  RP4 状态最小持有：不缓存 messages 副本
  RP5 不变性：api_key / model_name / base_url 构造后不可修改
  RP6 14 章分层合规：L1-L4 对齐
  RP7 只增不改：不修改任何现有代码
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.parse
from typing import Any, AsyncGenerator

import httpx

from .capabilities import (
    EndpointCapabilities,
    OPENAI_RESPONSES,
    VOLCENGINE_RESPONSES,
    DASHSCOPE_RESPONSES,
)
from .exceptions import (
    LLMAuthError,
    LLMError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMRequestError,
    LLMResponseError,
    LLMServerError,
    LLMTimeoutError,
)
from .types import (
    CompletionTokensDetails,
    LLMResponse,
    LLMStreamChunk,
    ModelSettings,
    PromptTokensDetails,
    ToolCallDelta,
    UsageInfo,
)

logger = logging.getLogger("pandaren.llm_client")


# ═══════════════════════════════════════════════════════════════
# 工具函数（复用 client.py 的 _dig 逻辑，但独立实现避免修改 client.py）
# ═══════════════════════════════════════════════════════════════

def _dig(data: dict[str, Any], dotted_path: str) -> Any:
    """按点号分隔的路径从嵌套 dict 中安全取值。

    路径中的 "usage." 前缀会被自动剥离——因为 capabilities 里记录的路径含 "usage." 前缀，
    而传入的 data 通常已经是 usage dict 本身。

    Returns:
        找到的值，或 None。
    """
    if dotted_path.startswith("usage."):
        dotted_path = dotted_path[len("usage."):]

    parts = dotted_path.split(".")
    current: Any = data
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


# ═══════════════════════════════════════════════════════════════
# SSE 哨兵（与 client.py 相同的私有类型，避免跨模块耦合）
# ═══════════════════════════════════════════════════════════════

class _SseDone:
    """SSE [DONE] 终止信号的专用哨兵类型。"""
    __slots__ = ()

    def __repr__(self) -> str:
        return "_SSE_DONE"


_SSE_DONE = _SseDone()


# ═══════════════════════════════════════════════════════════════
# 实现类
# ═══════════════════════════════════════════════════════════════

class ResponsesAPIClient:
    """Responses API LLM 客户端（httpx 全异步实现）。

    通过 /v1/responses 端点调用 LLM，支持 previous_response_id 增量续接。
    对外实现 LLMClient Protocol，与 OpenAICompatibleClient 接口完全一致。

    特性：
      - 自动增量检测：调用方始终传全量 messages，Client 内部通过
        _last_messages_len 自动计算增量（RP2 增量透明）
      - 自动降级：previous_response_id 过期时自动降级为全量请求（RP3）
      - tools 变化检测：tools 内容变化时自动冷启动
      - messages 缩短检测：Memory compact 后自动冷启动

    用法：
        # 推荐：工厂方法
        client = ResponsesAPIClient.for_openai_responses(
            api_key="sk-xxx", model_name="gpt-4o",
        )

        # 通用构造
        client = ResponsesAPIClient(
            api_key="sk-xxx",
            model_name="gpt-4o",
            base_url="https://api.openai.com/v1",
            capabilities=OPENAI_RESPONSES,
        )

        response = await client.call(messages, tools=tools)
    """

    def __init__(
        self,
        api_key: str,
        model_name: str,
        base_url: str,
        capabilities: EndpointCapabilities | None = None,
        timeout: float = 60.0,
        default_settings: ModelSettings | None = None,
        *,
        use_caching: bool = True,
        initial_response_id: str | None = None,
        initial_messages_len: int = 0,
    ) -> None:
        """通用构造器。优先使用 for_openai_responses / for_volcengine_responses /
        for_dashscope_responses 工厂方法——会自动绑定正确的 capabilities 常量。

        Args:
            api_key: 认证凭证，构造后不可修改（RP5 不变性）
            model_name: 模型标识，构造后不可修改
            base_url: API endpoint 基址（不含 /responses 后缀）
            capabilities: Provider 能力声明（L2），None = 纯透传模式
            timeout: 请求超时秒数，默认 60s
            default_settings: 默认调参，None = 完全依赖 provider 默认
            use_caching: 是否在请求体中附带 caching: {"type": "enabled"}
            initial_response_id: 跨 session 复用时传入已有的 response_id
            initial_messages_len: 跨 session 复用时传入上次的 messages 长度
        """
        if not api_key:
            raise ValueError("api_key 不能为空")
        if not model_name:
            raise ValueError("model_name 不能为空")
        if not base_url:
            raise ValueError("base_url 不能为空")

        # RP5 不变性：构造后冻结的属性
        self._api_key: str = api_key
        self._model_name: str = model_name
        self._base_url: str = base_url.rstrip("/")
        self._timeout: float = timeout
        self._default_settings: ModelSettings | None = default_settings
        self._capabilities: EndpointCapabilities | None = capabilities

        # RP4 最小状态
        self._previous_response_id: str | None = initial_response_id
        self._last_messages_len: int = initial_messages_len
        self._tools_hash: str | None = None
        self._instructions_hash: str | None = None
        self._use_caching: bool = use_caching

        # AsyncClient 单例复用
        self._http_client: httpx.AsyncClient = httpx.AsyncClient(timeout=timeout)

        logger.info(
            "ResponsesAPIClient created: model=%s base_url=%s "
            "use_caching=%s initial_response_id=%s",
            self._model_name,
            self._base_url,
            self._use_caching,
            self._previous_response_id[:8] + "..." if self._previous_response_id else None,
        )

    # ─── 工厂方法 ────────────────────────────────────────────

    @classmethod
    def for_openai_responses(
        cls,
        api_key: str,
        model_name: str = "gpt-4o",
        base_url: str = "https://api.openai.com/v1",
        *,
        timeout: float = 60.0,
        default_settings: ModelSettings | None = None,
        use_caching: bool = True,
        initial_response_id: str | None = None,
        initial_messages_len: int = 0,
    ) -> "ResponsesAPIClient":
        """OpenAI /v1/responses 工厂。"""
        return cls(
            api_key=api_key,
            model_name=model_name,
            base_url=base_url,
            capabilities=OPENAI_RESPONSES,
            timeout=timeout,
            default_settings=default_settings,
            use_caching=use_caching,
            initial_response_id=initial_response_id,
            initial_messages_len=initial_messages_len,
        )

    @classmethod
    def for_volcengine_responses(
        cls,
        api_key: str,
        model_name: str,
        base_url: str = "https://ark.cn-beijing.volces.com/api/v3",
        *,
        timeout: float = 60.0,
        default_settings: ModelSettings | None = None,
        use_caching: bool = True,
        initial_response_id: str | None = None,
        initial_messages_len: int = 0,
    ) -> "ResponsesAPIClient":
        """火山方舟 /v1/responses 工厂，常跑豆包 Seed 1.6+ / 2.0 系列。"""
        return cls(
            api_key=api_key,
            model_name=model_name,
            base_url=base_url,
            capabilities=VOLCENGINE_RESPONSES,
            timeout=timeout,
            default_settings=default_settings,
            use_caching=use_caching,
            initial_response_id=initial_response_id,
            initial_messages_len=initial_messages_len,
        )

    @classmethod
    def for_dashscope_responses(
        cls,
        api_key: str,
        model_name: str,
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        *,
        timeout: float = 60.0,
        default_settings: ModelSettings | None = None,
        use_caching: bool = True,
        initial_response_id: str | None = None,
        initial_messages_len: int = 0,
    ) -> "ResponsesAPIClient":
        """阿里百炼 /v1/responses 工厂，常跑通义千问 Qwen 系列。"""
        return cls(
            api_key=api_key,
            model_name=model_name,
            base_url=base_url,
            capabilities=DASHSCOPE_RESPONSES,
            timeout=timeout,
            default_settings=default_settings,
            use_caching=use_caching,
            initial_response_id=initial_response_id,
            initial_messages_len=initial_messages_len,
        )

    # ─── 只读属性（Protocol 要求 + 跨 session 复用）────────────

    @property
    def model_name(self) -> str:
        """模型名，只读暴露（RP5 不变性）。"""
        return self._model_name

    @property
    def capabilities(self) -> EndpointCapabilities | None:
        """端点能力声明（只读）。"""
        return self._capabilities

    @property
    def response_id(self) -> str | None:
        """当前 response chain 最新节点 ID（只读，供业务持久化）。"""
        return self._previous_response_id

    @property
    def last_messages_len(self) -> int:
        """上次 call 时 messages 列表长度（只读，供业务持久化）。"""
        return self._last_messages_len

    # ─── 对外接口（LLMClient Protocol）───────────────────────

    async def call(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        settings: ModelSettings | None = None,
        *,
        always_tools_count: int = 0,
    ) -> LLMResponse:
        """非流式 LLM 调用（Responses API 路径）。

        内部自动选择全量/增量路径：
          - 首次调用（无 previous_response_id）→ 全量
          - tools 变化 → 冷启动 → 全量
          - messages 缩短（compact）→ 冷启动 → 全量
          - 正常追加 → 增量（只发新增消息）

        Args:
            messages: 完整消息列表（由 MessageBuilder 组装）
            tools: 工具声明列表（原样透传）
            settings: 本次调用的调参覆盖；None 时使用 default_settings
            always_tools_count: ALWAYS 工具数（run_core.py 传入，本 Client 不使用）

        Returns:
            LLMResponse（与 OpenAICompatibleClient 返回格式完全一致）

        Raises:
            LLMAuthError / LLMRequestError / LLMRateLimitError /
            LLMServerError / LLMTimeoutError / LLMNetworkError /
            LLMResponseError
        """
        merged = self._merge_settings(self._default_settings, settings)

        # ── 检测 tools 变化 → 冷启动 ──
        new_tools_hash = self._compute_tools_hash(tools)
        if self._tools_hash is not None and new_tools_hash != self._tools_hash:
            self._invalidate("tools_changed")

        # ── 检测 instructions（system）变化 → 冷启动 ──
        new_instructions_hash = self._compute_instructions_hash(messages)
        if (
            self._instructions_hash is not None
            and new_instructions_hash != self._instructions_hash
        ):
            self._invalidate("instructions_changed")

        # ── 检测 messages 缩短（compact）→ 冷启动 ──
        if len(messages) < self._last_messages_len:
            self._invalidate("messages_shortened")

        # ── 检测等长重发（无增量可发）→ 冷启动 ──
        elif len(messages) == self._last_messages_len:
            self._invalidate("no_increment")

        # ── 路径选择 ──
        if self._previous_response_id is None:
            # 全量路径
            body = self._build_full_request(messages, tools, merged)
            path = "full"
        else:
            # 增量路径
            body = self._build_incremental_request(messages, tools, merged)
            path = "incremental"

        logger.info(
            "ResponsesAPI call: model=%s path=%s messages_len=%d "
            "response_id=%s",
            self._model_name,
            path,
            len(messages),
            (self._previous_response_id or "")[:8] or "none",
        )

        # ── 发送请求 ──
        try:
            api_response = await self._send_request(body, merged, stream=False)
        except (LLMRequestError, LLMServerError) as exc:
            # 检测 response_id 过期错误 → 自动降级
            if self._is_response_id_expired_error(exc) and path == "incremental":
                return await self._handle_expired(messages, tools, merged)
            raise

        # ── 转换响应 + 更新状态 ──
        result = self._convert_output_to_response(api_response)
        self._update_state_after_success(api_response, messages, new_tools_hash)
        return result

    async def stream_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        settings: ModelSettings | None = None,
        *,
        always_tools_count: int = 0,
    ) -> AsyncGenerator[LLMStreamChunk, None]:
        """流式 LLM 调用（Responses API 路径）。

        全量/增量路径选择逻辑与 call() 完全相同。
        流结束时从最终响应中提取 response_id 更新状态。

        Yields:
            LLMStreamChunk（与 OpenAICompatibleClient 完全一致）

        Raises:
            同 call()
        """
        merged = self._merge_settings(self._default_settings, settings)

        # ── 检测 tools 变化 → 冷启动 ──
        new_tools_hash = self._compute_tools_hash(tools)
        if self._tools_hash is not None and new_tools_hash != self._tools_hash:
            self._invalidate("tools_changed")

        # ── 检测 instructions（system）变化 → 冷启动 ──
        new_instructions_hash = self._compute_instructions_hash(messages)
        if (
            self._instructions_hash is not None
            and new_instructions_hash != self._instructions_hash
        ):
            self._invalidate("instructions_changed")

        # ── 检测 messages 缩短（compact）→ 冷启动 ──
        if len(messages) < self._last_messages_len:
            self._invalidate("messages_shortened")

        # ── 检测等长重发（无增量可发）→ 冷启动 ──
        elif len(messages) == self._last_messages_len:
            self._invalidate("no_increment")

        # ── 路径选择 ──
        if self._previous_response_id is None:
            body = self._build_full_request(messages, tools, merged)
            path = "full"
        else:
            body = self._build_incremental_request(messages, tools, merged)
            path = "incremental"

        logger.info(
            "ResponsesAPI stream: model=%s path=%s messages_len=%d "
            "response_id=%s",
            self._model_name,
            path,
            len(messages),
            (self._previous_response_id or "")[:8] or "none",
        )

        body["stream"] = True

        # ── DEBUG: 打印实际发送的请求体 ──
        logger.debug(
            "ResponsesAPI stream request body keys: %s, model=%s, "
            "has_instructions=%s, input_len=%d, tools_len=%d",
            list(body.keys()),
            body.get("model"),
            "instructions" in body,
            len(body.get("input", [])),
            len(body.get("tools", [])),
        )

        # usage 缓存（可能来自末尾 chunk）
        pending_usage: UsageInfo | None = None
        usage_flushed = False
        # 流式过程中收集 response_id（在 response.created 事件中）
        stream_response_id: str | None = None

        # ── 标记是否需要降级重试 ──
        need_fallback = False

        try:
            async with self._http_client.stream(
                "POST",
                self._build_url(merged),
                json=body,
                headers=self._build_headers(merged),
            ) as response:
                if not response.is_success:
                    resp_body = await response.aread()
                    body_text = resp_body.decode("utf-8", errors="replace")
                    logger.error(
                        "LLM API error (responses stream): status=%d model=%s body=%s",
                        response.status_code,
                        self._model_name,
                        body_text[:2000],
                    )
                    error = self._classify_http_error(
                        response.status_code,
                        body_text,
                        dict(response.headers),
                    )
                    # 流式也尝试降级
                    if self._is_response_id_expired_error(error) and path == "incremental":
                        # 流式降级：标记后跳出 context manager，用全量流式重试
                        self._invalidate("response_id_expired_stream")
                        need_fallback = True
                    else:
                        raise error
                else:
                    _sse_line_count = 0
                    _sse_data_count = 0
                    _event_types_seen: list[str] = []
                    async for line in response.aiter_lines():
                        _sse_line_count += 1
                        if not line:
                            continue

                        parsed = self._parse_sse_line(line)
                        if parsed is None:
                            continue
                        if isinstance(parsed, _SseDone):
                            logger.debug(
                                "ResponsesAPI stream [DONE]: total_lines=%d data_events=%d "
                                "event_types=%s",
                                _sse_line_count, _sse_data_count,
                                _event_types_seen[:10],
                            )
                            break

                        _sse_data_count += 1
                        # ── Responses API SSE 事件类型分发 ──
                        event_type = parsed.get("type", "")
                        if event_type and event_type not in _event_types_seen:
                            _event_types_seen.append(event_type)

                        # response.created 事件：提取 response_id
                        if event_type == "response.created":
                            resp_obj = parsed.get("response", {})
                            stream_response_id = resp_obj.get("id")

                        # response.completed 事件：提取最终 usage 和 response_id
                        elif event_type == "response.completed":
                            resp_obj = parsed.get("response", {})
                            if resp_obj.get("id"):
                                stream_response_id = resp_obj["id"]
                            usage_data = resp_obj.get("usage")
                            if usage_data:
                                pending_usage = self._build_usage_info(usage_data)

                        # response.output_item.added / response.content_part.added
                        # 这些是结构事件，通常不需要 yield 给上层

                        # response.output_text.delta — 文本增量
                        elif event_type == "response.output_text.delta":
                            delta_text = parsed.get("delta", "")
                            if delta_text:
                                yield LLMStreamChunk(delta_content=delta_text)

                        # response.reasoning_text.delta — 推理增量（如果有）
                        elif event_type == "response.reasoning_text.delta":
                            delta_text = parsed.get("delta", "")
                            if delta_text:
                                yield LLMStreamChunk(delta_reasoning_content=delta_text)

                        # response.refusal.delta — 拒绝增量
                        elif event_type == "response.refusal.delta":
                            delta_text = parsed.get("delta", "")
                            if delta_text:
                                yield LLMStreamChunk(refusal_delta=delta_text)

                        # response.function_call_arguments.delta — 工具调用参数增量
                        elif event_type == "response.function_call_arguments.delta":
                            fn_args_delta = parsed.get("delta", "")
                            item_id = parsed.get("item_id", "")
                            output_index = parsed.get("output_index", 0)
                            if fn_args_delta:
                                delta_payload: ToolCallDelta = {
                                    "index": output_index,
                                    "id": item_id,
                                    "name": "",  # name 在 output_item.added 中，此处只有增量
                                    "arguments_delta": fn_args_delta,
                                }
                                yield LLMStreamChunk(tool_call_delta=delta_payload)

                        # response.function_call_arguments.done — 工具调用完成
                        elif event_type == "response.function_call_arguments.done":
                            # 可以用来通知完整参数已到，但上层会自行组装
                            pass

                        # response.output_item.added — 新输出项（可能是 function_call）
                        elif event_type == "response.output_item.added":
                            item = parsed.get("item", {})
                            if item.get("type") == "function_call":
                                output_index = parsed.get("output_index", 0)
                                fn_name = item.get("name", "")
                                call_id = item.get("call_id", "") or item.get("id", "")
                                if fn_name:
                                    delta_payload = ToolCallDelta(
                                        index=output_index,
                                        id=call_id,
                                        name=fn_name,
                                        arguments_delta="",
                                    )
                                    yield LLMStreamChunk(tool_call_delta=delta_payload)

                        # response.output_text.done / response.output_item.done
                        elif event_type in (
                            "response.output_text.done",
                            "response.output_item.done",
                            "response.content_part.done",
                        ):
                            # 内容完成信号，不需要额外处理
                            pass

                        # response.completed — 终止信号
                        # 已在上面处理了 usage，这里输出 finish_reason
                        if event_type == "response.completed":
                            resp_obj = parsed.get("response", {})
                            status = resp_obj.get("status", "completed")
                            finish_reason = self._status_to_finish_reason(status)
                            yield LLMStreamChunk(
                                finish_reason=finish_reason,
                                usage=pending_usage,
                            )
                            usage_flushed = True

                    # 循环结束 — 兜底 flush usage
                    logger.debug(
                        "ResponsesAPI stream loop ended: total_lines=%d data_events=%d "
                        "event_types=%s stream_response_id=%s",
                        _sse_line_count, _sse_data_count,
                        _event_types_seen[:10],
                        stream_response_id[:8] + "..." if stream_response_id else "none",
                    )
                    if pending_usage is not None and not usage_flushed:
                        yield LLMStreamChunk(usage=pending_usage)

        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"ResponsesAPI 流式请求超时: {exc}") from exc
        except httpx.ConnectError as exc:
            raise LLMNetworkError(f"ResponsesAPI 流式连接失败: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMNetworkError(f"ResponsesAPI 流式网络异常: {exc}") from exc

        # ── 流式降级：response_id 过期 → 全量流式重试 ──
        if need_fallback:
            logger.info(
                "ResponsesAPI stream response_id expired, retrying with full request: "
                "model=%s",
                self._model_name,
            )
            new_tools_hash = self._compute_tools_hash(tools)
            fallback_body = self._build_full_request(messages, tools, merged)
            fallback_body["stream"] = True

            # 重置流式局部变量
            pending_usage = None
            usage_flushed = False
            stream_response_id = None

            try:
                async with self._http_client.stream(
                    "POST",
                    self._build_url(merged),
                    json=fallback_body,
                    headers=self._build_headers(merged),
                ) as response:
                    if not response.is_success:
                        resp_body = await response.aread()
                        raise self._classify_http_error(
                            response.status_code,
                            resp_body.decode("utf-8", errors="replace"),
                            dict(response.headers),
                        )

                    async for line in response.aiter_lines():
                        if not line:
                            continue

                        parsed = self._parse_sse_line(line)
                        if parsed is None:
                            continue
                        if isinstance(parsed, _SseDone):
                            break

                        event_type = parsed.get("type", "")

                        if event_type == "response.created":
                            resp_obj = parsed.get("response", {})
                            stream_response_id = resp_obj.get("id")

                        elif event_type == "response.completed":
                            resp_obj = parsed.get("response", {})
                            if resp_obj.get("id"):
                                stream_response_id = resp_obj["id"]
                            usage_data = resp_obj.get("usage")
                            if usage_data:
                                pending_usage = self._build_usage_info(usage_data)

                        elif event_type == "response.output_text.delta":
                            delta_text = parsed.get("delta", "")
                            if delta_text:
                                yield LLMStreamChunk(delta_content=delta_text)

                        elif event_type == "response.reasoning_text.delta":
                            delta_text = parsed.get("delta", "")
                            if delta_text:
                                yield LLMStreamChunk(delta_reasoning_content=delta_text)

                        elif event_type == "response.refusal.delta":
                            delta_text = parsed.get("delta", "")
                            if delta_text:
                                yield LLMStreamChunk(refusal_delta=delta_text)

                        elif event_type == "response.function_call_arguments.delta":
                            fn_args_delta = parsed.get("delta", "")
                            item_id = parsed.get("item_id", "")
                            output_index = parsed.get("output_index", 0)
                            if fn_args_delta:
                                delta_payload = ToolCallDelta(
                                    index=output_index,
                                    id=item_id,
                                    name="",
                                    arguments_delta=fn_args_delta,
                                )
                                yield LLMStreamChunk(tool_call_delta=delta_payload)

                        elif event_type == "response.function_call_arguments.done":
                            pass

                        elif event_type == "response.output_item.added":
                            item = parsed.get("item", {})
                            if item.get("type") == "function_call":
                                output_index = parsed.get("output_index", 0)
                                fn_name = item.get("name", "")
                                call_id = item.get("call_id", "") or item.get("id", "")
                                if fn_name:
                                    delta_payload = ToolCallDelta(
                                        index=output_index,
                                        id=call_id,
                                        name=fn_name,
                                        arguments_delta="",
                                    )
                                    yield LLMStreamChunk(tool_call_delta=delta_payload)

                        elif event_type in (
                            "response.output_text.done",
                            "response.output_item.done",
                            "response.content_part.done",
                        ):
                            pass

                        if event_type == "response.completed":
                            resp_obj = parsed.get("response", {})
                            status = resp_obj.get("status", "completed")
                            finish_reason = self._status_to_finish_reason(status)
                            yield LLMStreamChunk(
                                finish_reason=finish_reason,
                                usage=pending_usage,
                            )
                            usage_flushed = True

                    if pending_usage is not None and not usage_flushed:
                        yield LLMStreamChunk(usage=pending_usage)

            except httpx.TimeoutException as exc:
                raise LLMTimeoutError(f"ResponsesAPI 流式降级请求超时: {exc}") from exc
            except httpx.ConnectError as exc:
                raise LLMNetworkError(f"ResponsesAPI 流式降级连接失败: {exc}") from exc
            except httpx.HTTPError as exc:
                raise LLMNetworkError(f"ResponsesAPI 流式降级网络异常: {exc}") from exc

        # ── 流式结束后更新状态 ──
        if stream_response_id:
            self._previous_response_id = stream_response_id
            self._last_messages_len = len(messages)
            self._tools_hash = new_tools_hash
            self._instructions_hash = self._compute_instructions_hash(messages)
            logger.debug(
                "ResponsesAPI stream state updated: response_id=%s "
                "last_messages_len=%d",
                stream_response_id[:8] + "...",
                self._last_messages_len,
            )

    async def aclose(self) -> None:
        """关闭 httpx.AsyncClient，释放连接池资源。"""
        await self._http_client.aclose()
        logger.debug("ResponsesAPIClient aclose: model=%s", self._model_name)

    async def __aenter__(self) -> "ResponsesAPIClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    # ─── 请求构建 ─────────────────────────────────────────────

    def _build_full_request(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        merged: ModelSettings | None,
    ) -> dict[str, Any]:
        """构建全量请求体（首次/冷启动路径）。

        Responses API 的请求结构：
          {
            "model": "gpt-4o",
            "instructions": "你是一个助手...",  // 从 messages[0] 提取
            "input": [...],                    // 从 messages[1:] 转换
            "tools": [...],                    // 可选
            "caching": {"type": "enabled"},    // 可选
            ... (其他 ModelSettings 字段)
          }
        """
        body: dict[str, Any] = {
            "model": self._model_name,
        }

        # instructions 从 messages[0]（system message）提取
        if messages:
            system_msg = messages[0]
            instructions = self._extract_instructions(system_msg)
            if instructions:
                body["instructions"] = instructions

            # input 从 messages[1:] 转换
            if len(messages) > 1:
                body["input"] = self._convert_messages_to_input(messages[1:])

        # tools 转换
        if tools:
            body["tools"] = self._convert_tools(tools)

        # caching
        if self._use_caching:
            body["caching"] = {"type": "enabled"}

        # ModelSettings 合并
        self._apply_model_settings(body, merged)

        logger.debug(
            "ResponsesAPI _build_full_request: body_keys=%s, "
            "instructions_len=%d, input_len=%d, tools_len=%d, "
            "max_output_tokens=%s, model=%s",
            list(body.keys()),
            len(body.get("instructions", "")),
            len(body.get("input", [])),
            len(body.get("tools", [])),
            body.get("max_output_tokens"),
            body.get("model"),
        )

        return body

    def _build_incremental_request(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        merged: ModelSettings | None,
    ) -> dict[str, Any]:
        """构建增量请求体（热路径）。

        Responses API 增量请求：
          {
            "model": "gpt-4o",
            "previous_response_id": "resp_xxx",
            "input": [...],  // 只有增量部分
            "caching": {"type": "enabled"},
          }
        """
        body: dict[str, Any] = {
            "model": self._model_name,
            "previous_response_id": self._previous_response_id,
        }

        # 增量 input
        increment = self._detect_increment(messages)
        if increment:
            body["input"] = self._convert_messages_to_input(increment)

        # caching
        if self._use_caching:
            body["caching"] = {"type": "enabled"}

        # ModelSettings 合并（注意：增量请求不重发 instructions 和 tools）
        self._apply_model_settings(body, merged)

        return body

    def _detect_increment(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """从全量 messages 中提取增量部分。

        增量 = messages[_last_messages_len:]。
        若 messages 缩短（len < _last_messages_len），由 call() 层面的冷启动检测处理，
        本方法不会在缩短情况下被调用（因为 _previous_response_id 已被清空）。
        """
        return messages[self._last_messages_len:]

    @staticmethod
    def _extract_instructions(system_msg: dict[str, Any]) -> str | None:
        """从 system message 提取 instructions 文本。

        支持两种 content 格式：
          1. content: str（纯文本）
          2. content: [{type: "text", text: "..."}]（结构化）
        """
        content = system_msg.get("content")
        if content is None:
            return None

        if isinstance(content, str):
            return content

        if isinstance(content, list):
            # 拼接所有 text 类型块
            texts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
            return "\n".join(texts) if texts else None

        return None

    @staticmethod
    def _convert_messages_to_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """将 Chat Completions 格式的 messages 转换为 Responses API 的 input 格式。

        转换规则：
          - role: "user"      → {role: "user", content: ...}
          - role: "assistant" → {role: "assistant", content: ...}
          - role: "tool"      → {type: "function_call_output", call_id: ..., output: ...}
        """
        input_items: list[dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "")

            if role == "user":
                input_items.append({
                    "role": "user",
                    "content": msg.get("content", ""),
                })

            elif role == "assistant":
                # assistant 消息：可能有 content 和/或 tool_calls
                content = msg.get("content")
                tool_calls = msg.get("tool_calls")

                if content:
                    input_items.append({
                        "role": "assistant",
                        "content": content,
                    })

                # tool_calls 转换为 function_call 类型的 output items
                if tool_calls:
                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        input_items.append({
                            "type": "function_call",
                            "id": tc.get("id", ""),
                            "call_id": tc.get("id", ""),
                            "name": fn.get("name", ""),
                            "arguments": fn.get("arguments", ""),
                        })

            elif role == "tool":
                # tool result → function_call_output
                input_items.append({
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id", ""),
                    "output": msg.get("content", ""),
                })

            elif role == "system":
                # system 消息原样保留（不改写 role / content）
                input_items.append({
                    "role": "system",
                    "content": msg.get("content", ""),
                })

        return input_items

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """将 Chat Completions 格式的 tools 转换为 Responses API 格式。

        Chat Completions 格式：
          [{"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}]

        Responses API 格式：
          [{"type": "function", "name": "...", "description": "...", "parameters": {...}}]
        """
        converted: list[dict[str, Any]] = []
        for tool in tools:
            if tool.get("type") == "function" and "function" in tool:
                fn = tool["function"]
                item: dict[str, Any] = {
                    "type": "function",
                    "name": fn.get("name", ""),
                }
                if fn.get("description"):
                    item["description"] = fn["description"]
                if fn.get("parameters"):
                    item["parameters"] = fn["parameters"]
                if fn.get("strict") is not None:
                    item["strict"] = fn["strict"]
                converted.append(item)
            else:
                # 非 function 类型（如 Responses API 内置工具），原样透传
                converted.append(tool)
        return converted

    @staticmethod
    def _apply_model_settings(body: dict[str, Any], merged: ModelSettings | None) -> None:
        """将 ModelSettings 的非 None 字段合并到请求体。"""
        if merged is None:
            return

        if merged.temperature is not None:
            body["temperature"] = merged.temperature
        if merged.max_tokens is not None:
            body["max_output_tokens"] = merged.max_tokens  # Responses API 用 max_output_tokens
        if merged.top_p is not None:
            body["top_p"] = merged.top_p
        if merged.reasoning is not None:
            body["reasoning"] = merged.reasoning
        # tool_choice / parallel_tool_calls — 对齐 OpenAICompatibleClient
        if merged.tool_choice is not None:
            body["tool_choice"] = merged.tool_choice
        if merged.parallel_tool_calls is not None:
            body["parallel_tool_calls"] = merged.parallel_tool_calls
        # extra_body：provider 专属顶层字段展开
        if merged.extra_body:
            body.update(merged.extra_body)

    # ─── 状态管理 ─────────────────────────────────────────────

    def invalidate(self, reason: str) -> None:
        """清空 response_id 及相关状态，触发下次 call 走全量路径。"""
        old_id = self._previous_response_id
        self._previous_response_id = None
        self._last_messages_len = 0
        # 注意：不清空 _tools_hash，它在下次 call 时会被重新计算
        logger.info(
            "ResponsesAPI invalidate: reason=%s old_response_id=%s",
            reason,
            (old_id or "")[:8] or "none",
        )

    def _invalidate(self, reason: str) -> None:
        """内部别名，保持内部调用路径不变。"""
        self.invalidate(reason)

    def _update_state_after_success(
        self,
        api_response: dict[str, Any],
        messages: list[dict[str, Any]],
        new_tools_hash: str | None,
    ) -> None:
        """请求成功后更新内部状态。"""
        # 提取 response_id
        new_response_id = api_response.get("id")
        if new_response_id:
            self._previous_response_id = new_response_id

        # 更新 messages 长度
        self._last_messages_len = len(messages)

        # 更新 tools hash
        self._tools_hash = new_tools_hash

        # 更新 instructions hash（system 指令指纹，用于下次调用检测变化）
        self._instructions_hash = self._compute_instructions_hash(messages)

        logger.debug(
            "ResponsesAPI state updated: response_id=%s last_messages_len=%d",
            (self._previous_response_id or "")[:8] + "..." if self._previous_response_id else "none",
            self._last_messages_len,
        )

    # ─── tools / instructions 变化检测 ────────────────────────

    @staticmethod
    def _compute_tools_hash(tools: list[dict[str, Any]] | None) -> str | None:
        """计算 tools 的内容指纹。None 或空列表返回 None。"""
        if not tools:
            return None
        # 排序 keys 保证序列化稳定
        serialized = json.dumps(tools, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _compute_instructions_hash(
        messages: list[dict[str, Any]],
    ) -> str | None:
        """计算 system 指令（instructions）的内容指纹，用于检测 system 变化。

        规则：
          - 仅 role == "system" 且 content 为 str 的消息参与（list/结构化 content 不参与）
          - 非 dict 消息跳过
          - 顺序敏感（system 消息相对顺序变化视为不同）
          - 无参与项返回 None
        """
        texts: list[str] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "system":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                texts.append(content)
        if not texts:
            return None
        joined = "\n".join(texts)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]

    # ─── HTTP 层 ─────────────────────────────────────────────

    def _build_url(self, merged: ModelSettings | None) -> str:
        """构造 /responses 端点 URL。"""
        url = f"{self._base_url}/responses"
        if merged is not None and merged.extra_query:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(merged.extra_query)
        return url

    def _build_headers(self, merged: ModelSettings | None) -> dict[str, str]:
        """构造请求 headers。"""
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if merged is not None and merged.extra_headers:
            headers.update(merged.extra_headers)
        return headers

    async def _send_request(
        self,
        body: dict[str, Any],
        merged: ModelSettings | None = None,
        *,
        stream: bool = False,
    ) -> dict[str, Any]:
        """发送 HTTP POST 到 /responses 端点（非流式）。"""
        url = self._build_url(merged)
        headers = self._build_headers(merged)

        start_time = time.monotonic()
        try:
            response = await self._http_client.post(url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            duration_ms = (time.monotonic() - start_time) * 1000
            raise LLMTimeoutError(
                f"ResponsesAPI 请求超时: {exc}", duration_ms=duration_ms,
            ) from exc
        except httpx.ConnectError as exc:
            duration_ms = (time.monotonic() - start_time) * 1000
            raise LLMNetworkError(
                f"ResponsesAPI 连接失败: {exc}", duration_ms=duration_ms,
            ) from exc
        except httpx.HTTPError as exc:
            duration_ms = (time.monotonic() - start_time) * 1000
            raise LLMNetworkError(
                f"ResponsesAPI 网络异常: {exc}", duration_ms=duration_ms,
            ) from exc

        if not response.is_success:
            logger.error(
                "LLM API error (responses): status=%d model=%s body=%s",
                response.status_code,
                self._model_name,
                response.text[:2000],
            )
            raise self._classify_http_error(
                response.status_code,
                response.text,
                dict(response.headers),
            )

        try:
            return response.json()
        except Exception as exc:
            raise LLMResponseError(f"ResponsesAPI 响应 JSON 解析失败: {exc}") from exc

    # ─── 降级处理 ─────────────────────────────────────────────

    async def _handle_expired(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        merged: ModelSettings | None,
    ) -> LLMResponse:
        """response_id 过期时的降级处理。

        清空状态 → 用当前全量 messages 重建全量请求 → 重试一次。
        """
        logger.info(
            "ResponsesAPI response_id expired, falling back to full request: "
            "model=%s old_response_id=%s",
            self._model_name,
            (self._previous_response_id or "")[:8] or "none",
        )

        self._invalidate("response_id_expired")

        # 重走全量路径
        new_tools_hash = self._compute_tools_hash(tools)
        body = self._build_full_request(messages, tools, merged)

        api_response = await self._send_request(body, merged, stream=False)

        result = self._convert_output_to_response(api_response)
        self._update_state_after_success(api_response, messages, new_tools_hash)
        return result

    @staticmethod
    def _is_response_id_expired_error(exc: LLMError) -> bool:
        """检测异常是否为 response_id 过期错误。

        各 provider 返回的过期错误可能不同，这里做宽泛匹配：
          - HTTP 400 + "previous_response_id" / "not found" / "expired" 在错误消息中
          - HTTP 404（response 不存在）
        """
        msg = str(exc).lower()
        if isinstance(exc, LLMRequestError):
            # HTTP 400 + 关键词 或 HTTP 404（response 不存在）
            status = getattr(exc, "status_code", 0)
            if status == 404:
                return True
            return any(kw in msg for kw in (
                "previous_response_id", "not found", "expired",
                "invalid_response_id", "response_not_found",
            ))
        if isinstance(exc, LLMServerError) and getattr(exc, "status_code", 0) == 404:
            return True
        return False

    # ─── 响应转换（L4 归一）──────────────────────────────────

    def _convert_output_to_response(self, api_response: dict[str, Any]) -> LLMResponse:
        """将 Responses API 的 output 转换为 LLMResponse 格式。

        Responses API 响应结构：
          {
            "id": "resp_xxx",
            "object": "response",
            "created_at": 1234567890,
            "model": "gpt-4o",
            "status": "completed",
            "output": [
              {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "..."}]},
              {"type": "function_call", "id": "fc_xxx", "call_id": "call_xxx", "name": "...", "arguments": "..."},
            ],
            "usage": {"input_tokens": ..., "output_tokens": ..., ...}
          }
        """
        output = api_response.get("output", [])
        status = api_response.get("status", "completed")

        # 提取文本内容和工具调用
        content_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        reasoning_content: str | None = None

        for item in output:
            item_type = item.get("type", "")

            if item_type == "message":
                # 消息类型：提取 content
                for part in item.get("content", []):
                    part_type = part.get("type", "")
                    if part_type in ("output_text", "text"):
                        text = part.get("text", "")
                        if text:
                            content_parts.append(text)
                    elif part_type == "refusal":
                        # refusal 也在 content 中
                        pass

            elif item_type == "function_call":
                # 工具调用：转换为 Chat Completions 兼容格式
                call_id = item.get("call_id") or item.get("id", "")
                tc: dict[str, Any] = {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", ""),
                    },
                }
                tool_calls.append(tc)

            elif item_type == "reasoning":
                # 推理内容（部分 provider 返回）
                for part in item.get("content", []):
                    if part.get("type") == "text":
                        text = part.get("text", "")
                        if text:
                            reasoning_content = (reasoning_content or "") + text

            else:
                # 未知类型，warning 日志但不阻断
                logger.warning(
                    "ResponsesAPI unknown output item type: %s (skipped)",
                    item_type,
                )

        # 组装 content
        content = "\n".join(content_parts) if content_parts else None

        # 确定 finish_reason
        finish_reason = self._status_to_finish_reason(status)

        # 构建 usage
        usage_data = api_response.get("usage", {})
        usage = self._build_usage_info(usage_data)

        # 构建 LLMResponse
        result = LLMResponse(
            content=content,
            finish_reason=finish_reason,
            usage=usage,
            id=api_response.get("id", ""),
            model=api_response.get("model", ""),
            created=api_response.get("created_at", 0),
        )

        if tool_calls:
            result["tool_calls"] = tool_calls

        if reasoning_content:
            result["reasoning_content"] = reasoning_content

        return result

    @staticmethod
    def _status_to_finish_reason(status: str) -> str:
        """将 Responses API 的 status 转换为 Chat Completions 的 finish_reason。"""
        mapping = {
            "completed": "stop",
            "incomplete": "length",
            "cancelled": "stop",
            "failed": "error",
        }
        return mapping.get(status, "stop")

    # ─── Usage 构建（L4 归一）────────────────────────────────

    def _build_usage_info(self, usage_data: dict[str, Any]) -> UsageInfo:
        """从 Responses API 的 usage 构建 UsageInfo。

        Responses API 的 usage 格式：
          {
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "input_tokens_details": {"cached_tokens": 80},
            "output_tokens_details": {"reasoning_tokens": 10},
          }

        需要归一为 Chat Completions 的 UsageInfo 格式（prompt_tokens / completion_tokens）。
        """
        # Responses API 使用 input_tokens / output_tokens
        # Chat Completions 使用 prompt_tokens / completion_tokens
        input_tokens = usage_data.get("input_tokens", 0) or usage_data.get("prompt_tokens", 0)
        output_tokens = usage_data.get("output_tokens", 0) or usage_data.get("completion_tokens", 0)
        total_tokens = usage_data.get("total_tokens", 0) or (input_tokens + output_tokens)

        usage = UsageInfo(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=total_tokens,
        )

        # completion_tokens_details
        otd = usage_data.get("output_tokens_details") or usage_data.get("completion_tokens_details")
        if otd:
            details = CompletionTokensDetails()
            if otd.get("reasoning_tokens") is not None:
                details["reasoning_tokens"] = otd["reasoning_tokens"]
            if otd.get("output_tokens") is not None:
                details["output_tokens"] = otd["output_tokens"]
            if otd.get("text_tokens") is not None:
                details["text_tokens"] = otd["text_tokens"]
            usage["completion_tokens_details"] = details

        # prompt_tokens_details
        itd = usage_data.get("input_tokens_details") or usage_data.get("prompt_tokens_details")
        if itd:
            details_p = PromptTokensDetails()
            if itd.get("cached_tokens") is not None:
                details_p["cached_tokens"] = itd["cached_tokens"]
            if itd.get("text_tokens") is not None:
                details_p["text_tokens"] = itd["text_tokens"]
            if itd.get("cache_creation_input_tokens") is not None:
                details_p["cache_creation_input_tokens"] = itd["cache_creation_input_tokens"]
            usage["prompt_tokens_details"] = details_p

        # ── L4 归一：按 caps 字段路径回填非标准位置的缓存数据 ──
        if self._capabilities is not None:
            ptd_out = usage.get("prompt_tokens_details")
            if ptd_out is None:
                ptd_out = PromptTokensDetails()

            if "cached_tokens" not in ptd_out and self._capabilities.cached_tokens_field:
                val = _dig(usage_data, self._capabilities.cached_tokens_field)
                if val is not None:
                    ptd_out["cached_tokens"] = int(val)

            if "cache_creation_input_tokens" not in ptd_out and self._capabilities.cache_creation_field:
                val = _dig(usage_data, self._capabilities.cache_creation_field)
                if val is not None:
                    ptd_out["cache_creation_input_tokens"] = int(val)

            if ptd_out:
                usage["prompt_tokens_details"] = ptd_out

        return usage

    # ─── SSE 解析 ─────────────────────────────────────────────

    @staticmethod
    def _parse_sse_line(line: str) -> dict[str, Any] | _SseDone | None:
        """解析 SSE 行。

        Responses API 的 SSE 格式：
          event: response.output_text.delta
          data: {"type": "response.output_text.delta", "delta": "Hello"}

        Returns:
            _SseDone：遇到 [DONE]
            dict：成功解析的 JSON 数据
            None：心跳行或格式错误
        """
        if not line.startswith("data:"):
            return None

        payload_str = line[5:].strip()

        if payload_str == "[DONE]":
            return _SSE_DONE

        try:
            return json.loads(payload_str)
        except json.JSONDecodeError:
            logger.warning("ResponsesAPI SSE 行 JSON 解析失败: %r", line)
            return None

    # ─── 错误分类（复用 client.py 的逻辑）────────────────────

    @staticmethod
    def _classify_http_error(
        status_code: int,
        response_body: str,
        headers: dict[str, str],
    ) -> LLMError:
        """将 HTTP 非 2xx 状态码映射为强类型 LLMError 子类。"""
        message = f"HTTP {status_code}: {response_body[:200]}"

        if status_code in (401, 403):
            return LLMAuthError(message, status_code=status_code)

        if status_code in (400, 404):
            return LLMRequestError(message, status_code=status_code)

        if status_code == 408:
            return LLMTimeoutError(message)

        if status_code == 429:
            retry_after: float | None = None
            raw_ms = headers.get("retry-after-ms") or headers.get("x-ratelimit-reset-requests")
            raw_s = headers.get("retry-after")
            if raw_ms:
                try:
                    retry_after = float(raw_ms) / 1000.0
                except ValueError:
                    pass
            elif raw_s:
                try:
                    retry_after = float(raw_s)
                except ValueError:
                    pass
            return LLMRateLimitError(message, retry_after=retry_after)

        if 500 <= status_code < 600:
            return LLMServerError(message, status_code=status_code)

        return LLMServerError(message, status_code=status_code)

    # ─── ModelSettings 合并（复用 client.py 的逻辑）─────────

    @staticmethod
    def _merge_settings(
        base: ModelSettings | None,
        override: ModelSettings | None,
    ) -> ModelSettings | None:
        """合并两个 ModelSettings，override 中非 None 的字段覆盖 base。"""
        if base is None and override is None:
            return None
        if base is None:
            return override
        if override is None:
            return base

        def _pick(attr: str) -> Any:
            ov = getattr(override, attr)
            return ov if ov is not None else getattr(base, attr)

        return ModelSettings(
            temperature=_pick("temperature"),
            max_tokens=_pick("max_tokens"),
            top_p=_pick("top_p"),
            frequency_penalty=_pick("frequency_penalty"),
            presence_penalty=_pick("presence_penalty"),
            stop=_pick("stop"),
            seed=_pick("seed"),
            response_format=_pick("response_format"),
            tool_choice=_pick("tool_choice"),
            parallel_tool_calls=_pick("parallel_tool_calls"),
            include_usage=_pick("include_usage"),
            reasoning=_pick("reasoning"),
            target_model=_pick("target_model"),
            extra_body=_pick("extra_body"),
            extra_headers=_pick("extra_headers"),
            extra_query=_pick("extra_query"),
        )
