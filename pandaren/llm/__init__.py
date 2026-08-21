"""pandaren/llm/__init__.py — LLM 层公共符号导出

═══════════════════════════════════════════════════════════════════════
SDK 给业务层的"三件套"（新人先读这段）
═══════════════════════════════════════════════════════════════════════

想发一个带厂商特性（思考控制 / 缓存 / 搜索增强）的 LLM 请求，你会同时
用到下面三样东西。它们物理上分散在三个文件里，但逻辑上是一套工作流：

    ┌────────────────────────────────────────────────────────────────┐
    │ ① EndpointCapabilities                       (capabilities.py) │
    │    ─ 端点说明书 / 只读静态常量（维度 = provider × endpoint）      │
    │    ─ 身份：DASHSCOPE_CHAT / VOLCENGINE_CHAT /                     │
    │             VOLCENGINE_CONTEXT_API / OPENAI_CHAT /                │
    │             DEEPSEEK_CHAT /                                       │
    │             OPENAI_RESPONSES / VOLCENGINE_RESPONSES /             │
    │             DASHSCOPE_RESPONSES (L2 声明，ResponsesAPIClient 已实现)  │
    │    ─ 回答："这个端点能干什么？用哪套字段名？"                       │
    │    ─ 用法：                                                      │
    │         if client.capabilities.reasoning_control == \\             │
    │            "enable_thinking":                                    │
    │             ...  # 走百炼/DashScope 这条路                          │
    ├────────────────────────────────────────────────────────────────┤
    │ ② typed extras                  (providers/dashscope.py etc.)  │
    │    ─ 厂商专属 extra_body 的填写助手 / 每次调用前 new 一个             │
    │    ─ 身份：DashScopeExtra / VolcEngineExtra                       │
    │    ─ 回答："这家的 extra_body 字段名、嵌套结构是什么？"                │
    │    ─ 用法：                                                      │
    │         ds_extra = DashScopeExtra(                               │
    │             enable_thinking=True,                                │
    │             thinking_budget=4096,                                │
    │         )                                                        │
    ├────────────────────────────────────────────────────────────────┤
    │ ③ ModelSettings                                    (types.py)  │
    │    ─ 通用请求参数 + 装 extra_body 的信封 / 每次调用前 new 一个        │
    │    ─ 回答："temperature / max_tokens / 我的 extra_body 一起打包"   │
    │    ─ 用法：                                                      │
    │         settings = ModelSettings(                                │
    │             temperature=0.7,                                     │
    │             extra_body=ds_extra.as_extra_body(),                 │
    │         )                                                        │
    └────────────────────────────────────────────────────────────────┘

命名约定（重要）
═══════════════════════════════════════════════════════════════════════
所有公开标识符（常量名 / 类名 / 工厂方法名 / `provider` 字段值）一律
使用**平台/API 厂商**名，不是模型品牌名：

    平台名（本 SDK 规范）   常跑模型品牌       工厂方法
    ─────────────────────────────────────────────────────
    dashscope              通义千问 Qwen      for_dashscope
    volcengine             豆包 Doubao        for_volcengine
    openai                 GPT / o1 / o3      for_openai
    moonshot (未来)        Kimi               for_moonshot
    anthropic (未来)       Claude             for_anthropic

理由见 capabilities.py 顶部"命名约定"小节（一句话：能力声明是**平台级**
事实，同平台可跑多品牌，按平台命名才不会在"同平台多品牌"时出现歧义）。

完整链路（三件套一起上）：

    from pandaren.llm import (
        OpenAICompatibleClient,     # 执行器
        ModelSettings,              # ③ 请求信封
        DashScopeExtra,             # ② 填写助手
        DASHSCOPE_CHAT,             # ① 说明书（工厂方法会自动绑定）
    )

    client = OpenAICompatibleClient.for_dashscope(api_key=..., model_name=...)

    # ① 先查说明书决定走哪条路
    if client.capabilities and client.capabilities.reasoning_control == "enable_thinking":
        # ② 用 typed extra 填这条路的参数
        ds_extra = DashScopeExtra(enable_thinking=True, thinking_budget=4096)
        # ③ 装进 ModelSettings 送出门
        settings = ModelSettings(
            temperature=0.7,
            extra_body=ds_extra.as_extra_body(),
        )
    else:
        settings = ModelSettings(temperature=0.7)

    resp = await client.call(messages, settings=settings)

可运行演示：
  python assistant/real/simple_llm_test.py --demo-caps    # ① 说明书怎么用
  python assistant/real/simple_llm_test.py --demo-extras  # ② 填写助手怎么用

═══════════════════════════════════════════════════════════════════════
子模块职责
═══════════════════════════════════════════════════════════════════════
  exceptions.py    — LLMError 异常层次（7 个子类）
  types.py         — FinishReason / ModelSettings / UsageInfo / LLMResponse / LLMStreamChunk / ToolCallDelta
  protocol.py      — LLMClient Protocol（最小接口契约，无 httpx 依赖）
  client.py        — OpenAICompatibleClient 实现（httpx 全异步，走 /v1/chat/completions）
  responses_client.py — ResponsesAPIClient 实现（httpx 全异步，走 /v1/responses）
  router.py        — LLMRouter 多 provider 路由器（满足 LLMClient Protocol）
  capabilities.py  — ① EndpointCapabilities 能力矩阵（声明而非抽象）
  providers/       — ② 各 provider 的 typed extra 结构（DashScopeExtra / VolcEngineExtra）

想加一家新 provider？
═══════════════════════════════════════════════════════════════════════
  读 pandaren/llm/ADDING_A_PROVIDER.md（6 步 checklist + 模板片段）
  typed extra 模板在 pandaren/llm/providers/_template.py.example
"""

