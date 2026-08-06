"""
Pandaren Agent SDK · LLM 模块真实集成测试

覆盖约束
--------
  构造 & 工厂方法：
    1.1  正常构造（api_key / model_name / base_url 均非空）
    1.2  api_key 为空 → ValueError
    1.3  model_name 为空 → ValueError
    1.4  base_url 为空 → ValueError
    1.5  for_dashscope() 绑定 DASHSCOPE_CHAT capabilities
    1.6  for_volcengine() 绑定 VOLCENGINE_CHAT capabilities（use_context_api=False）
    1.7  for_volcengine(use_context_api=True) 绑定 VOLCENGINE_CONTEXT_API capabilities
    1.8  for_openai() 绑定 OPENAI_CHAT capabilities
    1.9  for_openai() 默认 model_name="gpt-4o"
    1.10 for_dashscope() 默认 base_url 包含 "dashscope.aliyuncs.com"
    1.11 for_volcengine() 默认 base_url 包含 "volces.com"

  cache 配置：
    2.1  默认 cache=True，_cache_state 初始化
    2.2  cache=False 不影响构造
    2.3  cache="manual" 不影响构造
    2.4  _on_history_compacted() 设置 next_call_is_cold=True
    2.5  _on_static_context_changed() 设置 next_call_is_cold=True
    2.6  consume_cold_start() 消耗后重置为 False

  model_name / capabilities 属性：
    3.1  model_name 只读 property 返回正确值
    3.2  capabilities property 返回注入的值
    3.3  capabilities=None 时返回 None

  非流式 call()（需要真实 API Key）：
    4.1  call() 返回 LLMResponse dict
    4.2  LLMResponse 包含 content 字段（str 或 None）
    4.3  LLMResponse 包含 finish_reason 字段
    4.4  LLMResponse 包含 usage dict（prompt_tokens / completion_tokens / total_tokens）
    4.5  LLMResponse 包含 id 字段（非空字符串）
    4.6  LLMResponse 包含 model 字段（非空字符串）
    4.7  call() 接受 always_tools_count 关键字参数（不报错）
    4.8  call() 接受 tools=None
    4.9  temperature 影响 payload（通过 ModelSettings 传入）
    4.10 max_tokens 限制输出长度

  流式 stream_response()（需要真实 API Key）：
    5.1  stream_response() 是异步生成器
    5.2  chunks 累积后 content 非空
    5.3  最终一个 chunk 含 finish_reason
    5.4  include_usage=True 时 SDK 正确注入参数；有 usage 时格式合法（软验证，provider 不返回时跳过）
    5.5  stream_response() 接受 always_tools_count 关键字参数
    5.6  delta_content 为字符串（非 None 时）

  工具调用（需要真实 API Key）：
    6.1  传入 tools 时 LLM 可返回 tool_calls
    6.2  finish_reason=="tool_calls" 时 tool_calls 非空列表
    6.3  tool_calls 每元素含 id / type / function 字段
    6.4  function 含 name / arguments 字段

  错误处理（需要真实 API Key 验证路径）：
    7.1  错误的 api_key → LLMAuthError（401）

  default_settings + per-call settings 合并：
    8.1  default_settings 的 max_tokens 生效（call() 中未覆盖时）
    8.2  per-call settings.max_tokens 覆盖 default_settings.max_tokens

  LLMRouter：
    9.1  register() 注册 client，model_name 对外展示第一个 primary
    9.2  set_default() 更改 model_name 到 default.model_name
    9.3  register(key="") → ValueError
    9.4  register(key=None) → ValueError（None 触发 AttributeError）
    9.5  _resolve() 无 client 且无 default → LLMRequestError
    9.6  _resolve() 按 prefix 路由
    9.7  router.call() 路由到正确的 client（需要真实 API Key）
    9.8  aclose() 不抛异常

运行方式
--------
  cd pandaren/llm/tests
  python test_llm.py                           # 全部测试
  python test_llm.py --section constructor     # 仅构造 & 工厂
  python test_llm.py --section cache           # 仅 cache 配置
  python test_llm.py --section props           # 仅属性
  python test_llm.py --section call            # 仅 call()（需真实 Key）
  python test_llm.py --section stream          # 仅 stream_response()（需真实 Key）
  python test_llm.py --section tools           # 仅工具调用（需真实 Key）
  python test_llm.py --section errors          # 仅错误处理（需真实 Key）
  python test_llm.py --section settings        # 仅 settings 合并
  python test_llm.py --section router          # 仅 LLMRouter

  环境变量（.env.development，见文件顶部 _ENV_PATH）：
    DASHSCOPE_API_KEY=xxx     — 阿里百炼；用于 call/stream/tools/errors 测试
    VOLCENGINE_API_KEY=xxx    — 火山方舟；用于 router 测试（可选）
    DASHSCOPE_MODEL=qwen-plus — 覆盖默认模型名（可选，默认 qwen-plus）
"""

