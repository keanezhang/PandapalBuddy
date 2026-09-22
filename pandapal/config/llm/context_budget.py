"""应用侧上下文预算策略与约束（唯一真相源）。

承载"由我们决定"的预算参数：

  · 窗口使用比例（用模型的多少窗口）
  · 固定槽位熔断线（system prompt / tool schema 的双尺子）
  · I1 / I6 校验参数

不含任何"模型事实"（见 ``model_context_windows.toml``），
不含任何"压缩机制"（见 ``pandaren`` 的 ``CompactionProfile``）。

设计依据见 ``pandaren/memory/COMPACT_BUDGET_LAYERING_SPEC.md``。
"""

from __future__ import annotations

import logging
import math

from pandaren.behavior.context_window_budget import SlotBudget

logger = logging.getLogger(__name__)

# ── 窗口策略：统一用模型窗口的 80%（无档位选择）────────────────────────────
CONTEXT_WINDOW_RATIO: float = 0.80


def compute_cw(model_max_context: int) -> int:
    """输入预算 CW = floor(M × CONTEXT_WINDOW_RATIO)。"""
    return max(1, math.floor(model_max_context * CONTEXT_WINDOW_RATIO))


# ── 固定槽位「熔断线」：双尺子（绝对上限 + CW 占比上限，取较小者）────────────
# ⚠️ 熔断线**不参与分配**，只负责越界告警 + 截断；记账用的是实测值（见 SPEC §2.4）。
SYSTEM_PROMPT_CAP = SlotBudget(absolute_cap=24_000, cw_share=0.26)
TOOL_SCHEMA_CAP = SlotBudget(absolute_cap=8_000, cw_share=0.10)
RECALL_TOKENS = 0

# ── 估算系统性偏差上界（∝ CW）──────────────────────────────────────────────
# 定义见 SPEC §2.3.2；初值保守，须用 provider 回传的真实 input_tokens 对比估算值取 P95 校准。
# 回落 CharBasedTokenEstimator（chars/4）时中文场景低估可达 4x → 触发 I1 WARNING。
ESTIMATOR_ERROR_RATIO = 0.15

# ── I6 防御下限 + 静态开销预留 ──────────────────────────────────────────────
# conv 低于 MIN_CONVERSATION_TOKENS → WARNING（模型窗口太小，固定开销挤死对话区），这种情况很少出现，只有在38K以下的小模型才会出现对对话窗口的挤兑，出发后可能无限压缩的死循环，所以问题不大。
MIN_CONVERSATION_TOKENS = 8_000
# 技能 + 子 Agent 摘要预留（实测 387，留 5x 余量）；由 SDK 在 loop.py 内组装，app 无法精确测。
STATIC_CONTEXT_RESERVE = 2_000

# ── 输出预留兜底（第 ③ 层，见 SPEC §2.3.3）──────────────────────────────────
#   ① llm_settings(max_tokens)   ← 精确、推荐【用户设定的值】
#   ② toml 的 max_output         ← 大模型的事实，不需要设置，超过时大模型会自动返回错误，不需要配置
#   ③ 本兜底 32000                ← 仅"未核实模型"，同时 WARNING
DEFAULT_MAX_OUTPUT_TOKENS: int = 32_000


def resolve_output_tokens_reserve(
    *,
    llm_max_tokens: int | None,
    model_max_output_tokens: int | None,
) -> tuple[int, int]:
    """按精确度三层取输出预留（SPEC §2.3.3）。

    ⚠️ **来源① 会被夹到来源②**：toml 的 `max_output` 是**模型事实**（来自厂商），
    它是用户 `llm_settings(max_tokens=)` 的**上界**，不是可以越过的"建议值"。越过会：
      · API 直接拒（400）——超出模型单次生成能力；
      · 或预留被高估 → 把对话区压得过狠（`CW = 0.80 × M` 已固定，只能压对话区）。
    夹取时打 WARNING，并提示核对 toml 那条 `max_output` 是否已过期（值可能写小了）。

    Returns:
        ``(output_tokens_reserve, 命中的优先级)``，优先级 ∈ {1, 2, 3}。
    """
    if llm_max_tokens is not None:
        reserve = int(llm_max_tokens)
        if model_max_output_tokens is not None and reserve > model_max_output_tokens:
            logger.warning(
                "llm_settings(max_tokens=%d) 高于该模型输出上限 %d"
                "（toml 的 max_output，厂商事实）→ 已夹到上限。"
                "请改小 max_tokens；若确认模型已升级，请核对 "
                "model_context_windows.toml 里这条 max_output 是否过期（SPEC §2.3.3）。",
                reserve, model_max_output_tokens,
            )
            return int(model_max_output_tokens), 1
        return reserve, 1
    if model_max_output_tokens is not None:
        return int(model_max_output_tokens), 2
    logger.warning(
        "输出预留回落到兜底 %d：未显式设置 llm_settings(max_tokens)，"
        "toml 也没有该模型的 max_output。固定 0.80 下等价于硬约束"
        "「输出预留 ≤ 8%% × M」，若 M 较小请显式设 max_tokens（见 SPEC §2.3.1 / §2.3.3）。",
        DEFAULT_MAX_OUTPUT_TOKENS,
    )
    return DEFAULT_MAX_OUTPUT_TOKENS, 3


def check_i6(conversation_tokens: int) -> bool:
    """校验 I6：``conv ≥ MIN_CONVERSATION_TOKENS``。

    违反 → WARNING（模型窗口太小，固定开销挤死对话区；**无档位可升**）。
    固定 0.80 下 ``conv = 0.80×M − sys 实占 − tool 熔断线``，故只有极小模型会触发。
    """
    if conversation_tokens >= MIN_CONVERSATION_TOKENS:
        return True
    logger.warning(
        "I6 校验失败：conversation=%d < MIN_CONVERSATION_TOKENS=%d。"
        "模型窗口过小，固定开销（system 实占 + tool 熔断线）挤死对话区；"
        "固定 0.80 下无档位可升 —— 请换更大窗口的模型，或精简 system prompt。",
        conversation_tokens, MIN_CONVERSATION_TOKENS,
    )
    return False


def output_reserve_hard_limit(model_max_context: int) -> int:
    """I1 在固定 0.80 下的等价硬约束：输出预留上限 = (1 − 0.80×(1+ε)) × M = 8% × M。"""
    return math.floor(
        model_max_context * (1 - CONTEXT_WINDOW_RATIO * (1 + ESTIMATOR_ERROR_RATIO))
    )


def check_i1(model_max_context: int, output_tokens_reserve: int) -> bool:
    """校验 I1：``CW × (1 + ε) + 输出预留 ≤ M``。

    固定比例 0.80 下等价于「输出预留 ≤ 8% × M」（SPEC §2.3.1）。
    返回 False 时 WARNING（无档位可降，唯一出路是显式设 ``max_tokens``）。
    """
    cw = compute_cw(model_max_context)
    total = cw * (1 + ESTIMATOR_ERROR_RATIO) + output_tokens_reserve
    if total <= model_max_context:
        return True

    limit = output_reserve_hard_limit(model_max_context)
    logger.warning(
        "I1 校验失败：CW(%d) × (1+ε=%.2f) + 输出预留(%d) = %d > M(%d)。"
        "固定 0.80 下等价于「输出预留 ≤ %d」；无档位可降，"
        "请显式设置 llm_settings(max_tokens ≤ %d)。",
        cw, ESTIMATOR_ERROR_RATIO, output_tokens_reserve, int(total),
        model_max_context, limit, limit,
    )
    return False
