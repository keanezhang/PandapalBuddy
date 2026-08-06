"""pandaren/llm/cache_strategy.py — 缓存断点策略

职责：
  - CacheDepth / CacheMode 类型定义
  - CachePosition 断点位置数据类
  - resolve_cache_positions()：按深度档位计算待打断点
  - apply_cache_positions()：在 messages / tools 上挂 cache_control
  - on_history_compacted() / on_static_context_changed()：生命周期通知

从 client.py 抽出，降低 Client 体量，便于独立测试和未来扩展
（Context API / Gemini cachedContent）。
"""

from __future__ import annotations

import copy
import logging
import warnings
from dataclasses import dataclass
from typing import Any, Literal

from .capabilities import EndpointCapabilities

logger = logging.getLogger("pandaren.llm_client")


# ═══════════════════════════════════════════════════════════════
# 缓存深度类型 & 模式
# ═══════════════════════════════════════════════════════════════

CacheDepth = Literal["off", "tools", "system", "history"]
"""缓存深度档位。

- "off"    : 不打任何断点
- "tools"  : 只在 ALWAYS 工具 schema 末尾打 1 个断点
- "system" : 在 ALWAYS 工具 + system message 末尾打 2 个断点（默认）
- "history": 3 个断点（+ 最后一个 assistant）
"""

CacheMode = Literal[True, False, "manual"]
"""缓存模式。

- True     : SDK 自动管理缓存断点（默认）
- False    : SDK 不主动挂任何显式断点
- "manual" : SDK 完全不碰 cache_control / context_id，业务走逃生舱接口自己挂
"""


# ═══════════════════════════════════════════════════════════════
# 断点位置
# ═══════════════════════════════════════════════════════════════

@dataclass
class CachePosition:
    """一个待打的 cache_control 断点位置。"""

    layer: Literal["tools", "system", "messages"]
    """断点所在层：
    - "tools"    : target = 工具索引（在 tools 参数数组中）
    - "system"   : target = system block 索引（在 messages[0].content blocks 中）
    - "messages" : target = message 索引（在 messages 数组中）
    """

    target: int
    """断点要打在哪个下标上。"""

    kind: Literal["always_tools_end", "system_end", "last_assistant"]
    """断点语义标识，用于日志和调试。"""


# ═══════════════════════════════════════════════════════════════
# 位置解析
# ═══════════════════════════════════════════════════════════════

def resolve_cache_positions(
    tools: list[dict[str, Any]] | None,
    messages: list[dict[str, Any]],
    always_tools_count: int,
    depth: CacheDepth,
    *,
    supports_tool_cache_control: bool = True,
) -> list[CachePosition]:
    """返回 0~3 个待打的断点位置，按断点编号 ①②③ 顺序。

    调用方（call() 方法）按 provider capability 决定怎么用这份位置表：
      - Anthropic / cache_control provider → 每个位置打一个 cache_control
      - 隐式 provider（OpenAI/DeepSeek）→ 不消费这份表，仅用于日志

    Args:
        tools: tools 参数数组（含 ALWAYS + search_tools + DEFERRED）；None 或空则跳过断点①
        messages: messages 参数数组
        always_tools_count: ALWAYS 工具数（由 ToolRegistry 提供，不含 search_tools）
        depth: 缓存深度档位
        supports_tool_cache_control: 该端点是否支持工具级 cache_control 断点。
            False 时跳过断点①（如 DashScope/Qwen：官方忽略 tools 上的 cache_control）。
            默认 True 以保持本纯函数对旧调用方的行为不变；SDK 自动路径由
            apply_cache_positions 按 capabilities.supports_tool_cache_control 传入真值。

    Returns:
        CachePosition 列表（0~3 个），按 ①②③ 顺序。
    """
    if depth == "off":
        return []

    positions: list[CachePosition] = []

    # ① ALWAYS 工具末尾（depth >= "tools" 且端点支持工具级 cache_control 时才打）
    # 官方事实：DashScope/Qwen 忽略 tools 上的 cache_control（只能挂 messages content），
    # 故 supports_tool_cache_control=False 时跳过①——tools 仍被 system 断点②覆盖，
    # 不损失缓存，只是省掉一个会被静默忽略的断点。
    if tools and always_tools_count > 0 and supports_tool_cache_control:
        positions.append(CachePosition(
            layer="tools",
            target=always_tools_count - 1,
            kind="always_tools_end",
        ))

    if depth == "tools":
        return positions

    # ② system message 末尾（depth >= "system"）
    # system message = messages[0]（按 13 章布局）
    if messages:
        system_msg = messages[0]
        content = system_msg.get("content")
        if content is not None:
            if isinstance(content, list) and content:
                # block 形式：断点打在最后一个 block 上
                positions.append(CachePosition(
                    layer="system",
                    target=len(content) - 1,
                    kind="system_end",
                ))
            elif isinstance(content, str) and content:
                # str 形式：target=0 表示整条 message（会被升级为 block 后打在 [0] 上）
                positions.append(CachePosition(
                    layer="system",
                    target=0,
                    kind="system_end",
                ))

    if depth == "system":
        return positions

    # ③ 最后一个 assistant（depth == "history"）
    last_asst_idx = _find_last_assistant(messages)
    if last_asst_idx is not None:
        positions.append(CachePosition(
            layer="messages",
            target=last_asst_idx,
            kind="last_assistant",
        ))

    return positions


