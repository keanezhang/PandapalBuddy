"""pandapal/config/budget/pricing.py — 单价解析 + 唯一计费函数（费用「真相源」）。

分层原则（价格全归应用层，SDK 只出用量事实、只提供通用停机机制）：
    SDK（pandaren）**完全不知道价格**。它每步 LLM 调用后把用量事实（`StepUsage`）交给
    应用层注入的通用 `StepGuard`，由守卫裁决是否停机。费用超限只是「停机的一种理由」，
    因此本模块提供 `cost_of_call`（唯一计费函数），供 guard / ledger / dashboard 三处共用。

    三处都只调 `cost_of_call` 这一个函数，零双口径。

单价三级回落（PRD·R5）—— 本模块的核心语义变更：
    ① 用户在凭据中填了单价 → 用用户的
    ② 用户没填，但系统默认表（``model_prices.toml``）命中 → 用默认价
    ③ 两者皆无             → **保存期即拒绝**，不允许该模型落盘

    因此「已保存的模型必然有确定单价」是**系统不变量**。装配期把每个可用模型的
    生效单价装进「价格账本」（:func:`install_price_book`），运行期 `cost_of_call`
    只查账本。查不到 ⇒ 不变量被违反 ⇒ P0 级降级信号，**而非常规路径**。

    ⚠️ 历史缺陷（本次重构要修掉的原始 bug）：旧实现有一张硬编码的 `APP_PRICE_TABLE`，
       查不到就静默返回 0 —— 费用恒 0 会让预算永远累加不上 → 预算永不触发停机 →
       静默超支。删掉模型白名单后「未定价」从罕见疏漏变成了默认状态，风险被放大。
       现在改为「保存期强制有价」，把问题拦在入口而非出口。

费用公式（唯一口径，正向三项相加 —— 见 docs/prd/费用计量/费用计量-需求设计.md §4）：
    记 In=输入token、Out=输出token、cached=命中缓存的输入token、
       Pin=输入全价、Pc=缓存命中价、Pout=输出价：

      净费用 net = cached/1k × Pc          # 命中部分按缓存价
                 + (In − cached)/1k × Pin  # 未命中部分按全价
                 + Out/1k × Pout           # 输出按输出价

    派生量（供展示，非主口径）：
      全价 full   = In/1k × Pin + Out/1k × Pout   # 「无缓存基线」，回答不省要花多少
      节省 saved  = cached/1k × (Pin − Pc)         # = full − net，回答缓存省了多少
    恒等式 net + saved == full（自洽）。

币种口径（PRD·R9）：
    - 单价一律 **CNY / 每 1k token**（用户按人民币采购，填写与展示均用 CNY）
    - 费用先按 CNY 算出，再按 ``EXCHANGE_RATE_USD`` 归一为 **USD** 返回
    - 归一为 USD 是为了对齐既有 `BudgetRow.spent_usd` 存储口径；预算额度以 CNY
      记于 `BudgetRow.limit_native`（`currency="CNY"`）
"""

from __future__ import annotations

import logging
from typing import Mapping, NamedTuple

from pandapal.config.llm.model_prices import (
    EXCHANGE_RATE_USD,
    ModelPrice,
    cny_to_usd,
    get_system_price,
    resolve_effective_price,
)
from pandapal.degradation import DegradationEvent, report_degradation

logger = logging.getLogger("pandapal.config.budget.pricing")

# 价格单位换算：表中价格以「每 1k token」计，避免 1000.0 散落在计算逻辑中
_TOKENS_PER_KILO: float = 1000.0
_COST_DECIMAL_PLACES: int = 8

__all__ = [
    "ModelPrice",
    "CallCost",
    "EXCHANGE_RATE_USD",
    "resolve_effective_price",
    "install_price_book",
    "price_book_size",
    "cost_of_call",
]


class CallCost(NamedTuple):
    """一次 llm_call 的费用（**USD**），源头一次算清，下游只做 sum。

    - `net_usd`：实际净费用（正向三项式，**主口径**：停机累加 / 会话末尾 / 看板消费）。
    - `full_usd`：全价基线（无缓存假设下要花多少），供看板对照。
    - `saved_usd`：命中缓存相对全价省下的钱（= full − net），供看板展示「省了多少」。
    - `input_usd` / `output_usd`：净费用里输入侧（命中价+全价两段）/ 输出侧的拆分，供看板分项。

    恒等式：`net_usd + saved_usd == full_usd`；`input_usd + output_usd == net_usd`。
    """

    net_usd: float
    full_usd: float
    saved_usd: float
    input_usd: float
    output_usd: float


# ── 价格账本（装配期安装，运行期只读）────────────────────────────────────────

