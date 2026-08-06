"""pandapal/scheduler/tests/test_plan_manager_model.py — Plan 审批 resume 保持同一模型。

事故延续：与 ask_user/HITL 不同，Plan 路径**不存 RunState**（见 PlanModeManager 注释），
PLAN_APPROVAL_REQUESTED 事件也不带 model_id/run_state。此前 resume 只从审批消息 content
取 model_id（前端不带 → None）→ 批准后回落默认 provider（dashscope），deepseek 会话被切走
并撞预算停机。

修复：pause 时按 run_id 内存登记 model_id（executor 显式传入），resume 时取回并透传给 executor。
这些用例锁定该 round-trip。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pandapal.scheduler.plan_manager import PlanModeManager

_RUN = "r-a5963111"
_SID = "sess-de6ccd486e77464e9860992053b1b860"


class _FakeExecutor:
    """捕获 execute() 的入参（同步记录，返回一个 no-op 协程供 spawn_background 调度）。"""
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)

        async def _noop():
            return True

        return _noop()


class _FakeBroadcast:
    def __init__(self) -> None:
        self.events: list = []

    async def send(self, ev, origin_channel_id=None) -> None:
        self.events.append(ev)


def _make_manager():
    mgr = PlanModeManager(repo=object(), broadcast=_FakeBroadcast(), router=object())
    execu = _FakeExecutor()
    mgr._executor = execu
    return mgr, execu


def _resume_msg(action="approve"):
    return SimpleNamespace(
        content={"plan_action": action, "run_id": _RUN, "session_id": _SID, "user_id": "test_user"},
        session_id=_SID,
        source_channel_id="ch",
        user_id="test_user",
    )


@pytest.mark.asyncio
async def test_plan_resume_recovers_model_id_from_pause():
    """★ 回归核心：pause 登记 deepseek → resume 透传 deepseek 给 executor（不回落 dashscope）。"""
    mgr, execu = _make_manager()
    await mgr.pause(None, _RUN, _SID, active_app_id="", model_id="deepseek-chat")
    assert mgr._pending_model[_RUN] == "deepseek-chat"

    await mgr.resume(_resume_msg("approve"))
    assert len(execu.calls) == 1
    assert execu.calls[0]["model_id"] == "deepseek-chat"   # 关键：按同一模型续跑
    assert execu.calls[0]["plan_action"] == "approve"


@pytest.mark.asyncio
async def test_plan_resume_content_model_id_fallback():
    """内存缺失（如进程重启）时回落 content.model_id。"""
    mgr, execu = _make_manager()
    # 未 pause 登记（模拟重启后 _pending_model 空）
    msg = _resume_msg("approve")
    msg.content["model_id"] = "qwen-max"
    await mgr.resume(msg)
    assert execu.calls[0]["model_id"] == "qwen-max"


@pytest.mark.asyncio
async def test_plan_abandon_clears_model_registry():
    """abandon 终态清理 model 登记，防泄漏。"""
    mgr, execu = _make_manager()
    await mgr.pause(None, _RUN, _SID, model_id="deepseek-chat")
    await mgr.resume(_resume_msg("abandon"))
    assert _RUN not in mgr._pending_model


@pytest.mark.asyncio
async def test_plan_resume_no_model_fail_fast():
    """既无内存登记也无 content → model_id 缺失 → **fail-fast 拒绝恢复**（ID 类零 default）。

    行为变更（静默降级审计 #8 / §1.1 原则一）：model_id 是 ID 类字段，缺失绝不回落默认模型/provider
    （那正是「deepseek 会话批准计划后被切回 dashscope 撞额度」的事故）。改为报错中止 + 冒泡 error 事件，
    不再调用 executor.execute。
    """
    mgr, execu = _make_manager()
    await mgr.resume(_resume_msg("approve"))
    # 不恢复：executor 一次都不该被调用
    assert execu.calls == []
    # 冒泡一条 error 事件到前端（error_code=resume_model_id_missing）
    events = mgr._broadcast.events
    assert len(events) == 1
    assert getattr(events[0], "error_code", "") == "resume_model_id_missing" or \
        events[0].payload.get("error_code") == "resume_model_id_missing"


@pytest.mark.asyncio
async def test_plan_resume_missing_plan_action_fail_fast():
    """plan_action 缺失/非法 → **fail-fast 拒绝恢复**（决策类零 default，绝不默认 approve）。

    静默降级审计 #2 / §1.1 原则一：决策/门禁类字段缺失时按最安全方向处理——不放行。
    """
    mgr, execu = _make_manager()
    msg = _resume_msg("approve")
    del msg.content["plan_action"]  # 模拟前端漏传决策字段
    await mgr.resume(msg)
    assert execu.calls == []  # 绝不默认 approve 执行
    events = mgr._broadcast.events
    assert len(events) == 1
    assert events[0].payload.get("error_code") == "plan_action_missing" or \
        getattr(events[0], "error_code", "") == "plan_action_missing"
