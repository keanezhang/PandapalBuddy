# 新增一家 LLM provider 的 Checklist

> 场景：你想接入一家新的 OpenAI 兼容 LLM（比如 Kimi/Moonshot、MiniMax、GLM/智谱、DeepSeek 原生 API 等）。
> 本文档是"复制-改名-跑通"式的 step-by-step 指南。

## 命名约定（重要）

`pandaren/llm/` 里的 provider 命名一律使用**平台/API 厂商名**，而非模型品牌名：

| 平台名（用作 `provider`/常量名/工厂名） | 常跑的模型品牌 | 官方 base_url 关键字 |
|---|---|---|
| `dashscope`（阿里云百炼） | 通义千问 Qwen | `dashscope.aliyuncs.com` |
| `volcengine`（火山引擎方舟 Ark） | 豆包 Doubao | `ark.cn-beijing.volces.com` |
| `openai` | OpenAI GPT / o-系列 | `api.openai.com` |
| `moonshot`（月之暗面） | Kimi | `api.moonshot.cn` |
| `deepseek` | DeepSeek V/R 系列 | `api.deepseek.com` |
| `zhipu`（智谱 AI） | GLM-4 / GLM-4.5 | `open.bigmodel.cn` |
| `minimax` | abab / M1 | `api.minimax.chat` |

判断规则：**一个平台可以跑多家品牌的模型**（比如 volcengine 上除了豆包还能部署第三方模型），
所以 `provider` 字段不能用品牌名。接入新家时先查它的官方 Python SDK 或 base_url，
拿到权威的平台名再命名。

> 另一个关键维度：常量是按「**provider × endpoint**」切分的，不是按 provider 整颗。
> 同一家的 `/chat/completions` / `/v1/responses` / `/context/create` 能力完全不同，
> 要分别建常量：`VOLCENGINE_CHAT` / `VOLCENGINE_RESPONSES` / `VOLCENGINE_CONTEXT_API`。

## TL;DR — 最小改动点

| 改点 | 文件 | 强制? | 工作量 |
|---|---|---|---|
| ① 加一条 `*_CHAT`（或其他端点）能力常量 | `pandaren/llm/capabilities.py` | ✅ 必须 | ~20 行 |
| ② 加一个 `for_xxx(...)` 工厂方法 | `pandaren/llm/client.py` | ✅ 必须 | ~20 行 |
| ③ 顶层导出 `XXX_CHAT` | `pandaren/llm/__init__.py` | ✅ 必须 | 2 行 |
| ④ 新建 typed Extra 结构 | `pandaren/llm/providers/xxx.py` | 🟡 有专属字段才需要 | 新文件 ~100 行 |
| ⑤ 导出 `XxxExtra` | `providers/__init__.py` + `llm/__init__.py` | 🟡 配合 ④ | 4 行 |
| ⑥ 扩 `REASONING_FIELDS` 映射表 | `client.py::_emit_capability_warnings` | 🟢 有新推理字段才需要 | 1 行 |

---

## 前置调研（加之前先回答 5 个问题）

1. **这家的 API 平台名叫什么？** 看官方 Python SDK 包名或 base_url 里的主域名。
   别用"模型品牌名"（qwen/doubao/kimi）当 `provider` 字段值，要用"平台名"（dashscope/volcengine/moonshot）。
2. **要接入哪个端点？** `/chat/completions` / `/v1/responses` / `/v1/messages` / 其他？
   能力是按「provider × endpoint」登记的，一次只建一条常量，不要想着一口气把所有端点都收了。
3. **这家的 `base_url` 是什么？** 是否有多个 region endpoint？
4. **它有没有"非标字段"？** 也就是 OpenAI 标准参数之外还要传点别的（推理开关、搜索开关、缓存 ID 等）？
5. **它的推理控制字段叫什么？** 是 `reasoning`（OpenAI）/ `reasoning_effort`（豆包 thinking）/ `thinking`（豆包 Seed）/ `enable_thinking`（千问）/ 还是一个全新的名字？

有了这五个答案，下面每一步都是照抄。

---

## Step ① 加能力常量 —— `pandaren/llm/capabilities.py`

照着现有的 `DASHSCOPE_CHAT` / `VOLCENGINE_CHAT` 抄一条。放在文件末尾常量区：

