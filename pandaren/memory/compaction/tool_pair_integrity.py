"""pandaren/memory/compaction/tool_pair_integrity.py — 工具对完整性守卫

OpenAI / Qwen / 豆包 / DeepSeek 等所有 OpenAI-兼容 API 都强制要求：
  - 每条 ``role=tool`` 消息的 ``tool_call_id`` 必须能在前面某条
    ``role=assistant`` 的 ``tool_calls[*].id`` 中找到对应。
  - 每条 ``role=assistant`` 含 ``tool_calls`` 时，紧跟在它后面的若干
    ``role=tool`` 消息必须覆盖所有 ``tool_calls[*].id``（实践上 OpenAI
    要求严格覆盖；部分 provider 容忍部分缺失）。

这是 **API 硬约束**，不是优化项。任何压缩策略切割消息列表时一旦
违反，下一次 LLM 调用直接 400 / 行为异常。

`ensure_tool_pair_integrity()` 是 SDK **不可关闭**的兜底守卫：
  - WindowedKeepPolicy / RoundBasedPolicy 内部最后一步必调
  - Memory.compact_if_needed() 在调用应用层注入的 CompressionPolicy
    返回结果后，再叠加一次防御
"""

from __future__ import annotations

import logging
from typing import Iterable

from ..models import MessageDict

logger = logging.getLogger("pandaren.memory.tool_pair_integrity")


# ─────────────────────────────────────────────
# 内部工具
# ─────────────────────────────────────────────

def _iter_tool_call_ids(msg: MessageDict) -> Iterable[str]:
    """从 assistant 消息里枚举其 tool_calls 中的 id。"""
    if msg.get("role") != "assistant":
        return
    tool_calls = msg.get("tool_calls") or []
    for tc in tool_calls:
        if isinstance(tc, dict):
            tc_id = tc.get("id")
            if isinstance(tc_id, str) and tc_id:
                yield tc_id


def _is_tool_result(msg: MessageDict) -> bool:
    return msg.get("role") == "tool" and isinstance(msg.get("tool_call_id"), str)


def _is_assistant_with_calls(msg: MessageDict) -> bool:
    return msg.get("role") == "assistant" and bool(msg.get("tool_calls"))


# ─────────────────────────────────────────────
# 公共 API
# ─────────────────────────────────────────────

