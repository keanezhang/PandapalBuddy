"""pandapal/config/tests/test_budget_ledger.py — BudgetLedger 按 provider 分账停机。

覆盖设计文档（docs/design/budget-ledger-代码设计.md）Step 8.5 的 6 个必测用例：
  1. 累计跨额度 → 停机（reason 含 provider）
  2. provider 隔离（一停一放）
  3. 未登记 / 未设额度 → 不拦截（Fail-Safe）
  4. set_budget 换算 + 非法输入
  5. 持久化往返（seed→record→flush→重建 恢复）
  6. FX 现值即时生效（读时派生 limit_usd）
"""

from __future__ import annotations

import asyncio

import pytest

from pandapal.config.budget.repo import InMemoryBudgetRepo
from pandapal.config.budget.ledger import BudgetError, BudgetLedger
def _run(coro):
    return asyncio.run(coro)


def _ledger(repo=None, fx=None):
    return BudgetLedger(repo or InMemoryBudgetRepo(), fx_table=fx)


# ── 1. 累计跨额度 → 停机 ────────────────────────────────────────────────
def test_record_step_halts_over_limit():
    led = _ledger()
    _run(led.set_budget("u1", "openai", "USD", 0.01))
    led.register_run("r1", "u1")
    v = led.record_step("r1", "openai", 0.02)  # 0.02 ≥ 0.01
    assert v.halt is True
    assert "预算耗尽" in v.reason and "openai" in v.reason
    assert led.is_exhausted("u1", "openai") is True


# ── 2. provider 隔离（一停一放）────────────────────────────────────────
def test_provider_isolation():
    led = _ledger()
    _run(led.set_budget("u1", "openai", "USD", 0.01))   # 会耗尽
    _run(led.set_budget("u1", "deepseek", "USD", 1.0))  # 充裕
    led.register_run("r1", "u1")
    assert led.record_step("r1", "openai", 0.02).halt is True
    assert led.record_step("r1", "deepseek", 0.02).halt is False
    assert led.is_exhausted("u1", "openai") is True
    assert led.is_exhausted("u1", "deepseek") is False


# ── 3. 未登记 / 未设额度 → 不拦截 ──────────────────────────────────────
def test_fail_safe_no_halt():
    led = _ledger()
    # 未登记 run
    assert led.record_step("ghost", "openai", 100.0).halt is False
    # 已登记但该 provider 未设额度
    led.register_run("r2", "u2")
    assert led.record_step("r2", "openai", 100.0).halt is False
    assert led.is_exhausted("u2", "openai") is False


# ── 4. set_budget 换算 + 非法输入 ─────────────────────────────────────
def test_set_budget_conversion_and_validation():
    led = _ledger(fx={"USD": 1.0, "CNY": 0.14})
    view = _run(led.set_budget("u1", "dashscope", "CNY", 50.0))
    assert abs(view.limit_usd - 7.0) < 1e-9   # 50 × 0.14
    assert view.currency == "CNY" and view.state == "normal"
    # 非法输入
    with pytest.raises(BudgetError):
        _run(led.set_budget("u1", "dashscope", "CNY", -1.0))
    with pytest.raises(BudgetError):
        _run(led.set_budget("u1", "dashscope", "JPY", 50.0))   # 未配汇率
    with pytest.raises(BudgetError):
        _run(led.set_budget("u1", "foobar", "USD", 50.0))      # 未知 provider


# ── 5. 持久化往返（seed→record→flush→重建 恢复）───────────────────────
def test_persistence_roundtrip():
    repo = InMemoryBudgetRepo()
    led1 = _ledger(repo)
    _run(led1.set_budget("u1", "openai", "USD", 1.0))
    led1.register_run("r1", "u1")
    led1.record_step("r1", "openai", 0.3)
    _run(led1.flush())

    led2 = _ledger(repo)          # 新账本，同 repo
    _run(led2.seed_from_store())
    assert abs(led2.spent("u1", "openai") - 0.3) < 1e-9
    assert led2.is_exhausted("u1", "openai") is False  # 0.3 < 1.0，limit 也恢复
    # 恢复后继续累加应能触发停机（证明 limit_native 也持久化了）
    led2.register_run("r2", "u1")
    assert led2.record_step("r2", "openai", 0.8).halt is True  # 0.3+0.8=1.1 ≥ 1.0


# ── 6. FX 现值即时生效（读时派生 limit_usd）───────────────────────────
def test_fx_current_rate_recompute():
    led = _ledger(fx={"USD": 1.0, "CNY": 0.14})
    _run(led.set_budget("u1", "dashscope", "CNY", 100.0))
    v1 = _run(led.get_status("u1"))[0]
    assert abs(v1.limit_usd - 14.0) < 1e-9   # 100 × 0.14
    led._fx_table["CNY"] = 0.20              # 运营调汇率
    v2 = _run(led.get_status("u1"))[0]
    assert abs(v2.limit_usd - 20.0) < 1e-9   # 立即变为 100 × 0.20（读时派生）


