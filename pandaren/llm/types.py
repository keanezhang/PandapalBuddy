"""pandaren/llm/types.py — LLM 公共数据契约

定义 LLM 层对外暴露的所有数据类型：
  - FinishReason       终止原因 Literal 类型别名
  - ModelSettings      调参设置 dataclass（所有字段 None = 不覆盖 provider 默认）
  - UsageInfo          Token 用量 TypedDict
  - LLMResponse        非流式响应 TypedDict
  - LLMStreamChunk     流式响应单个 chunk dataclass

设计原则：
  - 所有类型均为纯数据定义，无任何 IO 或网络依赖
  - 上层（engine、memory）可单独引用类型，无需依赖 httpx 等实现依赖
  - 显式优于隐式：所有行为均通过显式字段控制，禁止 provider 白名单、魔法默认值等隐式逻辑
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict


# ═══════════════════════════════════════════════════════════════
# 类型别名
# ═══════════════════════════════════════════════════════════════

FinishReason = Literal["stop", "tool_calls", "length", "content_filter", "function_call"]
"""LLM 终止原因类型别名。

- stop：正常结束
- tool_calls：请求调用工具
- length：达到 max_tokens 限制
- content_filter：内容安全过滤
- function_call：旧版 function_call（部分 provider 仍使用）
"""


# ═══════════════════════════════════════════════════════════════
# 调参设置
# ═══════════════════════════════════════════════════════════════

@dataclass
class ModelSettings:
    """LLM 调参设置。

    所有字段默认 None，表示不覆盖 provider 默认值。
    构造时传入的字段会在 _build_payload 中合并进 request body。

    参数分类：
      通用参数（主流 provider 均支持）：
        temperature, max_tokens, top_p, frequency_penalty, presence_penalty, stop, seed

      输出格式控制：
        response_format：结构化输出，支持三种形式：
                         - dict: {"type": "json_object"} 或 {"type": "json_schema", ...}
                         - type: dataclass 或 Pydantic BaseModel（SDK 自动转为 json_schema）
                         - None: 不设置
                         支持情况因 provider 而异

      工具调用控制：
        tool_choice：控制模型如何选择工具调用
                     - "none"：不调用任何工具
                     - "auto"：由模型决定（默认）
                     - "required"：必须调用至少一个工具
                     - {"type": "function", "function": {"name": "..."}}：强制调用指定工具
        parallel_tool_calls：是否允许并行调用多个工具（OpenAI 1106+ 支持）

      流式行为控制：
        include_usage：流式模式下是否注入 stream_options.include_usage
                       - None（默认）：不注入（保持最小兼容，任何 provider 都不会因此报错）
                       - True：注入，请求 provider 在流的末尾返回 usage 统计
                       - False：与 None 等价（明确禁止注入），便于在代码中显式表达"不需要"
                       仅对 stream_response() 生效。

      推理模型控制：
        reasoning：仅承载 OpenAI 规范的嵌套 reasoning 对象（o1/o3 系列）。
                   序列化为 payload 顶层: {"reasoning": {...}}
                   None = 不传递，由 provider 使用默认推理强度

                   重要：各家 provider 的"思考强度"协议完全不统一，此字段只对
                   OpenAI / DeepSeek-R1 等走原生嵌套 reasoning 对象的 provider 生效。
                   豆包、通义千问的协议必须走 extra_body（见下方示例）。

                   各家用法示例：

                   # ① OpenAI o1 / o3（嵌套 reasoning 对象）
                   ModelSettings(reasoning={"effort": "high"})
                   # → payload: {"reasoning": {"effort": "high"}}

                   # ② 火山豆包 doubao-seed-2-* （顶层 snake_case，用 extra_body）
                   ModelSettings(extra_body={"reasoning_effort": "high"})
                   # → payload: {"reasoning_effort": "high"}
                   # 注意：豆包不认嵌套 reasoning.effort，必须顶层 snake_case

                   # ③ 阿里通义千问 qwen3 / qwen3.6-plus（硬上限协议，用 extra_body）
                   ModelSettings(extra_body={
                       "enable_thinking": True,
                       "thinking_budget": 4096,     # 512 约等 low，4096 约等 high
                   })
                   # → payload: {"enable_thinking": true, "thinking_budget": 4096}
                   # 注意：thinking_budget 是"最多思考 N tokens"的硬截断，
                   #       与 OpenAI/豆包的软提示语义不同；到点强制收敛进入生成

                   详见：docs/学习总结/llm/思考模型_reasoning_协议三家对比.md

      Router 路由控制：
        target_model：LLMRouter 的路由键，指定本次调用由哪个 provider 处理。
                      None（默认）= 使用 LLMRouter 的 default/primary client。
                      非 None = 按此值做精确匹配 → 最长前缀匹配 → default。
                      此字段由 LLMRouter._resolve() 消费，不写入 HTTP 请求体。
                      单 client 场景下此字段被忽略。

      Provider 专属扩展：
        extra_body：无法通过标准字段传递的 provider 专属顶层参数，
                    展开后作为 payload 顶层字段（payload.update(extra_body)）。

                    推荐用 typed 助手填（IDE 补全 + 嵌套结构自动处理）：
                      from pandaren.llm import DashScopeExtra, VolcEngineExtra
                      extra_body=DashScopeExtra(enable_thinking=True).as_extra_body()
                      extra_body=VolcEngineExtra(thinking_mode="disabled").as_extra_body()
                    可用的 typed 助手见 pandaren/llm/providers/（文件名按**平台名**组织：
                    dashscope.py 跑 Qwen、volcengine.py 跑 Doubao）。
                    （三件套总览见 pandaren/llm/__init__.py 顶部 docstring）

                    裸 dict 写法永远保留（可用于 SDK 还没覆盖的字段）：
                      - 千问思考参数：{"enable_thinking": True, "thinking_budget": 4096}
                      - 豆包思考参数：{"reasoning_effort": "high"}
                      - 千问联网搜索：{"enable_search": True}
        extra_headers：附加到 HTTP 请求头的自定义字段
                       合并到 Authorization/Content-Type 之上，同名键会被覆盖
                       用于某些网关的自定义 header（X-DashScope-Plugin 等）
        extra_query：附加到请求 URL 的 query 参数
                     如 {"api-version": "2024-02-01"} → URL 追加 ?api-version=2024-02-01
                     用于 Azure OpenAI 等按 query string 路由 API 版本的 provider
    """

    # ── 通用参数 ──
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    stop: list[str] | None = None
    seed: int | None = None

    # ── 输出格式 / 工具调用 ──
    response_format: dict[str, Any] | type | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None

    # ── 流式行为 ──
    include_usage: bool | None = None

    # ── 推理模型 ──
    reasoning: dict[str, Any] | None = None       # 推理模型配置，仅承载 OpenAI 规范嵌套对象，比如：reasoning={"effort": "low"}

    # ── Router 路由控制 ──
    target_model: str | None = None               # LLMRouter 路由键；None = 走 default/primary；由 Router 消费，不写入 HTTP 请求体

    # ── Provider 专属扩展 ──
    extra_body: dict[str, Any] | None = None      # provider 专属顶层参数；推荐用 DashScopeExtra/VolcEngineExtra(...).as_extra_body() 填，见 pandaren/llm/providers/
    extra_headers: dict[str, str] | None = None   # 附加到 HTTP 请求头的自定义字段，比如：extra_headers={"X-DashScope-Plugin": "plugin_name"}
    extra_query: dict[str, str] | None = None     # 附加到请求 URL 的 query 参数，比如：extra_query={"api-version": "2024-02-01"}


# ═══════════════════════════════════════════════════════════════
# 响应数据类型
# ═══════════════════════════════════════════════════════════════

class CompletionTokensDetails(TypedDict, total=False):
    """completion_tokens 的详细分项（OpenAI 可选字段）。

    字段说明：
      reasoning_tokens: 思考/推理 token 数（思考模型特有，如 qwen3.6-plus, doubao-thinking）
      output_tokens:    可见输出 token 数（OpenAI 规范；部分 provider 用 text_tokens 替代）
      text_tokens:      文本 token 数（Qwen 非标准字段，等价于 output_tokens）
    """

    reasoning_tokens: int     # 思考/推理 token 数（思考模型特有，如 qwen3.6-plus），观察细节的时候使用，思考token占比等。
    output_tokens: int
    text_tokens: int


class PromptTokensDetails(TypedDict, total=False):
    """prompt_tokens 的详细分项（OpenAI 可选字段）。

    字段说明：
      cached_tokens:                缓存命中的 token 数（Qwen/豆包均支持，OpenAI 标准字段）
      text_tokens:                  文本 token 数（Qwen 非标准字段）
      cache_creation_input_tokens:  本次写入缓存的 token 数（百炼 Anthropic-compat
                                    显式缓存回执；第 1 次调用带 cache_control 时非 0，
                                    后续命中时为 0）
      cache_type:                   本次使用的缓存类别字符串（百炼显式缓存回执；
                                    目前观察到的值："ephemeral"）
      cache_creation:               按 TTL 桶拆分的缓存写入明细（百炼显式缓存回执），
                                    例如 {"ephemeral_5m_input_tokens": 4765} 表示
                                    写入了 4765 个 token 到 5 分钟 TTL 的 ephemeral 桶。
                                    键名由服务端定义，SDK 原样透传，未来百炼新增
                                    ephemeral_1h_input_tokens 等桶时无需 SDK 改动。

    百炼显式缓存语义速查（basis：explicit_cache_probe 实测）：
      - 第 1 次：cache_creation_input_tokens>0, cached_tokens=0    ← 写入缓存
      - 第 N 次：cache_creation_input_tokens=0, cached_tokens>0    ← 命中缓存
      - cache_creation.ephemeral_5m_input_tokens 与 cache_creation_input_tokens
        在"只挂了一种 TTL"的场景下数值相等；多桶场景下 cache_creation 给出按桶明细。
    """

    cached_tokens: int
    text_tokens: int
    cache_creation_input_tokens: int
    cache_type: str
    cache_creation: dict[str, int]


class UsageInfo(TypedDict):
    """Token 用量（OpenAI 兼容格式）。

    必选字段（所有 provider 均返回）：
      prompt_tokens:     输入 token 数
      completion_tokens: 输出 token 数（含推理 token）
      total_tokens:      总 token 数

    可选字段（部分 provider 返回）：
      completion_tokens_details: 输出 token 的详细分项（思考模型必须有）
      prompt_tokens_details:     输入 token 的详细分项
    """

    # ── 必选 ── 计费三件套足够，包含了思考模型的reasoning_tokens的量
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    # ── 可选 ──  调试或者前端UI展示细节时使用
    completion_tokens_details: NotRequired[CompletionTokensDetails]
    prompt_tokens_details: NotRequired[PromptTokensDetails]


class LLMResponse(TypedDict):
    """非流式 LLM 响应（OpenAI chat.completion 兼容格式）。

    必选字段（OpenAI 规范要求，所有 provider 均返回）：
      content:        模型的文本回复（tool_calls 时可为 null）
      finish_reason:  终止原因（stop / tool_calls / length / content_filter）
      usage:          Token 用量统计
      id:             响应唯一标识
      model:          实际使用的模型名
      created:        Unix 时间戳

    可选字段（OpenAI 规范定义，部分 provider 返回）：
      tool_calls:           工具调用列表（模型决定调用工具时出现）
      reasoning_content:    思考模型的推理过程原文（Provider 特有，非 OpenAI 标准）
                            Qwen/Doubao 思考模型会返回此字段
      refusal:              模型拒绝回答时的文本（OpenAI 规范字段）
    """

    # ── 必选 ──
    content: str | None
    finish_reason: str | None
    usage: UsageInfo
    id: str
    model: str
    created: int

    # ── 可选 ──
    tool_calls: NotRequired[list[dict[str, Any]] | None]
    reasoning_content: NotRequired[str | None]    # 思考模型的推理过程原文（Provider 特有，非 OpenAI 标准），前端展示UI或者细节调试时使用
    refusal: NotRequired[str | None]


# ═══════════════════════════════════════════════════════════════
# 流式 chunk 增量结构（子类型 TypedDict）
# ═══════════════════════════════════════════════════════════════

class ToolCallDelta(TypedDict):
    """流式工具调用增量（LLMStreamChunk.tool_call_delta 的结构契约）。

    字段说明：
      index:           tool call 序号（同一轮次内 0/1/2…）
      id:              tool call ID（首次出现时非空，后续 fragment 可能为空串）
      name:            工具名（首次出现时非空，后续 fragment 为空串）
      arguments_delta: 本次增量的 arguments 片段（JSON 字符串的一段）

    组装规则（上层消费方负责）：
      按 index 累积 id / name / arguments_delta：
        - id: 取首次出现的非空值
        - name: 取首次出现的非空值
        - arguments: 所有 arguments_delta 串接
    """

    index: int
    id: str
    name: str
    arguments_delta: str


@dataclass
class LLMStreamChunk:
    """流式响应的单个 chunk。

    一个 chunk 有且仅承载一种增量语义，由下列字段中非 None 的那一项决定：

    - delta_content：文本增量；上层按序拼接即可得到完整 content
    - delta_reasoning_content：思考增量（同时兼容 Qwen/Doubao 的 reasoning_content
                               和其他第三方的 reasoning 字段，由 client 层归一化）
    - refusal_delta：拒答增量；模型触发安全过滤/拒绝回答时出现，上层应区别于空响应处理
    - tool_call_delta：工具调用增量（ToolCallDelta 结构）；上层按 index 累积
                      id / name / arguments_delta，自行组装完整 tool_calls 列表
    - finish_reason：终止原因；仅流末尾一个 chunk 上有值
    - usage：Token 用量；仅当 provider 返回 usage 时随末尾 chunk 发出

    设计约束（重要）：
      client 层**不再**在终止 chunk 上重复输出完整 tool_calls 列表——
      该职责上移到上层引擎（engine.run_core）。client 只承担"无状态增量分发"。
      这样做的好处：
        1. 单一职责：client 不持有"完整工具调用对象"的生命周期
        2. 更易组合：上层可自由决定 tool_calls 的拼接策略（严格/容错）
        3. 流式 UI 可直接消费 tool_call_delta 逐字展示参数

    消费示例::

        tc_acc: dict[int, dict] = {}
        async for chunk in client.stream_response(messages):
            if chunk.delta_content:
                print(chunk.delta_content, end="", flush=True)
            if chunk.delta_reasoning_content:
                ...  # 展示思考过程
            if chunk.refusal_delta:
                ...  # 提示用户模型拒绝回答
            if chunk.tool_call_delta:
                d = chunk.tool_call_delta
                slot = tc_acc.setdefault(d["index"], {"id": "", "name": "", "arguments": ""})
                if d["id"]: slot["id"] = d["id"]
                if d["name"]: slot["name"] = d["name"]
                slot["arguments"] += d["arguments_delta"]
            if chunk.finish_reason:
                # 在此处组装完整 tool_calls
                complete = [tc_acc[i] for i in sorted(tc_acc)]
    """

    delta_content: str | None = None
    delta_reasoning_content: str | None = None
    refusal_delta: str | None = None
    tool_call_delta: ToolCallDelta | None = None
    finish_reason: str | None = None
    usage: UsageInfo | None = None
