"""pandaren/llm/cache_usage.py — 跨 provider 归一的缓存命中观测视图

提供 `extract_cache_usage()` 工具函数，让业务代码一行代码拿到
{hit_tokens, write_tokens, is_first_write} 三元组，不需要按 provider 分支。

与 L4 归一（client._build_usage_info）的边界：
  - L4 归一解决"cached_tokens 字段名统一读"（已在 client.py 中实现）
  - 本模块解决"{hit, write, is_first_write} 三元组跨 provider 语义统一"

用法：
    from pandaren.llm import extract_cache_usage

    cu = extract_cache_usage(resp["usage"], client.capabilities)
    # {'hit_tokens': 4765, 'write_tokens': 0, 'is_first_write': False, 'raw': {...}}
"""

from __future__ import annotations

from typing import Any, TypedDict

from .capabilities import EndpointCapabilities


class CacheUsage(TypedDict, total=False):
    """跨 provider 归一的缓存命中视图。

    字段可能为 None —— 表示当前 provider **不提供**该信息（协议事实），
    不是"没命中"。区分"没命中"和"无此概念"对计费估算很重要。
    """

    hit_tokens: int
    """本次命中的缓存 token 数（0 = 没命中；None = 协议无此字段）。"""

    write_tokens: int | None
    """本次写入缓存的 token 数；None = 协议无写入概念。"""

    is_first_write: bool | None
    """本次是否为首写；None = 无法判断。"""

    raw: dict[str, Any]
    """原始 usage dict 透传（方便调试）。"""


# 只有这些 provider 的 cache_creation 字段语义是"真写入量"，
# 可以安全推断 is_first_write。
# DeepSeek 的 prompt_cache_miss_tokens 是"本请求没命中的部分"，不是首写量。
_TRUE_WRITE_PROVIDERS = frozenset({"dashscope", "anthropic"})


def extract_cache_usage(
    usage: dict[str, Any],
    caps: EndpointCapabilities | None = None,
) -> CacheUsage:
    """从 UsageInfo dict 中提取归一化的缓存命中视图。

    Args:
        usage: LLMResponse["usage"] 返回的 UsageInfo dict（已经过 L4 归一）。
        caps:  client.capabilities；None 时降级到只读 OpenAI 标准路径。

    Returns:
        CacheUsage dict，各字段语义见 CacheUsage docstring。

    caps=None 降级行为：
      - hit_tokens：只读 usage.prompt_tokens_details.cached_tokens（OpenAI 标准路径）；
        读不到返回 0
      - write_tokens：只读 usage.prompt_tokens_details.cache_creation_input_tokens；
        读不到返回 None
      - is_first_write：始终返回 None（没有 provider 信息无法判断）
      - raw：直接透传入参 usage
    """
    ptd = usage.get("prompt_tokens_details") or {}

    # 命中量：L4 归一后统一在 cached_tokens
    hit_tokens: int = ptd.get("cached_tokens", 0) or 0

    # 写入量：L4 归一后统一在 cache_creation_input_tokens
    raw_write = ptd.get("cache_creation_input_tokens")
    write_tokens: int | None = int(raw_write) if raw_write is not None else None

    # 首写判断
    is_first_write: bool | None = None
    if caps is not None and caps.provider in _TRUE_WRITE_PROVIDERS:
        if write_tokens is not None:
            is_first_write = write_tokens > 0 and hit_tokens == 0
    # caps=None 或 provider 不在白名单时 → is_first_write = None（无法判断）

    return CacheUsage(
        hit_tokens=hit_tokens,
        write_tokens=write_tokens,
        is_first_write=is_first_write,
        raw=usage,
    )
