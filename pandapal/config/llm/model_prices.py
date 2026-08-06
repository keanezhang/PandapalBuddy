"""系统预置模型默认单价 + 汇率（单一真相源，只读）。

数据来源：同目录下的 ``model_prices.toml``，运行时读取（禁止编译期嵌入）。

╔══════════════════════════════════════════════════════════════════════════╗
║  ⚠️ 本表**不是**可用性白名单。                                            ║
║                                                                          ║
║  表中没有的 model_id 照样可以配置、装配、路由、使用——只是保存时必须由      ║
║  用户自己填写单价。任何「不在本表内就不装配 / 不展示 / 不允许保存」的       ║
║  逻辑，都是已删除的 _DECLARED_MODELS 白名单换马甲复活，明令禁止。          ║
║                                                                          ║
║  本表的两个用途（均为**引导性质**，不构成限制）：                          ║
║    ① 默认单价来源（单价三级回落的第 ② 级）                                ║
║    ② model_id combobox 的推荐清单（按 provider 归类）                     ║
╚══════════════════════════════════════════════════════════════════════════╝

单价三级回落（PRD·R5，实现见 :mod:`pandapal.config.budget.pricing`）：
    ① 用户在凭据中填了单价 → 用用户的
    ② 用户没填，但本表命中 → 用本表默认价
    ③ 两者皆无             → **拒绝保存**，要求用户填写

因此「已保存的模型必然有确定单价」是系统不变量。

口径：单价一律 **CNY / 每 1k token**；USD 为派生值，按 :data:`EXCHANGE_RATE_USD`
归一，不落盘（见 PRD·R9）。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "SystemModelPrice",
    "ModelPrice",
    "EXCHANGE_RATE_USD",
    "SYSTEM_PRICES",
    "get_system_price",
    "recommended_models",
    "resolve_effective_price",
    "cny_to_usd",
]


@dataclass(frozen=True)
class SystemModelPrice:
    """单个模型的系统默认单价（CNY / 每 1k token），运行时只读。"""

    model_id: str
    provider: str
    input_price_per_1k: float
    output_price_per_1k: float
    cache_read_price_per_1k: float


@dataclass(frozen=True)
class ModelPrice:
    """一个模型的**生效**单价（CNY / 每 1k token），不可变。

    「生效」= 已按三级回落解析完毕，来源可能是用户填写值或系统默认值。
    :attr:`source` 保留来源标记，供前端区分「系统默认价」与「我填的价」——
    用户需要知道账目反映的是估算价还是自己的真实采购价。
    """

    input_price_per_1k: float
    output_price_per_1k: float
    cache_read_price_per_1k: float
    source: str  # "user" | "system"


# ── 加载与校验 ───────────────────────────────────────────────────────────────

_PRICES_PATH = Path(__file__).resolve().parent / "model_prices.toml"

#: 单价字段名（三项一组，顺序即语义：输入 / 输出 / 缓存命中）
_PRICE_FIELDS = ("input_price_per_1k", "output_price_per_1k")


def _load_exchange_rate(raw: dict) -> float:
    """加载汇率。

    汇率属**金额类字段**：缺失 / 非数字 / ≤0 一律 fail-fast，
    绝不默认回落 7.0（CLAUDE.md §九：金额类字段缺失绝不给默认值）。
    """
    if "exchange_rate_usd" not in raw:
        raise ValueError("model_prices.toml: 缺少必填字段 exchange_rate_usd")
    rate = raw["exchange_rate_usd"]
    if not isinstance(rate, (int, float)) or isinstance(rate, bool):
        raise ValueError(
            f"model_prices.toml: exchange_rate_usd 必须是数字，实际为 {rate!r}"
        )
    if rate <= 0:
        raise ValueError(
            f"model_prices.toml: exchange_rate_usd 必须 > 0，实际为 {rate}"
        )
    return float(rate)


def _load_prices(raw: dict) -> tuple[SystemModelPrice, ...]:
    """加载并校验默认单价表。

    校验规则：
        - model_id 全表唯一（重复会让「用哪个价」变得不确定）
        - provider 非空（combobox 按 provider 归类需要）
        - 输入价 / 输出价必填且 ≥ 0
        - 缓存命中价可选，缺省 = 输入价（**保守估高**，绝不低估费用）

    注意：``prices`` 段允许为空——那只意味着「所有模型都要用户自己填价」，
    是合法状态，不是错误（对比 provider_catalog 的 providers 为空即致命）。
    """
    prices_raw = raw.get("prices", [])
    result: list[SystemModelPrice] = []
    seen_ids: set[str] = set()

    for i, p in enumerate(prices_raw):
        model_id = p.get("model_id", "")
        if not model_id:
            raise ValueError(f"model_prices.toml: prices[{i}] 缺少 model_id")
        if model_id in seen_ids:
            raise ValueError(f"model_prices.toml: model_id 重复: {model_id!r}")
        seen_ids.add(model_id)

        provider = p.get("provider", "")
        if not provider:
            raise ValueError(
                f"model_prices.toml: prices[{i}]({model_id}) 缺少 provider"
            )

        for field_name in _PRICE_FIELDS:
            if field_name not in p:
                raise ValueError(
                    f"model_prices.toml: prices[{i}]({model_id}) 缺少 {field_name}"
                )
            value = p[field_name]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(
                    f"model_prices.toml: prices[{i}]({model_id}) 的 {field_name} "
                    f"必须是数字，实际为 {value!r}"
                )
            if value < 0:
                raise ValueError(
                    f"model_prices.toml: prices[{i}]({model_id}) 的 {field_name} "
                    f"必须 ≥ 0，实际为 {value}"
                )

        input_price = float(p["input_price_per_1k"])
        # 缓存价缺省 → 取输入价（保守估高：宁可高估费用，绝不低估导致预算失守）
        cache_price = p.get("cache_read_price_per_1k")
        if cache_price is None:
            cache_price = input_price
        elif not isinstance(cache_price, (int, float)) or isinstance(cache_price, bool):
            raise ValueError(
                f"model_prices.toml: prices[{i}]({model_id}) 的 "
                f"cache_read_price_per_1k 必须是数字，实际为 {cache_price!r}"
            )
        elif cache_price < 0:
            raise ValueError(
                f"model_prices.toml: prices[{i}]({model_id}) 的 "
                f"cache_read_price_per_1k 必须 ≥ 0，实际为 {cache_price}"
            )

        result.append(
            SystemModelPrice(
                model_id=model_id,
                provider=provider,
                input_price_per_1k=input_price,
                output_price_per_1k=float(p["output_price_per_1k"]),
                cache_read_price_per_1k=float(cache_price),
            )
        )

    return tuple(result)


with _PRICES_PATH.open("rb") as _f:
    _RAW = tomllib.load(_f)

#: 汇率：1 USD = N CNY。静态值，随版本发布，**不接实时汇率源**——
#: 实时汇率会让「同一笔消费在不同时刻算出不同金额」，破坏账目可复现性。
EXCHANGE_RATE_USD: float = _load_exchange_rate(_RAW)

_PRICES: tuple[SystemModelPrice, ...] = _load_prices(_RAW)

#: model_id → SystemModelPrice。**查不到不是错误**，只表示该模型需用户自填单价。
SYSTEM_PRICES: dict[str, SystemModelPrice] = {p.model_id: p for p in _PRICES}


# ── Helper 函数 ──────────────────────────────────────────────────────────────


def get_system_price(model_id: str) -> SystemModelPrice | None:
    """查系统默认单价；未命中返回 None（表示「需用户自填」，而非「不可用」）。

    **精确匹配**，不做前缀 / 模糊匹配——模糊匹配会让 ``qwen3-max`` 悄悄套用
    ``qwen-max`` 的价，属于金额类字段的静默降级（§九）。
    """
    return SYSTEM_PRICES.get(model_id)


def recommended_models(provider: str) -> list[SystemModelPrice]:
    """某 provider 下的推荐模型清单，供前端 model_id combobox 展示。

    ⚠️ **仅作引导**：返回空列表只意味着「该 provider 无推荐模型」，
    前端应退化为纯文本输入框，**绝不能**据此限制用户可填的 model_id。
    """
    return [p for p in _PRICES if p.provider == provider]


def cny_to_usd(cny: float) -> float:
    """按系统汇率把 CNY 金额归一为 USD。"""
    return cny / EXCHANGE_RATE_USD


# ── 单价三级回落（保存期调用）────────────────────────────────────────────────
#
# 本函数放在这里而非 budget/pricing.py：它是**定价策略**，只依赖上面的系统价表，
# 不依赖预算/账本的任何东西。放在 budget 会让 llm 层（credentials_store 保存时
# 要校验单价）反向依赖 budget 层，形成 budget.pricing → llm.model_prices →
# llm.credentials_store → budget.pricing 的循环。


def resolve_effective_price(
    model_id: str,
    user_input_price: float | None = None,
    user_output_price: float | None = None,
    user_cache_price: float | None = None,
) -> ModelPrice | None:
    """按三级回落解析某模型的生效单价（PRD·R5）。

    **保存期**调用：返回 None 表示「无任何单价来源」，调用方必须**拒绝保存**并
    要求用户填写——绝不允许落盘一个没有单价的模型（§九：金额类字段缺失即失败）。

    Args:
        model_id: 用户填写的 model_id
        user_input_price: 用户填写的输入价（CNY/1k），未填传 None
        user_output_price: 用户填写的输出价（CNY/1k），未填传 None
        user_cache_price: 用户填写的缓存命中价（CNY/1k），未填传 None

    Returns:
        生效单价；三级皆空时返回 None（调用方须拒绝保存）

    Raises:
        ValueError: 用户只填了输入价与输出价中的一个（半套价无法计费），
            或填入负数
    """
    has_input = user_input_price is not None
    has_output = user_output_price is not None

    # 半套价拦截：只填一个等于「输入免费」或「输出免费」，几乎必然是误填。
    # 与其按 0 计费造成账目失真，不如让用户明确补齐。
    if has_input != has_output:
        raise ValueError(
            f"{model_id}: 输入价与输出价必须同时填写（当前只填了其中一个）"
        )

    # 「只填缓存价」拦截：缓存价单独存在无法计费，会直接落到 ② 系统默认表，
    # 用户填的数被**静默丢弃**——界面回显用户的数、实际按系统价计费，且绕过
    # 下方的负值校验（-1 也能写进 toml）。这正是 §九 禁止的金额类静默降级。
    if not has_input and user_cache_price is not None:
        raise ValueError(
            f"{model_id}: 只填了缓存命中价；自定义单价必须同时提供输入价与输出价"
        )

    # ── ① 用户填写值优先 ──
    if has_input and has_output:
        for label, value in (
            ("输入价", user_input_price),
            ("输出价", user_output_price),
            ("缓存命中价", user_cache_price),
        ):
            if value is not None and value < 0:
                raise ValueError(f"{model_id}: {label}必须 ≥ 0，实际为 {value}")
        # 缓存价缺省 → 取输入价（保守估高：宁可高估费用，绝不低估导致预算失守）
        cache = user_cache_price if user_cache_price is not None else user_input_price
        return ModelPrice(
            input_price_per_1k=float(user_input_price),  # type: ignore[arg-type]
            output_price_per_1k=float(user_output_price),  # type: ignore[arg-type]
            cache_read_price_per_1k=float(cache),  # type: ignore[arg-type]
            source="user",
        )

    # ── ② 系统默认表 ──
    system = get_system_price(model_id)
    if system is not None:
        return ModelPrice(
            input_price_per_1k=system.input_price_per_1k,
            output_price_per_1k=system.output_price_per_1k,
            cache_read_price_per_1k=system.cache_read_price_per_1k,
            source="system",
        )

    # ── ③ 无任何来源 → 调用方须拒绝保存 ──
    # ⚠️ 绝不在此处返回 0 价兜底。返回 None 是本函数最重要的契约。
    return None
