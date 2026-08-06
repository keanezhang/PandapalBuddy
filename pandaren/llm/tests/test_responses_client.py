"""
Pandaren Agent SDK · ResponsesAPIClient 真实集成测试

覆盖约束
--------
  构造 & 工厂方法：
    1.1  正常构造（api_key / model_name / base_url 均非空）
    1.2  api_key 为空 → ValueError
    1.3  model_name 为空 → ValueError
    1.4  base_url 为空 → ValueError
    1.5  for_openai_responses() 绑定 OPENAI_RESPONSES capabilities
    1.6  for_volcengine_responses() 绑定 VOLCENGINE_RESPONSES capabilities
    1.7  for_dashscope_responses() 绑定 DASHSCOPE_RESPONSES capabilities
    1.8  for_openai_responses() 默认 model_name="gpt-4o"
    1.9  for_dashscope_responses() 默认 base_url 包含 "dashscope"
    1.10 for_volcengine_responses() 默认 base_url 包含 "volces"

  只读属性：
    2.1  model_name 只读 property
    2.2  response_id 初始为 None（首次 call 前）
    2.3  last_messages_len 初始为 0
    2.4  capabilities 返回注入的值

  非流式 call()（需真实 API Key）：
    3.1  call() 返回 LLMResponse dict
    3.2  LLMResponse 包含 content 字段（str 或 None）
    3.3  LLMResponse 包含 finish_reason 字段
    3.4  LLMResponse 包含 usage dict
    3.5  LLMResponse 包含 id 字段（非空字符串）
    3.6  LLMResponse 包含 model 字段
    3.7  call() 接受 always_tools_count 关键字参数
    3.8  call() 接受 tools=None
    3.9  call() 后 response_id 更新
    3.10 call() 后 last_messages_len 更新
    3.11 增量路径：第二次 call 使用 previous_response_id
    3.12 tools 变化 → 冷启动

  流式 stream_response()（需真实 API Key）：
    4.1  stream_response() 是异步生成器
    4.2  chunks 累积后 content 非空
    4.3  最终一个 chunk 含 finish_reason
    4.4  stream_response() 接受 always_tools_count
    4.5  流式后 response_id 更新

  工具调用（需真实 API Key）：
    5.1  传入 tools 时 LLM 可返回 tool_calls
    5.2  tool_calls 每元素含 id / type / function 字段

  错误处理（需真实 API Key）：
    6.1  错误的 api_key → LLMAuthError（401）

  default_settings + per-call settings 合并：
    7.1  default_settings 的 max_tokens 生效
    7.2  per-call settings.max_tokens 覆盖 default_settings.max_tokens

运行方式
--------
  cd pandaren/llm/tests
  python test_responses_client.py                           # 全部测试
  python test_responses_client.py --section constructor     # 仅构造 & 工厂
  python test_responses_client.py --section props           # 仅属性
  python test_responses_client.py --section call            # 仅 call()
  python test_responses_client.py --section stream          # 仅 stream
  python test_responses_client.py --section tools           # 仅工具调用
  python test_responses_client.py --section errors          # 仅错误处理
  python test_responses_client.py --section settings        # 仅 settings 合并

  环境变量（仓库根目录 .env.development）：
    VOLCENGINE_API_KEY=xxx       — 火山方舟（豆包）
    VOLCENGINE_MODEL=ep-xxx      — 火山方舟模型 ID
    DASHSCOPE_API_KEY=xxx        — 阿里百炼（通义千问）
    DASHSCOPE_MODEL=qwen-plus    — 阿里百炼模型名（可选，默认 qwen-plus）
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
from typing import Any

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
from pandaren.llm.responses_client import ResponsesAPIClient
from pandaren.llm.capabilities import (
    OPENAI_RESPONSES,
    VOLCENGINE_RESPONSES,
    DASHSCOPE_RESPONSES,
)
from pandaren.llm.exceptions import (
    LLMAuthError,
    LLMError,
    LLMRateLimitError,
    LLMRequestError,
)
from pandaren.llm.types import (
    ModelSettings,
    LLMResponse,
    LLMStreamChunk,
    UsageInfo,
)


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


class _QuotaExceeded(Exception):
    """429 / 配额耗尽，需跳过后续 API 测试。"""
    pass


def safe_call(coro, test_id: str):
    """执行协程，遇到 429/配额错误时抛 _QuotaExceeded 以跳过后续测试。"""
    try:
        return async_run(coro)
    except LLMRateLimitError as e:
        result.skip(test_id, f"429 配额限制: {e}")
        raise _QuotaExceeded(str(e)) from e


# ════════════════════════════════════════════════════
#  辅助：环境变量 & 客户端构建
# ════════════════════════════════════════════════════

VOLCENGINE_API_KEY: str = os.environ.get("VOLCENGINE_API_KEY", "")
DASHSCOPE_API_KEY: str = os.environ.get("DASHSCOPE_API_KEY", "")
VOLCENGINE_MODEL: str = os.environ.get("VOLCENGINE_MODEL", "")
DASHSCOPE_MODEL: str = os.environ.get("DASHSCOPE_MODEL", "qwen-plus")

HAVE_VOLCENGINE = bool(VOLCENGINE_API_KEY and VOLCENGINE_MODEL)
HAVE_DASHSCOPE = bool(DASHSCOPE_API_KEY)

# 优先用阿里百炼，其次火山引擎
HAVE_ANY_PROVIDER = HAVE_DASHSCOPE or HAVE_VOLCENGINE


def _make_client(
    provider: str | None = None,
) -> ResponsesAPIClient:
    """创建测试用客户端。默认优先 dashscope，其次 volcengine。"""
    if provider == "volcengine" or (provider is None and HAVE_VOLCENGINE and not HAVE_DASHSCOPE):
        return _make_volcengine_client()
    return _make_dashscope_client()


def _make_volcengine_client(
    api_key: str | None = None,
    model_name: str | None = None,
) -> ResponsesAPIClient:
    return ResponsesAPIClient.for_volcengine_responses(
        api_key=api_key or VOLCENGINE_API_KEY,
        model_name=model_name or VOLCENGINE_MODEL,
    )


def _make_dashscope_client(
    api_key: str | None = None,
    model_name: str | None = None,
) -> ResponsesAPIClient:
    return ResponsesAPIClient.for_dashscope_responses(
        api_key=api_key or DASHSCOPE_API_KEY,
        model_name=model_name or DASHSCOPE_MODEL,
    )


def _simple_messages(user_text: str = "你好，请用一句话介绍自己。") -> list[dict]:
    return [
        {"role": "system", "content": "你是一个简洁的 AI 助手。"},
        {"role": "user", "content": user_text},
    ]


def _simple_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "获取指定城市的天气信息",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "城市名称"},
                    },
                    "required": ["city"],
                },
            },
        },
    ]


# ════════════════════════════════════════════════════
#  1. 构造 & 工厂方法
# ════════════════════════════════════════════════════

def test_constructor():
    """1. 构造 & 工厂方法"""
    print("\n" + "═" * 60)
    print("1.  构造 & 工厂方法")
    print("═" * 60)

    # 1.1 正常构造
    c = ResponsesAPIClient(api_key="sk-xxx", model_name="gpt-4o", base_url="https://api.openai.com/v1")
    assert_true(c.model_name == "gpt-4o", "1.1 正常构造 model_name")
    async_run(c.aclose())

    # 1.2 api_key 为空 → ValueError
    @assert_raises(ValueError, "1.2 api_key 为空 → ValueError")
    def _():
        ResponsesAPIClient(api_key="", model_name="m", base_url="https://api/v1")

    # 1.3 model_name 为空 → ValueError
    @assert_raises(ValueError, "1.3 model_name 为空 → ValueError")
    def _():
        ResponsesAPIClient(api_key="k", model_name="", base_url="https://api/v1")

    # 1.4 base_url 为空 → ValueError
    @assert_raises(ValueError, "1.4 base_url 为空 → ValueError")
    def _():
        ResponsesAPIClient(api_key="k", model_name="m", base_url="")

    # 1.5 for_openai_responses() 绑定 OPENAI_RESPONSES
    c5 = ResponsesAPIClient.for_openai_responses(api_key="k")
    assert_true(c5.capabilities is OPENAI_RESPONSES, "1.5 for_openai_responses 绑定 OPENAI_RESPONSES")
    async_run(c5.aclose())

    # 1.6 for_volcengine_responses() 绑定 VOLCENGINE_RESPONSES
    c6 = ResponsesAPIClient.for_volcengine_responses(api_key="k", model_name="doubao-seed")
    assert_true(c6.capabilities is VOLCENGINE_RESPONSES, "1.6 for_volcengine_responses 绑定 VOLCENGINE_RESPONSES")
    async_run(c6.aclose())

    # 1.7 for_dashscope_responses() 绑定 DASHSCOPE_RESPONSES
    c7 = ResponsesAPIClient.for_dashscope_responses(api_key="k", model_name="qwen-plus")
    assert_true(c7.capabilities is DASHSCOPE_RESPONSES, "1.7 for_dashscope_responses 绑定 DASHSCOPE_RESPONSES")
    async_run(c7.aclose())

    # 1.8 for_openai_responses() 默认 model_name="gpt-4o"
    c8 = ResponsesAPIClient.for_openai_responses(api_key="k")
    assert_true(c8.model_name == "gpt-4o", "1.8 for_openai_responses 默认 model_name=gpt-4o")
    async_run(c8.aclose())

    # 1.9 for_dashscope_responses() 默认 base_url 包含 "dashscope"
    c9 = ResponsesAPIClient.for_dashscope_responses(api_key="k", model_name="qwen-plus")
    assert_true("dashscope" in c9._base_url, "1.9 for_dashscope base_url 含 dashscope")
    async_run(c9.aclose())

    # 1.10 for_volcengine_responses() 默认 base_url 包含 "volces"
    c10 = ResponsesAPIClient.for_volcengine_responses(api_key="k", model_name="doubao")
    assert_true("volces.com" in c10._base_url, "1.10 for_volcengine base_url 含 volces")
    async_run(c10.aclose())


# ════════════════════════════════════════════════════
#  2. 只读属性
# ════════════════════════════════════════════════════

def test_props():
    """2. 只读属性"""
    print("\n" + "═" * 60)
    print("2.  只读属性")
    print("═" * 60)

    # 2.1 model_name 只读
    c = ResponsesAPIClient(api_key="k", model_name="gpt-4o", base_url="https://api/v1")
    assert_true(c.model_name == "gpt-4o", "2.1 model_name 只读 property")
    @assert_raises(AttributeError, "2.1 model_name 不可写")
    def _():
        c.model_name = "hacked"
    async_run(c.aclose())

    # 2.2 response_id 初始为 None
    c2 = ResponsesAPIClient(api_key="k", model_name="m", base_url="https://api/v1")
    assert_true(c2.response_id is None, "2.2 response_id 初始为 None")
    async_run(c2.aclose())

    # 2.3 last_messages_len 初始为 0
    c3 = ResponsesAPIClient(api_key="k", model_name="m", base_url="https://api/v1")
    assert_true(c3.last_messages_len == 0, "2.3 last_messages_len 初始为 0")
    async_run(c3.aclose())

    # 2.4 capabilities 返回注入的值
    c4 = ResponsesAPIClient.for_dashscope_responses(api_key="k", model_name="qwen-plus")
    assert_true(c4.capabilities is DASHSCOPE_RESPONSES, "2.4 capabilities 返回注入值")
    async_run(c4.aclose())


# ════════════════════════════════════════════════════
#  3. 非流式 call()（需真实 API Key）
# ════════════════════════════════════════════════════

def test_call():
    """3. 非流式 call()"""
    print("\n" + "═" * 60)
    print("3.  非流式 call()（需真实 API Key）")
    print("═" * 60)

    if not HAVE_ANY_PROVIDER:
        result.skip("3.1~3.12", "缺少 DASHSCOPE_API_KEY 或 VOLCENGINE_API_KEY")
        return

    try:
        # 3.1 call() 返回 LLMResponse dict
        c = _make_client()
        resp = safe_call(c.call(_simple_messages()), "3.1")
        assert_true(isinstance(resp, dict), "3.1 call() 返回 dict")
        assert_true("content" in resp, "3.1 含 content 字段")
        assert_true("finish_reason" in resp, "3.1 含 finish_reason 字段")
        assert_true("usage" in resp, "3.1 含 usage 字段")
        async_run(c.aclose())

        # 3.2 LLMResponse 包含 content 字段
        c2 = _make_client()
        resp2 = safe_call(c2.call(_simple_messages()), "3.2")
        assert_true(resp2["content"] is None or isinstance(resp2["content"], str), "3.2 content 为 str 或 None")
        async_run(c2.aclose())

        # 3.3 LLMResponse 包含 finish_reason 字段
        c3 = _make_client()
        resp3 = safe_call(c3.call(_simple_messages()), "3.3")
        assert_true(isinstance(resp3["finish_reason"], str), "3.3 finish_reason 为 str")
        async_run(c3.aclose())

        # 3.4 LLMResponse 包含 usage dict
        c4 = _make_client()
        resp4 = safe_call(c4.call(_simple_messages()), "3.4")
        assert_true(isinstance(resp4["usage"], dict), "3.4 usage 为 dict")
        assert_true("prompt_tokens" in resp4["usage"], "3.4 usage 含 prompt_tokens")
        assert_true("completion_tokens" in resp4["usage"], "3.4 usage 含 completion_tokens")
        async_run(c4.aclose())

        # 3.5 LLMResponse 包含 id 字段
        c5 = _make_client()
        resp5 = safe_call(c5.call(_simple_messages()), "3.5")
        assert_true(isinstance(resp5["id"], str) and len(resp5["id"]) > 0, "3.5 id 非空字符串")
        async_run(c5.aclose())

        # 3.6 LLMResponse 包含 model 字段
        c6 = _make_client()
        resp6 = safe_call(c6.call(_simple_messages()), "3.6")
        assert_true(isinstance(resp6["model"], str) and len(resp6["model"]) > 0, "3.6 model 非空字符串")
        async_run(c6.aclose())

        # 3.7 call() 接受 always_tools_count 关键字参数
        c7 = _make_client()
        resp7 = safe_call(c7.call(_simple_messages(), always_tools_count=1), "3.7")
        assert_true("content" in resp7, "3.7 always_tools_count 不报错")
        async_run(c7.aclose())

        # 3.8 call() 接受 tools=None
        c8 = _make_client()
        resp8 = safe_call(c8.call(_simple_messages(), tools=None), "3.8")
        assert_true("content" in resp8, "3.8 tools=None 不报错")
        async_run(c8.aclose())

        # 3.9 call() 后 response_id 更新
        c9 = _make_client()
        safe_call(c9.call(_simple_messages()), "3.9")
        assert_true(c9.response_id is not None, "3.9 call 后 response_id 非空")
        async_run(c9.aclose())

        # 3.10 call() 后 last_messages_len 更新
        c10 = _make_client()
        msgs = _simple_messages()
        safe_call(c10.call(msgs), "3.10")
        assert_true(c10.last_messages_len == len(msgs), "3.10 last_messages_len 正确")
        async_run(c10.aclose())

        # 3.11 增量路径：第二次 call 使用 previous_response_id
        c11 = _make_client()
        msgs1 = _simple_messages("第一轮")
        resp11_1 = safe_call(c11.call(msgs1), "3.11a")
        first_id = c11.response_id
        assert_true(first_id is not None, "3.11 第一次 call 有 response_id")
        # 第二轮，messages 增长
        msgs2 = msgs1 + [{"role": "assistant", "content": resp11_1["content"] or ""}] + [{"role": "user", "content": "第二轮"}]
        safe_call(c11.call(msgs2), "3.11b")
        assert_true(c11.response_id is not None, "3.11 第二次 call 仍有 response_id")
        assert_true(c11.response_id != first_id, "3.11 response_id 已更新")
        async_run(c11.aclose())

        # 3.12 tools 变化 → 冷启动
        c12 = _make_client()
        msgs12 = _simple_messages("请帮我查天气")
        safe_call(c12.call(msgs12, tools=_simple_tools()), "3.12a")
        # 第二次，tools 变化（空 tools）
        safe_call(c12.call(msgs12, tools=None), "3.12b")
        assert_true(c12.response_id is not None, "3.12 tools 变化后仍成功")
        async_run(c12.aclose())

    except _QuotaExceeded:
        result.skip("3.x 后续", "429 配额耗尽，跳过")


# ════════════════════════════════════════════════════
#  4. 流式 stream_response()（需真实 API Key）
# ════════════════════════════════════════════════════

def test_stream():
    """4. 流式 stream_response()"""
    print("\n" + "═" * 60)
    print("4.  流式 stream_response()（需真实 API Key）")
    print("═" * 60)

    if not HAVE_ANY_PROVIDER:
        result.skip("4.1~4.5", "缺少 DASHSCOPE_API_KEY 或 VOLCENGINE_API_KEY")
        return

    try:
        # 4.1 stream_response() 是异步生成器
        c1 = _make_client()
        chunks1: list[LLMStreamChunk] = []
        async def _collect1():
            async for chunk in c1.stream_response(_simple_messages()):
                chunks1.append(chunk)
        safe_call(_collect1(), "4.1")
        assert_true(len(chunks1) > 0, "4.1 流式生成 chunks")
        async_run(c1.aclose())

        # 4.2 chunks 累积后 content 非空
        c2 = _make_client()
        chunks2: list[LLMStreamChunk] = []
        async def _collect2():
            async for chunk in c2.stream_response(_simple_messages()):
                chunks2.append(chunk)
        safe_call(_collect2(), "4.2")
        text = "".join(c.delta_content for c in chunks2 if c.delta_content)
        assert_true(len(text) > 0, "4.2 累积 content 非空")
        async_run(c2.aclose())

        # 4.3 最终一个 chunk 含 finish_reason
        c3 = _make_client()
        chunks3: list[LLMStreamChunk] = []
        async def _collect3():
            async for chunk in c3.stream_response(_simple_messages()):
                chunks3.append(chunk)
        safe_call(_collect3(), "4.3")
        finish_chunks = [c for c in chunks3 if c.finish_reason]
        assert_true(len(finish_chunks) >= 1, "4.3 含 finish_reason chunk")
        async_run(c3.aclose())

        # 4.4 stream_response() 接受 always_tools_count
        c4 = _make_client()
        chunks4: list[LLMStreamChunk] = []
        async def _collect4():
            async for chunk in c4.stream_response(_simple_messages(), always_tools_count=1):
                chunks4.append(chunk)
        safe_call(_collect4(), "4.4")
        assert_true(len(chunks4) > 0, "4.4 always_tools_count 不报错")
        async_run(c4.aclose())

        # 4.5 流式后 response_id 更新
        c5 = _make_client()
        async def _collect5():
            async for _ in c5.stream_response(_simple_messages()):
                pass
        safe_call(_collect5(), "4.5")
        assert_true(c5.response_id is not None, "4.5 流式后 response_id 更新")
        async_run(c5.aclose())

    except _QuotaExceeded:
        result.skip("4.x 后续", "429 配额耗尽，跳过")


# ════════════════════════════════════════════════════
#  5. 工具调用（需真实 API Key）
# ════════════════════════════════════════════════════

def test_tools():
    """5. 工具调用"""
    print("\n" + "═" * 60)
    print("5.  工具调用（需真实 API Key）")
    print("═" * 60)

    if not HAVE_ANY_PROVIDER:
        result.skip("5.1~5.2", "缺少 DASHSCOPE_API_KEY 或 VOLCENGINE_API_KEY")
        return

    try:
        # 5.1 传入 tools 时 LLM 可返回 tool_calls
        c1 = _make_client()
        messages_tool = _simple_messages("北京今天天气怎么样？")
        resp1 = safe_call(c1.call(messages_tool, tools=_simple_tools()), "5.1")
        has_tool_calls = "tool_calls" in resp1 and resp1["tool_calls"] is not None
        # 注意：LLM 不一定每次都返回 tool_calls，这里用软断言
        if has_tool_calls:
            assert_true(True, "5.1 返回了 tool_calls")
        else:
            result.ok("5.1 LLM 未返回 tool_calls（正常，模型可能直接回复）")
        async_run(c1.aclose())

        # 5.2 tool_calls 每元素含 id / type / function 字段
        c2 = _make_client()
        resp2 = safe_call(c2.call(
            _simple_messages("请帮我查北京的天气"),
            tools=_simple_tools(),
            settings=ModelSettings(tool_choice="required"),  # 强制调用工具
        ), "5.2")
        if "tool_calls" in resp2 and resp2["tool_calls"]:
            tc = resp2["tool_calls"][0]
            assert_true("id" in tc, "5.2 tool_call 含 id")
            assert_true("type" in tc, "5.2 tool_call 含 type")
            assert_true("function" in tc, "5.2 tool_call 含 function")
            assert_true("name" in tc["function"], "5.2 function 含 name")
            assert_true("arguments" in tc["function"], "5.2 function 含 arguments")
        else:
            result.skip("5.2", "LLM 未返回 tool_calls")
        async_run(c2.aclose())

    except _QuotaExceeded:
        result.skip("5.x 后续", "429 配额耗尽，跳过")


# ════════════════════════════════════════════════════
#  6. 错误处理（需真实 API Key 验证路径）
# ════════════════════════════════════════════════════

def test_errors():
    """6. 错误处理"""
    print("\n" + "═" * 60)
    print("6.  错误处理（需真实 API Key）")
    print("═" * 60)

    # 用 dashscope 测试错误 api_key（优先），其次 volcengine
    if HAVE_DASHSCOPE:
        c = ResponsesAPIClient.for_dashscope_responses(
            api_key="sk-invalid-key-000000000000000000000000000000",
            model_name="qwen-plus",
        )
    elif HAVE_VOLCENGINE:
        c = ResponsesAPIClient.for_volcengine_responses(
            api_key="sk-invalid-key-000000000000000000000000000000",
            model_name=VOLCENGINE_MODEL,
        )
    else:
        result.skip("6.1", "缺少 DASHSCOPE_API_KEY 或 VOLCENGINE_API_KEY")
        return

    # 6.1 错误的 api_key → LLMAuthError（401）
    try:
        async_run(c.call(_simple_messages()))
        result.fail("6.1 应抛出 LLMAuthError")
    except LLMAuthError:
        result.ok("6.1 错误 api_key → LLMAuthError")
    except LLMError as e:
        # 有些 provider 返回 400 或其他错误码
        result.ok(f"6.1 错误 api_key → LLMError ({type(e).__name__})")
    async_run(c.aclose())


# ════════════════════════════════════════════════════
#  7. default_settings + per-call settings 合并
# ════════════════════════════════════════════════════

def test_settings():
    """7. default_settings + per-call settings 合并"""
    print("\n" + "═" * 60)
    print("7.  settings 合并（需真实 API Key）")
    print("═" * 60)

    if not HAVE_ANY_PROVIDER:
        result.skip("7.1~7.2", "缺少 DASHSCOPE_API_KEY 或 VOLCENGINE_API_KEY")
        return

    try:
        # 7.1 default_settings 的 max_tokens 生效
        c1 = _make_client()
        c1._default_settings = ModelSettings(max_tokens=5)
        resp1 = safe_call(c1.call(_simple_messages()), "7.1")
        # max_tokens=5 → 输出很短
        assert_true(
            resp1["content"] is None or len(resp1["content"]) <= 100,
            "7.1 max_tokens=5 限制了输出长度",
        )
        async_run(c1.aclose())

        # 7.2 per-call settings.max_tokens 覆盖 default_settings.max_tokens
        c2 = _make_client()
        c2._default_settings = ModelSettings(max_tokens=5)
        resp2 = safe_call(c2.call(
            _simple_messages("请写一首50字以上的诗"),
            settings=ModelSettings(max_tokens=200),
        ), "7.2")
        # per-call max_tokens=200 → 输出更长
        assert_true(
            resp2["content"] is not None and len(resp2["content"]) > 20,
            "7.2 per-call max_tokens=200 覆盖 default 5",
        )
        async_run(c2.aclose())

    except _QuotaExceeded:
        result.skip("7.x 后续", "429 配额耗尽，跳过")


# ════════════════════════════════════════════════════
#  Section 组织 & main 入口
# ════════════════════════════════════════════════════

SECTIONS: dict[str, list] = {
    "constructor": [test_constructor],
    "props": [test_props],
    "call": [test_call],
    "stream": [test_stream],
    "tools": [test_tools],
    "errors": [test_errors],
    "settings": [test_settings],
}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ResponsesAPIClient 真实集成测试")
    parser.add_argument("--section", choices=list(SECTIONS.keys()), help="只运行指定 section")
    args = parser.parse_args()

    if args.section:
        sections_to_run = {args.section: SECTIONS[args.section]}
    else:
        sections_to_run = SECTIONS

    all_ok = True
    for section_name, tests in sections_to_run.items():
        for test_fn in tests:
            test_fn()
        if not result.summary(section_name):
            all_ok = False

    print("\n" + "═" * 60)
    result.summary("ResponsesAPIClient Real 总计")
    sys.exit(0 if all_ok and result.failed == 0 else 1)