# ── 7. limit=0 → 视图显示耗尽（与 record_step 首步停机一致）──────────────
def test_zero_limit_view_is_exhausted():
    led = _ledger()
    view = _run(led.set_budget("u1", "openai", "USD", 0.0))
    # 额度为 0：视图须为 exhausted（而非绿色 normal），与 record_step 首步即停机一致
    assert view.state == "exhausted"
    assert view.usage_ratio >= 1.0
    led.register_run("r1", "u1")
    assert led.record_step("r1", "openai", 0.0001).halt is True


# ── 8. 无法归属的净费用记入兜底桶（不静默丢弃）+ 告警去重 ────────────────
def test_unattributed_spend_bucketed():
    led = _ledger()
    # 未登记 run（缺 user_id）
    assert led.record_step("ghost", "openai", 0.5).halt is False
    # 已登记但无 provider
    led.register_run("r1", "u1")
    assert led.record_step("r1", "", 0.3).halt is False
    assert abs(led.unattributed_spent() - 0.8) < 1e-9  # 0.5 + 0.3 都未蒸发
    # unregister 清理去重集，不影响兜底桶累计
    led.unregister_run("r1")
    assert abs(led.unattributed_spent() - 0.8) < 1e-9


# ── 9. 持久化失败 → 回滚内存额度（认知与行为一致）──────────────────────
def test_set_budget_rollback_on_persist_failure():
    class _FailingRepo(InMemoryBudgetRepo):
        async def upsert_budget(self, *a, **k):
            raise OSError("disk full")

    led = _ledger(_FailingRepo())
    with pytest.raises(BudgetError):
        _run(led.set_budget("u1", "openai", "USD", 5.0))
    # 内存回滚到未设额度：不应按新额度在运行时拦截
    led.register_run("r1", "u1")
    assert led.record_step("r1", "openai", 100.0).halt is False
    assert led.is_exhausted("u1", "openai") is False


# ── 7. JsonFileBudgetRepo 真实往返（AC-12 · 🔴 回归防护）──────────────────
#
# ⚠️ 上面第 5 条 test_persistence_roundtrip 用的是 InMemoryBudgetRepo——恰好是
#    「无 limit 但有 spent」分支写对了的那个孪生。生产实际接的是
#    JsonFileBudgetRepo，它曾在同一分支写 limit_native=0.0，导致从未设过预算的
#    用户重启后被永久锁死（"预算耗尽 $0.0000"）。测试选错被测对象制造了虚假绿灯，
#    因此下面这组必须直接打 JsonFileBudgetRepo。

def test_json_repo_never_written_budget_stays_none(tmp_path):
    """从未设预算却已消费 → 落盘 limit_native 必须是 None，不能是 0.0。"""
    from pandapal.config.budget.repo import JsonFileBudgetRepo

    path = tmp_path / "budgets.json"
    repo = JsonFileBudgetRepo(str(path))
    _run(repo.load_all())
    _run(repo.bump_spent([("alice", "openai", 0.0101)]))

    import json
    row = json.loads(path.read_text(encoding="utf-8"))["budgets"][0]
    assert row["limit_native"] is None, "写 0.0 会让用户重启后被永久锁死"
    assert abs(row["spent_usd"] - 0.0101) < 1e-9


def test_json_repo_no_lockout_after_restart(tmp_path):
    """AC-12：从未设预算的用户，重启后不得触发熔断。"""
    from pandapal.config.budget.repo import JsonFileBudgetRepo

    path = tmp_path / "budgets.json"
    repo1 = JsonFileBudgetRepo(str(path))
    led1 = _ledger(repo1)
    _run(led1.seed_from_store())
    led1.register_run("r1", "alice")
    assert led1.record_step("r1", "openai", 0.0101).halt is False
    _run(led1.flush())

    # 模拟重启：新 repo + 新账本，从磁盘恢复
    led2 = _ledger(JsonFileBudgetRepo(str(path)))
    _run(led2.seed_from_store())
    led2.register_run("r2", "alice")
    verdict = led2.record_step("r2", "openai", 0.0101)
    assert verdict.halt is False, f"用户从未设过预算却被熔断：{verdict.reason}"
    assert led2.is_exhausted("alice", "openai") is False


def test_json_repo_limit_round_trip(tmp_path):
    """已设额度的行，limit_native 与 currency 完整往返。"""
    from pandapal.config.budget.repo import JsonFileBudgetRepo

    path = tmp_path / "budgets.json"
    led1 = _ledger(JsonFileBudgetRepo(str(path)))
    _run(led1.set_budget("bob", "openai", "CNY", 700.0))
    led1.register_run("r1", "bob")
    led1.record_step("r1", "openai", 50.0)  # 50 USD = 350 CNY（汇率 7）
    _run(led1.flush())

    led2 = _ledger(JsonFileBudgetRepo(str(path)))
    _run(led2.seed_from_store())
    views = _run(led2.get_status("bob"))
    acct = next(v for v in views if v.provider == "openai")
    assert acct.currency == "CNY"
    assert abs(acct.limit_native - 700.0) < 1e-9
    assert abs(led2.spent("bob", "openai") - 50.0) < 1e-9