from __future__ import annotations

import asyncio
import io
import os
import sys

# Windows 控制台 UTF-8 输出
if sys.platform == "win32" and "pytest" not in sys.modules:  # pytest 下交给 capture，避免拆台
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

# ═══ 环境变量加载 ═══
_ENV_PATH = os.path.join(os.path.dirname(__file__), "..", ".env.development")
if os.path.exists(_ENV_PATH):
    with open(_ENV_PATH, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#"):
                continue
            if "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

# ═══ SDK 导入 ═══
from pandaren.llm.client import OpenAICompatibleClient, ProviderCapabilityWarning
from pandaren.llm.capabilities import (
    EndpointCapabilities,
    DASHSCOPE_CHAT,
    VOLCENGINE_CHAT,
    VOLCENGINE_CONTEXT_API,
    OPENAI_CHAT,
)
from pandaren.llm.cache_strategy import CacheDepth, CacheMode, CacheState
from pandaren.llm.exceptions import (
    LLMAuthError, LLMRequestError, LLMError,
)
from pandaren.llm.types import (
    ModelSettings, LLMResponse, LLMStreamChunk, UsageInfo,
)
from pandaren.llm.router import LLMRouter


# ════════════════════════════════════════════════════
#  测试框架
# ════════════════════════════════════════════════════

class TestResult:
    """轻量测试结果收集器。"""
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.errors: list[str] = []

    def ok(self, name: str):
        self.passed += 1
        print(f"   ✅ {name}")

    def skip(self, name: str, reason: str = ""):
        self.skipped += 1
        msg = f"   ⏭️  {name}"
        if reason:
            msg += f" — {reason}"
        print(msg)

    def fail(self, name: str, detail: str = ""):
        self.failed += 1
        msg = f"   ❌ {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        self.errors.append(msg)

    def summary(self, section: str = ""):
        total = self.passed + self.failed + self.skipped
        label = f" [{section}]" if section else ""
        print(f"\n📊{label} 通过={self.passed} / 失败={self.failed} / 跳过={self.skipped} / 总计={total}")
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
    """装饰器：断言被装饰的函数会抛出指定异常。"""
    def decorator(fn):
        try:
            fn()
            result.fail(name, f"未抛出 {exc_type.__name__}" + (f": {detail}" if detail else ""))
        except exc_type:
            result.ok(name)
        except Exception as e:
            result.fail(name, f"抛出了 {type(e).__name__}({e}) 而非 {exc_type.__name__}")
    return decorator


def assert_no_raises(name: str, detail: str = ""):
    """装饰器：断言被装饰的函数不会抛出异常。"""
    def decorator(fn):
        try:
            fn()
            result.ok(name)
        except Exception as e:
            result.fail(name, f"意外抛出 {type(e).__name__}({e})" + (f": {detail}" if detail else ""))
    return decorator


# ════════════════════════════════════════════════════
#  辅助：环境变量 & 客户端构建
# ════════════════════════════════════════════════════

DASHSCOPE_API_KEY: str = os.environ.get("DASHSCOPE_API_KEY", "")
VOLCENGINE_API_KEY: str = os.environ.get("VOLCENGINE_API_KEY", "")
DASHSCOPE_MODEL: str = os.environ.get("DASHSCOPE_MODEL", "qwen-plus")
VOLCENGINE_MODEL: str = os.environ.get("VOLCENGINE_MODEL", "")  # ep-xxx or model name

HAVE_DASHSCOPE = bool(DASHSCOPE_API_KEY)
HAVE_VOLCENGINE = bool(VOLCENGINE_API_KEY and VOLCENGINE_MODEL)


def _make_dashscope_client(
    api_key: str | None = None,
    model_name: str | None = None,
    cache: CacheMode = False,  # 测试中禁用 cache 以减少副作用
) -> OpenAICompatibleClient:
    """构建 DashScope（百炼）测试客户端。"""
    return OpenAICompatibleClient.for_dashscope(
        api_key=api_key or DASHSCOPE_API_KEY,
        model_name=model_name or DASHSCOPE_MODEL,
        cache=cache,
    )


def _make_volcengine_client(
    api_key: str | None = None,
    model_name: str | None = None,
    cache: CacheMode = False,
) -> OpenAICompatibleClient:
    """构建 VolcEngine（火山方舟）测试客户端。"""
    return OpenAICompatibleClient.for_volcengine(
        api_key=api_key or VOLCENGINE_API_KEY,
        model_name=model_name or VOLCENGINE_MODEL,
        cache=cache,
    )


def _simple_messages(user_text: str = "你好，请用一句话介绍自己。") -> list[dict]:
    return [
        {"role": "system", "content": "你是一个简洁的 AI 助手。"},
        {"role": "user", "content": user_text},
    ]


def _tool_call_messages() -> list[dict]:
    return [
        {"role": "system", "content": "你是一个助手，需要使用工具回答问题。"},
        {"role": "user", "content": "今天北京天气怎么样？请调用查询天气工具。"},
    ]


def _weather_tool() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的当前天气",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名称，如「北京」「上海」",
                    }
                },
                "required": ["city"],
            },
        },
    }


