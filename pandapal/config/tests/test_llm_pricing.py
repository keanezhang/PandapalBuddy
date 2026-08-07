"""pandapal/config/tests/test_llm_pricing.py — 单价三级回落 + 计费函数 + 用量记账守卫。

覆盖：
  - 单价三级回落三分支（用户填写值 / 系统默认表 / 无来源）
  - 定价表**不是白名单**（AC-06 防退化）
  - cost_of_call 正向三项式与恒等式（net+saved==full、input+output==net）
  - CNY→USD 归一口径（AC-03 / AC-04 算例）
  - 账本未命中时**确实上报降级**（不变量探测器，而非静默计 0）
  - CostBudgetGuard：净费用停机 + summary(run_id) 全量用量汇总
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pandapal.config.budget.guard import CostBudgetGuard
from pandapal.config.budget.pricing import (
    EXCHANGE_RATE_USD,
    ModelPrice,
    cost_of_call,
    install_price_book,
    resolve_effective_price,
)
from pandaren.behavior.step_guard import StepUsage


def _price(model_id: str, *user_price: float | None) -> ModelPrice:
    """解析单价并断言有解（测试辅助：把可空类型收敛掉）。"""
    p = resolve_effective_price(model_id, *user_price)
    assert p is not None, f"{model_id} 应有确定单价"
    return p


@pytest.fixture(autouse=True)
def _price_book():
    """装配期价格账本。

    现实中由 `run_local._build_blueprint` 从用户凭据派生安装；测试里显式安装
    等价内容。qwen-plus / gpt-4o 用系统默认价（CNY），未安装的模型用于验证
    「账本未命中 → 降级上报」。
    """
    install_price_book(
        {
            "qwen-plus": _price("qwen-plus"),
            "gpt-4o": _price("gpt-4o"),
        }
    )
    yield
    install_price_book({})


# ── 单价三级回落 ──────────────────────────────────────────────────────────────


def test_fallback_level1_user_price_wins():
    """① 用户填了单价 → 用用户的，压过系统默认表。"""
    p = resolve_effective_price("qwen-max", 0.007, 0.028)
    assert p is not None
    assert p.source == "user"
    assert p.input_price_per_1k == 0.007
    assert p.output_price_per_1k == 0.028


def test_fallback_level2_system_default():
    """② 用户没填，系统默认表命中 → 用默认价。"""
    p = resolve_effective_price("qwen-max")
    assert p is not None
    assert p.source == "system"
    assert p.input_price_per_1k > 0


def test_fallback_level3_no_source_returns_none():
    """③ 两者皆无 → 返回 None，调用方须拒绝保存。

    ⚠️ 这是本模块最重要的契约：绝不返回 0 价兜底。返回 0 会让该模型的消费
    静默计 0 → 预算永远累加不上 → 预算永不触发停机 → 静默超支。
    """
    assert resolve_effective_price("model-that-does-not-exist") is None


def test_pricing_table_is_not_a_whitelist():
    """AC-06 防退化：表外模型只要用户填了价，就必须能拿到确定单价。

    定价表只决定「用户要不要自己填价」，**绝不决定「这个模型能不能用」**。
    任何「不在表内就不可用」的逻辑都是 _DECLARED_MODELS 白名单换马甲复活。
    """
    p = resolve_effective_price("my-finetune-v3", 0.01, 0.04)
    assert p is not None and p.source == "user"


def test_half_price_rejected():
    """只填输入价或只填输出价 → 拒绝（半套价必然是误填）。"""
    with pytest.raises(ValueError, match="同时填写"):
        resolve_effective_price("x", 0.001, None)
    with pytest.raises(ValueError, match="同时填写"):
        resolve_effective_price("x", None, 0.001)


def test_negative_price_rejected():
    with pytest.raises(ValueError, match="≥ 0"):
        resolve_effective_price("x", -1.0, 0.001)


def test_cache_price_defaults_to_input_price():
    """缓存价缺省取输入价——保守估高，绝不低估费用导致预算失守。"""
    p = resolve_effective_price("x", 0.01, 0.02)
    assert p is not None and p.cache_read_price_per_1k == 0.01


# ── 计费口径 ──────────────────────────────────────────────────────────────────


def test_cost_of_call_forward_formula():
    """正向三项式与恒等式。单价以 CNY 计算，结果归一为 USD。"""
    price = _price("qwen-plus")
    c = cost_of_call("qwen-plus", 10000, 2000, 4000)

    # 手算：先按 CNY 三项相加，再按汇率归一为 USD
    hand_cny = (
        4000 / 1000 * price.cache_read_price_per_1k
        + 6000 / 1000 * price.input_price_per_1k
        + 2000 / 1000 * price.output_price_per_1k
    )
    assert abs(c.net_usd - round(hand_cny / EXCHANGE_RATE_USD, 8)) < 1e-9
    assert abs((c.net_usd + c.saved_usd) - c.full_usd) < 1e-9
    assert abs((c.input_usd + c.output_usd) - c.net_usd) < 1e-9


def test_ac03_system_default_price_arithmetic():
    """AC-03：qwen-max 系统默认价，1k 输入 + 1k 输出 → 0.0017143 USD。"""
    install_price_book({"qwen-max": _price("qwen-max")})
    c = cost_of_call("qwen-max", 1000, 1000, 0)
    # 官方价：0.0024 + 0.0096 = 0.0120 CNY；0.0120 / 7.0 → round8 = 0.00171429 USD
    assert abs(c.net_usd - round(0.012 / 7.0, 8)) < 1e-9


def test_ac04_user_price_overrides_system():
    """AC-04：同一模型，用户填的价压过系统默认价。"""
    install_price_book({"qwen-max": _price("qwen-max", 0.007, 0.028)})
    c = cost_of_call("qwen-max", 1000, 1000, 0)
    # 0.007 + 0.028 = 0.035 CNY；0.035 / 7.0 = 0.005 USD
    assert abs(c.net_usd - 0.005) < 1e-9


def test_cost_of_call_bounds():
    """命中不可能超过输入；负 token 归 0。"""
    c = cost_of_call("qwen-plus", 1000, -5, 99999)
    assert c.output_usd == 0.0
    assert c.saved_usd >= 0.0


def test_historical_model_falls_back_to_system_table():
    """账本里没有、但系统默认表有 → 仍能算出费用，**不得**归零。

    ⚠️ 看板核算的是**历史**消费，而价格账本只装当前已配置的模型。用户删掉某个
    模型后，其历史账单不该因此归零——那是账目失真，比未定价更隐蔽（用户会以为
    自己那段时间没花钱）。因此运行期在账本之外保留「系统默认表」这一级回落。
    """
    install_price_book({})  # 清空账本，模拟「该模型已从用户配置中删除」
    with patch("pandapal.config.budget.pricing.report_degradation") as mock_report:
        c = cost_of_call("qwen-max", 1000, 1000, 0)

    # qwen-max 官方价 0.0024+0.0096=0.012 CNY → /7.0 → round8 = 0.00171429 USD
    assert abs(c.net_usd - round(0.012 / 7.0, 8)) < 1e-9, "历史已知模型的费用被错误归零"
    mock_report.assert_not_called()  # 有系统默认价，不属于降级


def test_unpriced_model_reports_degradation():
    """账本未命中 ⇒ 不变量被违反 ⇒ **必须**上报降级，而非静默计 0。

    ⚠️ 这条测试守的是本次重构的原始缺陷：旧实现查不到价就静默返回 0，
    费用恒 0 让预算永不触发停机 → 静默超支。旧测试只断言了返回值是 0
    （把危险行为钉成了正确行为），删掉 report_degradation 也照样全绿。
    """
    with patch("pandapal.config.budget.pricing.report_degradation") as mock_report:
        cost = cost_of_call("model-not-in-book", 100, 100, 0)

    assert cost == (0.0, 0.0, 0.0, 0.0, 0.0)
    mock_report.assert_called_once()
    kwargs = mock_report.call_args.kwargs
    assert kwargs["category"] == "cost"
    assert kwargs["fallback"] == "model-not-in-book"


# ── 用量记账守卫 ──────────────────────────────────────────────────────────────


def test_guard_halts_over_budget():
    g = CostBudgetGuard(max_usd=0.001)
    d = g.should_halt(run_id="r", usage=StepUsage("qwen-plus", 10000, 2000, 4000, 0))
    assert d.halt is True and "花费超限" in d.reason
    assert g.spent("r") > 0.001


def test_guard_no_budget_accumulates_only():
    g = CostBudgetGuard(max_usd=None)
    d = g.should_halt(run_id="r", usage=StepUsage("qwen-plus", 1000, 100, 500, 0))
    assert d.halt is False
    assert g.spent("r") > 0.0


def test_summary_full_usage_breakdown():
    g = CostBudgetGuard(max_usd=None)
    # 两步同 run，含缓存命中 / 新写入 / 推理
    g.should_halt(run_id="R", usage=StepUsage(
        "qwen-plus", 1000, 100, 800, 0, cache_creation_tokens=50, reasoning_tokens=30))
    g.should_halt(run_id="R", usage=StepUsage(
        "qwen-plus", 2000, 200, 1600, 1, cache_creation_tokens=10, reasoning_tokens=40))
    s = g.summary("R")
    assert s is not None
    assert s.input_tokens == 3000 and s.cached_tokens == 2400
    assert s.miss_tokens == 600                       # 3000 − 2400
    assert s.cache_creation_tokens == 60
    assert s.output_tokens == 300 and s.reasoning_tokens == 70
    assert s.reply_tokens == 230                      # 300 − 70
    assert abs(s.hit_rate - 2400 / 3000) < 1e-9
    assert abs((s.net_cost_usd + s.saved_usd) - s.full_cost_usd) < 1e-9
    # 未记账的 run → None（前端据此降级不展示）
    assert g.summary("unknown") is None
    # to_dict 键与前端 ReplyUsage 对齐
    assert set(s.to_dict()) == {
        "model", "net_cost_usd", "full_cost_usd", "saved_usd", "input_tokens",
        "cached_tokens", "miss_tokens", "cache_creation_tokens", "output_tokens",
        "reply_tokens", "reasoning_tokens", "hit_rate",
    }


def test_summary_mixed_model_per_step_accurate():
    # 混合模型：per-step 累加保证净费用精确（用 run 总量单算会用错单价）
    g = CostBudgetGuard(max_usd=None)
    g.should_halt(run_id="M", usage=StepUsage("qwen-plus", 1000, 100, 0, 0))
    g.should_halt(run_id="M", usage=StepUsage("gpt-4o", 1000, 100, 0, 1))
    expect = (
        cost_of_call("qwen-plus", 1000, 100, 0).net_usd
        + cost_of_call("gpt-4o", 1000, 100, 0).net_usd
    )
    assert abs(g.summary("M").net_cost_usd - round(expect, 8)) < 1e-9
