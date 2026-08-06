"""Test script for cache SDK support (§15)."""
from pandaren.llm import OpenAICompatibleClient, extract_cache_usage, DEEPSEEK_CHAT, DASHSCOPE_CHAT
from pandaren.llm.cache_strategy import apply_cache_positions

# Test L4 归一: DeepSeek 的非标准字段应该被回填到 cached_tokens
client = OpenAICompatibleClient(
    api_key="test", model_name="deepseek-chat", base_url="http://localhost/v1",
    capabilities=DEEPSEEK_CHAT, cache=False,
)

# 模拟 DeepSeek 的 usage_data（字段名不标准）
usage_data = {
    "prompt_tokens": 1000,
    "completion_tokens": 200,
    "total_tokens": 1200,
    "prompt_cache_hit_tokens": 800,     # DeepSeek 专属字段
    "prompt_cache_miss_tokens": 200,    # DeepSeek 专属字段
}

usage_info = client._build_usage_info(usage_data)
ptd = usage_info.get("prompt_tokens_details", {})
print(f"DeepSeek cached_tokens (L4): {ptd.get('cached_tokens')}")
assert ptd.get("cached_tokens") == 800, f"Expected 800, got {ptd.get('cached_tokens')}"
print("L4 归一 DeepSeek OK!")

# Test extract_cache_usage with DeepSeek
cu = extract_cache_usage(usage_info, DEEPSEEK_CHAT)
print(f"extract_cache_usage: hit={cu['hit_tokens']}, write={cu['write_tokens']}, first_write={cu['is_first_write']}")
assert cu["hit_tokens"] == 800
assert cu["write_tokens"] == 200  # cache_creation_input_tokens was backfilled
assert cu["is_first_write"] is None  # DeepSeek not in _TRUE_WRITE_PROVIDERS
print("extract_cache_usage DeepSeek OK!")

# Test extract_cache_usage with DashScope (standard path)
client2 = OpenAICompatibleClient(
    api_key="test", model_name="qwen3-max", base_url="http://localhost/v1",
    capabilities=DASHSCOPE_CHAT, cache=False,
)
usage_data2 = {
    "prompt_tokens": 5000,
    "completion_tokens": 100,
    "total_tokens": 5100,
    "prompt_tokens_details": {
        "cached_tokens": 4765,
        "cache_creation_input_tokens": 0,
    },
}
usage_info2 = client2._build_usage_info(usage_data2)
cu2 = extract_cache_usage(usage_info2, DASHSCOPE_CHAT)
print(f"DashScope: hit={cu2['hit_tokens']}, write={cu2['write_tokens']}, first_write={cu2['is_first_write']}")
assert cu2["hit_tokens"] == 4765
assert cu2["write_tokens"] == 0
assert cu2["is_first_write"] == False  # dashscope in _TRUE_WRITE_PROVIDERS
print("extract_cache_usage DashScope OK!")

# Test first_write detection
usage_data3 = {
    "prompt_tokens": 5000,
    "completion_tokens": 100,
    "total_tokens": 5100,
    "prompt_tokens_details": {
        "cached_tokens": 0,
        "cache_creation_input_tokens": 4765,
    },
}
usage_info3 = client2._build_usage_info(usage_data3)
cu3 = extract_cache_usage(usage_info3, DASHSCOPE_CHAT)
print(f"DashScope first_write: hit={cu3['hit_tokens']}, write={cu3['write_tokens']}, first_write={cu3['is_first_write']}")
assert cu3["hit_tokens"] == 0
assert cu3["write_tokens"] == 4765
assert cu3["is_first_write"] == True
print("first_write detection OK!")

# Test caps=None fallback
cu4 = extract_cache_usage(usage_info2, None)
assert cu4["hit_tokens"] == 4765
assert cu4["is_first_write"] is None
print("extract_cache_usage caps=None fallback OK!")

# Test _apply_cache_positions
from pandaren.llm import DASHSCOPE_CHAT
client3 = OpenAICompatibleClient(
    api_key="test", model_name="qwen3-max", base_url="http://localhost/v1",
    capabilities=DASHSCOPE_CHAT, cache=True, cache_depth="system",
)
messages = [{"role": "system", "content": "You are helpful. " * 100}]
tools = [
    {"type": "function", "function": {"name": "tool_a", "parameters": {}}},
    {"type": "function", "function": {"name": "tool_b", "parameters": {}}},
    {"type": "function", "function": {"name": "search_tools", "parameters": {}}},
]
msgs_out, tools_out = apply_cache_positions(
    messages, tools, always_tools_count=2,
    cache=client3._cache, cache_depth=client3._cache_depth,
    capabilities=client3._capabilities,
)
# DashScope (supports_tool_cache_control=False)：工具级 cache_control 被官方忽略，
# SDK 不再挂断点① → tools 上不应有 cache_control（tools 仍被 system 断点②覆盖）
assert tools_out is not None, "tools_out should not be None when cache=True"
assert tools_out[1].get("cache_control") is None, f"Qwen 不支持工具级 cache_control，不应挂载: {tools_out[1]}"
assert tools_out[0].get("cache_control") is None
# messages[0] content should be upgraded to blocks with cache_control（断点②）
sys_content = msgs_out[0]["content"]
assert isinstance(sys_content, list), f"Expected list, got {type(sys_content)}"
assert sys_content[0].get("cache_control") == {"type": "ephemeral"}
print("_apply_cache_positions system depth OK (no tool breakpoint for Qwen)!")

# Verify original not modified
assert messages[0]["content"] == "You are helpful. " * 100
assert "cache_control" not in tools[1]
print("Deep copy verification OK!")

# 反向验证：supports_tool_cache_control=True 的端点（如 Anthropic 类）仍挂断点①
import dataclasses
CAPS_TOOL_CACHE = dataclasses.replace(DASHSCOPE_CHAT, supports_tool_cache_control=True)
msgs_o, tools_o = apply_cache_positions(
    messages, tools, always_tools_count=2,
    cache=True, cache_depth="system", capabilities=CAPS_TOOL_CACHE,
)
assert tools_o is not None
assert tools_o[1].get("cache_control") == {"type": "ephemeral"}, f"Got: {tools_o[1]}"
assert tools_o[0].get("cache_control") is None
print("tool-level breakpoint gated by supports_tool_cache_control OK!")

# Test cache=False does nothing
client4 = OpenAICompatibleClient(
    api_key="test", model_name="qwen3-max", base_url="http://localhost/v1",
    capabilities=DASHSCOPE_CHAT, cache=False,
)
msgs_out2, tools_out2 = apply_cache_positions(
    messages, tools, always_tools_count=2,
    cache=client4._cache, cache_depth=client4._cache_depth,
    capabilities=client4._capabilities,
)
assert msgs_out2 is messages  # no copy, same reference
assert tools_out2 is tools
print("cache=False passthrough OK!")

print("\n" + "=" * 60)
print("ALL §15 CACHE SDK TESTS PASSED!")
print("=" * 60)