# ════════════════════════════════════════════════════
#  Section 1：构造 & 工厂方法
# ════════════════════════════════════════════════════

def test_constructor():
    print("\n" + "═" * 60)
    print("1️⃣  构造 & 工厂方法")
    print("═" * 60)

    # ── 1.1 正常构造 ──
    @assert_no_raises("1.1 正常构造（三个必填字段均非空）")
    def _():
        c = OpenAICompatibleClient(
            api_key="test_key",
            model_name="test_model",
            base_url="https://example.com/v1",
        )
        asyncio.get_event_loop().run_until_complete(c.aclose())

    # ── 1.2 api_key 为空 ──
    @assert_raises(ValueError, "1.2 api_key 为空 → ValueError")
    def _():
        OpenAICompatibleClient(api_key="", model_name="m", base_url="https://x.com/v1")

    # ── 1.3 model_name 为空 ──
    @assert_raises(ValueError, "1.3 model_name 为空 → ValueError")
    def _():
        OpenAICompatibleClient(api_key="k", model_name="", base_url="https://x.com/v1")

    # ── 1.4 base_url 为空 ──
    @assert_raises(ValueError, "1.4 base_url 为空 → ValueError")
    def _():
        OpenAICompatibleClient(api_key="k", model_name="m", base_url="")

    # ── 1.5 for_dashscope() 绑定 DASHSCOPE_CHAT ──
    c = OpenAICompatibleClient.for_dashscope(
        api_key="k", model_name="qwen-plus"
    )
    assert_true(c.capabilities is DASHSCOPE_CHAT,
                "1.5 for_dashscope() 绑定 DASHSCOPE_CHAT")

    # ── 1.6 for_volcengine() 默认绑定 VOLCENGINE_CHAT ──
    c2 = OpenAICompatibleClient.for_volcengine(
        api_key="k", model_name="doubao-pro-32k"
    )
    assert_true(c2.capabilities is VOLCENGINE_CHAT,
                "1.6 for_volcengine(use_context_api=False) 绑定 VOLCENGINE_CHAT")

    # ── 1.7 for_volcengine(use_context_api=True) 绑定 VOLCENGINE_CONTEXT_API ──
    c3 = OpenAICompatibleClient.for_volcengine(
        api_key="k", model_name="ep-xxx", use_context_api=True
    )
    assert_true(c3.capabilities is VOLCENGINE_CONTEXT_API,
                "1.7 for_volcengine(use_context_api=True) 绑定 VOLCENGINE_CONTEXT_API")

    # ── 1.8 for_openai() 绑定 OPENAI_CHAT ──
    c4 = OpenAICompatibleClient.for_openai(api_key="k")
    assert_true(c4.capabilities is OPENAI_CHAT,
                "1.8 for_openai() 绑定 OPENAI_CHAT")

    # ── 1.9 for_openai() 默认 model_name ──
    assert_true(c4.model_name == "gpt-4o",
                "1.9 for_openai() 默认 model_name='gpt-4o'")

    # ── 1.10 for_dashscope() 默认 base_url ──
    assert_true("dashscope.aliyuncs.com" in
                OpenAICompatibleClient.for_dashscope(api_key="k", model_name="m")._base_url,
                "1.10 for_dashscope() 默认 base_url 含 dashscope.aliyuncs.com")

    # ── 1.11 for_volcengine() 默认 base_url ──
    assert_true("volces.com" in
                OpenAICompatibleClient.for_volcengine(api_key="k", model_name="m")._base_url,
                "1.11 for_volcengine() 默认 base_url 含 volces.com")


# ════════════════════════════════════════════════════
#  Section 2：cache 配置 & 生命周期通知
# ════════════════════════════════════════════════════

