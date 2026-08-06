"""pandaren/llm/router.py — LLMRouter 多 Provider 路由器

一个 OpenAICompatibleClient 实例绑定一个固定的 (api_key, model_name, base_url)；
当需要按模型名将请求路由到不同 provider（例如 qwen-* → dashscope,
doubao-* → volcengine, gpt-* → openai）时，应用层过去必须自己维护 if/elif
分派，侵入业务代码。

LLMRouter 通过组合 N 个 client 实例并对外仍满足 `LLMClient` Protocol，
让业务代码对多 provider 完全无感知。

设计原则：
  - 组合优先于继承：Router 不是 Client 的子类，而是持有一组 client 并按
    model_name 做分派。这使任何满足 LLMClient Protocol 的对象（包括第三方
    自实现）都可以被注册进来。
  - 路由规则固定且可预测：精确匹配 > 最长前缀匹配 > default。不引入
    动态的"模型能力发现"等隐式逻辑（原则 3：显式优于隐式）。
  - 资源生命周期集中管理：aclose() 关闭所有已注册的 client，上层无需
    分别持有和清理各 provider 的连接池。
  - Protocol 兼容：LLMRouter 本身可作为 LLMClient 注入给 AgentBuilder.llm()，
    因此 engine 层对单 client / 路由两种形态完全透明。

用法::

    from pandaren.llm import OpenAICompatibleClient, LLMRouter

    dashscope = OpenAICompatibleClient(
        api_key=os.environ["DASHSCOPE_KEY"],
        model_name="qwen-max",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    volcengine = OpenAICompatibleClient(
        api_key=os.environ["VOLC_KEY"],
        model_name="doubao-pro-32k",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
    )

    router = (
        LLMRouter()
        .register("qwen-", dashscope)
        .register("doubao-", volcengine)
        .set_default(dashscope)
    )

    # 即可当作单个 LLMClient 使用
    agent = AgentBuilder().llm(router).build()

    # 运行时通过 settings 指定 model_name 做路由
    await router.call(messages, settings=ModelSettings())   # 使用 default
"""

from __future__ import annotations

import logging
from typing import Any, AsyncGenerator

from .exceptions import LLMRequestError
from .protocol import LLMClient
from .types import LLMResponse, LLMStreamChunk, ModelSettings

logger = logging.getLogger("pandaren.llm.router")


