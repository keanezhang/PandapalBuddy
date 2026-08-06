"""pandapal.scheduler.agent_pool 单元测试。

覆盖：
  - Fail-Safe 校验（缺 blueprint/broadcast 立即 raise）
  - Semaphore 上限：max_concurrent=2 时 3 session 有一个 queued
  - Per-session Lock：同 session 连发按顺序执行
  - Agent 复用：同 session_id 二次 acquire 返回同一实例
  - cancel_session：running Agent 收到 cancel；pending acquire 被 CancelledError
  - 广播事件三态：queued / started / released
  - stop() 幂等 + 清理所有 agent
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pandapal.events.normalized import EventType, NormalizedEvent
from pandapal.scheduler.agent_pool import SessionAgentPool


# ─── Fakes ─────────────────────────────────────────────────────────────────


class FakeAgent:
    """轻量 Agent 桩：只暴露 cancel / aclose，materialize 返回不同实例。"""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.cancelled = False
        self.closed = False

    def cancel(self) -> None:
        self.cancelled = True

    async def aclose(self) -> None:
        self.closed = True


class FakeBlueprint:
    """每次 materialize 返回一个新的 FakeAgent。"""

    def __init__(self) -> None:
        self._counter = 0

    def materialize(self) -> FakeAgent:
        self._counter += 1
        return FakeAgent(tag=f"agent-{self._counter}")


class FakeBroadcast:
    """收集所有 send 的 NormalizedEvent，用于事件序列断言。"""

    def __init__(self) -> None:
        self.events: list[NormalizedEvent] = []

    async def send(self, event: NormalizedEvent, origin_channel_id: str | None = None) -> None:
        self.events.append(event)

    def statuses_for(self, session_id: str) -> list[str]:
        return [
            e.payload.get("status", "")
            for e in self.events
            if e.event_type == EventType.SESSION_CONCURRENCY
            and e.payload.get("session_id") == session_id
        ]


# ─── 构造校验 ─────────────────────────────────────────────────────────────


def test_pool_requires_blueprint() -> None:
    with pytest.raises(ValueError, match="blueprint"):
        SessionAgentPool(blueprint=None, broadcast=FakeBroadcast(), max_concurrent=2)


def test_pool_requires_broadcast() -> None:
    with pytest.raises(ValueError, match="broadcast"):
        SessionAgentPool(blueprint=FakeBlueprint(), broadcast=None, max_concurrent=2)


def test_pool_requires_positive_concurrency() -> None:
    with pytest.raises(ValueError, match="max_concurrent"):
        SessionAgentPool(
            blueprint=FakeBlueprint(),
            broadcast=FakeBroadcast(),
            max_concurrent=0,
        )


# ─── acquire / release + 事件三态 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_acquire_broadcasts_started_and_released() -> None:
    bp = FakeBlueprint()
    br = FakeBroadcast()
    pool = SessionAgentPool(blueprint=bp, broadcast=br, max_concurrent=2, idle_ttl_seconds=60.0)
    await pool.start()
    try:
        async with pool.acquire("s1", "u1") as agent:
            assert isinstance(agent, FakeAgent)
    finally:
        await pool.stop()

    statuses = br.statuses_for("s1")
    # 立即可用路径不会广播 queued
    assert "queued" not in statuses
    assert statuses == ["started", "released"]


@pytest.mark.asyncio
async def test_third_session_queued_then_started() -> None:
    bp = FakeBlueprint()
    br = FakeBroadcast()
    pool = SessionAgentPool(blueprint=bp, broadcast=br, max_concurrent=2, idle_ttl_seconds=60.0)
    await pool.start()

    started_barrier = asyncio.Event()
    hold_until = asyncio.Event()

    async def hold_slot(sid: str) -> None:
        async with pool.acquire(sid, "u"):
            started_barrier.set()
            await hold_until.wait()

    try:
        # s1/s2 拿满 slot
        t1 = asyncio.create_task(hold_slot("s1"))
        t2 = asyncio.create_task(hold_slot("s2"))
        # 让 s1/s2 进入 hold 状态
        await asyncio.sleep(0.02)

        async def use_s3() -> None:
            async with pool.acquire("s3", "u"):
                await asyncio.sleep(0.01)

        t3 = asyncio.create_task(use_s3())
        await asyncio.sleep(0.02)

        # 此时 s3 应该已经广播 queued（还未 started）
        s3_statuses = br.statuses_for("s3")
        assert s3_statuses == ["queued"], f"expected only queued, got {s3_statuses}"

        # 释放 s1/s2 → s3 拿到 slot
        hold_until.set()
        await asyncio.gather(t1, t2, t3)

        s3_statuses = br.statuses_for("s3")
        assert s3_statuses == ["queued", "started", "released"]
    finally:
        await pool.stop()


# ─── 同 session 复用 Agent ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_same_session_reuses_agent() -> None:
    bp = FakeBlueprint()
    br = FakeBroadcast()
    pool = SessionAgentPool(blueprint=bp, broadcast=br, max_concurrent=2, idle_ttl_seconds=60.0)
    await pool.start()
    try:
        async with pool.acquire("s1", "u") as a1:
            first_id = id(a1)
        async with pool.acquire("s1", "u") as a2:
            second_id = id(a2)
        assert first_id == second_id
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_different_sessions_get_different_agents() -> None:
    bp = FakeBlueprint()
    br = FakeBroadcast()
    pool = SessionAgentPool(blueprint=bp, broadcast=br, max_concurrent=2, idle_ttl_seconds=60.0)
    await pool.start()
    try:
        async with pool.acquire("s1", "u") as a1:
            async with pool.acquire("s2", "u") as a2:
                assert a1 is not a2
                assert a1.tag != a2.tag
    finally:
        await pool.stop()


# ─── cancel_session ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_running_session_sets_agent_cancel_flag() -> None:
    bp = FakeBlueprint()
    br = FakeBroadcast()
    pool = SessionAgentPool(blueprint=bp, broadcast=br, max_concurrent=2, idle_ttl_seconds=60.0)
    await pool.start()
    captured: dict[str, FakeAgent] = {}
    started = asyncio.Event()
    release = asyncio.Event()

    async def run_session() -> None:
        async with pool.acquire("s1", "u") as ag:
            captured["a"] = ag
            started.set()
            await release.wait()

    try:
        task = asyncio.create_task(run_session())
        await started.wait()
        await pool.cancel_session("s1")
        assert captured["a"].cancelled is True

        release.set()
        await task
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_cancel_pending_session_raises_cancelled() -> None:
    bp = FakeBlueprint()
    br = FakeBroadcast()
    pool = SessionAgentPool(blueprint=bp, broadcast=br, max_concurrent=1, idle_ttl_seconds=60.0)
    await pool.start()
    started = asyncio.Event()
    release = asyncio.Event()

    async def hold_s1() -> None:
        async with pool.acquire("s1", "u"):
            started.set()
            await release.wait()

    async def try_s2() -> None:
        async with pool.acquire("s2", "u"):
            pass

    try:
        t1 = asyncio.create_task(hold_s1())
        await started.wait()
        # s2 会 pending（s1 独占 slot）
        t2 = asyncio.create_task(try_s2())
        await asyncio.sleep(0.02)

        # cancel s2 → 应该以 CancelledError 结束
        await pool.cancel_session("s2")
        with pytest.raises(asyncio.CancelledError):
            await t2

        release.set()
        await t1

        # s2 至少广播了 queued + released（started 未发出）
        statuses = br.statuses_for("s2")
        assert "queued" in statuses
        assert "released" in statuses
        assert "started" not in statuses
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_cancel_unknown_session_is_noop() -> None:
    bp = FakeBlueprint()
    br = FakeBroadcast()
    pool = SessionAgentPool(blueprint=bp, broadcast=br, max_concurrent=2, idle_ttl_seconds=60.0)
    await pool.start()
    try:
        # 幂等：不抛异常
        await pool.cancel_session("nonexistent")
        await pool.cancel_session("")
    finally:
        await pool.stop()


# ─── stop / shutdown ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_closes_all_agents() -> None:
    bp = FakeBlueprint()
    br = FakeBroadcast()
    pool = SessionAgentPool(blueprint=bp, broadcast=br, max_concurrent=2, idle_ttl_seconds=60.0)
    await pool.start()
    async with pool.acquire("s1", "u") as a1:
        pass
    async with pool.acquire("s2", "u") as a2:
        pass
    await pool.stop()
    assert a1.closed and a2.closed


@pytest.mark.asyncio
async def test_start_stop_idempotent() -> None:
    bp = FakeBlueprint()
    br = FakeBroadcast()
    pool = SessionAgentPool(blueprint=bp, broadcast=br, max_concurrent=2, idle_ttl_seconds=60.0)
    await pool.start()
    await pool.start()  # 幂等
    await pool.stop()
    await pool.stop()   # 幂等


@pytest.mark.asyncio
async def test_acquire_after_shutdown_raises() -> None:
    bp = FakeBlueprint()
    br = FakeBroadcast()
    pool = SessionAgentPool(blueprint=bp, broadcast=br, max_concurrent=2, idle_ttl_seconds=60.0)
    await pool.start()
    await pool.stop()
    with pytest.raises(RuntimeError, match="shutting down"):
        async with pool.acquire("s1", "u"):
            pass


# ─── 顺序性（同 session）─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_same_session_sequential_ordering() -> None:
    """同一 session 连发两个 acquire，per-session lock 保证串行。"""
    bp = FakeBlueprint()
    br = FakeBroadcast()
    pool = SessionAgentPool(blueprint=bp, broadcast=br, max_concurrent=4, idle_ttl_seconds=60.0)
    await pool.start()
    order: list[str] = []

    async def worker(tag: str, delay: float) -> None:
        async with pool.acquire("same-session", "u"):
            order.append(f"{tag}:enter")
            await asyncio.sleep(delay)
            order.append(f"{tag}:exit")

    try:
        # 先起一个慢的
        t1 = asyncio.create_task(worker("A", 0.05))
        await asyncio.sleep(0.005)
        # 再起一个快的（同 session，应该等 A 结束）
        t2 = asyncio.create_task(worker("B", 0.005))
        await asyncio.gather(t1, t2)
    finally:
        await pool.stop()

    # A 必须完整进出，B 才能开始（否则同 session lock 失效）
    assert order == ["A:enter", "A:exit", "B:enter", "B:exit"], order


# ─── get_stats ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_stats_reports_running_and_max() -> None:
    bp = FakeBlueprint()
    br = FakeBroadcast()
    pool = SessionAgentPool(blueprint=bp, broadcast=br, max_concurrent=3, idle_ttl_seconds=60.0)
    await pool.start()
    started = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async with pool.acquire("s1", "u"):
            started.set()
            await release.wait()

    try:
        t = asyncio.create_task(hold())
        await started.wait()
        stats = pool.get_stats()
        assert stats["running"] == 1
        assert stats["max_concurrent"] == 3
        assert stats["total"] == 1
        release.set()
        await t
    finally:
        await pool.stop()
