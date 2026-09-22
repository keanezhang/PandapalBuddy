"""pandapal/config/llm/context_window_resolver.py — 模型 → 上下文预算解析器。

把「模型 id」解析成一组可直接喂给 ``AgentBuilder.context_budget()`` 的配额参数，
使 token 预算**跟随模型配置**、并保持**比例 × 模型上限**的动态性。

链路::

    model_id
      → model_max_context        （环境变量 > [models] > [patterns] > [default]）
      → tier                     （按 model_max_context 落入的区间）
      → 各字段配额               （比例 × model_max_context，带兜底下限）
      → AgentBuilder.context_budget(**budget.to_builder_kwargs())

⚠️ 映射表是静态数据，而厂商"稳定别名"背后的服务端版本会漂移
    （`deepseek-chat`：V3.x 是 128K、V4 是 1M）。因此提供最高优先级的环境变量
    覆盖 `PANDAPAL_MODEL_MAX_CONTEXT`（改完即生效，不用改代码）。

为什么固定槽位也要走这张表（而不是写死在应用层）？
    实测（COMPACT_BUDGET_AUDIT.md）显示 system_prompt / tool_schema 的真实占用
    并不随窗口增长（coding 17,590 / office 2,236；13 个工具 schema 3,374）。
    所以它们的"比例"必须**随档位递减**——窗口越大比例越小——否则 1M 模型下
    0.15 的比例会给出 150,000 的配额（实测只需要 2 万）。
    比例与兜底下限都放在 toml 里，改配置即可，不用改代码。

CLI::

    .venv/bin/python -m pandapal.config.llm.context_window_resolver --table
    .venv/bin/python -m pandapal.config.llm.context_window_resolver --model my-model-id
"""

from __future__ import annotations

import fnmatch
import logging
import math
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pandaren.memory.constants import DEFAULT_RESERVED_OUTPUT_TOKENS

logger = logging.getLogger(__name__)

_TOML_PATH = Path(__file__).resolve().parent / "model_context_windows.toml"

#: 环境变量覆盖（最高优先级）。
#: 为什么需要？→ 本表是静态数据，而厂商的"稳定别名"会漂移
#: （`deepseek-chat` 背后 V3.x 是 128K、V4 是 1M）。表必然过期，
#: 所以要留一个"改完即生效、不用改代码"的应急口。
_ENV_OVERRIDE = "PANDAPAL_MODEL_MAX_CONTEXT"

#: toml 不可用时的最终兜底（保守；同时打 WARNING）
_FALLBACK_DEFAULT_MAX_CONTEXT: int = 128_000
_FALLBACK_TIER: dict[str, Any] = {
    "name": "fallback",
    "max_context": 10**9,
    "context_window_ratio": 0.80,
    "system_prompt_ratio": 0.172,
    "system_prompt_floor": 22_000,
    "tool_schema_ratio": 0.078,
    "tool_schema_floor": 8_000,
    "recall_ratio": 0.0,
}

#: I1 安全校验：CW + 输出预留 不得超过模型上限的这个比例（留估算误差余量）。
#: 1M 窗口下估算器 ±20% 就是 ±160K 的绝对误差，零余量必 400。
_SAFETY_RATIO: float = 0.9


# ── 表加载（进程内缓存）────────────────────────────────────────────────────
_TABLE: dict[str, Any] | None = None


def _load_table() -> dict[str, Any]:
    global _TABLE
    if _TABLE is not None:
        return _TABLE
    try:
        with _TOML_PATH.open("rb") as f:
            table = tomllib.load(f)
        table.setdefault("tiers", [])
        table["tiers"] = sorted(table["tiers"], key=lambda t: int(t.get("max_context", 0)))
        if not table["tiers"]:
            raise ValueError("model_context_windows.toml 未定义任何 [[tiers]]")
        _TABLE = table
    except Exception:  # noqa: BLE001
        logger.exception(
            "context_window_resolver: %s 读取失败，回落内置默认档位", _TOML_PATH.name,
        )
        _TABLE = {
            "default": {"value": _FALLBACK_DEFAULT_MAX_CONTEXT},
            "models": {},
            "patterns": {},
            "tiers": [dict(_FALLBACK_TIER)],
        }
    return _TABLE