def _find_last_assistant(messages: list[dict[str, Any]]) -> int | None:
    """从后往前扫描 messages，返回最后一个 role=="assistant" 的索引。

    B1 形态兼容性：messages 末端是 [assistant_{N-1}] [user: <system-reminder>] [user: 当前输入]，
    扫描会越过末尾的 user 消息，天然落在正确位置。
    """
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            return i
    return None


# ═══════════════════════════════════════════════════════════════
# 断点应用
# ═══════════════════════════════════════════════════════════════

def apply_cache_positions(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    always_tools_count: int,
    *,
    cache: CacheMode,
    cache_depth: CacheDepth,
    capabilities: EndpointCapabilities | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
    """按缓存配置在 messages / tools 上挂 cache_control 断点。

    返回深拷贝后的 (messages, tools)，原入参不被修改。
    仅 cache=True 且 caps.explicit_cache=="cache_control" 时才实际挂断点。

    Args:
        messages: 原始 messages 列表
        tools: 原始 tools 列表
        always_tools_count: ALWAYS 工具数（不含 search_tools）
        cache: 缓存模式
        cache_depth: 缓存深度档位
        capabilities: Provider 能力声明

    Returns:
        (modified_messages, modified_tools)
    """
    from ._internal.cache_primitives import (
        _attach_cache_control_to_tool,
        _attach_cache_control_to_message,
    )

    # 不需要挂断点的情况
    if cache is not True:
        return messages, tools

    if capabilities is None or capabilities.explicit_cache != "cache_control":
        return messages, tools

    positions = resolve_cache_positions(
        tools, messages, always_tools_count, cache_depth,
        supports_tool_cache_control=capabilities.supports_tool_cache_control,
    )

    if not positions:
        # 零位置 → 零挂载（严格约束，避免触发 DashScope 显隐互斥问题）
        return messages, tools

    # 深拷贝一份，不修改业务传入的原始结构
    messages2 = copy.deepcopy(messages)
    tools2 = copy.deepcopy(tools) if tools else None

    cache_type = capabilities.cache_control_type or "ephemeral"

    # 文本过短告警（§2.4.1：粗估）
    MIN_CHAR_HINT = 600

    for pos in positions:
        if pos.layer == "tools":
            if tools2:
                _attach_cache_control_to_tool(tools2, pos.target, cache_type)
        elif pos.layer == "system":
            # system message = messages2[0]
            if messages2:
                msg = messages2[0]
                content = msg.get("content")
                if isinstance(content, str):
                    # 需要升级为 block 形式再打
                    if (len(content) < MIN_CHAR_HINT
                            and capabilities.min_cache_tokens is not None
                            and capabilities.min_cache_tokens > 0):
                        warnings.warn(
                            f"system message 内容过短（{len(content)} 字符 < {MIN_CHAR_HINT}），"
                            f"可能无法达到 min_cache_tokens={capabilities.min_cache_tokens} 的缓存门槛。",
                            UserWarning,
                            stacklevel=4,
                        )
                    msg["content"] = [{
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": cache_type},
                    }]
                elif isinstance(content, list) and content:
                    if pos.target < len(content):
                        content[pos.target]["cache_control"] = {"type": cache_type}
        else:  # pos.layer == "messages"
            _attach_cache_control_to_message(messages2, pos.target, cache_type)

    return messages2, tools2


# ═══════════════════════════════════════════════════════════════
# 生命周期通知 & 日志
# ═══════════════════════════════════════════════════════════════

def log_cache_config(
    cache: CacheMode,
    cache_depth: CacheDepth,
    capabilities: EndpointCapabilities | None,
) -> None:
    """实例化完成后打印 cache 配置 info 日志。"""
    caps = capabilities
    provider_label = f"{caps.provider}:{caps.endpoint}" if caps else "unknown"

    if cache is False:
        logger.info(
            "cache disabled for %s\n"
            "  note: implicit server-side cache may still apply (cannot be turned off by SDK)",
            provider_label,
        )
        if cache_depth != "system":
            logger.info(
                "cache=False, cache_depth=%r is ignored.",
                cache_depth,
            )
        return

    if cache == "manual":
        logger.info(
            "cache='manual' for %s — SDK will not manage cache_control / context_id.",
            provider_label,
        )
        return

    # cache=True
    if caps is None:
        logger.info(
            "cache=True requested but no capabilities injected; "
            "cache management requires capabilities to determine mechanism.",
        )
        return

    if caps.explicit_cache == "cache_control":
        depth_desc = {
            "off": "no breakpoints",
            "tools": "breakpoint at ALWAYS-tools end",
            "system": "breakpoints at ALWAYS-tools end + system end",
            "history": "breakpoints at ALWAYS-tools end + system end + last assistant",
        }
        desc = depth_desc.get(cache_depth, "")
        if not caps.supports_tool_cache_control and cache_depth in ("tools", "system", "history"):
            # 该端点忽略工具级 cache_control（如 DashScope/Qwen）：断点①不生成
            desc += " — note: this endpoint ignores tool-level cache_control, "
            desc += "so the ALWAYS-tools breakpoint is skipped (tools still covered by the system breakpoint)"
        logger.info(
            "cache enabled for %s\n"
            "  mechanism: cache_control (explicit, up to 3 breakpoints)\n"
            "  depth:     %s (%s)\n"
            "  note:      first call writes prefixes at %s%% pricing (provider capability)",
            provider_label,
            cache_depth,
            desc,
            caps.cache_write_surcharge_percent or "100",
        )
    elif caps.explicit_cache == "context_id":
        logger.info(
            "cache enabled for %s\n"
            "  mechanism: context_id (Context API)\n"
            "  depth:     %s\n"
            "  note:      context will be lazily created on first call",
            provider_label,
            cache_depth,
        )
    else:
        # 隐式缓存 provider
        logger.info(
            "cache=True requested for %s, but provider only supports implicit caching. "
            "SDK will not attach cache_control. Server-side implicit cache will be used "
            "if applicable; stability guaranteed by 13-chapter PC1-PC7 principles.",
            provider_label,
        )


class CacheState:
    """缓存运行时状态——持有 context_id / cached_content_name / 冷启动标记。

    Client 实例持有一个 CacheState，生命周期通知通过此对象完成。
    """

    __slots__ = ("context_id", "cached_content_name", "next_call_is_cold")

    def __init__(self) -> None:
        self.context_id: str | None = None
        self.cached_content_name: str | None = None
        self.next_call_is_cold: bool = False

    @staticmethod
    def _mask_id(id_str: str) -> str:
        """对 context_id / cached_content_name 做脱敏，只显示前 8 位 + ***。"""
        return id_str[:8] + "***" if len(id_str) > 8 else "***"

    def on_history_compacted(self) -> None:
        """Memory 通知 history 被非追加式修改，下一次 call 按冷启动处理。

        触发场景：Memory.compact_if_needed() 执行了 STM→LTM 摘要。
        """
        self.next_call_is_cold = True
        if self.context_id is not None:
            logger.info("history compacted; invalidating context_id=%s", self._mask_id(self.context_id))
            self.context_id = None
        if self.cached_content_name is not None:
            logger.info("history compacted; invalidating cached_content=%s", self._mask_id(self.cached_content_name))
            self.cached_content_name = None

    def on_static_context_changed(self) -> None:
        """静态前缀（tools / messages[0]）发生非追加式修改，下一次 call 冷启动。

        触发场景：ToolRegistry 热更新、XML 排序规则变更、agent 配置热更新。
        """
        self.next_call_is_cold = True
        if self.context_id is not None:
            logger.info("static context changed; invalidating context_id=%s", self._mask_id(self.context_id))
            self.context_id = None
        if self.cached_content_name is not None:
            logger.info("static context changed; invalidating cached_content=%s", self._mask_id(self.cached_content_name))
            self.cached_content_name = None

    def consume_cold_start(self) -> bool:
        """消耗冷启动标记。返回 True 表示本次是冷启动，同时重置标记。"""
        if self.next_call_is_cold:
            self.next_call_is_cold = False
            logger.info(
                "history compacted; this call writes a new prefix at 125%% pricing. "
                "Subsequent calls will hit the new cached prefix normally."
            )
            return True
        return False