```python
MOONSHOT_CHAT: Final[EndpointCapabilities] = EndpointCapabilities(
    provider="moonshot",                 # ← 平台名，不是品牌名"kimi"
    endpoint="chat_completions",         # ← 见 EndpointKind Literal 的取值

    # ── 缓存机制 ──
    # 显式缓存（客户端主动控制）：
    #   "none"            - 没有显式缓存
    #   "cache_control"   - 在 message content 上挂 cache_control 块（Claude-compat）
    #   "context_id"      - 单独的 /context/create 端点 + context_id（火山方舟专属）
    #   "responses_api"   - 走 /v1/responses 端点 + previous_response_id 串接
    #   [TODO] 全新机制先扩 capabilities.py 里的 ExplicitCacheMode Literal
    explicit_cache="none",
    implicit_cache=True,                 # 是否有服务端自动前缀缓存（独立于 explicit）

    # ── 缓存可观测性（L4 返回字段路径）──
    # 命中量字段：几乎都报告。注意 DeepSeek 字段名自成一派。
    cached_tokens_field="usage.prompt_tokens_details.cached_tokens",
    # 写入量字段：只有显式协议 / 显式区分 hit/miss 的隐式协议才有；纯隐式填 None
    cache_creation_field=None,

    # ── 缓存细节 ──
    max_cache_breakpoints=0,             # cache_control 断点上限，不支持填 0
    min_cache_tokens=None,               # 最少 token 门槛，不公开填 None
    cache_ttl_seconds=None,              # 显式缓存 TTL；不支持填 None
    cache_control_type=None,             # "ephemeral" 等；仅 explicit_cache=="cache_control" 有效
    cache_write_surcharge_percent=None,  # 首写单价溢价（%）；无显式缓存填 None

    # ── 推理/思考 ──
    # 推理控制字段名：
    #   "none"              - 不支持客户端控制推理
    #   "reasoning"         - OpenAI o1/o3 风格
    #   "reasoning_effort"  - 豆包 thinking 系列
    #   "thinking"          - 豆包 Seed 系列
    #   "enable_thinking"   - 千问风格
    #   [TODO] 全新字段名先扩 ReasoningControlField Literal
    reasoning_control="none",
    reasoning_control_values=(),
    reasoning_budget_field=None,         # 硬上限字段名，如 "thinking_budget"
    returns_reasoning_content=False,     # 响应是否带 message.reasoning_content

    # ── 工具调用 ──
    supports_parallel_tool_calls=True,   # parallel_tool_calls=True 是否被识别
    supports_tool_choice_required=True,  # tool_choice="required" 是否被识别
    supports_tool_cache_control=False,   # tools 上挂 cache_control 是否有效；仅 Anthropic 类端点填 True，
                                         # DashScope/Qwen 及隐式 provider 填 False（工具级 cache_control 被忽略）

    notes="Moonshot（Kimi）官方 /chat/completions 端点。...",
)
```

> ⚠️ 如果这家的字段取值不在现有 `ExplicitCacheMode` / `ReasoningControlField` / `EndpointKind` 枚举里，**先扩 Literal 类型**再加常量，否则 type checker 会报错。
>
> ⚠️ `provider` 字段必须是**平台名**（比如 `"moonshot"`），不是"品牌名"（`"kimi"`）。
> `client.py::_emit_capability_warnings` 里的告警分支是按 `caps.provider` 分流的，品牌名会导致告警漏分流。

---

## Step ② 加工厂方法 —— `pandaren/llm/client.py`

在 `OpenAICompatibleClient` 类里照 `for_dashscope` / `for_volcengine` 抄：

```python
# 顶部 import 加上新常量
from .capabilities import (
    EndpointCapabilities,
    DASHSCOPE_CHAT,
    VOLCENGINE_CHAT,
    VOLCENGINE_CONTEXT_API,
    OPENAI_CHAT,
    MOONSHOT_CHAT,      # ← 加一行
)

# 类里加工厂方法
@classmethod
def for_moonshot(
    cls,
    api_key: str,
    model_name: str,
    base_url: str = "https://api.moonshot.cn/v1",    # [TODO] 这家的默认 endpoint
    *,
    timeout: float = 60.0,
    default_settings: ModelSettings | None = None,
) -> "OpenAICompatibleClient":
    """Moonshot（Kimi）OpenAI 兼容 /chat/completions 工厂。

    命名约定：方法名取**平台名**（moonshot），而非模型品牌名（kimi）。
    常跑模型：kimi-k2、moonshot-v1-* 系列。

    Args:
        base_url: 默认 Moonshot 公网 endpoint；
            企业版 / 私有化部署请显式传对应 endpoint
    """
    return cls(
        api_key=api_key,
        model_name=model_name,
        base_url=base_url,
        capabilities=MOONSHOT_CHAT,
        timeout=timeout,
        default_settings=default_settings,
    )
```

> ⚠️ **不要**把 `base_url` 写死——各家都有多 region，工厂只负责绑正确的 capabilities。

---

## Step ③ 顶层导出 —— `pandaren/llm/__init__.py`

```python
from .capabilities import (
    EndpointCapabilities,
    ExplicitCacheMode,
    ReasoningControlField,
    DASHSCOPE_CHAT,
    VOLCENGINE_CHAT,
    VOLCENGINE_CONTEXT_API,
    OPENAI_CHAT,
    MOONSHOT_CHAT,          # ← 加一行
)

__all__ = [
    ...
    "DASHSCOPE_CHAT",
    "VOLCENGINE_CHAT",
    "VOLCENGINE_CONTEXT_API",
    "OPENAI_CHAT",
    "MOONSHOT_CHAT",        # ← 加一行
    ...
]
```

到这里，使用者已经可以用：

```python
from pandaren.llm import OpenAICompatibleClient, MOONSHOT_CHAT

client = OpenAICompatibleClient.for_moonshot(api_key=..., model_name="kimi-k2")
```

**如果这家没有任何专属 extra_body 字段**（纯标准 OpenAI 协议），到此完工。以下为可选。