# ── 解析结果 ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ResolvedBudget:
    """一次解析的完整结果（不可变）。"""

    model_id: str
    matched_rule: str
    tier: str
    model_max_context: int
    context_window: int
    system_prompt_tokens: int
    tool_schema_tokens: int
    recall_tokens: int
    conversation_tokens: int
    fell_back: bool = False

    def to_builder_kwargs(self) -> dict[str, Any]:
        """转成 ``AgentBuilder.context_budget(**kwargs)`` 的参数。

        固定槽位走绝对值（由 resolver 按比例 × 模型上限算好），
        conversation 由 ContextWindowBudget 自动吸收剩余。
        """
        return {
            "context_window": self.context_window,
            "system_prompt_tokens_abs": self.system_prompt_tokens,
            "tool_schema_tokens_abs": self.tool_schema_tokens,
            "recall_ratio": 0.0,
        }

    def summary(self) -> str:
        return (
            f"model={self.model_id or '(空)'} [{self.matched_rule}] "
            f"tier={self.tier} M={self.model_max_context:,} "
            f"→ CW={self.context_window:,} "
            f"(system={self.system_prompt_tokens:,} "
            f"tool={self.tool_schema_tokens:,} "
            f"recall={self.recall_tokens:,} "
            f"conversation={self.conversation_tokens:,})"
            + ("  ⚠️ 回落默认档位" if self.fell_back else "")
        )


# ── 对外接口 ───────────────────────────────────────────────────────────────
def resolve_model_max_context(model_id: str) -> tuple[int, str, bool]:
    """解析模型的上下文上限。

    优先级：环境变量 > [models] 精确 > [patterns] 通配 > [default] 兜底。

    Returns:
        (model_max_context, 命中的规则名, 是否回落默认值)
    """
    # ① 环境变量覆盖（最高优先级；写错值会直接 400，故显式留痕）
    env_raw = (os.environ.get(_ENV_OVERRIDE) or "").strip()
    if env_raw:
        try:
            env_value = int(env_raw)
            if env_value <= 0:
                raise ValueError(env_raw)
            logger.warning(
                "context_window_resolver: 使用环境变量 %s=%d 覆盖模型上限"
                "（忽略 model_id=%r 的查表结果）",
                _ENV_OVERRIDE, env_value, model_id,
            )
            return env_value, f"env:{_ENV_OVERRIDE}", False
        except ValueError:
            logger.warning(
                "context_window_resolver: 环境变量 %s=%r 不是正整数，已忽略",
                _ENV_OVERRIDE, env_raw,
            )

    table = _load_table()
    mid = (model_id or "").strip()

    models: dict[str, Any] = table.get("models") or {}
    if mid and mid in models:
        return int(models[mid]), f"exact:{mid}", False

    for pattern, value in (table.get("patterns") or {}).items():
        if mid and fnmatch.fnmatch(mid.lower(), str(pattern).lower()):
            return int(value), f"pattern:{pattern}", False

    default = int((table.get("default") or {}).get("value", _FALLBACK_DEFAULT_MAX_CONTEXT))
    if mid:
        logger.warning(
            "context_window_resolver: 模型 %r 未在 %s 命中任何规则，回落默认上限 %d。"
            "大窗口模型请补一条，否则只会用 128K 预算。",
            mid, _TOML_PATH.name, default,
        )
    else:
        logger.warning(
            "context_window_resolver: model_id 为空，使用默认上限 %d", default,
        )
    return default, "default", True


def _resolve_tier(model_max_context: int) -> dict[str, Any]:
    tiers: list[dict[str, Any]] = _load_table().get("tiers") or []
    for tier in tiers:
        if model_max_context <= int(tier.get("max_context", 0)):
            return tier
    return tiers[-1] if tiers else dict(_FALLBACK_TIER)