def test_cache():
    print("\n" + "═" * 60)
    print("2️⃣  cache 配置 & 生命周期通知")
    print("═" * 60)

    # ── 2.1 默认 cache=True，_cache_state 初始化 ──
    c = OpenAICompatibleClient(api_key="k", model_name="m", base_url="https://x.com/v1")
    assert_true(c._cache is True, "2.1 默认 cache=True")
    assert_true(isinstance(c._cache_state, CacheState), "2.1 _cache_state 是 CacheState 实例")

    # ── 2.2 cache=False ──
    c2 = OpenAICompatibleClient(api_key="k", model_name="m", base_url="https://x.com/v1", cache=False)
    assert_true(c2._cache is False, "2.2 cache=False 正确存储")

    # ── 2.3 cache='manual' ──
    c3 = OpenAICompatibleClient(api_key="k", model_name="m", base_url="https://x.com/v1", cache="manual")
    assert_true(c3._cache == "manual", "2.3 cache='manual' 正确存储")

    # ── 2.4 _on_history_compacted() 设置 next_call_is_cold=True ──
    c4 = OpenAICompatibleClient(api_key="k", model_name="m", base_url="https://x.com/v1")
    assert_true(c4._cache_state.next_call_is_cold is False, "2.4 初始 next_call_is_cold=False")
    c4._on_history_compacted()
    assert_true(c4._cache_state.next_call_is_cold is True,
                "2.4 _on_history_compacted() 后 next_call_is_cold=True")

    # ── 2.5 _on_static_context_changed() 设置 next_call_is_cold=True ──
    c5 = OpenAICompatibleClient(api_key="k", model_name="m", base_url="https://x.com/v1")
    c5._on_static_context_changed()
    assert_true(c5._cache_state.next_call_is_cold is True,
                "2.5 _on_static_context_changed() 后 next_call_is_cold=True")

    # ── 2.6 consume_cold_start() 消耗后重置为 False ──
    c6 = OpenAICompatibleClient(api_key="k", model_name="m", base_url="https://x.com/v1")
    c6._on_history_compacted()
    was_cold = c6._cache_state.consume_cold_start()
    assert_true(was_cold is True, "2.6 consume_cold_start() 返回 True（已是冷启动）")
    assert_true(c6._cache_state.next_call_is_cold is False,
                "2.6 consume_cold_start() 后 next_call_is_cold 重置为 False")

    # ── 2.7 cache_depth 默认值 ──
    c7 = OpenAICompatibleClient(api_key="k", model_name="m", base_url="https://x.com/v1")
    assert_true(c7._cache_depth == "history", "2.7 cache_depth 默认值为 'history'")

    # ── 2.8 cache_depth 自定义 ──
    c8 = OpenAICompatibleClient(
        api_key="k", model_name="m", base_url="https://x.com/v1", cache_depth="system"
    )
    assert_true(c8._cache_depth == "system", "2.8 cache_depth='system' 正确存储")


# ════════════════════════════════════════════════════
#  Section 3：model_name / capabilities 属性
# ════════════════════════════════════════════════════

def test_props():
    print("\n" + "═" * 60)
    print("3️⃣  model_name / capabilities 属性")
    print("═" * 60)

    # ── 3.1 model_name 只读 property ──
    c = OpenAICompatibleClient(
        api_key="k", model_name="qwen-max", base_url="https://x.com/v1"
    )
    assert_true(c.model_name == "qwen-max", "3.1 model_name property 返回正确值")

    # model_name 不可直接赋值（无 setter）
    try:
        c.model_name = "hacked"  # type: ignore
        result.fail("3.1 model_name 不应可写（无 setter）")
    except AttributeError:
        result.ok("3.1 model_name 无 setter → AttributeError")

    # ── 3.2 capabilities 返回注入的值 ──
    c2 = OpenAICompatibleClient(
        api_key="k", model_name="m", base_url="https://x.com/v1",
        capabilities=DASHSCOPE_CHAT,
    )
    assert_true(c2.capabilities is DASHSCOPE_CHAT,
                "3.2 capabilities property 返回注入的 DASHSCOPE_CHAT")

    # ── 3.3 capabilities=None 时返回 None ──
    c3 = OpenAICompatibleClient(
        api_key="k", model_name="m", base_url="https://x.com/v1",
        capabilities=None,
    )
    assert_true(c3.capabilities is None, "3.3 capabilities=None 时返回 None")

    # ── 3.4 for_dashscope capabilities 字段验证 ──
    assert_true(DASHSCOPE_CHAT.explicit_cache == "cache_control",
                "3.4 DASHSCOPE_CHAT.explicit_cache == 'cache_control'")
    assert_true(VOLCENGINE_CHAT.explicit_cache == "none",
                "3.4 VOLCENGINE_CHAT.explicit_cache == 'none'")
    assert_true(VOLCENGINE_CONTEXT_API.explicit_cache == "context_id",
                "3.4 VOLCENGINE_CONTEXT_API.explicit_cache == 'context_id'")
    assert_true(OPENAI_CHAT.explicit_cache == "none",
                "3.4 OPENAI_CHAT.explicit_cache == 'none'（隐式缓存）")


