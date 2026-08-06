"""pandaren/llm/_internal/cache_primitives.py — 缓存逃生舱底层 helper

极少数场景业务需要完全手动控制缓存挂载（cache="manual" 模式），此模块提供：
  - attach_cache_control：在指定 message 的 content 末尾挂 cache_control 断点
  - _attach_cache_control_to_tool：在 tools 参数数组中指定位置打断点（SDK 内部用）
  - _attach_cache_control_to_system_block：在 system block 数组中指定位置打断点（SDK 内部用）
  - _attach_cache_control_to_message：在 messages 数组中指定 message 打断点（SDK 内部用）
  - ContextCreateResult：/context/create 响应的类型化封装

这些符号**不从 pandaren.llm 顶层导出**——降格为"明确的逃生舱"。

SDK 自动模式（cache=True）下的缓存断点挂载逻辑在 client.py 的 _apply_cache_positions 中，
内部调用本模块的 _attach_* 系列函数。
"""

from __future__ import annotations

import copy
import warnings
from dataclasses import dataclass
from typing import Any

from ..types import UsageInfo


# ═══════════════════════════════════════════════════════════════
# ContextCreateResult
# ═══════════════════════════════════════════════════════════════

@dataclass
class ContextCreateResult:
    """火山方舟 /context/create 响应的类型化封装。

    仅 cache="manual" 模式下业务自行管理 context_id 时使用。
    """

    context_id: str
    """创建得到的 context_id，后续请求透传此 ID。"""

    usage: UsageInfo
    """usage.prompt_tokens = 本次写入缓存的 token 数。"""

    raw: dict[str, Any]
    """原始响应 dict，供调试。"""


# ═══════════════════════════════════════════════════════════════
# attach_cache_control（逃生舱公开接口）
# ═══════════════════════════════════════════════════════════════

def attach_cache_control(
    messages: list[dict[str, Any]],
    *,
    positions: list[int] | None = None,
    cache_type: str = "ephemeral",
    max_breakpoints: int | None = None,
) -> list[dict[str, Any]]:
    """在指定 message 的 content 末尾挂 cache_control 断点。

    Args:
        messages:  消息列表（OpenAI 格式，支持 content: str 或 list[block]）
        positions: 要挂断点的消息下标列表，支持负数索引（-1 = 最后一条）。
                   默认 [0]
        cache_type: cache_control.type 取值，目前所有实现均为 "ephemeral"
        max_breakpoints: 断点数上限；超过时发出 warning（不阻断）。None = 不检查。

    Returns:
        深拷贝后的 messages 列表，原入参不被修改。

    行为细节：
      - 若目标 message.content 是 str，升级成 [{"type":"text","text":...}]
      - 若 content 已经是 list，在最后一个 text block 上挂
      - 最后一个 block 已有 cache_control 时覆盖更新（不叠加）
      - 超过 max_breakpoints 时 warn（不阻断）
    """
    if positions is None:
        positions = [0]

    if max_breakpoints is not None and len(positions) > max_breakpoints:
        warnings.warn(
            f"挂了 {len(positions)} 个 cache_control 断点，超过 max_breakpoints="
            f"{max_breakpoints}。服务端通常只保留最后 N 个。",
            UserWarning,
            stacklevel=2,
        )

    result = copy.deepcopy(messages)
    for pos in positions:
        if pos < -len(result) or pos >= len(result):
            continue
        _inject_cache_control_to_message_content(result[pos], cache_type)
    return result


# ═══════════════════════════════════════════════════════════════
# SDK 内部用的三个 _attach 助手
# ═══════════════════════════════════════════════════════════════

def _attach_cache_control_to_tool(
    tools: list[dict[str, Any]],
    target_index: int,
    cache_type: str = "ephemeral",
) -> None:
    """在 tools 参数数组中 target_index 位置的工具上打 cache_control。

    直接原地修改 tools（调用方应先深拷贝）。

    注意：只有 **Anthropic messages（Claude 原生）** 支持在 tool schema 级别挂
    cache_control；**阿里百炼 DashScope（Qwen）会静默忽略** tools 上的 cache_control
    （官方：工具定义不支持独立缓存，只能挂在 messages content 上）。SDK 自动模式据
    ``caps.supports_tool_cache_control`` 决定是否调用本函数，业务手动调用需自行确认端点支持。
    """
    if target_index < 0 or target_index >= len(tools):
        return
    tool = tools[target_index]
    tool["cache_control"] = {"type": cache_type}


def _attach_cache_control_to_system_block(
    system_blocks: list[dict[str, Any]],
    target_index: int,
    cache_type: str = "ephemeral",
) -> None:
    """在 system message 的 content block 数组中 target_index 位置打 cache_control。

    直接原地修改 system_blocks（调用方应先深拷贝）。
    适用于 system message content 已经是 list[block] 形式的场景。
    """
    if target_index < 0 or target_index >= len(system_blocks):
        return
    block = system_blocks[target_index]
    block["cache_control"] = {"type": cache_type}


def _attach_cache_control_to_message(
    messages: list[dict[str, Any]],
    target_index: int,
    cache_type: str = "ephemeral",
) -> None:
    """在 messages 数组中 target_index 位置的 message 的 content 末尾打 cache_control。

    直接原地修改 messages（调用方应先深拷贝）。
    """
    if target_index < 0 or target_index >= len(messages):
        return
    _inject_cache_control_to_message_content(messages[target_index], cache_type)


# ═══════════════════════════════════════════════════════════════
# 内部工具
# ═══════════════════════════════════════════════════════════════

def _inject_cache_control_to_message_content(
    message: dict[str, Any],
    cache_type: str,
) -> None:
    """在单条 message 的 content 末尾注入 cache_control。

    处理两种 content 形态：
      - str → 升级成 [{"type":"text","text":..., "cache_control":{"type":...}}]
      - list[block] → 在最后一个 block 上挂 cache_control（已有则覆盖）
    """
    content = message.get("content")
    if content is None:
        return

    if isinstance(content, str):
        # 升级为 block 形式
        message["content"] = [
            {
                "type": "text",
                "text": content,
                "cache_control": {"type": cache_type},
            }
        ]
    elif isinstance(content, list) and content:
        # 在最后一个 block 上挂
        last_block = content[-1]
        if isinstance(last_block, dict):
            last_block["cache_control"] = {"type": cache_type}