def ensure_tool_pair_integrity(
    kept: list[MessageDict],
    full: list[MessageDict] | None = None,
) -> list[MessageDict]:
    """修复 ``kept`` 中可能出现的孤儿 tool_call / tool_result。

    修复策略（按优先级）：

    1. **正向修复（孤儿 tool_result）**：
       ``kept`` 中存在 ``role=tool`` 但其 ``tool_call_id`` 在 ``kept`` 内
       找不到对应的 assistant tool_call，则：
         - 若 ``full`` 中有对应的 assistant，将该 assistant 按原序前置插入。
         - 若 ``full`` 中也找不到，删除该孤儿 tool_result。

    2. **反向修复（孤儿 tool_call）**：
       ``kept`` 中某 assistant 调了若干 tool_calls，但它们的 id 在 ``kept``
       后续消息中没有对应的 tool_result，则：
         - 若 ``full`` 中有对应的 tool_result，按原序追加到该 assistant 后面。
         - 若 ``full`` 中也找不到，**保留 assistant 但移除其 tool_calls 字段**
           （把它降级为纯文本 assistant；不删整条消息以保留对话语义）。

    3. **稳定性保证**：
       - 重复出现同一 ``tool_call_id`` 时（理论上不应发生，但防御性处理）
         以第一个 assistant 为准。
       - 永远不返回带孤儿的消息序列。
       - 不修改入参；返回新列表（浅拷贝消息字典；tool_calls 列表会重建）。

    Args:
        kept: 被压缩策略选中保留的消息列表（不含 system）。
        full: 原始完整消息列表，用于"捞回"被切走的配对消息。
              ``None`` 表示无源可捞，只能选择 *删孤儿* 路径。

    Returns:
        修复后的消息列表（按时间序）。
    """
    if not kept:
        return []

    full = full or []

    # ── 索引 full 里所有 tool_call → assistant 的对应关系 ──
    # full_tc_to_assistant_idx: tool_call_id → full 中对应 assistant 的 index
    full_tc_to_assistant_idx: dict[str, int] = {}
    for i, msg in enumerate(full):
        if _is_assistant_with_calls(msg):
            for tc_id in _iter_tool_call_ids(msg):
                # 重复 id 取首次（防御）
                full_tc_to_assistant_idx.setdefault(tc_id, i)

    # full_tc_to_result_idx: tool_call_id → full 中对应 tool_result 的 index
    full_tc_to_result_idx: dict[str, int] = {}
    for i, msg in enumerate(full):
        if _is_tool_result(msg):
            tc_id = msg.get("tool_call_id")
            if isinstance(tc_id, str):
                full_tc_to_result_idx.setdefault(tc_id, i)

    # ── 第一步：正向修复（孤儿 tool_result）──
    # 收集 kept 当前所有 tool_call ids（assistant 提供的）
    kept_provided_call_ids: set[str] = set()
    for msg in kept:
        kept_provided_call_ids.update(_iter_tool_call_ids(msg))

    # 找出 kept 中孤儿 tool_result（其 tool_call_id 不在 kept_provided 中）
    orphan_tool_call_ids: list[str] = []
    for msg in kept:
        if _is_tool_result(msg):
            tc_id = msg.get("tool_call_id")
            if isinstance(tc_id, str) and tc_id not in kept_provided_call_ids:
                orphan_tool_call_ids.append(tc_id)

    # 尝试从 full 捞回对应的 assistant（去重，按 full 中 index 排序）
    rescue_assistant_indices: list[int] = []
    seen_indices: set[int] = set()
    cannot_rescue: set[str] = set()
    for tc_id in orphan_tool_call_ids:
        idx = full_tc_to_assistant_idx.get(tc_id)
        if idx is None:
            cannot_rescue.add(tc_id)
            continue
        if idx not in seen_indices:
            seen_indices.add(idx)
            rescue_assistant_indices.append(idx)
    rescue_assistant_indices.sort()

    if cannot_rescue:
        logger.warning(
            "ensure_tool_pair_integrity: %d orphan tool_result(s) with no rescue source, "
            "deleting them. tool_call_ids=%s",
            len(cannot_rescue),
            sorted(cannot_rescue),
        )

    # 构建第一步修复后的列表：
    #   在每个孤儿 tool_result 前面插入对应的 assistant（而非全部前置到开头），
    #   确保 assistant 与其 tool_result 的配对顺序正确。
    step1: list[MessageDict] = []
    inserted_asst_indices: set[int] = set()
    for msg in kept:
        if _is_tool_result(msg):
            tc_id = msg.get("tool_call_id")
            if isinstance(tc_id, str) and tc_id in cannot_rescue:
                continue  # 删除无源孤儿
            if isinstance(tc_id, str) and tc_id not in kept_provided_call_ids:
                # 孤儿 tool_result：在其前面插入对应的 assistant
                rescue_idx = full_tc_to_assistant_idx.get(tc_id)
                if rescue_idx is not None and rescue_idx not in inserted_asst_indices:
                    inserted_asst_indices.add(rescue_idx)
                    step1.append(dict(full[rescue_idx]))
        step1.append(dict(msg))

    # ── 第二步：反向修复（孤儿 tool_call）──
    # 重新计算 step1 里 tool_result 提供的 ids
    step1_provided_result_ids: set[str] = set()
    for msg in step1:
        if _is_tool_result(msg):
            tc_id = msg.get("tool_call_id")
            if isinstance(tc_id, str):
                step1_provided_result_ids.add(tc_id)

    # 扫描 step1 里每个 assistant 的 tool_calls，找缺失 result 的
    # 同时把 step1 中已有的对应 tool_result 移到 assistant 后面，
    # 并跟踪已消费的索引，防止重复添加。
    step2: list[MessageDict] = []
    consumed_result_indices: set[int] = set()
    for idx, msg in enumerate(step1):
        if idx in consumed_result_indices:
            continue  # 已被前面的 assistant 消费
        if not _is_assistant_with_calls(msg):
            step2.append(msg)
            continue

        tool_calls = msg.get("tool_calls") or []
        tc_ids_in_calls: set[str] = {
            tc.get("id") for tc in tool_calls
            if isinstance(tc, dict) and isinstance(tc.get("id"), str)
        }

        # 收集 step1 中后续所有属于该 assistant 的 tool_result
        matching_results: list[tuple[int, MessageDict]] = []
        for j in range(idx + 1, len(step1)):
            if _is_tool_result(step1[j]) and step1[j].get("tool_call_id") in tc_ids_in_calls:
                matching_results.append((j, step1[j]))

        kept_calls: list = []
        rescue_results: list[MessageDict] = []
        dropped_call_ids: list[str] = []

        for tc in tool_calls:
            if not isinstance(tc, dict):
                kept_calls.append(tc)
                continue
            tc_id = tc.get("id")
            if not isinstance(tc_id, str):
                kept_calls.append(tc)
                continue
            if tc_id in step1_provided_result_ids:
                kept_calls.append(tc)
                continue
            # 缺失 result：尝试从 full 捞
            rescue_idx = full_tc_to_result_idx.get(tc_id)
            if rescue_idx is not None:
                rescue_results.append(dict(full[rescue_idx]))
                step1_provided_result_ids.add(tc_id)
                kept_calls.append(tc)
            else:
                # 无源可捞：移除该 tool_call（保留 assistant 文本）
                dropped_call_ids.append(tc_id)

        if dropped_call_ids:
            logger.warning(
                "ensure_tool_pair_integrity: %d orphan tool_call(s) with no rescue source, "
                "removed from assistant message. tool_call_ids=%s",
                len(dropped_call_ids),
                dropped_call_ids,
            )

        new_msg = dict(msg)
        if kept_calls:
            new_msg["tool_calls"] = kept_calls
        else:
            # 全部删掉：移除 tool_calls 字段并退化为纯文本 assistant
            new_msg.pop("tool_calls", None)
            if not new_msg.get("content"):
                # 防御：完全空的 assistant 消息会被部分 provider 拒绝；
                # 给一个最小占位
                new_msg["content"] = "[tool calls removed during compaction]"
        step2.append(new_msg)
        # 先添加 step1 中已有的 matching results（保持配对顺序），再添加 rescue 的
        for j, result_msg in matching_results:
            step2.append(dict(result_msg))
            consumed_result_indices.add(j)
        step2.extend(rescue_results)

    return step2
