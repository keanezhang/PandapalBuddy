"""pandapal/config/llm/context_window_resolver.py — 模型 id → 模型事实 解析器。

只解析**模型事实**：

    model_id
      → (model_max_context, model_max_output | None)
        优先级：环境变量 > [models] 精确 > [patterns] 通配 > [default] 兜底

窗口比例、槽位熔断线、输出预留等**应用策略**不在这里 ——
见 ``pandapal/config/llm/context_budget.py``（唯一真相源）。

⚠️ 映射表是静态数据，而厂商"稳定别名"背后的服务端版本会漂移
    （``deepseek-chat``：V3.x 是 128K、V4 是 1M）。因此提供最高优先级的环境变量
    覆盖 ``PANDAPAL_MODEL_MAX_CONTEXT``（改完即生效，不用改代码）。

CLI::

    python -m pandapal.config.llm.context_window_resolver --model my-model-id
"""

from __future__ import annotations

import fnmatch
import logging
import os
import tomllib
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TOML_PATH = Path(__file__).resolve().parent / "model_context_windows.toml"

#: 环境变量覆盖（最高优先级；只覆盖 max_context）。
_ENV_OVERRIDE = "PANDAPAL_MODEL_MAX_CONTEXT"

#: toml 不可用时的最终兜底（保守；同时打 WARNING）
_FALLBACK_TABLE: dict[str, Any] = {
    "default": {"max_context": 128_000, "max_output": 32_000},
    "models": {},
    "patterns": [],
}


# ── 表加载（进程内缓存）────────────────────────────────────────────────────
_TABLE: dict[str, Any] | None = None


def _load_table() -> dict[str, Any]:
    global _TABLE
    if _TABLE is not None:
        return _TABLE
    try:
        with _TOML_PATH.open("rb") as f:
            _TABLE = tomllib.load(f)
    except Exception:  # noqa: BLE001
        logger.exception(
            "context_window_resolver: %s 读取失败，回落内置默认值 %d",
            _TOML_PATH.name, _FALLBACK_TABLE["default"]["max_context"],
        )
        _TABLE = dict(_FALLBACK_TABLE)
    return _TABLE


def _opt_int(value: Any) -> int | None:
    """把 toml 里的可选输出上限转成 int | None。"""
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


# ── 对外接口 ───────────────────────────────────────────────────────────────
def resolve_model_max_context(model_id: str) -> tuple[int, int | None]:
    """解析模型的上下文上限与输出上限（模型事实）。

    优先级：环境变量 > [models] 精确 > [patterns] 通配 > [default] 兜底。

    Returns:
        ``(model_max_context, model_max_output | None)``
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
            return env_value, None
        except ValueError:
            logger.warning(
                "context_window_resolver: 环境变量 %s=%r 不是正整数，已忽略",
                _ENV_OVERRIDE, env_raw,
            )

    table = _load_table()
    mid = (model_id or "").strip()

    # ② [models] 精确匹配
    models: dict[str, Any] = table.get("models") or {}
    if mid and mid in models:
        entry = models[mid]
        if isinstance(entry, dict):
            return int(entry["max_context"]), _opt_int(entry.get("max_output"))
        return int(entry), None

    # ③ [patterns] 通配匹配（按声明顺序，首个命中生效）
    for entry in table.get("patterns") or []:
        glob = str(entry.get("glob", ""))
        if mid and glob and fnmatch.fnmatch(mid.lower(), glob.lower()):
            return int(entry["max_context"]), _opt_int(entry.get("max_output"))

    # ④ [default] 兜底
    default = table.get("default") or {}
    default_ctx = int(default.get("max_context", 128_000))
    default_out = _opt_int(default.get("max_output"))
    if mid:
        logger.warning(
            "context_window_resolver: 模型 %r 未在 %s 命中任何规则，回落默认上限 %d"
            "（输出预留走兜底）。大窗口模型请补一条，否则只会用 128K 预算。",
            mid, _TOML_PATH.name, default_ctx,
        )
    else:
        logger.warning(
            "context_window_resolver: model_id 为空，使用默认上限 %d", default_ctx,
        )
    return default_ctx, default_out


# ── CLI ────────────────────────────────────────────────────────────────────
def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="模型 id → 模型事实（上下文 / 输出上限）")
    parser.add_argument("--model", required=True, help="模型 id")
    args = parser.parse_args()

    max_context, max_output = resolve_model_max_context(args.model)
    print(f"model_id       = {args.model}")
    print(f"max_context    = {max_context:,}")
    print(f"max_output     = {max_output if max_output is not None else '(未核实 → 走兜底)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
