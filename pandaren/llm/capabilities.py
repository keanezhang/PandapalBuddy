"""pandaren/llm/capabilities.py — 端点能力矩阵（声明而非抽象）

设计哲学（重要）
────────────────────────────────────────────────────────────
本模块把"各家 LLM 差异"**公开展示给业务层**，而不是"封装抹平"。
参见 docs/工程化设计文档/框架设计/14_llm_abstraction_principle.md：

    L1 协议方言  → SDK 抹平（messages / tools / response 形状统一）
    L2 能力差异  → SDK 声明（通过 EndpointCapabilities 让业务层查询）
    L3 语义差异  → SDK 透传（provider_extra / extra_body 原样送到 body）
    L4 返回差异  → SDK 归一（usage / reasoning_content 统一字段名）

`EndpointCapabilities` 属于 **L2 声明**：SDK 不偷偷替业务层决策"这家要不要开缓存"，
只如实告诉业务层"这家能不能做某事、用哪套字段做"。

为什么是 `frozen=True` 的常量表？
    能力矩阵是**静态的协议事实**，不需要运行时探测——发现一家 provider 新支持了
    某能力，直接改这张表就行，不引入隐式逻辑。

为什么是 **「provider × endpoint」** 维度而不是「provider」维度？
────────────────────────────────────────────────────────────
同一家 provider 在不同端点上**能力完全不同**，按 provider 整颗定义会塌掉：

  - 火山方舟 `/chat/completions`：explicit_cache="none"（cache_control 静默忽略）
  - 火山方舟 `/context/create`：  explicit_cache="context_id"（独立上下文 API）
  - 火山方舟 `/v1/responses`：    explicit_cache="responses_api"（串接 ID）

所以常量以「平台 × 端点」命名：`VOLCENGINE_CHAT` / `VOLCENGINE_CONTEXT_API` /
`VOLCENGINE_RESPONSES`。业务层按端点常量查能力，不按 provider 名硬编码分支。

命名约定（重要）
────────────────────────────────────────────────────────────
常量名和 `provider` 字段值一律使用**平台/API 厂商名**，不是模型品牌名：

    平台名（本 SDK 规范）        常见模型品牌           官方 base_url 关键词
    ──────────────────────────────────────────────────────────────────────
    dashscope                   通义千问 Qwen          dashscope.aliyuncs.com
    volcengine                  豆包 Doubao            ark.cn-beijing.volces.com
    openai                      GPT / o1 / o3          api.openai.com
    deepseek                    DeepSeek V/R 系列      api.deepseek.com
    moonshot                    Kimi                   api.moonshot.cn
    anthropic                   Claude                 api.anthropic.com

理由：同一家平台可能同时提供多个模型品牌（百炼既跑 qwen 也跑 claude-compat），
能力声明（协议方言、缓存机制、工具调用支持）本质上是**平台级**事实，
按平台命名才不会在"同平台多品牌"时出现歧义。

用法示例
────────────────────────────────────────────────────────────
    from pandaren.llm import OpenAICompatibleClient
    from pandaren.llm.capabilities import VOLCENGINE_CHAT

    # 姿势 A（推荐）：工厂方法，自动绑定正确常量
    client = OpenAICompatibleClient.for_volcengine(
        api_key=...,
        model_name="doubao-seed-2-0-pro-260215",
    )

    # 姿势 B：显式注入 capabilities 常量
    client = OpenAICompatibleClient(
        api_key=...,
        model_name="doubao-seed-2-0-pro-260215",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        capabilities=VOLCENGINE_CHAT,
    )

    # 姿势 C：纯透传（不注入 capabilities），SDK 零干预
    client = OpenAICompatibleClient(
        api_key=..., model_name="xxx", base_url="...",
    )

    # 业务层按能力写分支（显式、可追溯）
    if client.capabilities is None:
        pass  # 未声明能力，业务层自行承担后果
    elif client.capabilities.explicit_cache == "none":
        # 火山方舟 chat / OpenAI chat / DeepSeek chat：不支持显式缓存
        # 但仍可能有隐式缓存（看 client.capabilities.implicit_cache）
        pass
    elif client.capabilities.explicit_cache == "context_id":
        # 火山方舟 Context API：先 POST /context/create 拿 context_id
        ...
    elif client.capabilities.explicit_cache == "cache_control":
        # 百炼 Claude-compat / Anthropic：挂 cache_control 断点
        ...
    elif client.capabilities.explicit_cache == "responses_api":
        # OpenAI / 火山 Seed 2.0 / 百炼 走 /v1/responses 端点
        # 使用 ResponsesAPIClient 实现
        ...

和 typed extras 的配合关系（"三件套"第 ② 件在 providers/）
────────────────────────────────────────────────────────────
本模块只**声明**能力、不**构造**参数。"走这条路时参数该怎么填"由
`pandaren/llm/providers/` 下的 typed extras 负责。两者字段一一对应：

    capabilities 告诉你走哪条路       →  对应的 typed extra 帮你填参数
    ──────────────────────────────────────────────────────────
    DASHSCOPE_CHAT.reasoning_control       DashScopeExtra(
      == "enable_thinking"                     enable_thinking=...,
                                               thinking_budget=...,
                                           )
    ──────────────────────────────────────────────────────────
    VOLCENGINE_CHAT.reasoning_control      VolcEngineExtra(
      == "thinking"                            thinking_mode="disabled"
                                                          |"enabled"|"auto",
                                           )
    ──────────────────────────────────────────────────────────
    caps.explicit_cache == "context_id"    VolcEngineExtra(context_id=...)
    ──────────────────────────────────────────────────────────
    caps.explicit_cache == "cache_control" 业务层在 messages content 上挂
                                           {"cache_control":{"type":"ephemeral"}}
                                           （协议字段在 message 层，
                                            不在 extra_body 里，
                                            故 typed extras 不覆盖此场景）

典型写法（capabilities 决策 → typed extras 填参 → ModelSettings 送出）：

    if client.capabilities.reasoning_control == "enable_thinking":
        extra = DashScopeExtra(enable_thinking=True, thinking_budget=4096)
    elif client.capabilities.reasoning_control == "thinking":
        extra = VolcEngineExtra(thinking_mode="disabled")
    else:
        extra = None

    settings = ModelSettings(
        temperature=0.7,
        extra_body=extra.as_extra_body() if extra else None,
    )

三件套总览见 pandaren/llm/__init__.py 顶部 docstring。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


# ═══════════════════════════════════════════════════════════════
# 枚举类型
# ═══════════════════════════════════════════════════════════════

EndpointKind = Literal["chat_completions", "context_api", "responses_api", "messages"]
"""端点类型。

