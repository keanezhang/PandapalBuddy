"""pandaren/sub_agent/tests/test_isolation.py — 子 Agent 委派隔离测试

背景（bug 修复验证）：
  旧实现 SubAgentRegistry 持有全局共享的子 Agent 实例（含实例级 Memory），
  多会话并发委派同一子 Agent → 上下文互相 reset/覆盖（串扰）。
  修复后：registry 注册 materialize 工厂，每次委派产出全新实例（独立 Memory），
  用后即弃 → 多会话并发物理隔离。

覆盖：
  - F1 工厂语义：register(蓝图) → 每次委派产出新实例（is not）
  - F2 并发隔离：asyncio.gather 两个 session 并发委派 → 实例独立、各自 memory 不交叉
  - F3 契约收紧：register(Agent 实例) → TypeError（兼容路径已移除）
  - F4 注册表行为回归：唯一性 / unregister 幂等 / set_status / drain /
       refresh_health（蓝图存在性）/ build_agent_summaries（HEALTHY-only、exclude、预算）

运行：python -m pytest pandaren/sub_agent/tests/test_isolation.py -q
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pandaren.identity.models import Identity, TrustLevel, PERMISSION_ALL
from pandaren.agent import AgentStatus
from pandaren.sub_agent.exceptions import SubAgentRegistrationError
from pandaren.sub_agent.registry import SubAgentRegistry


# ════════════════════════════════════════════════════
#  假组件（最小实现，不依赖完整 LLM 栈）
# ════════════════════════════════════════════════════

class _FakeResult:
    """假 AgentResult：_execute_delegate 只读 success/output/error/run_id。"""

    def __init__(self, output: str = "ok", success: bool = True):
        self.success = success
        self.output = output
        self.error = None if success else "fake-error"
        self.run_id = f"run-{id(self):x}"


class _FakeAgent:
    """假 Agent：暴露 identity + run()。run 模拟「独立 Memory」——只写本实例。

    strict_isolation=True（默认）：run 内断言本实例历史只含本 session 消息，
    模拟真实 Memory 按 session 隔离；共享实例兼容场景（F3）传 False。
    """

    _next_serial = 0

    def __init__(self, identity: Identity, strict_isolation: bool = True):
        self.identity = identity
        # 每实例独立"对话历史"桩（对应真实 Memory._messages）
        self.messages: list[tuple[str, str]] = []
        self.strict_isolation = strict_isolation
        _FakeAgent._next_serial += 1
        self.serial = _FakeAgent._next_serial

    async def run(
        self,
        task: str,
        *,
        session_id: str,
        resume_state=None,
        hitl_decision=None,
        interaction_response=None,
        metadata=None,
        skill_name=None,
        plan_action=None,
        edited_plan_content=None,
        settings=None,
    ) -> _FakeResult:
        # 模拟原 bug 场景：写入本实例历史 → 短暂执行 → 读取验证
        self.messages.append((session_id, task))
        await asyncio.sleep(0.02)
        if self.strict_isolation:
            # 断言：本实例历史只含本 session 的消息（若共享实例则会被并发方污染）
            assert all(sid == session_id for sid, _ in self.messages), (
                f"会话串扰: instance(serial={self.serial}) 历史含多 session: {self.messages}"
            )
        return _FakeResult(output=f"[{session_id}] {task}")


class _FakeBlueprint:
    """假蓝图：有 identity + materialize()，每次产出全新 _FakeAgent。"""

    def __init__(self, agent_id: str, agent_name: str, when_to_use: str = "测试用子代理"):
        self.identity = Identity(
            agent_id=agent_id,
            agent_name=agent_name,
            when_to_use=when_to_use,
            sensitive_permissions=PERMISSION_ALL,
            trust_level=TrustLevel.SUB_AGENT,
        )
        self.materialize_count = 0

    def materialize(self) -> _FakeAgent:
        self.materialize_count += 1
        return _FakeAgent(self.identity)


def _make_context(session_id: str) -> SimpleNamespace:
    """构造最小 ToolContext 替身（call_agent 消费的字段）。"""
    return SimpleNamespace(
        agent_id="caller.main",
        trust_level=TrustLevel.ORCHESTRATOR,  # 可委派任何子代理
        session_id=session_id,
        metadata={},
        run_id=f"run-caller-{session_id}",
        step_n=1,
    )


# ════════════════════════════════════════════════════
#  F1：工厂语义 — 每次委派产出新实例
# ════════════════════════════════════════════════════

def test_f1_factory_semantics_new_instance_per_delegate():
    reg = SubAgentRegistry()
    bp = _FakeBlueprint("iso.agent", "隔离代理")
    reg.register(bp)

    ctx_a = _make_context("session-A")
    ctx_b = _make_context("session-B")

    async def _run():
        r1 = await reg.call_agent("隔离代理", "任务一", ctx_a)
        r2 = await reg.call_agent("隔离代理", "任务二", ctx_b)
        return r1, r2

    r1, r2 = asyncio.run(_run())

    assert r1.success is True, f"委派一应成功: {r1.error}"
    assert r2.success is True, f"委派二应成功: {r2.error}"
    # 关键断言：两次委派产出不同实例（每次 materialize）
    assert bp.materialize_count == 2, (
        f"每次委派应 materialize 一次，实际 {bp.materialize_count}"
    )


# ════════════════════════════════════════════════════
#  F2：并发隔离 — 两个 session 同时委派互不污染
# ════════════════════════════════════════════════════

def test_f2_concurrent_delegates_are_isolated():
    reg = SubAgentRegistry()
    bp = _FakeBlueprint("iso.concurrent", "并发代理")
    reg.register(bp)

    ctx_a = _make_context("session-A")
    ctx_b = _make_context("session-B")

    async def _run():
        # 并发：两个 session 同时委派同一子代理（旧实现必串扰）
        r_a, r_b = await asyncio.gather(
            reg.call_agent("并发代理", "A 的任务", ctx_a),
            reg.call_agent("并发代理", "B 的任务", ctx_b),
        )
        return r_a, r_b

    r_a, r_b = asyncio.run(_run())

    assert r_a.success is True, f"并发委派 A 应成功: {r_a.error}"
    assert r_b.success is True, f"并发委派 B 应成功: {r_b.error}"
    # 每个实例都只看到自己 session 的消息（_FakeAgent.run 内部已断言）
    assert bp.materialize_count == 2, (
        f"并发两次委派应各自 materialize，实际 {bp.materialize_count}"
    )


# ════════════════════════════════════════════════════
#  F3：契约收紧 — register(Agent 实例) 被拒绝（TypeError）
# ════════════════════════════════════════════════════

def test_f3_register_agent_instance_rejected():
    """register() 仅接受蓝图；Agent 实例直接注册 → TypeError（兼容路径已移除）。"""
    reg = SubAgentRegistry()
    agent = _FakeAgent(
        Identity(
            agent_id="iso.rejected",
            agent_name="被拒代理",
            when_to_use="拒绝测试",
            sensitive_permissions=PERMISSION_ALL,
            trust_level=TrustLevel.SUB_AGENT,
        )
    )

    with pytest.raises(TypeError, match="只接受蓝图"):
        reg.register(agent)  # 旧用法：直接注册实例 → 必须拒绝

    assert reg.agent_count() == 0, "拒绝后注册表为空"
    assert reg.get_identity("iso.rejected") is None, "拒绝后无元数据残留"


# ════════════════════════════════════════════════════
#  F4：注册表行为回归（与旧实现一致）
# ════════════════════════════════════════════════════

def test_f4_registry_behavior_regression():
    reg = SubAgentRegistry()

    # ── 唯一性检查 ──
    bp1 = _FakeBlueprint("iso.dup", "重复代理")
    reg.register(bp1)
    with pytest.raises(SubAgentRegistrationError):
        reg.register(_FakeBlueprint("iso.dup", "重复代理二"))
    assert reg.agent_count() == 1

    # ── unregister 幂等 ──
    reg2 = SubAgentRegistry()
    reg2.register(_FakeBlueprint("iso.unreg", "注销代理"))
    assert reg2.agent_count() == 1
    reg2.unregister("iso.unreg")
    assert reg2.agent_count() == 0
    reg2.unregister("iso.unreg")  # 幂等，不抛

    # ── 状态管理：set_status / drain ──
    reg3 = SubAgentRegistry()
    reg3.register(_FakeBlueprint("iso.status", "状态代理"))
    assert reg3.get_status("iso.status") == AgentStatus.HEALTHY
    reg3.set_status("iso.status", AgentStatus.UNHEALTHY)
    assert reg3.get_status("iso.status") == AgentStatus.UNHEALTHY
    reg3.drain("iso.status")
    assert reg3.get_status("iso.status") == AgentStatus.DRAINING
    with pytest.raises(SubAgentRegistrationError):
        reg3.set_status("not.registered", AgentStatus.HEALTHY)

    # ── refresh_health：蓝图存在性检测 ──
    reg4 = SubAgentRegistry()
    reg4.register(_FakeBlueprint("iso.health", "健康代理"))
    reg4.set_status("iso.health", AgentStatus.UNHEALTHY)
    reg4.refresh_health()
    assert reg4.get_status("iso.health") == AgentStatus.HEALTHY, (
        "蓝图存在 → refresh_health 恢复 HEALTHY"
    )
    reg4.drain("iso.health")
    reg4.refresh_health()
    assert reg4.get_status("iso.health") == AgentStatus.DRAINING, (
        "DRAINING 不被 refresh_health 恢复"
    )

    # ── build_agent_summaries：HEALTHY-only + exclude + 预算 ──
    reg5 = SubAgentRegistry()
    for i in range(5):
        reg5.register(
            _FakeBlueprint(
                f"iso.sum.{i}", f"摘要代理{i}", f"负责任务{i}类型的工作",
            )
        )
    summaries = reg5.build_agent_summaries(context_window=128_000)
    assert len(summaries) > 0
    names_all = [s.agent_name for s in summaries]

    summaries_excluded = reg5.build_agent_summaries(
        context_window=128_000, exclude_agent_id="iso.sum.0",
    )
    names_excluded = [s.agent_name for s in summaries_excluded]
    assert "摘要代理0" not in names_excluded
    assert "摘要代理0" in names_all

    reg5.set_status("iso.sum.1", AgentStatus.UNHEALTHY)
    summaries_healthy = reg5.build_agent_summaries(context_window=128_000)
    assert "摘要代理1" not in [s.agent_name for s in summaries_healthy], (
        "UNHEALTHY 不出现在摘要中"
    )

    tiny = reg5.build_agent_summaries(context_window=100)
    assert len(tiny) <= len(summaries_healthy), "1% 预算裁剪生效"

    # ── version 递增（脏检查）──
    v0 = reg5.version
    reg5.register(_FakeBlueprint("iso.ver", "版本代理"))
    assert reg5.version == v0 + 1