---

## Step ④ 新建 typed Extra 结构 —— `pandaren/llm/providers/xxx.py`

**判断标准**：这家有没有非标字段需要通过 `extra_body` 传？
- **有** → 继续本 step
- **没有** → 跳过（比如 DeepSeek 纯标准的 `/chat/completions`，就不需要 Extra）

操作：

```bash
# 从模板复制。文件名用**平台名**，不是品牌名。
cp pandaren/llm/providers/_template.py.example pandaren/llm/providers/moonshot.py
```

然后逐项替换 `_template.py.example` 里的 `[TODO]` 标记：
- 把类名 `TemplateExtra` 改成 `MoonshotExtra`（平台名 + `Extra`，不是 `KimiExtra`）
- 把示例字段替换为真实字段（字段名 1:1 对齐官方文档）
- 每个字段都要写 docstring，标注对应的 body 结构（比如 `body: {"enable_thinking": <bool>}`）
- 嵌套 vs 扁平的写法分别参考 `volcengine.py::thinking_mode`（嵌套）和 `dashscope.py::enable_thinking`（扁平）
- **保留 `raw` 逃生舱口**，所有 Extra 都要有

---

## Step ⑤ 导出 Extra —— `providers/__init__.py` + `llm/__init__.py`

```python
# pandaren/llm/providers/__init__.py
from .dashscope import DashScopeExtra
from .volcengine import VolcEngineExtra
from .moonshot import MoonshotExtra          # ← 加一行

__all__ = [
    "DashScopeExtra",
    "VolcEngineExtra",
    "MoonshotExtra",                          # ← 加一行
]

# pandaren/llm/__init__.py
from .providers import DashScopeExtra, VolcEngineExtra, MoonshotExtra   # ← 加

__all__ = [
    ...
    "DashScopeExtra",
    "VolcEngineExtra",
    "MoonshotExtra",                          # ← 加一行
]
```

---

## Step ⑥ 扩推理字段告警表 —— `client.py::_emit_capability_warnings`

**只在这家引入了一个"全新的推理字段名"时才改**（比如 moonshot 用了 `think_mode`，之前没见过）。

在 `_emit_capability_warnings` 里找到这段：

```python
REASONING_FIELDS = {
    "reasoning": "reasoning",
    "reasoning_effort": "reasoning_effort",
    "thinking": "thinking",
    "enable_thinking": "enable_thinking",
    "thinking_budget": "enable_thinking",
    # [TODO] 加一行：
    "think_mode": "think_mode",
}
```

不加的话，使用者如果"给其他家 provider 错传了 `think_mode`"，SDK 的"参数字段名错配告警"就漏检了。

---

## Step ⑦ 自测（不写单元测试也要手跑一次）

```python
# assistant/real/simple_llm_test.py 里加一个 demo，或写个一次性脚本
from pandaren.llm import OpenAICompatibleClient, ModelSettings, MoonshotExtra

async def smoke():
    async with OpenAICompatibleClient.for_moonshot(
        api_key=os.environ["MOONSHOT_API_KEY"],
        model_name="kimi-k2",
    ) as client:
        # 验证 capabilities 生效
        assert client.capabilities is not None
        assert client.capabilities.provider == "moonshot"   # ← 平台名，不是"kimi"

        # 验证 Extra 能装进 ModelSettings
        settings = ModelSettings(
            temperature=0.7,
            extra_body=MoonshotExtra(some_toggle=True).as_extra_body(),
        )

        resp = await client.call(
            messages=[{"role": "user", "content": "ping"}],
            settings=settings,
        )
        print(resp["content"])
```

跑通 → 收工。

---

## 不需要动的地方（值得确认）

以下文件**新增 provider 时不会动**——如果你发现自己在改它们，很可能走错了：

- ❌ `pandaren/llm/types.py::ModelSettings` — 通用字段稳定，专属字段全走 `extra_body`
- ❌ `pandaren/llm/protocol.py::LLMClient` — 接口契约不变
- ❌ `pandaren/llm/router.py::LLMRouter` — 多 provider 路由逻辑与具体家无关
- ❌ `pandaren/llm/client.py::call` / `stream_response` / `_build_payload` — 通用执行路径
- ❌ 业务层（`pandaren/agent/`、`pandaren/tools/` 等） — 对 provider 差异无感

这就是 capabilities + extra_body 分层设计的价值：**把差异全部集中到"声明层 + 填写助手"**，执行层只有一条代码路径。

---

## 参考现有实现

- **只有推理控制、没有显式缓存**：参考 `providers/dashscope.py` + `DASHSCOPE_CHAT`（Qwen 系列）
- **有 Context API 类显式缓存**：参考 `providers/volcengine.py` + `VOLCENGINE_CONTEXT_API`（Doubao 系列）
- **纯标准、无专属字段**：参考 `OPENAI_CHAT`（无对应 Extra，只有 capability 常量）
- **同家多端点对照**：参考 `VOLCENGINE_CHAT` / `VOLCENGINE_CONTEXT_API` / `VOLCENGINE_RESPONSES`
  三个同平台不同端点常量，能力差异一目了然