#: model_id → 生效单价。由 `install_price_book` 在装配期一次性安装。
#: 运行期 `cost_of_call` 只读此账本，不再有任何硬编码价格表。
_price_book: dict[str, ModelPrice] = {}


def install_price_book(prices: Mapping[str, ModelPrice]) -> None:
    """装配期安装价格账本（全量替换，幂等）。

    由 `run_local._build_blueprint` 在装配 LLMRouter 时调用：每个注册进路由的
    model_id，都必须在此账本中有对应生效单价——「能路由的」与「能计费的」严格一致。

    Args:
        prices: model_id → 生效单价（已按三级回落解析完毕）
    """
    _price_book.clear()
    _price_book.update(prices)
    logger.info(
        "[pricing] 价格账本已安装：%d 个模型（汇率 1 USD = %.4f CNY）",
        len(_price_book),
        EXCHANGE_RATE_USD,
    )


def price_book_size() -> int:
    """当前账本中的模型数（供自检 / 测试用）。"""
    return len(_price_book)


# ── 唯一计费函数（运行期）────────────────────────────────────────────────────


def cost_of_call(
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
) -> CallCost:
    """把一次调用按 §4 正向三项式算成费用（**返回 USD**）。

    唯一计费函数——停机 / 会话末尾 / 看板都调它。
    单价取自装配期安装的价格账本（唯一真相源）。

    负 token 归 0；命中 token 超过输入 token 时按输入 token 封顶（口径自洽）。

    ⚠️ 账本查不到该 model_id ⇒ **系统不变量被违反**（保存期本应拦下无价模型）。
       此时按 0 计费并发出降级信号——这不是常规降级路径，而是缺陷探测器，
       正常运行时 `unpriced` 计数应恒为 0（见 PRD 6.3 P0 告警）。
    """
    price = _price_book.get(model_name)
    if price is None:
        # 账本未命中 → 回落系统默认表。
        #
        # 为什么运行期也需要这一级：账本只装**当前已配置**的模型，而看板要核算
        # **历史**消费。用户删掉某个模型后，其历史账单不该因此归零——那是账目失真，
        # 比未定价更隐蔽。系统默认表覆盖了常见模型，正好补上这个缺口。
        system = get_system_price(model_name)
        if system is not None:
            price = ModelPrice(
                input_price_per_1k=system.input_price_per_1k,
                output_price_per_1k=system.output_price_per_1k,
                cache_read_price_per_1k=system.cache_read_price_per_1k,
                source="system",
            )

    if price is None:
        # 三级皆空 ⇒ 不变量违反。可能的成因——
        #   a) 有绕过保存期校验的写入路径（如用户手工编辑 toml）
        #   b) 装配期未把该模型装进账本（run_local 与 model_registry 不一致）
        #   c) 系统默认表升级后移除了该模型，且未触发「待补价」流程
        # 排障：核对 model_registry 装配清单与账本键名是否逐字符一致
        #      （豆包 endpoint id 尤易错）。
        report_degradation(
            DegradationEvent.MODEL_UNPRICED,
            category="cost",
            source="budget.pricing.cost_of_call",
            expected="model_id in installed price book",
            fallback=model_name,
            dedup_key=f"unpriced:{model_name}",
        )
        return CallCost(0.0, 0.0, 0.0, 0.0, 0.0)

    in_tok = max(0, input_tokens)
    out_tok = max(0, output_tokens)
    cached = min(max(0, cached_tokens), in_tok)  # 命中不可能超过输入
    p_in = price.input_price_per_1k
    p_cache = price.cache_read_price_per_1k
    p_out = price.output_price_per_1k

    # 净费用 = 命中×缓存价 + 未命中×全价 + 输出×输出价（正向三项相加，一眼可读）
    # 全程以 CNY 计算，最后一步统一归一为 USD——避免中途换算引入多次舍入误差。
    hit_cny = cached / _TOKENS_PER_KILO * p_cache
    miss_cny = (in_tok - cached) / _TOKENS_PER_KILO * p_in
    output_cny = out_tok / _TOKENS_PER_KILO * p_out
    full_cny = in_tok / _TOKENS_PER_KILO * p_in + output_cny

    input_usd = round(cny_to_usd(hit_cny + miss_cny), _COST_DECIMAL_PLACES)
    output_usd = round(cny_to_usd(output_cny), _COST_DECIMAL_PLACES)
    net_usd = round(input_usd + output_usd, _COST_DECIMAL_PLACES)

    # 派生：全价基线 + 命中节省（saved = full − net，按差值定义保证恒等式成立）
    full_usd = round(cny_to_usd(full_cny), _COST_DECIMAL_PLACES)
    saved_usd = round(full_usd - net_usd, _COST_DECIMAL_PLACES)
    return CallCost(net_usd, full_usd, saved_usd, input_usd, output_usd)
