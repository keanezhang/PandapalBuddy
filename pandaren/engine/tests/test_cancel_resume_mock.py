"""pandaren/engine/tests/test_cancel_resume_mock.py — 取消 / HITL-resume 级联回归测试。

锁定本轮修复的三个 bug（每个锁在自己的代码接缝上，不驱动完整 LLM 循环）：

  Fix A — call_agent 单一真相源
    曾有两处 call_agent 定义（AgentToolFactory=HIGH/审计，SubAgentRegistry
    .register_builtin_tools=LOW/不审计），后者是从未被调用的死代码。删除后只剩
    一处（HIGH + 强制审计）。回归点：死方法不得复活、敏感度不得降级。

  Fix B — HITL/Interaction resume 注入 cancel_token → 子 Agent 级联武装
    resume 路径曾漏注入 ctx.metadata["cancel_token"]，导致 _execute_delegate 取到
    None、子 Agent 的 link_parent 从未建立 → 用户 STOP 对子 Agent 失效（实测跑了 13s）。
    回归点：ctx.metadata 带 cancel_token 时，_execute_delegate 必须把它作为
    parent_cancel_token 透传给子 Agent。

  Fix C — Layer 2 工具竞速 helper（_execute_tools_with_cancel_race）
    三处（主路径 / HITL resume / Interaction resume）共用单一实现，防漂移。resume
    两路曾是裸 asyncio.wait_for，取消在工具执行期间从不生效。回归点：四条分支
    （工具先完成 / 取消+grace内优雅收尾 / 取消+超grace强杀留痕 / step_timeout 兜底）。

对应真实场景见 raw_log：phase=llm（子 Agent LLM 阶段被取消）与 phase=tool
（父 call_agent 工具阶段撞上 Layer 2 竞速、grace 内优雅收尾）两条路径均覆盖。

运行：python pandaren/engine/tests/test_cancel_resume_mock.py  或  pytest 本文件。
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from pandaren.cancellation import CancelToken, CancelledSignal
from pandaren.engine import run_core
from pandaren.engine.run_core import RunCoreMixin
from pandaren.identity.models import TrustLevel
from pandaren.sub_agent.registry import SubAgentRegistry
from pandaren.tool.builtin.agent import AgentToolFactory
from pandaren.tool.types import SensitivityLevel


def _run(coro):
    return asyncio.run(coro)


# ════════════════════════════════════════════════════
#  Fix A — call_agent 单一真相源
# ════════════════════════════════════════════════════

def test_call_agent_single_source_of_truth():
    """死代码方法/标志不得复活；唯一定义必须 HIGH + 强制审计。"""
    reg = SubAgentRegistry()
    assert not hasattr(reg, "register_builtin_tools"), (
        "SubAgentRegistry.register_builtin_tools 是死代码，不得复活"
    )
    assert not hasattr(reg, "_builtin_tools_registered"), (
        "_builtin_tools_registered 标志应随死方法一并删除"
    )

    tools = AgentToolFactory().create_tools()
    call_agent_tools = [t for t in tools if t.name == "call_agent"]
    assert len(call_agent_tools) == 1, "call_agent 应只有一处工厂定义"

    policy = call_agent_tools[0].policy
    assert policy.sensitivity == SensitivityLevel.HIGH, (
        f"call_agent 敏感度不得降级，应为 HIGH，实际 {policy.sensitivity}"
    )
    assert policy.audit_required is True, "call_agent 必须强制审计（HC4）"


# ════════════════════════════════════════════════════
#  Fix B — 级联武装（CancelToken.link_parent 原语 + _execute_delegate 透传）
# ════════════════════════════════════════════════════

def test_cascade_primitive_parent_fires_child():
    """link_parent 后：父取消 → 子异步级联取消。"""
    async def scenario():
        parent = CancelToken()
        child = CancelToken()
        monitor = child.link_parent(parent)
        try:
            assert not child.cancelled, "初始未取消"
            parent.cancel("Cancelled by user")
            # 让 _monitor 协程获得一次调度机会
            for _ in range(3):
                await asyncio.sleep(0)
            assert child.cancelled, "父取消后子必须级联取消"
            assert child.reason == "Cancelled by user", "reason 应随级联透传"
        finally:
            if not monitor.done():
                monitor.cancel()
    _run(scenario())


def test_cascade_primitive_parent_already_cancelled():
    """父已取消时 link_parent → 子同步立即取消（快速路径，不依赖调度）。"""
    async def scenario():
        parent = CancelToken()
        parent.cancel("stop-early")
        child = CancelToken()
        monitor = child.link_parent(parent)
        try:
            assert child.cancelled, "父已取消 → 子应同步取消"
        finally:
            if not monitor.done():
                monitor.cancel()
    _run(scenario())


class _FakeIdentity:
    def __init__(self, agent_id, agent_name, trust_level):
        self.agent_id = agent_id
        self.agent_name = agent_name
        self.trust_level = trust_level
        self.when_to_use = "fake sub-agent"


class _FakeSubAgent:
    """最小子 Agent 替身：只记录 run() 收到的 metadata。"""
    def __init__(self, identity):
        self.identity = identity
        self.received_metadata = "UNSET"

    async def run(self, task, *, session_id=None, metadata=None):
        self.received_metadata = metadata
        return SimpleNamespace(success=True, output="ok", error=None, run_id="r-fake")


def _make_registry_with_child():
    reg = SubAgentRegistry()
    child_identity = _FakeIdentity("code-explorer.v1", "code-explorer", TrustLevel.SUB_AGENT)
    child_agent = _FakeSubAgent(child_identity)
    # register() 仅接受蓝图（materialize 工厂）；_FakeSubAgent 直接注册已移除
    reg.register(_SubAgentBlueprint(child_agent))
    return reg, child_agent


class _SubAgentBlueprint:
    """测试蓝图：包装 _FakeSubAgent，materialize 返回同一实例。"""

    def __init__(self, agent):
        self.identity = agent.identity
        self._agent = agent

    def materialize(self):
        return self._agent


def _make_ctx(metadata: dict):
    return SimpleNamespace(
        agent_id="pandapal",
        trust_level=TrustLevel.ORCHESTRATOR,  # 可委派任意 Agent
        session_id="sess-test",
        run_id="r-parent",
        step_n=1,
        metadata=metadata,
    )


def test_execute_delegate_forwards_parent_cancel_token():
    """Fix B 正向：ctx.metadata 带 cancel_token → 透传为 parent_cancel_token。"""
    async def scenario():
        reg, child_agent = _make_registry_with_child()
        parent_token = CancelToken()
        ctx = _make_ctx({"cancel_token": parent_token})

        res = await reg.call_agent("code-explorer", "explore", ctx)
        assert res.success, "委派应成功"
        assert isinstance(child_agent.received_metadata, dict), (
            "子 Agent 应收到 delegate metadata，实际："
            f"{child_agent.received_metadata!r}（None=级联未武装，即回归 bug）"
        )
        assert child_agent.received_metadata.get("parent_cancel_token") is parent_token, (
            "父 cancel_token 必须原样透传为 parent_cancel_token（级联链的唯一通道）"
        )
    _run(scenario())


def test_execute_delegate_no_token_when_metadata_absent():
    """Fix B 反向对照：ctx.metadata 无 cancel_token → 不传 parent 元数据（保持旧语义）。"""
    async def scenario():
        reg, child_agent = _make_registry_with_child()
        ctx = _make_ctx({})  # 无 cancel_token

        res = await reg.call_agent("code-explorer", "explore", ctx)
        assert res.success
        assert child_agent.received_metadata is None, (
            "无 cancel_token 时不应伪造 parent metadata"
        )
    _run(scenario())


# ════════════════════════════════════════════════════
#  Fix B — 结构守卫：三处 ctx.metadata 必须注入 cancel_token
# ════════════════════════════════════════════════════

def test_cancel_token_injected_at_all_three_sites():
    """原始 bug 本体：resume 路径漏注入 cancel_token。

    三处工具执行前构建 ctx.metadata 都必须注入 cancel_token：
    主路径 + HITL resume + Interaction resume。委派侧测试假设 ctx 已带 token，
    此守卫兜住"注入点被再次删除"这一精确回归（不驱动完整循环，读源码计数）。
    """
    import inspect
    src = inspect.getsource(run_core)
    count = src.count('"cancel_token": self._cancel_token')
    assert count == 3, (
        f"应有 3 处 cancel_token 注入（主路径/HITL resume/Interaction resume），"
        f"实际 {count} 处 —— 少于 3 说明某条 resume 路径的注入被删除（回归 Fix B）"
    )


# ════════════════════════════════════════════════════
#  Fix C — Layer 2 工具执行 vs 取消闸门 竞速 helper
# ════════════════════════════════════════════════════

class _HarnessStub:
    """execute_tools_concurrent 替身，行为由注入的 coroutine 工厂决定。"""
    def __init__(self, tool_coro_factory):
        self._factory = tool_coro_factory

    async def execute_tools_concurrent(self, calls, ctx):
        return await self._factory()


class _LoopStub:
    """只提供 helper 所需的两个属性。"""
    def __init__(self, harness, token):
        self._harness_executor = harness
        self._cancel_token = token


_CALLS = [{"name": "call_agent", "args": {}}]


def _helper(loop_stub, remaining=5.0):
    return RunCoreMixin._execute_tools_with_cancel_race(
        loop_stub, _CALLS, ctx=None, remaining=remaining, step_n=1,
    )


def test_race_tools_finish_first_returns_result():
    """分支①：无取消，工具先完成 → 返回结果。"""
    async def scenario():
        async def fast_tool():
            return ["RESULT"]
        stub = _LoopStub(_HarnessStub(fast_tool), CancelToken())
        res = await _helper(stub)
        assert res == ["RESULT"], "工具先完成应原样返回结果"
    _run(scenario())


def test_race_cancel_wins_graceful_within_grace():
    """分支②（phase=tool 真实场景）：取消先到，工具在 grace 内优雅收尾。

    对应 raw_log：'Layer2 · cancel WON tool race' + 'finished gracefully'。
    断言抛 CancelledSignal(phase='tool')、orphaned=[]（零残留）。
    """
    async def scenario():
        async def slow_but_finishes():
            await asyncio.sleep(0.1)   # < grace
            return ["LATE"]
        token = CancelToken()
        token.cancel("Cancelled by user")  # 取消已就绪 → cancel_wait 立即胜出
        stub = _LoopStub(_HarnessStub(slow_but_finishes), token)
        try:
            await _helper(stub)
            assert False, "取消胜出必须抛 CancelledSignal"
        except CancelledSignal as sig:
            assert getattr(sig, "phase", None) == "tool", "应标注 phase='tool'"
            assert getattr(sig, "orphaned_tools", None) == [], (
                "grace 内优雅收尾 → 零 orphaned"
            )
    _run(scenario())


def test_race_cancel_wins_orphaned_past_grace():
    """分支③：取消先到，工具超出 grace 未返回 → 强杀 + orphaned 留痕。"""
    async def scenario():
        _saved = run_core.CANCEL_GRACE_SECONDS
        run_core.CANCEL_GRACE_SECONDS = 0.05   # 压缩 grace 让测试秒级
        try:
            async def hangs():
                await asyncio.sleep(5.0)        # 远超 grace
                return ["NEVER"]
            token = CancelToken()
            token.cancel("Cancelled by user")
            stub = _LoopStub(_HarnessStub(hangs), token)
            try:
                await _helper(stub)
                assert False, "取消胜出必须抛 CancelledSignal"
            except CancelledSignal as sig:
                assert getattr(sig, "phase", None) == "tool"
                assert getattr(sig, "orphaned_tools", None) == ["call_agent"], (
                    "超 grace 未收尾的工具必须打 orphaned 留痕"
                )
        finally:
            run_core.CANCEL_GRACE_SECONDS = _saved
    _run(scenario())


def test_race_step_timeout_fallback():
    """分支④：无取消，工具超 remaining → step_timeout 兜底抛 TimeoutError。"""
    async def scenario():
        async def hangs():
            await asyncio.sleep(5.0)
            return ["NEVER"]
        stub = _LoopStub(_HarnessStub(hangs), CancelToken())  # 未取消
        try:
            await _helper(stub, remaining=0.05)
            assert False, "两者都未完成应抛 asyncio.TimeoutError"
        except asyncio.TimeoutError:
            pass
    _run(scenario())


# ════════════════════════════════════════════════════
#  独立运行入口
# ════════════════════════════════════════════════════

def _main():
    tests = [
        test_call_agent_single_source_of_truth,
        test_cascade_primitive_parent_fires_child,
        test_cascade_primitive_parent_already_cancelled,
        test_execute_delegate_forwards_parent_cancel_token,
        test_execute_delegate_no_token_when_metadata_absent,
        test_cancel_token_injected_at_all_three_sites,
        test_race_tools_finish_first_returns_result,
        test_race_cancel_wins_graceful_within_grace,
        test_race_cancel_wins_orphaned_past_grace,
        test_race_step_timeout_fallback,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"   ✅ {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"   ❌ {t.__name__} — {type(e).__name__}: {e}")
    print(f"\n📊 通过={passed} / 失败={failed} / 总计={passed + failed}")
    return failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if _main() else 1)