from .exceptions import (
    LLMError,
    LLMAuthError,
    LLMRequestError,
    LLMRateLimitError,
    LLMServerError,
    LLMNetworkError,
    LLMTimeoutError,
    LLMResponseError,
)
from .types import (
    FinishReason,
    ModelSettings,
    UsageInfo,
    CompletionTokensDetails,
    PromptTokensDetails,
    LLMResponse,
    LLMStreamChunk,
    ToolCallDelta,
)
from .protocol import LLMClient
from .client import OpenAICompatibleClient, ProviderCapabilityWarning
from .responses_client import ResponsesAPIClient
from .cache_strategy import CacheDepth, CacheMode
from .router import LLMRouter
from .schema import json_schema, output_type_to_response_format
from .capabilities import (
    EndpointCapabilities,
    EndpointKind,
    ExplicitCacheMode,
    ReasoningControlField,
    DASHSCOPE_CHAT,
    VOLCENGINE_CHAT,
    VOLCENGINE_CONTEXT_API,
    OPENAI_CHAT,
    DEEPSEEK_CHAT,
    # Responses API 端点声明（L2 事实记录，调用路径由 responses_client.py 的 ResponsesAPIClient 实现）
    OPENAI_RESPONSES,
    VOLCENGINE_RESPONSES,
    DASHSCOPE_RESPONSES,
)
from .providers import DashScopeExtra, VolcEngineExtra
from .cache_usage import CacheUsage, extract_cache_usage

__all__ = [
    # 异常
    "LLMError",
    "LLMAuthError",
    "LLMRequestError",
    "LLMRateLimitError",
    "LLMServerError",
    "LLMNetworkError",
    "LLMTimeoutError",
    "LLMResponseError",
    # 类型
    "FinishReason",
    "ModelSettings",
    "UsageInfo",
    "CompletionTokensDetails",
    "PromptTokensDetails",
    "LLMResponse",
    "LLMStreamChunk",
    "ToolCallDelta",
    # Protocol
    "LLMClient",
    # 实现
    "OpenAICompatibleClient",
    "ResponsesAPIClient",
    "LLMRouter",
    "ProviderCapabilityWarning",
    # 工具函数
    "json_schema",
    "output_type_to_response_format",
    # 能力矩阵（① 端点说明书）
    "EndpointCapabilities",
    "EndpointKind",
    "ExplicitCacheMode",
    "ReasoningControlField",
    "DASHSCOPE_CHAT",
    "VOLCENGINE_CHAT",
    "VOLCENGINE_CONTEXT_API",
    "OPENAI_CHAT",
    "DEEPSEEK_CHAT",
    "OPENAI_RESPONSES",
    "VOLCENGINE_RESPONSES",
    "DASHSCOPE_RESPONSES",
    # typed extras（② 厂商 extra_body 填写助手）
    "DashScopeExtra",
    "VolcEngineExtra",
    # 缓存观测（§5 / §15）
    "CacheUsage",
    "extract_cache_usage",
    # 缓存配置类型
    "CacheDepth",
    "CacheMode",
]
