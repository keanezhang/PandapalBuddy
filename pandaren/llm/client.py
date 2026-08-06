"""pandaren/llm/client.py — OpenAICompatibleClient 实现

支持任何兼容 /v1/chat/completions 的 API（千问、豆包、OpenAI 等）。
失败时抛出强类型 LLMError 子类，由 AgentLoop 负责重试决策。

设计原则：
  S1 不变性：api_key / model_name / _http_client 构造后冻结
  E4 失败安全：ModelSettings 所有字段默认 None，timeout 默认 60s
  显式错误分类：HTTP 非 2xx 全部映射为强类型 LLMError 子类，禁止 httpx 原始异常逃逸
  透传保证：tools 列表原样写入 payload，tool_calls 原样提取
  AsyncClient 单例复用：连接池在实例生命周期内只创建一次
  流式职责单一：client 层只做"无状态增量分发"，完整 tool_calls 的组装上移到引擎层

私有实现细节（不对外暴露）：
  _SseDone / _SSE_DONE：SSE [DONE] 终止信号的专用哨兵类型
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import warnings
from typing import Any, AsyncGenerator, ClassVar

import httpx

from .capabilities import (
    EndpointCapabilities,
    DASHSCOPE_CHAT,
    DEEPSEEK_CHAT,
    OPENAI_CHAT,
    VOLCENGINE_CHAT,
    VOLCENGINE_CONTEXT_API,
)
from .cache_strategy import (
    CacheDepth,
    CacheMode,
    CacheState,
    apply_cache_positions,
    log_cache_config,
)
from .exceptions import (
    LLMAuthError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMRequestError,
    LLMResponseError,
    LLMServerError,
    LLMTimeoutError,
    LLMError,
)
from .types import (
    LLMResponse,
    LLMStreamChunk,
    ModelSettings,
    ToolCallDelta,
    UsageInfo,
    CompletionTokensDetails,
    PromptTokensDetails,
)

logger = logging.getLogger("pandaren.llm_client")


def _dig(data: dict[str, Any], dotted_path: str) -> Any:
    """按点号分隔的路径从嵌套 dict 中安全取值。

    路径格式示例：
      - "usage.prompt_tokens_details.cached_tokens"
      - "usage.prompt_cache_hit_tokens"

    路径中的 "usage." 前缀会被自动剥离——因为 capabilities 里记录的路径含 "usage." 前缀，
    而传入的 data 通常已经是 usage dict 本身（从 API 响应的 "usage" 字段取出）。

    Returns:
        找到的值，或 None（路径中任一层级不存在时返回 None，不抛异常）。
    """
    # 剥离 "usage." 前缀（capabilities 里的路径包含这个前缀，但 data 已经是 usage 对象）
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





class ProviderCapabilityWarning(UserWarning):
    """当请求中出现当前 provider 不支持的字段时抛出的告警。

    设计目的——**保证用户知情权**：
      SDK 原则是"个性化字段透传不映射"，但透传后如果 provider 静默忽略
      （比如火山方舟 chat 不解析 cache_control），用户会以为自己开启了某能力
      实际却没生效。这个 warning 负责在此类情况下**显式告知**。

    业务层可按需过滤：
        warnings.filterwarnings("ignore", category=ProviderCapabilityWarning)

    或通过 logger（logger name = "pandaren.llm_client"）统一处理。
    """


# ═══════════════════════════════════════════════════════════════
# SSE 哨兵（私有实现细节）
# ═══════════════════════════════════════════════════════════════

class _SseDone:
    """SSE [DONE] 终止信号的专用哨兵类型。

    使用专用类型而非空 dict，避免类型混淆：
      - isinstance(parsed, _SseDone) 替代 parsed is _SSE_DONE
      - 类型标注 dict[str, Any] | _SseDone | None 语义清晰
    """
    __slots__ = ()

    def __repr__(self) -> str:
        return "_SSE_DONE"


_SSE_DONE = _SseDone()


# ═══════════════════════════════════════════════════════════════
# 实现类
# ═══════════════════════════════════════════════════════════════

class OpenAICompatibleClient:
    """OpenAI 兼容 LLM 客户端（httpx 全异步实现）。

    支持任何兼容 /v1/chat/completions 的 API（百炼 Qwen、火山方舟 Doubao、OpenAI 等）。
    失败时抛出强类型 LLMError 子类，由 AgentLoop 负责重试决策。

    用法：
        async with OpenAICompatibleClient(api_key=..., model_name=...) as client:
            response = await client.call(messages)

    或手动管理生命周期：
        client = OpenAICompatibleClient(api_key=..., model_name=...)
        try:
            response = await client.call(messages)
        finally:
            await client.aclose()
    """

    def __init__(
        self,
        api_key: str,
        model_name: str,
        base_url: str,
        capabilities: EndpointCapabilities | None = None,
        timeout: float = 60.0,
        default_settings: ModelSettings | None = None,
        cache: CacheMode = True,
        cache_depth: CacheDepth = "history",
    ) -> None:
        """
        通用构造器。如果你用的是已内置的 provider，优先使用 `for_volcengine` /
        `for_dashscope` / `for_openai` 工厂方法——会自动绑定正确的 capabilities 常量。

        Args:
            api_key: 认证凭证，构造后不可修改（S1 不变性）
            model_name: 模型标识（或火山方舟接入点 ID），构造后不可修改
            base_url: API endpoint，不同 provider 只差 URL
            capabilities: Provider 能力声明（L2）。
                - 传入常量（如 VOLCENGINE_CHAT）→ 每次调用前扫描请求体，对当前
                  provider 不支持的字段发出 `ProviderCapabilityWarning`，
                  保证使用者对"字段被静默忽略"类行为有知情权
                - None（默认）→ 纯透传模式，不做任何能力校验与告警；
                  适用于 SDK 未内置的 provider 或"我自己知道在做什么"的场景
            timeout: 请求超时秒数，默认 60s（E4 失败安全默认值）
            default_settings: 默认调参，None = 完全依赖 provider 默认
            cache: 缓存模式（默认 True = SDK 自动管理）。
                - True: SDK 根据 provider capability 做最佳机械动作
                - False: SDK 不主动挂任何显式断点
                - "manual": SDK 完全不碰 cache_control / context_id，业务走逃生舱接口自己挂
            cache_depth: 缓存深度档位（默认 "system"）。
                - "off": 不打任何断点（cache=True 时等价于 cache=False）
                - "tools": 只在 ALWAYS 工具 schema 末尾打 1 个断点
                - "system": 在 ALWAYS 工具 + system message 末尾打 2 个断点
                - "history": 3 个断点（+ 最后一个 assistant）
        """
        if not api_key:
            raise ValueError("api_key 不能为空")
        if not model_name:
            raise ValueError("model_name 不能为空")
        if not base_url:
            raise ValueError("base_url 不能为空")

        # S1 不变性：构造后冻结的属性，全部私有
        self._api_key: str = api_key
        self._model_name: str = model_name
        self._base_url: str = base_url.rstrip("/")
        self._timeout: float = timeout
        self._default_settings: ModelSettings | None = default_settings
        self._capabilities: EndpointCapabilities | None = capabilities

        # 缓存配置
        self._cache: CacheMode = cache
        self._cache_depth: CacheDepth = cache_depth

        # 缓存运行时状态（委托给 CacheState）
        self._cache_state: CacheState = CacheState()

        # 原则 5：AsyncClient 单例复用，连接池只创建一次
        self._http_client: httpx.AsyncClient = httpx.AsyncClient(timeout=timeout)

        # 启动 info 日志：透明披露当前缓存配置
        log_cache_config(self._cache, self._cache_depth, self._capabilities)

    # ─── 工厂方法：按 provider 快速构造（推荐姿势）────────────
    #
    # 命名约定：工厂方法用**平台/API 厂商**名（for_volcengine / for_dashscope /
    # for_openai / for_moonshot / ...），不用模型品牌名。见
    # capabilities.py 顶部的"命名约定"说明。
    #
    # 为什么不把 base_url 写死？
    #   各家都有多个 region endpoint（volces 的北京 / 上海，dashscope 的北京 / 新加坡），
    #   强行写死默认值会让切 region 变成暗坑。工厂方法只负责绑定正确的 capabilities，
    #   base_url 仍由调用方显式传。

    @classmethod
    def for_volcengine(
        cls,
        api_key: str,
        model_name: str,
        base_url: str = "https://ark.cn-beijing.volces.com/api/v3",
        *,
        use_context_api: bool = False,
        timeout: float = 60.0,
        default_settings: ModelSettings | None = None,
        cache: CacheMode = True,
        cache_depth: CacheDepth = "history",
    ) -> "OpenAICompatibleClient":
        """火山引擎方舟（VolcEngine Ark）工厂，常跑豆包 Doubao 系列。

        Args:
            model_name: 对 chat 端点可以是模型名或接入点 ID；
                走 Context API 时必须是接入点 ID（ep-xxx）
            base_url: 默认北京区；海外 / 其他 region 请显式传
            use_context_api: True 时绑定 `VOLCENGINE_CONTEXT_API` 能力
                （explicit_cache="context_id"）；False 绑定 `VOLCENGINE_CHAT`
            cache: 缓存模式（默认 True）
            cache_depth: 缓存深度档位（默认 "system"）
        """
        caps = VOLCENGINE_CONTEXT_API if use_context_api else VOLCENGINE_CHAT
        return cls(
            api_key=api_key,
            model_name=model_name,
            base_url=base_url,
            capabilities=caps,
            timeout=timeout,
            default_settings=default_settings,
            cache=cache,
            cache_depth=cache_depth,
        )

    @classmethod
    def for_dashscope(
        cls,
        api_key: str,
        model_name: str,
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        *,
        timeout: float = 60.0,
        default_settings: ModelSettings | None = None,
        cache: CacheMode = True,
        cache_depth: CacheDepth = "history",
    ) -> "OpenAICompatibleClient":
        """阿里云百炼（DashScope）OpenAI 兼容模式工厂，常跑通义千问 Qwen 系列。

        Args:
            base_url: 默认阿里云北京区 compatible-mode；
                海外 / 新加坡 region 请显式传对应 endpoint
            cache: 缓存模式（默认 True）
            cache_depth: 缓存深度档位（默认 "system"）
        """
        return cls(
            api_key=api_key,
            model_name=model_name,
            base_url=base_url,
            capabilities=DASHSCOPE_CHAT,
            timeout=timeout,
            default_settings=default_settings,
            cache=cache,
            cache_depth=cache_depth,
        )

    @classmethod
    def for_openai(
        cls,
        api_key: str,
        model_name: str = "gpt-4o",
        base_url: str = "https://api.openai.com/v1",
        *,
        timeout: float = 60.0,
        default_settings: ModelSettings | None = None,
        cache: CacheMode = True,
        cache_depth: CacheDepth = "history",
    ) -> "OpenAICompatibleClient":
        """OpenAI 官方 /chat/completions 工厂。

        Args:
            cache: 缓存模式（默认 True）
            cache_depth: 缓存深度档位（默认 "system"）
        """
        return cls(
            api_key=api_key,
            model_name=model_name,
            base_url=base_url,
            capabilities=OPENAI_CHAT,
            timeout=timeout,
            default_settings=default_settings,
            cache=cache,
            cache_depth=cache_depth,
        )

    @classmethod
    def for_deepseek(
        cls,
        api_key: str,
        model_name: str = "deepseek-v4-pro",
        base_url: str = "https://api.deepseek.com",
        *,
        timeout: float = 60.0,
        default_settings: ModelSettings | None = None,
        cache: CacheMode = True,
        cache_depth: CacheDepth = "history",
    ) -> "OpenAICompatibleClient":
        """DeepSeek 官方 /chat/completions 工厂。

        DeepSeek API 完全兼容 OpenAI 协议，base_url 默认使用 api.deepseek.com。
        隐式 prefix cache 自动开启，命中量通过 prompt_cache_hit_tokens 字段报告。

        Args:
            api_key: DeepSeek API Key
            model_name: 模型名，默认 deepseek-v4-pro
            base_url: 默认 https://api.deepseek.com
            cache: 缓存模式（默认 True）
            cache_depth: 缓存深度档位（默认 "history"）
        """
        return cls(
            api_key=api_key,
            model_name=model_name,
            base_url=base_url,
            capabilities=DEEPSEEK_CHAT,
            timeout=timeout,
            default_settings=default_settings,
            cache=cache,
            cache_depth=cache_depth,
        )

    # ──────────────────────────────────────────────────────────────────────
    # 统一 provider 工厂（应用层入口，内部按名称路由到对应工厂方法）
    # ──────────────────────────────────────────────────────────────────────

    _PROVIDER_FACTORIES: ClassVar[dict[str, str]] = {
        "dashscope": "for_dashscope",
        "volcengine": "for_volcengine",
        "openai": "for_openai",
        "deepseek": "for_deepseek",
    }

    @classmethod
    def for_provider(
        cls,
        provider: str,
        api_key: str,
        model_name: str,
        base_url: str | None = None,
        *,
        timeout: float = 60.0,
        default_settings: "ModelSettings | None" = None,
        cache: "CacheMode" = True,
        cache_depth: "CacheDepth" = "history",
    ) -> "OpenAICompatibleClient":
        """按 provider 名称路由到对应工厂方法。

        应用层使用此入口，无需感知内部工厂分发细节。
        SDK 负责按 provider 绑定正确的 EndpointCapabilities 和默认 base_url。

        Args:
            provider: provider 名称（"dashscope" / "volcengine" / "openai"）。
            api_key:  API 密钥。
            model_name: 模型名称。
            base_url: 可选覆盖 URL；None 时使用各 provider 工厂方法的内置默认值。

        Raises:
            ValueError: provider 不在支持列表中。
        """
        factory_name = cls._PROVIDER_FACTORIES.get(provider)
        if factory_name is None:
            supported = sorted(cls._PROVIDER_FACTORIES)
            raise ValueError(
                f"Unsupported provider: {provider!r}. "
                f"Supported: {supported}"
            )
        factory = getattr(cls, factory_name)
        kwargs: dict = dict(
            api_key=api_key,
            model_name=model_name,
            timeout=timeout,
            default_settings=default_settings,
            cache=cache,
            cache_depth=cache_depth,
        )
        if base_url is not None:
            kwargs["base_url"] = base_url
        return factory(**kwargs)

    @property
    def model_name(self) -> str:
        """模型名，只读暴露（S1 不变性）。"""
        return self._model_name

    @property
    def provider(self) -> str:
        """激活的平台/API 厂商名（dashscope/volcengine/openai/deepseek），只读。

        取自绑定的端点能力声明（`for_provider`/`for_*` 工厂会注入）。纯透传模式
        （未注入 capabilities）时返回 ""。供应用层按 provider 分账（BudgetLedger）。
        """
        return self._capabilities.provider if self._capabilities else ""

    @property
    def capabilities(self) -> EndpointCapabilities | None:
        """端点能力声明（只读）。

        None 表示构造时未注入能力声明（纯透传模式），此时不做任何字段校验与告警。
        业务层可据此查询：
            if client.capabilities is not None:
                if client.capabilities.explicit_cache == "context_id":
                    ...
        """
        return self._capabilities

    # ─── 缓存管理接口（委托给 cache_strategy 模块）──────────────

    def _on_history_compacted(self) -> None:
        """Memory 通知 history 被非追加式修改，下一次 call 按冷启动处理。"""
        self._cache_state.on_history_compacted()

    def _on_static_context_changed(self) -> None:
        """静态前缀（tools / messages[0]）发生非追加式修改，下一次 call 冷启动。"""
        self._cache_state.on_static_context_changed()

    # ─── 对外接口 ───────────────────────────────────────────────

    async def call(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        settings: ModelSettings | None = None,
        *,
        always_tools_count: int = 0,
    ) -> LLMResponse:
        """非流式 LLM 调用。

        Args:
            messages: 消息列表（由 MessageBuilder 组装）
            tools: 工具声明列表（pandaren 自定义工具 + built-in tools 混合，原样透传）
            settings: 本次调用的调参覆盖；None 时使用 default_settings
            always_tools_count: ALWAYS 工具数（由 ToolRegistry 提供），用于缓存断点 ① 定位。
                0 = 不打 tools 层断点。

        Returns:
            LLMResponse: {content, tool_calls, finish_reason, usage, id, model, created, ...}

        Raises:
            LLMAuthError: 401/403
            LLMRequestError: 400
            LLMRateLimitError: 429（携带 retry_after）
            LLMServerError: 5xx
            LLMTimeoutError: 超时（httpx.TimeoutException / HTTP 408）
            LLMNetworkError: 连接失败 / DNS 解析失败
            LLMResponseError: 响应 JSON 解析失败
        """
        # 应用缓存断点（可能深拷贝 messages/tools）
        messages_for_call, tools_for_call = apply_cache_positions(
            messages, tools, always_tools_count,
            cache=self._cache,
            cache_depth=self._cache_depth,
            capabilities=self._capabilities,
        )

        # 冷启动标记消耗
        self._cache_state.consume_cold_start()

        merged = self._merge_settings(self._default_settings, settings)
        url = self._build_url(merged)
        payload = self._build_payload(messages_for_call, tools_for_call, merged, stream=False)
        headers = self._build_headers(merged)

        # 能力声明校验（仅当注入了 capabilities 时生效；发送前执行，不阻断请求）
        self._emit_capability_warnings(payload, self._capabilities)

        logger.debug(
            "LLM call: model=%s messages=%d tools=%d",
            self._model_name,
            len(messages),
            len(tools) if tools else 0,
        )

        try:
            response = await self._http_client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"LLM 请求超时: {exc}") from exc
        except httpx.ConnectError as exc:
            raise LLMNetworkError(f"LLM 连接失败: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMNetworkError(f"LLM 网络异常: {exc}") from exc

        if not response.is_success:
            print(f"    ❌ LLM API 返回 {response.status_code}: {response.text[:500]}")
            logger.error(
                "LLM API error: status=%d model=%s url=%s body=%s",
                response.status_code,
                self._model_name,
                url,
                response.text[:2000],
            )
            raise self._classify_http_error(
                response.status_code,
                response.text,
                dict(response.headers),
            )

        try:
            data = response.json()
        except Exception as exc:
            raise LLMResponseError(f"LLM 响应 JSON 解析失败: {exc}") from exc

        result = self._extract_response(data)

        # 缓存命中观测（诊断显式/隐式缓存命中率用）
        self._log_cache_usage(result.get("usage"))

        logger.debug(
            "LLM call done: model=%s content_len=%s tool_calls=%s "
            "finish_reason=%s prompt_tokens=%d completion_tokens=%d",
            self._model_name,
            len(result.get("content") or "") ,
            len(result.get("tool_calls") or []),
            result.get("finish_reason"),
            result.get("usage", {}).get("prompt_tokens", 0),
            result.get("usage", {}).get("completion_tokens", 0),
        )
        return result

    async def stream_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        settings: ModelSettings | None = None,
        *,
        always_tools_count: int = 0,
    ) -> AsyncGenerator[LLMStreamChunk, None]:
        """流式 LLM 调用，返回 AsyncIterator[LLMStreamChunk]。

        调用方用 `async for chunk in client.stream_response(...)` 消费，
        可随时 `break` 中断，连接在 context manager 退出时清理。

        流式增量语义（client 层只做无状态分发，完整 tool_calls 的组装在上层）：
          - delta.content        → LLMStreamChunk(delta_content=...)
          - delta.reasoning_content / delta.reasoning
                                 → LLMStreamChunk(delta_reasoning_content=...)
          - delta.refusal        → LLMStreamChunk(refusal_delta=...)
          - delta.tool_calls[n]  → LLMStreamChunk(tool_call_delta={index,id,name,arguments_delta})
          - finish_reason/usage  → LLMStreamChunk(finish_reason=..., usage=...)
                                   （不再重复输出完整 tool_calls 列表）

        choices=[] 处理：
          部分 provider（如 DashScope/Qwen）会在最后发一个 choices=[] 的 usage-only chunk，
          该 chunk 只携带用量统计，本方法内部暂存，在后续终止 chunk 或循环结束时
          随 finish_reason chunk 一并发出；若整个流从未出现 finish_reason（极端情况），
          会在 [DONE] 之前单独 yield 一个仅含 usage 的 chunk，保证用量不丢失。

        Raises:
            同 call()
        """
        # 应用缓存断点（可能深拷贝 messages/tools）
        messages_for_call, tools_for_call = apply_cache_positions(
            messages, tools, always_tools_count,
            cache=self._cache,
            cache_depth=self._cache_depth,
            capabilities=self._capabilities,
        )

        # 冷启动标记消耗
        self._cache_state.consume_cold_start()

        merged = self._merge_settings(self._default_settings, settings)
        url = self._build_url(merged)
        payload = self._build_payload(messages_for_call, tools_for_call, merged, stream=True)
        headers = self._build_headers(merged)

        # 能力声明校验（仅当注入了 capabilities 时生效；发送前执行，不阻断请求）
        self._emit_capability_warnings(payload, self._capabilities)

        logger.debug(
            "LLM stream: model=%s messages=%d tools=%d",
            self._model_name,
            len(messages),
            len(tools) if tools else 0,
        )

        # usage 缓存：可能来自 choices=[] 的 usage-only chunk 或内联 usage
        pending_usage: UsageInfo | None = None
        # 是否已随某个终止 chunk 发出 usage（避免末尾重复）
        usage_flushed = False

        try:
            async with self._http_client.stream("POST", url, json=payload, headers=headers) as response:
                if not response.is_success:
                    body = await response.aread()
                    body_text = body.decode("utf-8", errors="replace")
                    logger.error(
                        "LLM API error (stream): status=%d model=%s url=%s body=%s",
                        response.status_code,
                        self._model_name,
                        url,
                        body_text[:2000],
                    )
                    raise self._classify_http_error(
                        response.status_code,
                        body_text,
                        dict(response.headers),
                    )

                async for line in response.aiter_lines():
                    if not line:
                        continue

                    parsed = self._parse_sse_line(line)
                    if parsed is None:
                        # 格式错误或心跳行，跳过
                        continue
                    if isinstance(parsed, _SseDone):
                        # [DONE] 信号，结束流
                        break

                    # 任何 chunk 都可能携带内联 usage（stream_options.include_usage 启用时）
                    usage_data = parsed.get("usage")
                    if usage_data:
                        pending_usage = self._build_usage_info(usage_data)

                    # ── choices=[] usage-only chunk（DashScope/Qwen 等 provider）──
                    choices = parsed.get("choices") or []
                    if not choices:
                        # usage 已在上方缓存，不向上 yield，等终止 chunk 一并发出
                        continue

                    # ── 正常 choice chunk ──
                    first_choice = choices[0]
                    delta = first_choice.get("delta") or {}
                    finish_reason: str | None = first_choice.get("finish_reason")

                    # ── 文本/推理/拒答增量（同一 chunk 内可并存，各自独立 yield）──
                    content = delta.get("content")
                    # 同时兼容 DashScope / VolcEngine 的 reasoning_content 与其他第三方的 reasoning
                    # 优先级：reasoning_content > reasoning
                    reasoning = delta.get("reasoning_content")
                    if reasoning is None:
                        reasoning = delta.get("reasoning")
                    refusal = delta.get("refusal")

                    if content:
                        yield LLMStreamChunk(delta_content=content)
                    if reasoning:
                        yield LLMStreamChunk(delta_reasoning_content=reasoning)
                    if refusal:
                        yield LLMStreamChunk(refusal_delta=refusal)

                    # ── tool_calls 增量：逐 fragment 分发，不再累积 ──
                    for tc_chunk in (delta.get("tool_calls") or []):
                        idx = tc_chunk.get("index", 0)
                        fn_delta = tc_chunk.get("function") or {}
                        tc_id = tc_chunk.get("id") or ""
                        fn_name = fn_delta.get("name") or ""
                        fn_args = fn_delta.get("arguments") or ""

                        # 只有任一字段非空才发出增量（避免发送空 chunk）
                        if tc_id or fn_name or fn_args:
                            delta_payload: ToolCallDelta = {
                                "index": idx,
                                "id": tc_id,
                                "name": fn_name,
                                "arguments_delta": fn_args,
                            }
                            yield LLMStreamChunk(tool_call_delta=delta_payload)

                    # ── 终止 chunk：输出 finish_reason + usage ──
                    if finish_reason is not None:
                        yield LLMStreamChunk(
                            finish_reason=finish_reason,
                            usage=pending_usage,
                        )
                        # 仅当确实随本 chunk 发出了非空 usage 才算已 flush。
                        # 部分 provider（如 DashScope/Qwen）finish_reason chunk 在前、
                        # choices=[] 的 usage-only chunk 在后：此时 pending_usage 尚为 None，
                        # 若在此就置 usage_flushed=True，末尾兜底 flush 会被跳过，导致真正的
                        # usage 永远吐不出去（token 全 0）。故只有非空 usage 才标记已 flush，
                        # 保证后到的 usage-only chunk 仍能在循环结束时被兜底发出。
                        if pending_usage is not None:
                            usage_flushed = True

                # 循环正常结束（[DONE] 或服务端关闭）——兜底 flush usage
                if pending_usage is not None and not usage_flushed:
                    yield LLMStreamChunk(usage=pending_usage)

                # 缓存命中观测（诊断显式/隐式缓存命中率用）；pending_usage 为最终用量
                self._log_cache_usage(pending_usage)

        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"LLM 流式请求超时: {exc}") from exc
        except httpx.ConnectError as exc:
            raise LLMNetworkError(f"LLM 流式连接失败: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMNetworkError(f"LLM 流式网络异常: {exc}") from exc

    async def aclose(self) -> None:
        """关闭 httpx.AsyncClient，释放连接池资源。

        应在 Agent 生命周期结束时调用。
        也可通过 async context manager（async with）自动触发。
        """
        await self._http_client.aclose()
        logger.debug("LLMClient aclose: model=%s", self._model_name)

    async def __aenter__(self) -> "OpenAICompatibleClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    # ─── 内部方法 ──────────────────────────────────────────────

    def _log_cache_usage(self, usage: UsageInfo | dict[str, Any] | None) -> None:
        """打印本次调用的缓存命中观测，用于诊断显式/隐式缓存命中率。

        仅当注入了 capabilities 且该端点声明了 cached_tokens_field（有命中观测能力）
        时才打点，避免对无缓存观测能力的 provider 产生噪音。INFO 级别；prompt_tokens
        为 0 时跳过。纯观测路径，任何异常都不得影响主调用——静默吞掉。
        """
        caps = self._capabilities
        if usage is None or caps is None or caps.cached_tokens_field is None:
            return
        try:
            prompt = int(usage.get("prompt_tokens") or 0)
            if prompt <= 0:
                return
            from .cache_usage import extract_cache_usage

            cu = extract_cache_usage(dict(usage), caps)
            hit = cu.get("hit_tokens") or 0
            write = cu.get("write_tokens")
            logger.info(
                "cache usage [%s:%s]: hit=%d/%d (%.1f%%) write=%s first_write=%s",
                caps.provider,
                caps.endpoint,
                hit,
                prompt,
                hit / prompt * 100.0,
                write if write is not None else "-",
                cu.get("is_first_write"),
            )
        except Exception:  # noqa: BLE001 — 观测不得影响主链路
            logger.debug("cache usage logging skipped due to error", exc_info=True)

    def _build_headers(self, merged: ModelSettings | None) -> dict[str, str]:
        """构造请求 headers。

        基础 header：Authorization + Content-Type。
        merged.extra_headers 覆盖基础 header（同名键以用户传入为准），
        便于替换 Authorization（如 Bearer → ApiKey）或追加自定义 header。
        """
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if merged is not None and merged.extra_headers:
            headers.update(merged.extra_headers)
        return headers

    def _build_url(self, merged: ModelSettings | None) -> str:
        """构造请求 URL，合并 extra_query。"""
        url = f"{self._base_url}/chat/completions"
        if merged is not None and merged.extra_query:
            # 若 base_url 已带 query（罕见），用 & 拼接
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(merged.extra_query)
        return url

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        merged: ModelSettings | None,
        stream: bool = False,
    ) -> dict[str, Any]:
        """构建 request payload。

        merged 由上层调用前通过 _merge_settings 合并 default + call-time 得到。
        ModelSettings 中非 None 的字段写入 payload，None 字段不写（让 provider 决定默认值）。

        extra_body 中的字段作为顶层字段展开，用于千问 enable_search 等 provider 专属参数。
        """
        payload: dict[str, Any] = {
            "model": self._model_name,
            "messages": messages,
        }

        if merged is not None:
            if merged.temperature is not None:
                payload["temperature"] = merged.temperature
            if merged.max_tokens is not None:
                payload["max_tokens"] = merged.max_tokens
            if merged.top_p is not None:
                payload["top_p"] = merged.top_p
            if merged.frequency_penalty is not None:
                payload["frequency_penalty"] = merged.frequency_penalty
            if merged.presence_penalty is not None:
                payload["presence_penalty"] = merged.presence_penalty
            if merged.stop is not None:
                payload["stop"] = merged.stop
            if merged.seed is not None:
                payload["seed"] = merged.seed
            if merged.response_format is not None:
                payload["response_format"] = self._resolve_response_format(merged.response_format)
            if merged.tool_choice is not None:
                payload["tool_choice"] = merged.tool_choice
            if merged.parallel_tool_calls is not None:
                payload["parallel_tool_calls"] = merged.parallel_tool_calls
            if merged.reasoning is not None:
                payload["reasoning"] = merged.reasoning
            # extra_body：provider 专属顶层字段，展开写入 payload 顶层
            if merged.extra_body:
                payload.update(merged.extra_body)

        # 原则 4：tools 原样写入，不区分 pandaren 工具与 built-in 工具
        if tools:
            payload["tools"] = tools

        if stream:
            payload["stream"] = True
            # 显式控制 stream_options.include_usage —— 仅当调用方明确开启时才注入，
            # 避免部分不支持该参数的 provider 直接返回 HTTP 400。
            if merged is not None and merged.include_usage is True:
                payload["stream_options"] = {"include_usage": True}

        return payload

    @staticmethod
    def _resolve_response_format(response_format: dict[str, Any] | type) -> dict[str, Any]:
        """将 response_format 解析为 API 所需的 dict。

        如果传入的是 type（dataclass 或 Pydantic BaseModel），自动转为 json_schema 格式；
        如果传入的已经是 dict，原样返回。
        """
        if isinstance(response_format, dict):
            return response_format

        if isinstance(response_format, type):
            from .schema import output_type_to_response_format
            return output_type_to_response_format(response_format)

        # 不应到达这里，但做个兜底
        return response_format  # type: ignore[return-value]

    @staticmethod
    def _merge_settings(
        base: ModelSettings | None,
        override: ModelSettings | None,
    ) -> ModelSettings | None:
        """合并两个 ModelSettings，override 中非 None 的字段覆盖 base。

        两者都为 None 时返回 None。

        此方法必须覆盖 ModelSettings 所有字段——新增字段时需同步更新。
        """
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

    def _extract_response(self, data: dict[str, Any]) -> LLMResponse:
        """从非流式响应 JSON 中提取 OpenAI 兼容字段。

        提取 OpenAI chat.completion 响应中的必须和可选字段：
          必选：content, finish_reason, usage, id, model, created
          可选：tool_calls, reasoning_content, refusal

        tool_calls 原样提取，不过滤 built-in tool（原则 4：透传保证）。
        usage 字段必须存在，无数据时填 0。
        choices 为空列表时优雅降级（返回 content=None, tool_calls=None）。
        """
        choices = data.get("choices") or []
        choice = choices[0] if choices else {}
        message = choice.get("message", {})
        usage_data = data.get("usage", {})

        # ── 构建 UsageInfo（复用 _build_usage_info 避免重复逻辑）──
        usage = self._build_usage_info(usage_data)

        # ── 构建 LLMResponse ──
        result = LLMResponse(
            content=message.get("content"),
            finish_reason=choice.get("finish_reason"),
            usage=usage,
            id=data.get("id", ""),
            model=data.get("model", ""),
            created=data.get("created", 0),
        )

        # 可选字段：tool_calls
        tool_calls = message.get("tool_calls")
        if tool_calls:
            result["tool_calls"] = tool_calls

        # 可选字段：reasoning_content（思考模型特有，DashScope / VolcEngine 返回）
        # 同时兼容 reasoning 字段（其他第三方平台）
        reasoning_content = message.get("reasoning_content") or message.get("reasoning")
        if reasoning_content:
            result["reasoning_content"] = reasoning_content

        # 可选字段：refusal（OpenAI 规范字段）
        refusal = message.get("refusal")
        if refusal:
            result["refusal"] = refusal

        return result

    def _build_usage_info(self, usage_data: dict[str, Any]) -> UsageInfo:
        """从 API 响应的 usage 字段构建 UsageInfo（OpenAI 兼容格式）。

        流式和非流式共用此方法，统一处理：
          - 必选字段：prompt_tokens, completion_tokens, total_tokens
          - 可选字段：completion_tokens_details, prompt_tokens_details

        L4 归一逻辑（§4）：
          当 self._capabilities 存在时，按 caps.cached_tokens_field / cache_creation_field
          回填 prompt_tokens_details 中的 cached_tokens / cache_creation_input_tokens，
          确保业务代码跨 provider 统一用：
            resp["usage"]["prompt_tokens_details"]["cached_tokens"]
          读取命中数。
        """
        usage = UsageInfo(
            prompt_tokens=usage_data.get("prompt_tokens", 0),
            completion_tokens=usage_data.get("completion_tokens", 0),
            total_tokens=usage_data.get("total_tokens", 0),
        )

        # 可选：completion_tokens_details
        ctd = usage_data.get("completion_tokens_details")
        if ctd:
            details = CompletionTokensDetails()
            if ctd.get("reasoning_tokens") is not None:
                details["reasoning_tokens"] = ctd["reasoning_tokens"]
            if ctd.get("output_tokens") is not None:
                details["output_tokens"] = ctd["output_tokens"]
            if ctd.get("text_tokens") is not None:
                details["text_tokens"] = ctd["text_tokens"]
            usage["completion_tokens_details"] = details

        # 可选：prompt_tokens_details
        ptd = usage_data.get("prompt_tokens_details")
        if ptd:
            details = PromptTokensDetails()
            if ptd.get("cached_tokens") is not None:
                details["cached_tokens"] = ptd["cached_tokens"]
            if ptd.get("text_tokens") is not None:
                details["text_tokens"] = ptd["text_tokens"]
            # 百炼 Anthropic-compat 显式缓存回执（cache_control 命中/写入时出现）：
            #   - cache_creation_input_tokens: 本次写入的 token 数
            #   - cache_type:                  "ephemeral" 等缓存类别
            #   - cache_creation:              按 TTL 桶的明细 dict（原样透传）
            if ptd.get("cache_creation_input_tokens") is not None:
                details["cache_creation_input_tokens"] = ptd["cache_creation_input_tokens"]
            if ptd.get("cache_type") is not None:
                details["cache_type"] = ptd["cache_type"]
            if ptd.get("cache_creation") is not None:
                details["cache_creation"] = ptd["cache_creation"]
            usage["prompt_tokens_details"] = details

        # ── L4 归一：按 caps 字段路径回填非标准位置的缓存数据 ──
        # 针对 DeepSeek（usage.prompt_cache_hit_tokens）、
        # Anthropic（usage.cache_read_input_tokens）等字段名跟 OpenAI 标准不同的 provider，
        # 确保 prompt_tokens_details.cached_tokens 始终有值。
        if self._capabilities is not None:
            ptd_out = usage.get("prompt_tokens_details")
            if ptd_out is None:
                ptd_out = PromptTokensDetails()

            # 回填 cached_tokens（命中量）
            if "cached_tokens" not in ptd_out and self._capabilities.cached_tokens_field:
                val = _dig(usage_data, self._capabilities.cached_tokens_field)
                if val is not None:
                    ptd_out["cached_tokens"] = int(val)

            # 回填 cache_creation_input_tokens（写入量）
            if "cache_creation_input_tokens" not in ptd_out and self._capabilities.cache_creation_field:
                val = _dig(usage_data, self._capabilities.cache_creation_field)
                if val is not None:
                    ptd_out["cache_creation_input_tokens"] = int(val)

            # 只有确实填入了内容才写回（避免空 dict 噪音）
            if ptd_out:
                usage["prompt_tokens_details"] = ptd_out

        return usage

    @staticmethod
    def _emit_capability_warnings(
        payload: dict[str, Any],
        caps: EndpointCapabilities | None,
    ) -> None:
        """扫描请求 payload，对"当前 provider 不支持的字段"发出告警。

        保证用户知情权的核心实现——SDK 透传原则下，不支持的字段会被 provider
        静默忽略，若不显式告知，使用者可能误以为自己开启了某能力。此方法在每次
        调用前被触发，通过 `warnings.warn(ProviderCapabilityWarning)` + logger.warning
        双通道告警。

        规则（按 2026 年事实最小覆盖）：
          1. messages[*].content 若为 list 且任一 block 带 cache_control，
             但 caps.explicit_cache != "cache_control" → 告警
             （典型：豆包 chat 挂 cache_control 没效果）
          2. tools 上挂 cache_control 且 caps.supports_tool_cache_control=False → 告警
             （如 DashScope/Qwen 及所有隐式 provider：工具级 cache_control 被静默忽略；
              仅 Anthropic messages 支持工具级断点，故独立于 explicit_cache 判断）
          3. 顶层 cache_control → 告警（协议上就不存在这个位置）
          4. payload 里出现 "context_id" 但 caps.explicit_cache != "context_id" → 告警
             （典型：给 OpenAI / 百炼 传 context_id）
          5. payload 顶层出现思考类字段，但字段名与 caps.reasoning_control 不匹配 → 告警
             例如给火山方舟（thinking）传了 enable_thinking（百炼协议）
          6. payload 顶层出现 "parallel_tool_calls": true，但 caps.supports_parallel_tool_calls=False → 告警
          7. tool_choice == "required" 但 caps.supports_tool_choice_required=False → 告警

        caps 为 None 时**完全不做**任何校验（未知 provider，保持纯透传语义）。

        Args:
            payload: 即将发送的请求体（已合并 messages/tools/extra_body）
            caps:    注入的 EndpointCapabilities；None = 跳过
        """
        if caps is None:
            return

        provider = caps.provider
        endpoint = caps.endpoint

        def _warn(msg: str) -> None:
            full = f"[{provider}:{endpoint}] {msg}"
            warnings.warn(full, ProviderCapabilityWarning, stacklevel=3)
            logger.warning("ProviderCapabilityWarning: %s", full)

        # ── 1+3) cache_control 误用（messages / 顶层）───────
        if caps.explicit_cache != "cache_control":
            # messages 里挂 cache_control
            for idx, msg in enumerate(payload.get("messages") or []):
                content = msg.get("content") if isinstance(msg, dict) else None
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and "cache_control" in block:
                            # 按当前 provider 给出最实用的迁移建议：
                            # - 火山方舟：同家有 Context API 可用，直接指路
                            # - context_id 类 caps 自身（不会走到这里，见外层 if）
                            # - 其他（OpenAI 等）：承认"只能隐式"
                            if caps.provider == "volcengine":
                                suggest = (
                                    " 火山方舟 chat 不解析 cache_control（实测返回 200 但被静默丢弃），"
                                    "若需显式缓存请走 Context API：POST /context/create 获取 "
                                    "context_id 后在后续 /chat 请求中带上（model 需为接入点 ID ep-xxx）。"
                                )
                            else:
                                suggest = (
                                    " 该 provider 不支持显式缓存，只能依赖服务端隐式缓存"
                                    "（命中不稳定、无写入字段可观测）。"
                                )
                            _warn(
                                f"messages[{idx}].content 挂了 cache_control，"
                                f"但该 provider explicit_cache={caps.explicit_cache!r}，"
                                "此字段会被服务端静默忽略。" + suggest
                            )
                            break  # 同一条 message 只告警一次

            # 顶层 cache_control（协议上不存在这个位置）
            if "cache_control" in payload:
                _warn(
                    "payload 顶层出现 cache_control 字段，该字段应挂在 message "
                    "content 块内部，顶层位置不会被任何 provider 识别。"
                )

        # ── 2) tools 级 cache_control：仅 Anthropic 类端点支持 ──
        # 独立于 explicit_cache 判断——DashScope 虽是 cache_control 端点，但工具级
        # cache_control 仍被官方忽略（supports_tool_cache_control=False）。
        if not caps.supports_tool_cache_control:
            for tool in payload.get("tools") or []:
                if isinstance(tool, dict) and "cache_control" in tool:
                    _warn(
                        "tools 上挂了 cache_control，但该端点 "
                        "supports_tool_cache_control=False，会被服务端静默忽略"
                        "（如 DashScope/Qwen：工具级缓存不支持，cache_control "
                        "只能挂在 messages content 上；仅 Anthropic 支持工具级断点）。"
                    )
                    break

        # ── 4) context_id 误用 ──────────────────────────────
        if "context_id" in payload and caps.explicit_cache != "context_id":
            _warn(
                f"payload 携带 context_id，但该 provider explicit_cache="
                f"{caps.explicit_cache!r}，不支持 Context API。该字段将被静默忽略。"
            )

        # ── 5) 推理/思考字段名错配 ──────────────────────────
        REASONING_FIELDS = {
            "reasoning": "reasoning",                  # OpenAI o1/o3
            "reasoning_effort": "reasoning_effort",    # 豆包 thinking 系列
            "thinking": "thinking",                    # 豆包 Seed 系列
            "enable_thinking": "enable_thinking",      # 千问
            "thinking_budget": "enable_thinking",      # 千问配套字段
        }
        for field_name, owner in REASONING_FIELDS.items():
            if field_name in payload:
                if caps.reasoning_control == "none":
                    _warn(
                        f"payload 携带推理字段 {field_name!r}，但该 provider 不支持"
                        "客户端控制推理强度（reasoning_control='none'），"
                        "字段将被静默忽略。"
                    )
                elif owner != caps.reasoning_control:
                    # 只对主字段告警，避免对 thinking_budget 重复告警
                    if field_name == owner:
                        _warn(
                            f"payload 使用了 {field_name!r} 控制推理强度，"
                            f"但该 provider 的推理控制字段是 "
                            f"{caps.reasoning_control!r}。字段名协议不匹配，"
                            "可能被服务端静默忽略。"
                        )

        # ── 6) parallel_tool_calls 不被支持 ──────────────────
        if (
            payload.get("parallel_tool_calls") is True
            and not caps.supports_parallel_tool_calls
        ):
            _warn(
                "payload 设置 parallel_tool_calls=True，但该 provider 不支持"
                "并行工具调用，字段将被静默忽略。"
            )

        # ── 7) tool_choice='required' 不被支持 ────────────────
        tc = payload.get("tool_choice")
        if tc == "required" and not caps.supports_tool_choice_required:
            _warn(
                "payload 设置 tool_choice='required'，但该 provider 不支持"
                "强制工具调用，可能降级为 'auto' 或报错。"
            )

    @staticmethod
    def _parse_sse_line(line: str) -> dict[str, Any] | _SseDone | None:
        """解析单行 SSE 数据。

        Returns:
            _SseDone：遇到 [DONE] 终止信号
            dict：成功解析的 JSON 数据
            None：心跳行（如 \": ping\"）或格式错误，调用方应跳过
        """
        if not line.startswith("data:"):
            # 心跳或注释行，跳过
            return None

        payload_str = line[5:].strip()

        if payload_str == "[DONE]":
            return _SSE_DONE

        try:
            return json.loads(payload_str)
        except json.JSONDecodeError:
            logger.warning("SSE 行 JSON 解析失败，已跳过: %r", line)
            return None

    @staticmethod
    def _classify_http_error(
        status_code: int,
        response_body: str,
        headers: dict[str, str],
    ) -> LLMError:
        """将 HTTP 非 2xx 状态码映射为强类型 LLMError 子类。

        原则 3：显式错误分类——任何 HTTP 错误都必须经此方法转换，
        不允许 httpx 原始异常逃逸到 AgentLoop。

        Returns:
            对应的 LLMError 子类实例（供 raise 使用）
        """
        message = f"HTTP {status_code}: {response_body[:200]}"

        if status_code in (401, 403):
            return LLMAuthError(message, status_code=status_code)

        if status_code == 400:
            return LLMRequestError(message, status_code=400)

        if status_code == 408:
            return LLMTimeoutError(message)

        if status_code == 429:
            retry_after: float | None = None
            # 优先读 Retry-After-Ms（毫秒），其次读 Retry-After（秒）
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

        # 其他非 2xx（如 404 等）归入 LLMServerError
        return LLMServerError(message, status_code=status_code)
