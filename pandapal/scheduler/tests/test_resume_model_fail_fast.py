"""HITL / ask_user resume：model_id 缺失 → fail-fast（ID 类零 default）。

行为变更（静默降级审计 #1/#6 · §1.1 原则一「ID 类没有 default，缺失即报错」）：
run 启动时 executor 已把具体 model_id（用户所选或 Agent 自身模型名）写入 RunState.metadata，
建立「每个持久化 run 必带具体 model_id」的不变量。resume 时取不到 = 不变量被破坏 = bug →
报错中止 + 冒泡 error 事件，**绝不 `or None` 回落默认模型/provider**（那正是本次事故）。

正例（metadata 带 model_id）→ 正常透传给 executor 续跑，锁定「不误伤」。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pandapal.scheduler.hitl_manager import HITLManager
from pandapal.scheduler.interaction_manager import InteractionManager
from pandaren.engine.models import RunState

_SID = "sess-11112222333344445555666677778888"
_RUN = "r-deadbeef"


class _FakeRepo:
    def __init__(self, serialized: bytes) -> None:
        self._serialized = serialized
        self.deleted: list[tuple[str, str]] = []

    async def get_run_state(self, session_id, run_id):
        return self._serialized

    async def delete_run_state(self, session_id, run_id):
        self.deleted.append((session_id, run_id))


class _FakeExecutor:
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


def _run_state(*, with_model: bool) -> RunState:
    rs = RunState(run_id=_RUN, agent_id="pandapal", step_n=1, session_id=_SID)
    meta = {"pending_approval": {"tool_name": "x"}, "pending_interaction": {"tool_name": "ask_user"}}
    if with_model:
        meta["model_id"] = "deepseek-chat"
    rs.metadata = meta
    return rs


def _hitl_mgr(serialized):
    mgr = HITLManager(
        repo=_FakeRepo(serialized), bridge=object(), broadcast=_FakeBroadcast(),
        router=object(), reply_id_mgr=object(),
    )
    mgr._executor = _FakeExecutor()
    return mgr


def _interaction_mgr(serialized):
    mgr = InteractionManager(
        repo=_FakeRepo(serialized), broadcast=_FakeBroadcast(), reply_id_mgr=object(),
    )
    mgr._executor = _FakeExecutor()
    return mgr


def _hitl_msg():
    return SimpleNamespace(
        msg_id="m1", user_id="u1", session_id=_SID, source_channel_id="ch",
        content={"run_id": _RUN, "session_id": _SID, "decision": "approved", "user_id": "u1"},
    )


def _interaction_msg():
    return SimpleNamespace(
        msg_id="m1", user_id="u1", session_id=_SID, source_channel_id="ch",
        content={"run_id": _RUN, "session_id": _SID, "response": "answer"},
    )


@pytest.mark.asyncio
async def test_hitl_resume_missing_model_id_fail_fast():
    mgr = _hitl_mgr(None)  # serialized 由下方替换
    rs = _run_state(with_model=False)
    mgr._repo._serialized = mgr._serialize(rs, _SID)

    await mgr.resume(_hitl_msg())

    assert mgr._executor.calls == []                       # 不恢复
    assert any(getattr(e, "payload", {}).get("error_code") == "resume_model_id_missing"
               for e in mgr._broadcast.events)              # 冒泡 error


@pytest.mark.asyncio
async def test_hitl_resume_with_model_id_proceeds():
    mgr = _hitl_mgr(None)
    rs = _run_state(with_model=True)
    mgr._repo._serialized = mgr._serialize(rs, _SID)

    await mgr.resume(_hitl_msg())

    assert len(mgr._executor.calls) == 1
    assert mgr._executor.calls[0]["model_id"] == "deepseek-chat"  # 同模型续跑


@pytest.mark.asyncio
async def test_interaction_resume_missing_model_id_fail_fast():
    mgr = _interaction_mgr(None)
    rs = _run_state(with_model=False)
    mgr._repo._serialized = mgr._serialize(rs, _SID)

    await mgr.resume(_interaction_msg())

    assert mgr._executor.calls == []
    assert any(getattr(e, "payload", {}).get("error_code") == "resume_model_id_missing"
               for e in mgr._broadcast.events)


@pytest.mark.asyncio
async def test_interaction_resume_with_model_id_proceeds():
    mgr = _interaction_mgr(None)
    rs = _run_state(with_model=True)
    mgr._repo._serialized = mgr._serialize(rs, _SID)

    await mgr.resume(_interaction_msg())

    assert len(mgr._executor.calls) == 1
    assert mgr._executor.calls[0]["model_id"] == "deepseek-chat"