def resolve_budget(
    model_id: str,
    reserved_output_tokens: int = DEFAULT_RESERVED_OUTPUT_TOKENS,
) -> ResolvedBudget:
    """模型 id → 完整上下文预算（比例 × 模型上限）。

    ``reserved_output_tokens`` 用于 I1 校验（CW + 输出预留 ≤ 0.9 × 模型上限）。
    应用层若在 ``llm_settings(max_tokens=...)`` 配了非默认值，应传实际值。
    """
    m, rule, fell_back = resolve_model_max_context(model_id)
    tier = _resolve_tier(m)
    tier_name = str(tier.get("name", "unknown"))

    cw = max(1, math.floor(m * float(tier.get("context_window_ratio", 0.80))))
    sys_t = max(
        math.floor(m * float(tier.get("system_prompt_ratio", 0.15))),
        int(tier.get("system_prompt_floor", 0)),
    )
    tool_t = max(
        math.floor(m * float(tier.get("tool_schema_ratio", 0.10))),
        int(tier.get("tool_schema_floor", 0)),
    )
    recall_t = math.floor(m * float(tier.get("recall_ratio", 0.0)))

    # 防御：固定槽位必须给 conversation 留下正数（E4/E5：不拒绝启动，但显式留痕）
    min_conv = max(1_000, int(cw * 0.10))
    if sys_t + tool_t + recall_t + min_conv > cw:
        logger.warning(
            "context_window_resolver: 固定槽位过大 (system=%d + tool=%d + recall=%d "
            "+ 最小 conversation=%d > CW=%d, model=%r tier=%s)，"
            "已按 CW 比例回落 (system=15%%, tool=10%%)。",
            sys_t, tool_t, recall_t, min_conv, cw, model_id, tier_name,
        )
        sys_t = max(1, math.floor(cw * 0.15))
        tool_t = max(1, math.floor(cw * 0.10))
        recall_t = 0

    conv_t = cw - sys_t - tool_t - recall_t

    budget = ResolvedBudget(
        model_id=model_id or "",
        matched_rule=rule,
        tier=tier_name,
        model_max_context=m,
        context_window=cw,
        system_prompt_tokens=sys_t,
        tool_schema_tokens=tool_t,
        recall_tokens=recall_t,
        conversation_tokens=conv_t,
        fell_back=fell_back,
    )
    _safety_limit = m * _SAFETY_RATIO
    if budget.context_window + reserved_output_tokens > _safety_limit:
        logger.warning(
            "context_window_resolver: I1 校验失败——CW %d + 输出预留 %d = %d > "
            "model_max_context(%d) × %.2f = %d，零余量（估算误差即 400）。"
            "建议下调档位 context_window_ratio 或 max_tokens。",
            budget.context_window, reserved_output_tokens,
            budget.context_window + reserved_output_tokens,
            m, _SAFETY_RATIO, int(_safety_limit),
        )

    logger.info("context_window_resolver: %s", budget.summary())
    return budget


def describe_table() -> str:
    """渲染默认档位表（供文档 / CLI 展示）。"""
    lines = [
        f"{'档位':<10}{'M 上限':>12}{'CW/M':>7}{'system 比例':>13}{'system':>10}"
        f"{'tool 比例':>11}{'tool':>9}{'recall':>8}{'conversation':>14}",
        "-" * 96,
    ]
    for tier in _load_table().get("tiers") or []:
        m = int(tier.get("max_context", 0))
        # 展示用代表值（typical_context）；缺失则回落到档位上界
        probe_m = int(tier.get("typical_context") or min(m, 1_000_000))
        cw = math.floor(probe_m * float(tier.get("context_window_ratio", 0.80)))
        sys_t = max(math.floor(probe_m * float(tier.get("system_prompt_ratio", 0))),
                    int(tier.get("system_prompt_floor", 0)))
        tool_t = max(math.floor(probe_m * float(tier.get("tool_schema_ratio", 0))),
                     int(tier.get("tool_schema_floor", 0)))
        recall_t = math.floor(probe_m * float(tier.get("recall_ratio", 0.0)))
        lines.append(
            f"{str(tier.get('name', '?')):<10}{('≤' + f'{m:,}'):>12}"
            f"{float(tier.get('context_window_ratio', 0)):>7.2f}"
            f"{float(tier.get('system_prompt_ratio', 0)):>13.3f}"
            f"{sys_t:>10,}"
            f"{float(tier.get('tool_schema_ratio', 0)):>11.3f}"
            f"{tool_t:>9,}{recall_t:>8,}{cw - sys_t - tool_t - recall_t:>14,}"
        )
    lines.append("")
    lines.append(
        "注：按各档位上界所示 M 计算（超大档取 min(上界, 1,000,000)）。"
        "比例随档位递减——因为固定槽位的真实占用不随窗口增长。"
    )
    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────────────
def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="模型 → 上下文预算解析（比例 × 模型上限）",
    )
    parser.add_argument("--model", default="", help="模型 id（留空则打印档位表）")
    parser.add_argument("--table", action="store_true", help="打印默认档位表")
    args = parser.parse_args()

    if args.table or not args.model:
        print(describe_table())
        if not args.model:
            return 0

    budget = resolve_budget(args.model)
    print()
    print(budget.summary())
    print()
    print("  → AgentBuilder.context_budget(**...) 参数：")
    for k, v in budget.to_builder_kwargs().items():
        print(f"      {k} = {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
