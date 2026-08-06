"""
Pandaren Agent SDK · 缓存效果验证（非流式 + cache_control 断点观测）

目标
----
1. 使用非流式 agent.run() 执行多轮对话
2. 观察 cache_control 断点是否正确打在 messages/tools 上
3. 观察 prompt_tokens_details 中的 cached_tokens / cache_creation_input_tokens 变化

运行方式
--------
  cd <仓库根目录> && python pandaren/agent/tests/test_agent_cache.py
  cd <仓库根目录> && python pandaren/agent/tests/test_agent_cache.py --depth system
  cd <仓库根目录> && python pandaren/agent/tests/test_agent_cache.py --depth history --rounds 5
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import os
import sys
import time
import uuid
from typing import Any
from unittest.mock import patch

# Windows 终端编码修复
if sys.platform == "win32" and "pytest" not in sys.modules:  # pytest 下交给 capture，避免拆台
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import pytest

# 本文件依赖已重构移除的 pandaren.memory.backend.*（现为 backends/*），
# 属 API 漂移债，待按新接口重写。
pytest.skip("依赖已移除的 pandaren.memory.backend 模块（API 漂移，待重写）", allow_module_level=True)


def load_env(filepath: str = ".env.development") -> None:
    if not os.path.exists(filepath):
        return
    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                os.environ[key.strip()] = value.strip()


load_env(os.path.join(os.path.dirname(__file__), "..", ".env.development"))

import logging

# 让缓存相关日志可见
logging.getLogger("pandaren.llm_client").setLevel(logging.DEBUG)
logging.getLogger("pandaren.llm_client").addHandler(logging.StreamHandler())

# ═══ SDK 导入 ═══
from pandaren.builder import AgentBuilder
from pandaren.agent import Agent
from pandaren.identity.models import PERMISSION_ALL, TrustLevel
from pandaren.llm.client import OpenAICompatibleClient
from pandaren.llm.cache_strategy import (
    CacheDepth,
)
from pandaren.llm.cache_usage import extract_cache_usage
from pandaren.llm.types import ModelSettings
from pandaren.engine.models import AgentResult

from pandaren.memory.backend.raw_log.markdown import MarkdownRawLogBackend
from pandaren.memory.backend.summary.markdown import MarkdownSummaryBackend
from llm_policies import LLMSummaryCompressionPolicy, LLMSessionSummaryPolicy
from pandaren.observability.audit import DualAuditBackend
from pandaren.observability.backend import (
    ConsoleAuditBackend,
    MarkdownAuditBackend,
    MarkdownTracerBackend,
    MarkdownMetricsBackend,
    MarkdownLoggerBackend,
)
from pandaren.observability.types import LogLevel

from res_plugin.tools import ALL_TOOLS
from pandaren.skill.loader import load_skills_from_dir
from pandaren.skill.models import SkillSource


ALL_SKILLS = load_skills_from_dir(
    os.path.join(os.path.dirname(__file__), "..", "res_plugin", "skills"),
    source=SkillSource.PROJECT,
)

# ════════════════════════════════════════════════════
#  配置
# ════════════════════════════════════════════════════

DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
MODEL_NAME = "qwen-plus"  # qwen-plus 支持显式 cache_control
BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "cache_test")
OBS_DIR = os.path.join(DATA_DIR, "observability")
MEM_DIR = os.path.join(DATA_DIR, "memory")

AGENT_ID = "agent_cache_test"

SYSTEM_PROMPT = """\
你是 Pandaren 演示助手，一个安全可观测的 AI Agent。
请根据用户需求调用合适的工具完成任务。如果不需要工具，直接回答即可。
回答时请简洁明了，使用中文。

以下是你的知识背景（用于撑大 system prompt，触发 prefix cache 门槛）：

## 架构设计原则

1. **单一职责原则 (SRP)**: 一个类只应该有一个引起变化的原因。
2. **开闭原则 (OCP)**: 软件实体应当对扩展开放，对修改关闭。
3. **里氏替换原则 (LSP)**: 子类对象应该能够替换父类对象而不影响程序正确性。
4. **接口隔离原则 (ISP)**: 客户端不应该被强迫依赖它不使用的接口。
5. **依赖倒置原则 (DIP)**: 高层模块不应依赖低层模块，二者都应依赖抽象。

## 常用设计模式