- "chat_completions"  OpenAI 风格 /v1/chat/completions（绝大多数 provider 的默认端点）
- "context_api"       火山方舟 /context/create + /chat 双段式上下文缓存接口
- "responses_api"     OpenAI 风格 /v1/responses（OpenAI / 火山 Seed 2.0 / 百炼都提供）
- "messages"          Anthropic /v1/messages（原生协议；非 OpenAI 兼容）
"""


ExplicitCacheMode = Literal["none", "cache_control", "context_id", "responses_api"]
"""显式缓存机制分类（客户端是否能主动控制"这段内容进缓存"）。

- "none"         该端点**不支持**显式缓存
                 （可能仍有隐式缓存，看 implicit_cache 字段；cache_control 会被静默忽略）
                 典型代表：火山方舟 chat、OpenAI chat、DeepSeek chat
- "cache_control" 挂 `cache_control: {"type": "ephemeral"}` 在 message content 上
                 典型代表：Anthropic messages、阿里百炼 chat（Claude-compat 协议）
- "context_id"   走独立的"上下文缓存"接口（如火山方舟 /context/create）换 context_id，
                 后续 chat 请求带 context_id 命中
                 典型代表：火山方舟 Context API（豆包 1.5 / pro / deepseek 托管走这条）
- "responses_api" 走 OpenAI-style Responses API 端点，通过 `caching: {"type": "enabled"}`
                 + `previous_response_id` 串接多轮命中缓存
                 典型代表：OpenAI /v1/responses、火山方舟 Seed 1.6+/2.0、百炼 /v1/responses
                 SDK 通过 `ResponsesAPIClient` 实现此调用路径。
"""


ReasoningControlField = Literal["none", "reasoning", "reasoning_effort", "thinking", "enable_thinking"]
"""推理/思考参数的字段名（OpenAI 兼容 /chat/completions 端点）。

- "none"             该 provider 不支持客户端控制推理强度（或模型为"仅思考"型，无法关闭）
- "reasoning"        OpenAI o1/o3 系列，嵌套对象：{"reasoning": {"effort": "high"}}
- "reasoning_effort" 火山方舟 thinking 系列，顶层扁平：{"reasoning_effort": "high"}
                     （详见 docs/学习总结/llm/思考模型_reasoning_协议三家对比.md）