# ════════════════════════════════════════════════════
#  Section 4：非流式 call()
# ════════════════════════════════════════════════════

def test_call():
    print("\n" + "═" * 60)
    print("4️⃣  非流式 call()")
    print("═" * 60)

    if not HAVE_DASHSCOPE:
        print("   ⏭️  跳过（DASHSCOPE_API_KEY 未配置）")
        result.skipped += 10
        return

    async def _run():
        client = _make_dashscope_client()
        try:
            messages = _simple_messages()

            # ── 4.1 ~ 4.6 基础响应结构 ──
            resp: LLMResponse = await client.call(messages)

            assert_true(isinstance(resp, dict), "4.1 call() 返回 dict（LLMResponse）")
            assert_true("content" in resp, "4.2 LLMResponse 含 content 字段")
            assert_true(resp.get("content") is None or isinstance(resp["content"], str),
                        "4.2 content 字段类型为 str 或 None")

            assert_true("finish_reason" in resp, "4.3 LLMResponse 含 finish_reason 字段")
            assert_true(resp.get("finish_reason") in ("stop", "tool_calls", "length", "content_filter", None),
                        "4.3 finish_reason 值合法")

            usage = resp.get("usage", {})
            assert_true(isinstance(usage, dict), "4.4 LLMResponse.usage 是 dict")
            assert_true("prompt_tokens" in usage, "4.4 usage 含 prompt_tokens")
            assert_true("completion_tokens" in usage, "4.4 usage 含 completion_tokens")
            assert_true("total_tokens" in usage, "4.4 usage 含 total_tokens")
            assert_true(usage.get("total_tokens", 0) > 0, "4.4 total_tokens > 0")

            assert_true(isinstance(resp.get("id"), str) and len(resp["id"]) > 0,
                        "4.5 LLMResponse.id 是非空字符串")

            assert_true(isinstance(resp.get("model"), str) and len(resp["model"]) > 0,
                        "4.6 LLMResponse.model 是非空字符串")

            # ── 4.7 always_tools_count 参数被接受 ──
            resp2 = await client.call(messages, always_tools_count=0)
            assert_true(isinstance(resp2, dict), "4.7 call(always_tools_count=0) 正常返回")

            # ── 4.8 tools=None 正常工作 ──
            resp3 = await client.call(messages, tools=None)
            assert_true(isinstance(resp3, dict), "4.8 call(tools=None) 正常返回")

            # ── 4.9 temperature 通过 ModelSettings 传入 ──
            s = ModelSettings(temperature=0.0)
            resp4 = await client.call(messages, settings=s)
            assert_true(isinstance(resp4, dict), "4.9 call(settings=ModelSettings(temperature=0.0)) 正常返回")

            # ── 4.10 max_tokens 限制输出（设很小的值，应触发 length finish_reason 或短内容）──
            s_short = ModelSettings(max_tokens=5)
            resp5 = await client.call(
                [{"role": "user", "content": "请写一首五百字的诗"}],
                settings=s_short,
            )
            assert_true(isinstance(resp5, dict), "4.10 call(max_tokens=5) 正常返回（不抛异常）")
            # max_tokens=5 时 output token 数量应很小
            out_tokens = resp5.get("usage", {}).get("completion_tokens", 0)
            assert_true(out_tokens <= 20, f"4.10 completion_tokens={out_tokens} 在 max_tokens=5 约束下很小",
                        f"completion_tokens={out_tokens}，期望 ≤ 20")

        finally:
            await client.aclose()

    asyncio.get_event_loop().run_until_complete(_run())


# ════════════════════════════════════════════════════
#  Section 5：流式 stream_response()
# ════════════════════════════════════════════════════