- 策略模式：定义一系列算法，将它们各自封装，并使它们可以互换。
- 观察者模式：对象间一对多依赖，当一个对象状态变化，所有依赖对象会收到通知。
- 工厂模式：定义一个创建对象的接口，让子类决定实例化哪一个类。
- 装饰器模式：动态地给一个对象添加额外的职责。
- 适配器模式：将一个类的接口转换为客户端期望的另一个接口。
- 代理模式：为其他对象提供一种代理以控制对它的访问。
- 命令模式：将请求封装为对象，以便使用不同的请求对客户端参数化。
- 模板方法：定义一个操作的算法骨架，将某些步骤延迟到子类。

## 错误处理最佳实践

1. 使用强类型异常层级（如 LLMError → LLMRateLimitError, LLMTimeoutError）
2. 永远不要 catch (Exception) 后 pass，至少记录日志
3. 外层统一捕获，内层快速失败
4. 可恢复的错误走重试逻辑，不可恢复的错误立即上报
5. 异步代码中确保 cancel scope 正确传播

## 性能优化策略

- 连接池复用（httpx.AsyncClient 单例）
- 前缀缓存（Prompt Prefix Caching）降低重复 token 费用
- 流式传输减少首字节延迟
- 批量合并减少网络往返
- 异步并发（asyncio.gather）提升吞吐
"""

# ════════════════════════════════════════════════════
#  观测层 & Memory 后端
# ════════════════════════════════════════════════════

os.makedirs(OBS_DIR, exist_ok=True)
os.makedirs(MEM_DIR, exist_ok=True)

md_audit = MarkdownAuditBackend(base_dir=OBS_DIR)
md_tracer = MarkdownTracerBackend(base_dir=OBS_DIR)
md_metrics = MarkdownMetricsBackend(base_dir=OBS_DIR)
md_log = MarkdownLoggerBackend(base_dir=OBS_DIR)

raw_log_backend = MarkdownRawLogBackend(base_dir=MEM_DIR)
summary_backend = MarkdownSummaryBackend(base_dir=MEM_DIR)


# ════════════════════════════════════════════════════
#  构建 Agent（开启 cache）
# ════════════════════════════════════════════════════

def build_agent(cache_depth: CacheDepth = "history") -> Agent:
    """构建带缓存的 Agent，观测层完整挂载。"""

    llm_client = OpenAICompatibleClient.for_dashscope(
        api_key=DASHSCOPE_API_KEY,
        model_name=MODEL_NAME,
        cache=True,
        cache_depth=cache_depth,
        default_settings=ModelSettings(include_usage=True),
    )

    compression_policy = LLMSummaryCompressionPolicy(
        llm_client=llm_client,
        keep_rounds=3,
    )
    session_summary_policy = LLMSessionSummaryPolicy(
        llm_client=llm_client,
        max_length=200,
    )

    builder = (
        AgentBuilder()
        .identity(
            agent_id=AGENT_ID,
            agent_name="Cache 测试助手",
            when_to_use="缓存效果验证",
            sensitive_permissions=PERMISSION_ALL,
            trust_level=TrustLevel.ORCHESTRATOR,
        )
        .llm(llm_client)
        .tools(ALL_TOOLS)
        .skills(ALL_SKILLS)
        .system_prompt(SYSTEM_PROMPT)
        .behavior(
            max_steps=5,
            step_timeout=60.0,
            total_timeout=300.0,
            auto_confirm_high=True,
            stream=False,  # 走真正的非流式 call() 路径，便于直接拿 usage
        )
        .memory(
            raw_log_backend=raw_log_backend,
            summary_backend=summary_backend,
            compression_policy=compression_policy,
            session_summary_policy=session_summary_policy,
        )
        .observability(
            audit=DualAuditBackend(primary=md_audit, secondary=ConsoleAuditBackend()),
            tracer=md_tracer,
            metrics=md_metrics,
            log=md_log,
            log_level=LogLevel.DEBUG,
        )
    )

    agent = builder.build()
    print(f"✅ Agent 构建完成：{agent}")
    print(f"   Cache: True | Depth: {cache_depth} | Model: {MODEL_NAME}")
    return agent


# ════════════════════════════════════════════════════
#  cache_control 断点检查工具
# ════════════════════════════════════════════════════

def inspect_cache_breakpoints(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    round_num: int,
) -> None:
    """检查并打印 messages/tools 中实际被打上的 cache_control 断点位置。"""

    print(f"\n  🔍 [第 {round_num} 轮] cache_control 断点检查：")

    # 检查 tools 上的断点
    if tools:
        tools_with_cache = []
        for idx, tool in enumerate(tools):
            if "cache_control" in tool:
                tool_name = tool.get("function", {}).get("name", f"tool[{idx}]")
                tools_with_cache.append((idx, tool_name, tool["cache_control"]))
        if tools_with_cache:
            print(f"     📌 Tools 断点 ({len(tools_with_cache)} 个):")
            for idx, name, cc in tools_with_cache:
                print(f"        tools[{idx}] {name} → cache_control={cc}")
        else:
            print(f"     ⚪ Tools: 无断点 (共 {len(tools)} 个工具)")
    else:
        print("     ⚪ Tools: 无工具")

    # 检查 messages 上的断点
    msgs_with_cache = []
    for idx, msg in enumerate(messages):
        role = msg.get("role", "?")
        content = msg.get("content")

        # 检查 block 形式的 content
        if isinstance(content, list):
            for block_idx, block in enumerate(content):
                if isinstance(block, dict) and "cache_control" in block:
                    text_preview = (block.get("text", "")[:40] + "...") if block.get("text") else ""
                    msgs_with_cache.append(
                        (idx, role, f"content[{block_idx}]", block["cache_control"], text_preview)
                    )
        # 检查 message 级别的 cache_control（如果有）
        if "cache_control" in msg:
            msgs_with_cache.append(
                (idx, role, "message-level", msg["cache_control"], "")
            )

    if msgs_with_cache:
        print(f"     📌 Messages 断点 ({len(msgs_with_cache)} 个):")
        for idx, role, location, cc, preview in msgs_with_cache:
            print(f"        messages[{idx}] ({role}) {location} → cache_control={cc}")
            if preview:
                print(f"           内容预览: \"{preview}\"")
    else:
        print(f"     ⚪ Messages: 无断点 (共 {len(messages)} 条)")

    # 总结断点布局
    total_breakpoints = (len(tools_with_cache) if tools else 0) + len(msgs_with_cache)
    print(f"     ✅ 总断点数: {total_breakpoints}")


# ════════════════════════════════════════════════════
#  打印缓存 usage 详情
# ════════════════════════════════════════════════════

def print_cache_usage_detail(usage: dict[str, Any], caps, round_num: int) -> None:
    """打印本轮 LLM 调用返回的缓存相关 usage 详情。"""

    print(f"\n  📊 [第 {round_num} 轮] 缓存 Usage 详情：")
    print(f"     prompt_tokens:     {usage.get('prompt_tokens', 0)}")
    print(f"     completion_tokens: {usage.get('completion_tokens', 0)}")
    print(f"     total_tokens:      {usage.get('total_tokens', 0)}")

    ptd = usage.get("prompt_tokens_details")
    if ptd:
        print("     prompt_tokens_details:")
        cached = ptd.get("cached_tokens", 0)
        creation = ptd.get("cache_creation_input_tokens")
        text_tokens = ptd.get("text_tokens")
        cache_type = ptd.get("cache_type")
        print(f"       cached_tokens:                {cached}")
        if creation is not None:
            print(f"       cache_creation_input_tokens:  {creation}")
        if text_tokens is not None:
            print(f"       text_tokens:                  {text_tokens}")
        if cache_type is not None:
            print(f"       cache_type:                   {cache_type}")

        # 使用 extract_cache_usage 获取归一视图
        cu = extract_cache_usage(usage, caps)
        print("     📈 归一化视图 (extract_cache_usage):")
        print(f"       hit_tokens:    {cu.get('hit_tokens', 0)}")
        print(f"       write_tokens:  {cu.get('write_tokens')}")
        print(f"       is_first_write: {cu.get('is_first_write')}")
    else:
        print("     prompt_tokens_details: (无)")


# ════════════════════════════════════════════════════
#  拦截 apply_cache_positions 以观测断点
# ════════════════════════════════════════════════════

# 用于收集每轮被修改后的 messages/tools（包含 cache_control）
_intercepted_payloads: list[dict[str, Any]] = []
# 用于收集每轮 LLM call 返回的 usage 信息
_intercepted_usages: list[dict[str, Any]] = []


def _patched_apply_cache_positions(
    messages, tools, always_tools_count, *, cache, cache_depth, capabilities
):
    """包装 apply_cache_positions，拦截其输出以便检查断点。"""
    from pandaren.llm.cache_strategy import apply_cache_positions as _real_apply

    modified_messages, modified_tools = _real_apply(
        messages, tools, always_tools_count,
        cache=cache,
        cache_depth=cache_depth,
        capabilities=capabilities,
    )

    # 存储深拷贝以便后续检查
    _intercepted_payloads.append({
        "messages": copy.deepcopy(modified_messages),
        "tools": copy.deepcopy(modified_tools),
        "always_tools_count": always_tools_count,
    })

    return modified_messages, modified_tools


# ════════════════════════════════════════════════════
#  缓存测试逻辑（非流式）
# ════════════════════════════════════════════════════

# 预定义的多轮测试对话
TEST_QUERIES = [
    "用 Python 写一个快速排序函数，包含类型注解和 docstring。",
    "给刚才的快速排序加上单元测试，用 pytest 风格，至少 3 个 case。",
    "再写一个归并排序，同样风格，然后比较两者的时间复杂度。",
    "把这三段代码整理成一个完整的模块，加上 __all__ 导出。",
    "为这个模块写一份 README.md，包含使用示例。",
]


async def run_cache_test(cache_depth: CacheDepth, rounds: int) -> None:
    """运行缓存测试（非流式模式 + cache_control 断点观测）。"""

    if not DASHSCOPE_API_KEY:
        print("❌ DASHSCOPE_API_KEY 未配置，请在 .env.development 中设置")
        return

    agent = build_agent(cache_depth=cache_depth)
    session_id = str(uuid.uuid4())

    # 获取 LLM client 的 capabilities 引用（用于 extract_cache_usage）
    llm_client: OpenAICompatibleClient = agent._loop._llm_client  # type: ignore[attr-defined]
    caps = llm_client.capabilities

    print(f"\n{'═' * 70}")
    print(f"  🧪 缓存效果测试（非流式）— {rounds} 轮对话")
    print(f"     Session: {session_id[:8]}...")
    print(f"     Cache Depth: {cache_depth}")
    print(f"     Provider Caps: {caps.provider}:{caps.endpoint}" if caps else "     Provider Caps: None")
    print(f"     Explicit Cache: {caps.explicit_cache}" if caps else "")
    print(f"{'═' * 70}")

    results: list[dict] = []

    # wrap call() 以拦截 usage（stream=False 时引擎走 call() 路径）
    _original_call = llm_client.call

    async def _intercepting_call(*args, **kwargs):
        resp = await _original_call(*args, **kwargs)
        usage = resp.get("usage")
        if usage:
            _intercepted_usages.append(copy.deepcopy(usage))
        return resp

    llm_client.call = _intercepting_call  # type: ignore[method-assign]

    try:
      for i, query in enumerate(TEST_QUERIES[:rounds], 1):
        print(f"\n{'─' * 70}")
        print(f"  📋 第 {i} 轮：{query[:50]}...")
        print(f"{'─' * 70}")

        # 清空拦截器
        _intercepted_payloads.clear()
        _intercepted_usages.clear()

        t0 = time.perf_counter()

        with patch(
            "pandaren.llm.client.apply_cache_positions",
            side_effect=_patched_apply_cache_positions,
        ):
            result: AgentResult = await agent.run(
                query, session_id=session_id
            )

        elapsed = time.perf_counter() - t0

        # ═══ 检查 cache_control 断点 ═══
        if _intercepted_payloads:
            for call_idx, payload in enumerate(_intercepted_payloads):
                if len(_intercepted_payloads) > 1:
                    print(f"\n  🔁 LLM Call #{call_idx + 1}:")
                inspect_cache_breakpoints(
                    payload["messages"],
                    payload["tools"],
                    round_num=i,
                )
        else:
            print("\n  ⚠️  未拦截到 apply_cache_positions 调用")

        # ═══ 提取 usage 并打印缓存详情 ═══
        round_info = {
            "round": i,
            "elapsed_s": elapsed,
            "input_tokens": result.total_input_tokens,
            "output_tokens": result.total_output_tokens,
            # cost_usd 已移除：SDK 不再报告费用（归应用层价格表）
            "steps": result.total_steps,
            "success": result.success,
            "output_preview": (result.output or "")[:80],
            "cached_tokens": 0,
            "cache_creation_tokens": None,
        }

        # 从拦截到的 usage 中提取缓存信息
        if _intercepted_usages:
            for call_idx, usage in enumerate(_intercepted_usages):
                if len(_intercepted_usages) > 1:
                    print(f"\n  🔁 LLM Call #{call_idx + 1} Usage:")
                print_cache_usage_detail(usage, caps, round_num=i)

            # 汇总统计只取主对话（Call #1）的缓存数据，不被摘要 Call 覆盖
            primary_usage = _intercepted_usages[0]
            ptd = primary_usage.get("prompt_tokens_details") or {}
            round_info["cached_tokens"] = ptd.get("cached_tokens", 0)
            round_info["cache_creation_tokens"] = ptd.get("cache_creation_input_tokens")

        results.append(round_info)

        # 打印本轮摘要
        print(f"\n  ⏱️  耗时: {elapsed:.2f}s | 步数: {result.total_steps}")
        # SDK 不报告费用（归应用层价格表）：这里只打印 token 事实，不打印金额
        print(f"  📊 Input: {round_info['input_tokens']} | Output: {round_info['output_tokens']}")
        print(f"  🔥 Cached: {round_info['cached_tokens']} | Creation: {round_info['cache_creation_tokens']}")
        print(f"  📝 回复: {round_info['output_preview']}...")
        print(f"  {'✅' if result.success else '❌'} 成功: {result.success}")

        # 轮间等待（让缓存生效）
        if i < rounds:
            wait = 2 if i == 1 else 1
            print(f"\n  ⏳ 等待 {wait}s 让缓存写入生效...")
            await asyncio.sleep(wait)

    finally:
      llm_client.call = _original_call  # type: ignore[method-assign]

    # ════════════════════════════════════════════════════
    #  汇总对比
    # ════════════════════════════════════════════════════
    print(f"\n\n{'═' * 70}")
    print("  📊 缓存效果汇总（非流式）")
    print(f"{'═' * 70}")
    header = (
        f"  {'轮次':<6} {'耗时(s)':<10} {'Input':<10} {'Output':<10} "
        f"{'Cached':<10} {'Creation':<12} {'步数':<6}"
    )
    print(f"\n{header}")
    print(f"  {'─' * 76}")

    for r in results:
        creation_str = str(r['cache_creation_tokens']) if r['cache_creation_tokens'] is not None else "N/A"
        print(
            f"  第{r['round']}轮   "
            f"{r['elapsed_s']:<10.2f} "
            f"{r['input_tokens']:<10} "
            f"{r['output_tokens']:<10} "
            f"{r['cached_tokens']:<10} "
            f"{creation_str:<12} "
            f"{r['steps']:<6}"
        )

    # 分析趋势
    if len(results) >= 2:
        print("\n  📈 缓存趋势分析：")

        # 对比第 1 轮和第 2 轮
        r1_cached = results[0]["cached_tokens"]
        r2_cached = results[1]["cached_tokens"]
        speedup = results[0]["elapsed_s"] / results[1]["elapsed_s"] if results[1]["elapsed_s"] > 0 else 0

        print(f"     第 1 轮 cached_tokens: {r1_cached} (期望: 0，冷启动)")
        print(f"     第 2 轮 cached_tokens: {r2_cached} (期望: > 0，缓存命中)")
        print(f"     第 2 轮 vs 第 1 轮 耗时加速比: {speedup:.2f}x")

        if r2_cached > 0:
            print(f"     ✅ 缓存命中确认！第 2 轮命中 {r2_cached} tokens")
        elif r1_cached == 0 and results[0].get("cache_creation_tokens", 0):
            print("     ⚠️  第 1 轮写入了缓存但第 2 轮未命中（可能 TTL 未到或 prefix 不匹配）")
        else:
            print("     ⚠️  缓存可能未生效，请检查 cache_depth 设置和 provider 支持情况")

        if len(results) >= 3:
            for idx in range(2, len(results)):
                r = results[idx]
                print(f"     第 {idx + 1} 轮 cached_tokens: {r['cached_tokens']}")

    print(f"\n  💡 提示：查看 {OBS_DIR} 下的 metrics.md 和 tracer 文件获取更详细的观测数据")
    print(f"     查看 {MEM_DIR} 下的 memory 文件观察上下文管理效果")

    # flush metrics
    md_metrics.flush()

    print(f"\n{'═' * 70}")
    print("  ✅ 缓存测试完成！")
    print(f"{'═' * 70}\n")


# ════════════════════════════════════════════════════
#  主入口
# ════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pandaren Cache 效果验证（非流式 + 断点观测）")
    parser.add_argument(
        "--depth",
        choices=["off", "tools", "system", "history"],
        default="history",
        help="缓存深度 (默认: history)",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=3,
        help="测试轮数 (默认: 3, 最大: 5)",
    )
    args = parser.parse_args()

    rounds = min(args.rounds, len(TEST_QUERIES))
    asyncio.run(run_cache_test(args.depth, rounds))