- "thinking"         火山方舟 Seed 系列，嵌套开关：{"thinking": {"type": "disabled"|"enabled"|"auto"}}
- "enable_thinking"  百炼 Qwen3 系列，顶层 bool + budget 组合：
                     {"enable_thinking": true, "thinking_budget": 4096}
"""


# ═══════════════════════════════════════════════════════════════
# 核心数据类
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class EndpointCapabilities:
    """端点能力矩阵（只读常量，描述「平台 × 端点」组合上的协议事实）。

    设计原则：
      - **frozen=True**：构造后不可改；能力矩阵是静态协议事实
      - **只描述、不决策**：字段值只回答"能/不能""用哪套字段"，不替业务层选方案
      - **字段粒度细化**：宁愿多几个字段，不要一个大而全的 "features" 字符串
      - **所有枚举值穷举**：Literal 类型，IDE 补全能看到全部可选值
      - **常量维度 = 「provider × endpoint」**：同一家的不同端点能力完全不同，
        按 provider 聚合会塌掉（见模块顶部 docstring）

    字段分组：
      provider / endpoint                  身份识别
      explicit_cache / implicit_cache      缓存机制（L2 能力声明）
      cached_tokens_field /                缓存可观测性（L4 返回字段；报告命中/写入的 usage 路径）
      cache_creation_field
      max_cache_breakpoints / min_cache_tokens / cache_ttl_seconds / cache_control_type
                                           缓存细节（L2 能力声明）
      cache_write_surcharge_percent        缓存计费（L3 语义事实）
      reasoning_control / reasoning_control_values / reasoning_budget_field
                                           推理/思考相关（L2 能力声明）
      returns_reasoning_content            L4 返回字段
      supports_parallel_tool_calls / supports_tool_choice_required
                                           工具调用相关（L2）
      notes                                人读备注（docstring 风格，不参与代码逻辑）
    """

    # ─── 身份识别 ─────────────────────────────────────────────
    provider: str
    """Provider 的**平台/API 厂商**名，用于日志 + 告警信息。

    规范：用**平台名**而非模型品牌名。见本模块顶部"命名约定"小节。
    典型值：dashscope / volcengine / openai / deepseek / moonshot / anthropic。
    """

    endpoint: EndpointKind
    """端点类型。见 EndpointKind docstring。

    同一家 provider 的不同端点能力完全不同（如火山方舟 chat vs Context API），
    所以 capability 是按「provider × endpoint」定义的，不是按 provider 定义。"""

    # ─── 缓存机制 ─────────────────────────────────────────────
    explicit_cache: ExplicitCacheMode
    """显式缓存机制（客户端主动控制）。见 ExplicitCacheMode docstring。"""

    implicit_cache: bool
    """是否有服务端自动的**隐式**前缀缓存（对客户端透明，无需任何字段即自动命中）。

    与 explicit_cache 是两个正交维度：
    - 火山方舟 chat: explicit_cache="none" + implicit_cache=True    （只有隐式）
    - 阿里百炼 chat: explicit_cache="cache_control" + implicit_cache=True  （两种并存，单请求二选一）
    - DeepSeek chat: explicit_cache="none" + implicit_cache=True    （只有隐式）
    - Anthropic:    explicit_cache="cache_control" + implicit_cache=False （只有显式）
    """

    # ─── 缓存可观测性（L4 返回字段路径）───────────────────────
    cached_tokens_field: str | None
    """响应 usage 中"命中读取 token 数"的字段路径（点号分隔）。

    L4 返回归一的**配置位**：SDK 的通用 usage 提取器会按这个路径抓命中量，
    业务层不需要记"哪家在哪儿"。若该端点协议本身就不报告命中量（罕见），填 None。

    实际值对照（2026 年事实）：
    - OpenAI chat / 火山方舟 chat / 阿里百炼 chat: "usage.prompt_tokens_details.cached_tokens"
    - OpenAI Responses / 火山 Responses / 阿里 Responses: "usage.input_tokens_details.cached_tokens"
    - DeepSeek chat: "usage.prompt_cache_hit_tokens"  ← 字段名跟别家完全不同
    - Anthropic messages: "usage.cache_read_input_tokens"  ← 同样自成一派
    """

    cache_creation_field: str | None
    """响应 usage 中"本次写入缓存 token 数"的字段路径（点号分隔）。

    只有**显式缓存协议**或**显式区分 hit/miss 的隐式协议**才有这个字段。
    纯隐式缓存（OpenAI chat、火山方舟 chat）协议本身**就没有**写入量字段——
    这是协议差异不是字段名方言，填 None 表示"写入量对客户端不可见"。

    实际值对照：
    - 阿里百炼 chat（OpenAI/Claude-compat）: "usage.prompt_tokens_details.cache_creation_input_tokens"
    - 火山方舟 Context API（/context/create 响应）: "usage.prompt_tokens"
      （整段写入就是首次 create 响应报告的 prompt_tokens）
    - DeepSeek chat: "usage.prompt_cache_miss_tokens"
      （DeepSeek 把 miss 也报出来了，用于计费透明度）
    - Anthropic messages: "usage.cache_creation_input_tokens"
    - 其他纯隐式：None
    """

    # ─── 缓存细节 ─────────────────────────────────────────────
    max_cache_breakpoints: int
    """单次请求最多能挂几个 cache_control 断点。

    - Anthropic / 阿里百炼: 4（超过 4 个时仅最后 4 个生效）
    - 火山方舟 chat: 0（不支持）
    - 火山方舟 Context API: 0（整段上下文作为一个单元缓存，不需要断点）
    - Responses API: 0（用串接 ID 不用 breakpoint）
    """

    min_cache_tokens: int | None
    """显式/隐式缓存的最少 token 门槛。低于此值的内容不会被缓存。
    - 阿里百炼 显式 ephemeral: 1024
    - OpenAI 隐式: 1024
    - 火山方舟 Context API: 由服务端决定（填 None）
    - 不知道/未公开: None
    """

    cache_ttl_seconds: int | None
    """显式缓存的 TTL（秒）。命中后是否重置视 provider 而定。
    - 阿里百炼 ephemeral: 300（5 分钟，命中重置）
    - 火山方舟 Context API: 由创建时参数决定（填 None）
    - 无显式缓存: None
    """

    cache_control_type: str | None
    """cache_control 的 type 取值（当 explicit_cache == "cache_control" 时有效）。
    - 阿里百炼/Anthropic: "ephemeral"
    - 其他: None
    """

    cache_write_surcharge_percent: int | None
    """首次写入缓存的单价溢价（相对标准输入单价的百分比）。
    - Anthropic / 阿里百炼 ephemeral: 125（首次写入按 125% 计费）
    - 火山方舟（所有路径）: 100（无溢价，按标准价计费）
    - 无显式缓存: None
    """

    # ─── 推理/思考能力 ─────────────────────────────────────────
    reasoning_control: ReasoningControlField
    """客户端通过哪种字段控制推理强度。见 ReasoningControlField docstring。"""

    reasoning_control_values: tuple[str, ...]
    """推理强度的合法取值。
    - OpenAI reasoning.effort:       ("low", "medium", "high")
    - 火山方舟 reasoning_effort:      ("low", "medium", "high")
    - 火山方舟 thinking.type:         ("disabled", "enabled", "auto")
    - 阿里百炼 enable_thinking:       ("true", "false")   —— bool 值转字符串声明，方便统一校验
    - 不支持:                         ()
    """

    reasoning_budget_field: str | None
    """推理"预算"字段名（硬上限 token 数）。
    - 阿里百炼 Qwen3: "thinking_budget"（整数，到点强制收敛进入生成）
    - 其他: None
    """

    returns_reasoning_content: bool
    """响应是否返回思考过程原文（message.reasoning_content / message.reasoning）。
    阿里百炼 Qwen / 火山方舟 豆包 思考模型都会返回；OpenAI o1/o3 不会返回，只返回 reasoning_tokens 计数。"""

    # ─── 工具调用能力 ─────────────────────────────────────────
    supports_parallel_tool_calls: bool
    """是否支持 parallel_tool_calls: true/false（OpenAI 1106+ 引入）。"""

    supports_tool_choice_required: bool
    """tool_choice 是否支持 "required"（强制调用某个工具）。
    OpenAI / 阿里百炼 / 火山方舟都支持；部分小众 provider 只支持 auto/none。"""

    # ─── 缓存：工具级 cache_control 支持性 ─────────────────────
    supports_tool_cache_control: bool = False
    """该端点是否支持在 **tools 定义** 上挂 cache_control 断点。

    官方事实（2026）：
      - Anthropic messages（Claude 原生）: True —— 工具 schema 是可缓存前缀，
        在 tools 上挂 cache_control 会生成一个有效断点。
      - 阿里百炼 DashScope（Qwen）: False —— 官方明确"工具定义不支持独立缓存，
        在工具定义中添加缓存标记会被忽略"，cache_control 只能挂在 messages content 上。
        （tools 仍作为 system 之前的前缀被 system 断点覆盖，无需也不能单独挂。）
      - 其余隐式缓存 provider（explicit_cache != "cache_control"）: 该字段不被消费。

    默认 False（安全侧）：未知/未声明端点不主动在 tools 上挂断点，避免"以为开了、
    实际被静默忽略"。SDK 的自动断点逻辑（cache_strategy.resolve_cache_positions）
    仅在本字段为 True 时才生成 tools 层断点①。"""

    # ─── 备注 ─────────────────────────────────────────────────
    notes: str = ""
    """人读备注，打印 capabilities 时展示。不参与代码逻辑判断。"""


# ═══════════════════════════════════════════════════════════════
# 内置「provider × endpoint」能力表
# ═══════════════════════════════════════════════════════════════
#
# 下面这些常量的事实依据：
#   - 阿里百炼 Context Cache: https://help.aliyun.com/zh/model-studio/context-cache
#   - 阿里百炼 深度思考:      https://help.aliyun.com/zh/model-studio/deep-thinking
#   - 火山方舟 chat + Context API: 实测数据见 assistant/real/explicit_cache_probe.py
#     （以及 docs/学习总结/prefix_cache/explicit_vs_implicit_cache.md 2.5 / 2.6 节）
#   - OpenAI 推理模型: https://platform.openai.com/docs/guides/reasoning
#   - DeepSeek 缓存:   https://api-docs.deepseek.com/guides/kv_cache
#
# 命名：`<PROVIDER>_<ENDPOINT>`，全大写下划线分隔。
#   - CHAT       → /v1/chat/completions
#   - CONTEXT_API→ /context/create + /chat（火山方舟独有）
#   - RESPONSES  → /v1/responses
#   - MESSAGES   → /v1/messages（Anthropic 原生）
#
# 新增「provider × endpoint」时：
#   1. 在下方新增一个 const（用平台名 + 端点后缀，如 MOONSHOT_CHAT / ANTHROPIC_MESSAGES）
#   2. 在 pandaren/llm/client.py 里加一个对应的 `for_xxx` 工厂方法
#   3. 如果该端点有独特字段，在 pandaren/llm/providers/ 下加一个 typed extra
#   完整 checklist 见 pandaren/llm/ADDING_A_PROVIDER.md
#
# ═══════════════════════════════════════════════════════════════

DASHSCOPE_CHAT = EndpointCapabilities(
    provider="dashscope",
    endpoint="chat_completions",

    # 缓存：显式 cache_control + 隐式并存，单请求命中一种
    explicit_cache="cache_control",
    implicit_cache=True,
    cached_tokens_field="usage.prompt_tokens_details.cached_tokens",
    cache_creation_field="usage.prompt_tokens_details.cache_creation_input_tokens",
    max_cache_breakpoints=4,
    min_cache_tokens=1024,
    cache_ttl_seconds=300,
    cache_control_type="ephemeral",
    cache_write_surcharge_percent=125,       # 百炼显式缓存首写 125% 计费

    # 推理（Qwen3 混合思考）
    reasoning_control="enable_thinking",
    reasoning_control_values=("true", "false"),
    reasoning_budget_field="thinking_budget",
    returns_reasoning_content=True,

    # 工具
    supports_parallel_tool_calls=True,
    supports_tool_choice_required=True,
    supports_tool_cache_control=False,       # 官方：工具级 cache_control 被忽略，只能挂 messages content

    notes=(
        "阿里云百炼（DashScope）OpenAI 兼容模式，常跑 Qwen 系列。"
        "缓存：显式 cache_control（ephemeral）和隐式两种，互斥，单请求只命中一种。"
        "思考：Python SDK 需通过 extra_body 传 enable_thinking / thinking_budget。"
        "部分 Qwen3 开源版支持 /think / /no_think 动态切换（提示词控制）。"
    ),
)


VOLCENGINE_CHAT = EndpointCapabilities(
    provider="volcengine",
    endpoint="chat_completions",

    # 缓存：实测火山方舟 OpenAI 兼容层静默忽略 cache_control（C 组非法值返回 200）
    # 只有隐式自动 prefix cache，命中不稳定
    explicit_cache="none",
    implicit_cache=True,
    cached_tokens_field="usage.prompt_tokens_details.cached_tokens",
    cache_creation_field=None,               # ⚠️ 协议本身就没有这个字段（不是漏抓）
    max_cache_breakpoints=0,
    min_cache_tokens=None,
    cache_ttl_seconds=None,
    cache_control_type=None,
    cache_write_surcharge_percent=None,

    # 推理（Doubao Seed 系列用 thinking 嵌套对象；thinking 系列用 reasoning_effort 顶层）
    # 这里按最常见的 Seed 协议登记；业务层如果用的是 thinking 子系列，走 extra_body 即可
    reasoning_control="thinking",
    reasoning_control_values=("disabled", "enabled", "auto"),
    reasoning_budget_field=None,
    returns_reasoning_content=True,

    # 工具
    supports_parallel_tool_calls=True,
    supports_tool_choice_required=True,

    notes=(
        "火山引擎方舟（VolcEngine Ark）OpenAI 兼容 /chat/completions 端点，常跑 Doubao 系列。"
        "缓存：**仅隐式**，cache_control 被静默忽略（实测见 explicit_cache_probe.py C 组）。"
        "想要显式缓存必须切到 /context/create + context_id，见 VOLCENGINE_CONTEXT_API。"
        "思考：Seed 系列用 thinking={type:...}；thinking 系列用 reasoning_effort='low|medium|high'。"
    ),
)


VOLCENGINE_CONTEXT_API = EndpointCapabilities(
    provider="volcengine",
    endpoint="context_api",

    # 缓存：显式 Context API，客户端握住写入权
    explicit_cache="context_id",
    implicit_cache=False,                    # Context API 路径本身就是显式，不叠加隐式
    cached_tokens_field="usage.prompt_tokens_details.cached_tokens",  # /chat 响应里报告
    cache_creation_field="usage.prompt_tokens",  # /context/create 响应的 usage.prompt_tokens 就是写入量
    max_cache_breakpoints=0,                 # 整段上下文作为一个单元，不用断点
    min_cache_tokens=None,                   # 由火山方舟服务端决定
    cache_ttl_seconds=None,                  # 创建时 TTL 参数决定，默认值以官方文档为准
    cache_control_type=None,                 # 不走 cache_control 字段
    cache_write_surcharge_percent=100,       # 无溢价

    # 推理
    reasoning_control="thinking",
    reasoning_control_values=("disabled", "enabled", "auto"),
    reasoning_budget_field=None,
    returns_reasoning_content=True,

    # 工具
    supports_parallel_tool_calls=True,
    supports_tool_choice_required=True,

    notes=(
        "火山方舟 Context API（显式缓存）。流程：POST /context/create → 拿 context_id → "
        "后续 /chat 请求带 context_id（model 字段必须是接入点 ID ep-xxx）。"
        "写入量 = /context/create 响应的 usage.prompt_tokens；"
        "命中量 = /chat 响应的 prompt_tokens_details.cached_tokens。"
    ),
)


OPENAI_CHAT = EndpointCapabilities(
    provider="openai",
    endpoint="chat_completions",

    # 缓存：OpenAI chat 只有隐式（2024Q4 起自动开启）
    explicit_cache="none",
    implicit_cache=True,
    cached_tokens_field="usage.prompt_tokens_details.cached_tokens",
    cache_creation_field=None,               # 隐式缓存无写入字段
    max_cache_breakpoints=0,
    min_cache_tokens=1024,                   # OpenAI 隐式缓存门槛 1024 token
    cache_ttl_seconds=None,
    cache_control_type=None,
    cache_write_surcharge_percent=None,

    # 推理（o1 / o3 系列）
    reasoning_control="reasoning",
    reasoning_control_values=("low", "medium", "high"),
    reasoning_budget_field=None,
    returns_reasoning_content=False,         # OpenAI 不返回思考原文，只返回 reasoning_tokens

    # 工具
    supports_parallel_tool_calls=True,
    supports_tool_choice_required=True,

    notes=(
        "OpenAI 原生 /chat/completions 端点。隐式缓存 2024Q4 起自动开启，"
        "显式缓存需走 Responses API（见 OPENAI_RESPONSES）或 Assistants API（本 SDK 不覆盖）。"
    ),
)


DEEPSEEK_CHAT = EndpointCapabilities(
    provider="deepseek",
    endpoint="chat_completions",

    # 缓存：DeepSeek 只有隐式 prefix cache（自动开启、自动命中）
    # cache_control 字段不识别（跟火山方舟 chat 同构——静默忽略，不报错）
    explicit_cache="none",
    implicit_cache=True,
    cached_tokens_field="usage.prompt_cache_hit_tokens",   # ⚠️ DeepSeek 专属字段名，不是 prompt_tokens_details.cached_tokens
    cache_creation_field="usage.prompt_cache_miss_tokens", # DeepSeek 把 miss 也报出来，用于计费透明度
    max_cache_breakpoints=0,
    min_cache_tokens=None,                   # 服务端决定，官方未明确公开门槛
    cache_ttl_seconds=None,
    cache_control_type=None,
    cache_write_surcharge_percent=None,      # 无溢价，miss 按标准价；hit 有独立折扣价

    # 推理（DeepSeek-R1 系列）
    # DeepSeek 的思考模式是**模型级**开关（用哪个 model_name 决定），客户端无字段可控
    reasoning_control="none",
    reasoning_control_values=(),
    reasoning_budget_field=None,
    returns_reasoning_content=True,          # R1 会在 message.reasoning_content 返回思考过程

    # 工具
    supports_parallel_tool_calls=True,
    supports_tool_choice_required=True,

    notes=(
        "DeepSeek 官方 /chat/completions 端点（api.deepseek.com）。"
        "缓存：**仅隐式** prefix cache，cache_control 字段被静默忽略（与火山方舟 chat 同构）。"
        "usage 报告字段名与 OpenAI 不同：prompt_cache_hit_tokens / prompt_cache_miss_tokens，"
        "不是 prompt_tokens_details.cached_tokens——业务层统计命中率时需要做字段适配。"
        "思考：DeepSeek-R1 走独立模型名触发（deepseek-reasoner），无客户端字段开关。"
    ),
)


# ═══════════════════════════════════════════════════════════════
# Responses API 端点声明
# ═══════════════════════════════════════════════════════════════
#
# 下面三条常量描述的是各家 /v1/responses 端点的能力。
# SDK 通过 `ResponsesAPIClient`（pandaren/llm/responses_client.py）
# 实现了 Responses API 调用路径，对外满足 LLMClient Protocol。
#
# 用法示例：
#   from pandaren.llm import ResponsesAPIClient
#   client = ResponsesAPIClient.for_openai_responses(api_key=..., model_name="gpt-4o")
#   resp = await client.call(messages, tools=tools)
#
# 保留这些常量的理由（和 VOLCENGINE_CONTEXT_API 同样的先例）：
#   1. 能力矩阵是**协议事实**，不该被"SDK 实现到没到"耽误记录
#   2. 业务层调研显式缓存时，`capabilities.py` 就是权威文档
#   3. ResponsesAPIClient 的工厂方法自动绑定这些常量
#
# 参考官方文档：
#   * OpenAI:   https://platform.openai.com/docs/api-reference/responses
#   * 火山方舟:   https://www.volcengine.com/docs/82379/1494384（Responses API）
#   * 阿里百炼:   https://help.aliyun.com/zh/model-studio/responses-api
#
# ═══════════════════════════════════════════════════════════════

OPENAI_RESPONSES = EndpointCapabilities(
    provider="openai",
    endpoint="responses_api",

    # 缓存：Responses API 本身支持隐式缓存（与 chat 同逻辑），
    # 且通过 previous_response_id 串接多轮时自动命中前缀
    explicit_cache="responses_api",
    implicit_cache=True,                     # Responses 端点同样有前缀自动缓存
    cached_tokens_field="usage.input_tokens_details.cached_tokens",
    cache_creation_field=None,               # 隐式性质，无写入字段
    max_cache_breakpoints=0,                 # Responses API 用串接 ID 而不是 breakpoint
    min_cache_tokens=1024,                   # 沿用 OpenAI 隐式缓存门槛
    cache_ttl_seconds=None,                  # 服务端决定
    cache_control_type=None,                 # 不走 cache_control 字段
    cache_write_surcharge_percent=None,

    # 推理（o1 / o3 系列在 Responses API 下）
    reasoning_control="reasoning",
    reasoning_control_values=("low", "medium", "high"),
    reasoning_budget_field=None,
    returns_reasoning_content=False,         # Responses 返回 reasoning items，但无文本

    # 工具
    supports_parallel_tool_calls=True,
    supports_tool_choice_required=True,

    notes=(
        "OpenAI /v1/responses 端点（Responses API）。支持 previous_response_id 串接多轮、"
        "内置工具（file_search / web_search / code_interpreter）、reasoning items 等 chat 没有的特性。"
        "SDK 通过 ResponsesAPIClient 实现此调用路径。"
    ),
)


VOLCENGINE_RESPONSES = EndpointCapabilities(
    provider="volcengine",
    endpoint="responses_api",

    # 缓存：火山方舟 Seed 1.6+ / 2.0 走 Responses API 才能拿到显式缓存控制
    # 协议通过 `caching: {"type": "enabled"}` + `previous_response_id` 实现前缀复用
    explicit_cache="responses_api",
    implicit_cache=True,                     # Responses 端点同样有自动前缀缓存（与 chat 端点一致）
    cached_tokens_field="usage.input_tokens_details.cached_tokens",  # 待实测确认字段名
    cache_creation_field=None,               # 待实测；目前按"无显式写入量字段"登记
    max_cache_breakpoints=0,
    min_cache_tokens=None,                   # 服务端决定
    cache_ttl_seconds=None,                  # 响应级自然 TTL + 显式 caching 开关控制
    cache_control_type=None,                 # 不走 cache_control 字段
    cache_write_surcharge_percent=None,      # 待确认计费口径

    # 推理（Seed 2.0 在 Responses API 下沿用 thinking）
    reasoning_control="thinking",
    reasoning_control_values=("disabled", "enabled", "auto"),
    reasoning_budget_field=None,
    returns_reasoning_content=True,

    # 工具
    supports_parallel_tool_calls=True,
    supports_tool_choice_required=True,

    notes=(
        "火山方舟 /v1/responses 端点。豆包 Seed 1.6+ / 2.0 通过此端点获得显式缓存能力"
        "（chat/completions 端点则只有隐式）。Seed 以下的 Doubao 1.5 / pro / deepseek 托管"
        "不走 Responses 路径，显式缓存走 /context/create（见 VOLCENGINE_CONTEXT_API）。"
        "SDK 通过 ResponsesAPIClient 实现此调用路径；"
        "cached_tokens_field / cache_creation_field / cache_write_surcharge 待实测探针补齐后更新。"
    ),
)


DASHSCOPE_RESPONSES = EndpointCapabilities(
    provider="dashscope",
    endpoint="responses_api",

    # 缓存：阿里百炼 Responses API 端点也支持显式缓存
    # 与 chat 端点的 cache_control 是**两条独立通路**，业务层二选一即可
    explicit_cache="responses_api",
    implicit_cache=True,                     # 阿里百炼 Responses 端点同样自动前缀缓存
    cached_tokens_field="usage.input_tokens_details.cached_tokens",  # 待实测确认字段名
    cache_creation_field="usage.input_tokens_details.cache_creation_input_tokens",
                                             # 阿里百炼在 Responses 端点大概率沿用"miss 首写"账单模型，待实测
    max_cache_breakpoints=0,
    min_cache_tokens=1024,                   # 沿用百炼缓存门槛，待实测确认
    cache_ttl_seconds=None,                  # 响应级 TTL + caching 开关控制
    cache_control_type=None,
    cache_write_surcharge_percent=125,       # 沿用 chat 端点 125% 首写规则，待实测确认

    # 推理（Qwen3 混合思考在 Responses 端点的字段形态待实测）
    # 假设与 chat 端点一致，如果实测不同再翻转
    reasoning_control="enable_thinking",
    reasoning_control_values=("true", "false"),
    reasoning_budget_field="thinking_budget",
    returns_reasoning_content=True,

    # 工具
    supports_parallel_tool_calls=True,
    supports_tool_choice_required=True,

    notes=(
        "阿里百炼 /v1/responses 端点。与 DASHSCOPE_CHAT 并列的第二条显式缓存通路——"
        "chat 端点挂 cache_control / Responses 端点用 caching+previous_response_id，"
        "两条路业务层二选一即可，不能混用。"
        "SDK 通过 ResponsesAPIClient 实现此调用路径；"
        "若干字段（cached_tokens_field / cache_creation_field / min_cache_tokens / "
        "cache_write_surcharge / 推理字段形态）待实测探针补齐。"
    ),
)