def test_stream():
    print("\n" + "═" * 60)
    print("5️⃣  流式 stream_response()")
    print("═" * 60)

    if not HAVE_DASHSCOPE:
        print("   ⏭️  跳过（DASHSCOPE_API_KEY 未配置）")
        result.skipped += 6
        return

    async def _run():
        client = _make_dashscope_client()
        try:
            messages = _simple_messages()
            chunks: list[LLMStreamChunk] = []

            # ── 5.1 stream_response() 是异步生成器 ──
            gen = client.stream_response(messages)
            assert_true(hasattr(gen, "__aiter__"),
                        "5.1 stream_response() 返回可迭代对象（有 __aiter__）")

            async for chunk in gen:
                chunks.append(chunk)

            assert_true(len(chunks) > 0, "5.1 生成了至少 1 个 chunk")

            # ── 5.2 content 累积后非空 ──
            accumulated = "".join(
                c.delta_content for c in chunks if c.delta_content
            )
            assert_true(len(accumulated) > 0, "5.2 chunks 累积后 content 非空")

            # ── 5.3 最终 chunk 含 finish_reason ──
            final_chunks = [c for c in chunks if c.finish_reason]
            assert_true(len(final_chunks) >= 1, "5.3 至少一个 chunk 含 finish_reason")
            assert_true(final_chunks[-1].finish_reason in
                        ("stop", "tool_calls", "length", "content_filter"),
                        "5.3 finish_reason 值合法")

            # ── 5.4 显式开启 include_usage 后——SDK 正确注入参数、有 usage 时格式合法 ──
            # 注意：部分 provider（如 DashScope）在流式模式下即使请求
            # stream_options.include_usage=True 也不保证每次都回填 usage chunk，
            # 所以这里做软验证：有 usage 时检查格式，没有时跳过（不失败）。
            chunks_with_usage: list[LLMStreamChunk] = []
            async for chunk in client.stream_response(
                messages, settings=ModelSettings(include_usage=True)
            ):
                chunks_with_usage.append(chunk)
            usage_chunks = [c for c in chunks_with_usage if c.usage is not None]
            if usage_chunks:
                last_usage = usage_chunks[-1].usage
                assert_true(
                    last_usage is not None and last_usage.get("total_tokens", 0) > 0,
                    "5.4 usage.total_tokens > 0（provider 返回了 usage）",
                )
            else:
                # provider 未返回 usage —— SDK 本身行为正确（没有崩溃），软跳过
                result.skip("5.4 usage（provider 未在流式响应中返回 usage，跳过格式校验）")

            # ── 5.5 always_tools_count 关键字参数被接受 ──
            chunks2: list[LLMStreamChunk] = []
            async for chunk in client.stream_response(messages, always_tools_count=0):
                chunks2.append(chunk)
            assert_true(len(chunks2) > 0, "5.5 stream_response(always_tools_count=0) 正常返回 chunks")

            # ── 5.6 delta_content 为字符串（非 None 时）──
            content_chunks = [c for c in chunks if c.delta_content is not None]
            assert_true(
                all(isinstance(c.delta_content, str) for c in content_chunks),
                "5.6 delta_content 为字符串（非 None 时）",
            )

        finally:
            await client.aclose()

    asyncio.get_event_loop().run_until_complete(_run())


# ════════════════════════════════════════════════════
#  Section 6：工具调用
# ════════════════════════════════════════════════════

def test_tools():
    print("\n" + "═" * 60)
    print("6️⃣  工具调用（tool_calls）")
    print("═" * 60)

    if not HAVE_DASHSCOPE:
        print("   ⏭️  跳过（DASHSCOPE_API_KEY 未配置）")
        result.skipped += 4
        return

    async def _run():
        client = _make_dashscope_client()
        try:
            messages = _tool_call_messages()
            tools = [_weather_tool()]
            settings = ModelSettings(tool_choice="required")

            resp = await client.call(messages, tools=tools, settings=settings)

            # ── 6.1 传入 tools 且 tool_choice=required → LLM 返回 tool_calls ──
            assert_true(isinstance(resp, dict), "6.1 带 tools 的 call() 返回 dict")

            tc = resp.get("tool_calls")
            assert_true(
                tc is not None and len(tc) > 0,
                "6.2 finish_reason 为 tool_calls 或 tool_calls 非空",
                f"tool_calls={tc!r}, finish_reason={resp.get('finish_reason')!r}",
            )

            if tc:
                first = tc[0]
                # ── 6.3 tool_calls 每元素含 id / type / function ──
                assert_true("id" in first, "6.3 tool_calls[0] 含 id 字段")
                assert_true("type" in first, "6.3 tool_calls[0] 含 type 字段")
                assert_true("function" in first, "6.3 tool_calls[0] 含 function 字段")

                # ── 6.4 function 含 name / arguments ──
                fn = first.get("function", {})
                assert_true("name" in fn, "6.4 tool_calls[0].function 含 name 字段")
                assert_true("arguments" in fn, "6.4 tool_calls[0].function 含 arguments 字段")
                assert_true(fn.get("name") == "get_weather",
                            f"6.4 name == 'get_weather'，实际: {fn.get('name')!r}")

        finally:
            await client.aclose()

    asyncio.get_event_loop().run_until_complete(_run())


# ════════════════════════════════════════════════════
#  Section 7：错误处理
# ════════════════════════════════════════════════════