class LLMRouter:
    """多 Provider 路由器，满足 `LLMClient` Protocol。

    路由顺序：
      1. 精确匹配：存在 `client.model_name == target_model` 的已注册 client
      2. 前缀匹配：取满足 `target_model.startswith(prefix)` 的最长 prefix 对应 client
      3. 默认回退：set_default() 指定的 client
      4. 无匹配且无 default → 抛 LLMRequestError

    target_model 的来源优先级：
      - 调用时的 settings.extra_body 中可选携带 "_route_model_name"（若希望显式覆盖）
      - 否则使用第一个已注册 client 所对应的 model_name 做示范性路由？不——
        LLMRouter 不猜测；当 ModelSettings 未显式指定路由键时，直接走 default。

    路由键选择说明：
      Router 按"客户端自己的 model_name"做索引。换句话说，当业务调用 router.call()
      时，由哪个 client 处理这次请求，取决于 Router 内部的注册表 + default。
      Router 不会根据 messages 的内容去"猜"模型——这是显式原则。
    """

    __slots__ = ("_exact", "_prefixes", "_default", "_primary")

    def __init__(self) -> None:
        # 精确匹配表：model_name → client
        self._exact: dict[str, LLMClient] = {}
        # 前缀匹配表：prefix → client
        self._prefixes: dict[str, LLMClient] = {}
        # 默认 client（无匹配时的 fallback）
        self._default: LLMClient | None = None
        # 主 client：决定 `router.model_name` 对外展示什么
        # 默认取第一个注册进来的 client；set_default 后改为 default
        self._primary: LLMClient | None = None

    # ─── 构造期 API ────────────────────────────────────────────

    def register(self, key: str, client: LLMClient) -> "LLMRouter":
        """注册一个 client，按 key 路由。

        key 的语义：
          - 以 `-` / `*` 等字符结尾表示前缀匹配，如 "qwen-"、"gpt-"
          - 否则视为精确匹配（key == model_name）

        约束：key 不能为空；同一 key 重复注册会覆盖旧值（方便热更新）。
        """
        if not key:
            raise ValueError("LLMRouter.register: key 不能为空")
        if client is None:  # type: ignore[truthy-bool]
            raise ValueError("LLMRouter.register: client 不能为 None")

        # 约定：以 `-` 或 `*` 结尾的 key 视为前缀；其余视为精确匹配
        is_prefix = key.endswith("-") or key.endswith("*")
        normalized = key.rstrip("*")
        if is_prefix:
            self._prefixes[normalized] = client
            logger.debug("LLMRouter register prefix: %r → %s", normalized, getattr(client, "model_name", "?"))
        else:
            self._exact[normalized] = client
            logger.debug("LLMRouter register exact: %r → %s", normalized, getattr(client, "model_name", "?"))

        # 首个注册的 client 暂作为 primary（未设 default 时对外展示 model_name）
        if self._primary is None:
            self._primary = client
        return self

    def set_default(self, client: LLMClient) -> "LLMRouter":
        """设置无匹配时的默认 client。

        同时将 router.model_name 对外展示改为 default.model_name，
        使上层引擎在未显式路由时看到一个确定的模型名。
        """
        if client is None:  # type: ignore[truthy-bool]
            raise ValueError("LLMRouter.set_default: client 不能为 None")
        self._default = client
        self._primary = client
        return self

    # ─── Protocol 实现 ─────────────────────────────────────────

    @property
    def model_name(self) -> str:
        """对外展示的 model_name。

        - 设置了 default：返回 default.model_name
        - 否则返回首个注册 client 的 model_name
        - 都没有：返回空字符串（意味着 Router 未初始化，任何 call 都会抛错）
        """
        if self._primary is None:
            return ""
        return self._primary.model_name

    @property
    def provider(self) -> str:
        """对外展示的 provider（取 primary/default client 的 provider）。

        供 `Agent.provider` 等未显式路由的场景回退到一个确定的 provider，
        避免 Router 无该属性时 getattr 回落空串导致预算按 provider 分账失真。
        逐 run 的精确 provider 应由 engine 通过 `resolve(settings).provider` 获取。
        """
        if self._primary is None:
            return ""
        return getattr(self._primary, "provider", "") or ""

    def resolve(self, settings: ModelSettings | None) -> LLMClient:
        """公开路由解析：按 `settings.target_model` 选出本次调用实际生效的 client。

        engine 用它读取「有效 client」的 model_name / provider，使 LLM_CALL 事件、
        Tracer span、审计与预算归属反映真正被路由到的模型，而非 default。
        """
        return self._resolve(settings)

    async def call(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        settings: ModelSettings | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """按路由规则选 client 执行非流式调用。

        ``**kwargs`` 原样透传给目标 client（如 engine 传入的 always_tools_count 等），
        使 Router 对 client 的完整调用签名保持透明——新增 client 参数无需改 Router。
        """
        client = self._resolve(settings)
        return await client.call(messages, tools=tools, settings=settings, **kwargs)

    async def stream_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        settings: ModelSettings | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[LLMStreamChunk, None]:
        """按路由规则选 client 执行流式调用。``**kwargs`` 原样透传（见 call）。"""
        client = self._resolve(settings)
        # 兼容第三方 client 可能未实现 stream_response 的情况
        if not hasattr(client, "stream_response"):
            raise LLMRequestError(
                f"LLMRouter: 目标 client {client.model_name!r} 未实现 stream_response"
            )
        async for chunk in client.stream_response(
            messages, tools=tools, settings=settings, **kwargs
        ):
            yield chunk

    async def aclose(self) -> None:
        """关闭所有已注册 client 的连接池。

        逐个调用 client.aclose()，单个失败不影响其他 client 清理。
        default 若也在注册表中不会重复关闭（使用 id() 去重）。
        """
        seen: set[int] = set()
        clients: list[LLMClient] = []
        for c in (*self._exact.values(), *self._prefixes.values()):
            if id(c) not in seen:
                seen.add(id(c))
                clients.append(c)
        if self._default is not None and id(self._default) not in seen:
            clients.append(self._default)

        for c in clients:
            close = getattr(c, "aclose", None)
            if close is None:
                continue
            try:
                await close()
            except Exception as e:  # noqa: BLE001
                logger.warning("LLMRouter.aclose: client %s close failed: %s", getattr(c, "model_name", "?"), e)

    # ─── 内部路由解析 ──────────────────────────────────────────

    def _resolve(self, settings: ModelSettings | None) -> LLMClient:
        """决定本次调用由哪个 client 处理。

        路由键来自 settings.target_model（ModelSettings 专属路由字段）。
        该字段由 Router 消费，不写入 HTTP 请求体。
        调用方若希望在单次调用中临时切换 provider，可在 settings 中传入：

            ModelSettings(target_model="doubao-pro-32k")

        未显式指定 target_model（None）时，直接走 default（若有），否则走 primary。
        """
        route_key: str | None = None
        if settings is not None:
            route_key = settings.target_model
        if not route_key:
            # 未显式指定路由键：走 default（若有），否则走 primary
            target = self._default or self._primary
            if target is None:
                raise LLMRequestError(
                    "LLMRouter: 未注册任何 client，且无 default，无法路由"
                )
            return target

        # 1) 精确匹配
        if route_key in self._exact:
            return self._exact[route_key]

        # 2) 最长前缀匹配
        best_prefix: str | None = None
        for prefix in self._prefixes:
            if route_key.startswith(prefix):
                if best_prefix is None or len(prefix) > len(best_prefix):
                    best_prefix = prefix
        if best_prefix is not None:
            return self._prefixes[best_prefix]

        # 3) default
        if self._default is not None:
            return self._default

        raise LLMRequestError(
            f"LLMRouter: 模型 {route_key!r} 无匹配 client，且未配置 default"
        )