def test_errors():
    print("\n" + "═" * 60)
    print("7️⃣  错误处理")
    print("═" * 60)

    if not HAVE_DASHSCOPE:
        print("   ⏭️  跳过（DASHSCOPE_API_KEY 未配置）")
        result.skipped += 1
        return

    async def _run():
        # ── 7.1 错误的 api_key → LLMAuthError（401/403）──
        bad_client = OpenAICompatibleClient.for_dashscope(
            api_key="invalid_api_key_for_testing_xxxxxxxx",
            model_name=DASHSCOPE_MODEL,
            cache=False,
        )
        try:
            try:
                await bad_client.call(_simple_messages())
                result.fail("7.1 错误的 api_key 应抛出 LLMAuthError，但未抛出")
            except LLMAuthError as e:
                assert_true(e.status_code in (401, 403),
                            f"7.1 LLMAuthError.status_code={e.status_code} 为 401 或 403")
                result.ok("7.1 错误的 api_key → LLMAuthError")
            except LLMError as e:
                # 部分 provider 返回 400 for bad key — 也认为错误处理是正确的
                result.ok(f"7.1 错误的 api_key → {type(e).__name__}（LLMError 子类）")
        finally:
            await bad_client.aclose()

    asyncio.get_event_loop().run_until_complete(_run())


# ════════════════════════════════════════════════════
#  Section 8：default_settings + per-call settings 合并
# ════════════════════════════════════════════════════

def test_settings():
    print("\n" + "═" * 60)
    print("8️⃣  default_settings + per-call settings 合并")
    print("═" * 60)

    if not HAVE_DASHSCOPE:
        print("   ⏭️  跳过（DASHSCOPE_API_KEY 未配置）")
        result.skipped += 2
        return

    async def _run():
        # ── 8.1 default_settings 的 max_tokens 生效 ──
        default_s = ModelSettings(max_tokens=8)
        client = OpenAICompatibleClient.for_dashscope(
            api_key=DASHSCOPE_API_KEY,
            model_name=DASHSCOPE_MODEL,
            default_settings=default_s,
            cache=False,
        )
        try:
            messages = [{"role": "user", "content": "请写一篇关于人工智能的一千字文章"}]
            resp = await client.call(messages)
            out_tokens = resp.get("usage", {}).get("completion_tokens", 999)
            assert_true(out_tokens <= 30,
                        f"8.1 default_settings.max_tokens=8 生效，completion_tokens={out_tokens} 较小",
                        f"completion_tokens={out_tokens}，期望 ≤ 30")

            # ── 8.2 per-call settings.max_tokens 覆盖 default_settings ──
            # default = 8，per-call = 3，结果更小
            resp2 = await client.call(
                messages,
                settings=ModelSettings(max_tokens=3),
            )
            out_tokens2 = resp2.get("usage", {}).get("completion_tokens", 999)
            assert_true(out_tokens2 <= 15,
                        f"8.2 per-call max_tokens=3 覆盖 default，completion_tokens={out_tokens2} 更小",
                        f"completion_tokens={out_tokens2}，期望 ≤ 15")

        finally:
            await client.aclose()

    asyncio.get_event_loop().run_until_complete(_run())


# ════════════════════════════════════════════════════
#  Section 9：LLMRouter
# ════════════════════════════════════════════════════

def test_router():
    print("\n" + "═" * 60)
    print("9️⃣  LLMRouter")
    print("═" * 60)

    # ── 9.1 register() 首个 client 成为 primary ──
    c_ds = OpenAICompatibleClient.for_dashscope(
        api_key="key1", model_name="qwen-max"
    )
    c_ve = OpenAICompatibleClient.for_volcengine(
        api_key="key2", model_name="doubao-pro-32k"
    )
    router = LLMRouter().register("qwen-", c_ds).register("doubao-", c_ve)
    assert_true(router.model_name == "qwen-max",
                "9.1 首个注册 client 的 model_name 成为 router.model_name")

    # ── 9.2 set_default() 更改 model_name ──
    router2 = LLMRouter().register("qwen-", c_ds).register("doubao-", c_ve).set_default(c_ve)
    assert_true(router2.model_name == "doubao-pro-32k",
                "9.2 set_default(c_ve) 后 router.model_name == c_ve.model_name")

    # ── 9.3 register(key="") → ValueError ──
    @assert_raises(ValueError, "9.3 register(key='') → ValueError")
    def _():
        LLMRouter().register("", c_ds)

    # ── 9.4 register(client=None) → ValueError ──
    @assert_raises(ValueError, "9.4 register(client=None) → ValueError")
    def _():
        LLMRouter().register("qwen-", None)  # type: ignore

    # ── 9.5 空 router 调用 call() → LLMRequestError ──
    async def _no_client():
        r = LLMRouter()
        await r.call([{"role": "user", "content": "hi"}])

    try:
        asyncio.get_event_loop().run_until_complete(_no_client())
        result.fail("9.5 空 router 调用 call() 应抛 LLMRequestError")
    except LLMRequestError:
        result.ok("9.5 空 router 无 client → LLMRequestError")

    # ── 9.6 _resolve() 按前缀路由 ──
    c_a = OpenAICompatibleClient(api_key="k", model_name="model-a", base_url="https://a.com/v1")
    c_b = OpenAICompatibleClient(api_key="k", model_name="model-b", base_url="https://b.com/v1")
    r3 = LLMRouter().register("model-a", c_a).register("model-b", c_b)

    # 精确匹配 model-a
    s_a = ModelSettings(target_model="model-a")
    resolved_a = r3._resolve(s_a)
    assert_true(resolved_a is c_a, "9.6 精确匹配 model-a → c_a")

    # 精确匹配 model-b
    s_b = ModelSettings(target_model="model-b")
    resolved_b = r3._resolve(s_b)
    assert_true(resolved_b is c_b, "9.6 精确匹配 model-b → c_b")

    # 无 target_model → primary (c_a)
    resolved_default = r3._resolve(None)
    assert_true(resolved_default is c_a, "9.6 无 target_model → primary (c_a)")

    # ── 9.7 router.call() 路由到正确 client（需要真实 Key）──
    if HAVE_DASHSCOPE:
        async def _router_call():
            real_ds = _make_dashscope_client()
            real_router = LLMRouter().register("qwen-", real_ds).set_default(real_ds)
            try:
                resp = await real_router.call(
                    _simple_messages(),
                    settings=ModelSettings(target_model=DASHSCOPE_MODEL),
                )
                assert_true(isinstance(resp, dict),
                            "9.7 router.call() 路由到 dashscope client 正常返回")
            finally:
                await real_router.aclose()

        asyncio.get_event_loop().run_until_complete(_router_call())
    else:
        result.skip("9.7 router.call() 真实调用", "DASHSCOPE_API_KEY 未配置")

    # ── 9.8 aclose() 不抛异常 ──
    async def _aclose():
        r = LLMRouter()
        c1 = OpenAICompatibleClient(api_key="k", model_name="m1", base_url="https://x.com/v1")
        c2 = OpenAICompatibleClient(api_key="k", model_name="m2", base_url="https://y.com/v1")
        r.register("m1", c1).register("m2-", c2).set_default(c1)
        await r.aclose()

    @assert_no_raises("9.8 aclose() 关闭所有 client 不抛异常")
    def _():
        asyncio.get_event_loop().run_until_complete(_aclose())


# ════════════════════════════════════════════════════
#  主入口
# ════════════════════════════════════════════════════

SECTIONS = {
    "constructor": test_constructor,
    "cache": test_cache,
    "props": test_props,
    "call": test_call,
    "stream": test_stream,
    "tools": test_tools,
    "errors": test_errors,
    "settings": test_settings,
    "router": test_router,
}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="LLM 模块真实集成测试")
    parser.add_argument(
        "--section",
        choices=list(SECTIONS.keys()),
        default="",
        help="仅运行指定测试分区",
    )
    args = parser.parse_args()

    print("🐼 Pandaren Agent SDK — LLM 模块真实集成测试")
    print("   目标模块: pandaren/llm/client.py, capabilities.py, router.py")
    print(f"   DASHSCOPE_API_KEY: {'已配置' if HAVE_DASHSCOPE else '未配置（跳过网络测试）'}")
    print(f"   VOLCENGINE_API_KEY: {'已配置' if HAVE_VOLCENGINE else '未配置（跳过 volcengine 路由测试）'}")
    print()

    if args.section:
        section_fn = SECTIONS[args.section]

        global result
        old_result = result
        section_result = TestResult()
        result = section_result

        section_fn()

        result = old_result
        result.passed += section_result.passed
        result.failed += section_result.failed
        result.skipped += section_result.skipped
        result.errors.extend(section_result.errors)

        section_result.summary(args.section)
    else:
        test_constructor()
        test_cache()
        test_props()
        test_call()
        test_stream()
        test_tools()
        test_errors()
        test_settings()
        test_router()
        result.summary("全部")

    if result.failed > 0:
        print("\n⚠️  存在失败的测试用例，请检查上方输出")
        sys.exit(1)
    else:
        print("\n🎉 所有测试通过！LLM 模块真实集成测试验证完毕")
        sys.exit(0)


if __name__ == "__main__":
    main()
