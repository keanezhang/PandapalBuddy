"""SessionAgentPool 会话模式锁定测试。

覆盖不变式：
  - inv-1 会话级模式锁定（首次绑定后不复用消息 mode）
  - inv-2 resume 缺省沿用 entry.bound_mode，不回退 default_mode
  - inv-3 首次绑定正确性（消息 mode / 缺省落 default）
  - inv-4 内容刷新不被锁定误伤（同 mode 片段变更仍 rebind）
  - inv-5 非法 mode 早退，保持当前绑定
  - inv-6 竞态复用锁定（xfail：竞态分支当前不可达）
  - inv-7 rebind 失败保底，保持旧绑定
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from pandapal.local.prompt_fragments import PromptAssembler
from pandapal.scheduler.agent_pool import SessionAgentPool


CODING_OFFICE = {"coding": "CODING_PROMPT", "office": "OFFICE_PROMPT"}


# ─── Fakes ─────────────────────────────────────────────────────────────────


class FakeAgent:
    """记录 rebind 调用序列的 Agent 桩；可注入 rebind 失败。"""

    def __init__(self, tag: str, rebind_error: Exception | None = None) -> None:
        self.tag = tag
        self.prompts: list[str] = []
        self.rebind_attempts: list[str] = []
        self.rebind_error = rebind_error
        self.cancelled = False
        self.closed = False

    def rebind_system_prompt(self, prompt: str) -> None:
        self.rebind_attempts.append(prompt)
        if self.rebind_error is not None:
            raise self.rebind_error
        self.prompts.append(prompt)

    def cancel(self) -> None:
        self.cancelled = True

    async def aclose(self) -> None:
        self.closed = True


class FakeBlueprint:
    """materialize 返回自增 tag 的 FakeAgent，并记录全部产出实例。"""

    def __init__(self, rebind_error: Exception | None = None) -> None:
        self._counter = 0
        self.agents: list[FakeAgent] = []
        self.rebind_error = rebind_error

    def materialize(self) -> FakeAgent:
        self._counter += 1
        agent = FakeAgent(tag=f"agent-{self._counter}", rebind_error=self.rebind_error)
        self.agents.append(agent)
        return agent


class FakeBroadcast:
    """静默收集 send 事件。"""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def send(self, event: Any, origin_channel_id: str | None = None) -> None:
        self.events.append(event)


def _make_pool(
    blueprint: FakeBlueprint,
    *,
    prompt_by_mode: dict[str, str] | None = None,
    default_mode: str = "office",
    prompt_assembler: Any = None,
) -> SessionAgentPool:
    # 模拟真实契约：blueprint 构建期已把 default_mode 的 prompt 烤入 Agent Memory，
    # 故 Pool 的初始 bound_prompt 应取自 blueprint.system_prompt（见 agent_pool 新造分支）。
    if prompt_assembler is not None:
        blueprint.system_prompt = prompt_assembler.get(default_mode) or ""
    else:
        blueprint.system_prompt = (prompt_by_mode or {}).get(default_mode, "")
    return SessionAgentPool(
        blueprint=blueprint,
        broadcast=FakeBroadcast(),
        max_concurrent=4,
        idle_ttl_seconds=60.0,
        prompt_by_mode=prompt_by_mode or {},
        default_mode=default_mode,
        prompt_assembler=prompt_assembler,
    )


# ─── inv-1 锁定 + inv-3 首次绑定 ───────────────────────────────────────────


async def test_first_coding_lock_then_office_ignored() -> None:
    """inv-1 + inv-3：首次 coding 锁定后，后续 office 不复用、不切模式、不重复 rebind。"""
    bp = FakeBlueprint()
    pool = _make_pool(bp, prompt_by_mode=CODING_OFFICE, default_mode="office")
    await pool.start()
    try:
        async with pool.acquire("s1", "u", mode="coding") as a1:
            pass
        entry = pool._agents["s1"]
        async with pool.acquire("s1", "u", mode="office") as a2:
            pass

        assert a2 is a1
        assert entry.bound_mode == "coding"
        assert entry.bound_prompt == "CODING_PROMPT"
        assert a1.prompts == ["CODING_PROMPT"]
    finally:
        await pool.stop()


# ─── inv-2 resume 缺省沿用绑定 ─────────────────────────────────────────────


async def test_resume_none_keeps_bound_mode() -> None:
    """inv-2 + inv-1：resume 缺省 mode=None 沿用已绑定 coding，绝不回退 default office。"""
    bp = FakeBlueprint()
    pool = _make_pool(bp, prompt_by_mode=CODING_OFFICE, default_mode="office")
    await pool.start()
    try:
        async with pool.acquire("s1", "u", mode="coding") as a1:
            pass
        async with pool.acquire("s1", "u", mode=None):
            pass

        entry = pool._agents["s1"]
        assert entry.bound_mode == "coding"
        assert entry.bound_prompt == "CODING_PROMPT"
        assert a1.prompts == ["CODING_PROMPT"]
    finally:
        await pool.stop()


# ─── inv-3 首次 mode=None 落 default ───────────────────────────────────────


async def test_new_session_none_uses_default_mode() -> None:
    """inv-3 + inv-2 baseline：新会话首次 mode=None 落 default office，且无多余 rebind。"""
    bp = FakeBlueprint()
    pool = _make_pool(bp, prompt_by_mode=CODING_OFFICE, default_mode="office")
    await pool.start()
    try:
        async with pool.acquire("s1", "u", mode=None) as a1:
            pass

        entry = pool._agents["s1"]
        assert entry.bound_mode == "office"
        assert entry.bound_prompt == "OFFICE_PROMPT"
        assert a1.prompts == []
    finally:
        await pool.stop()


# ─── inv-4 内容刷新不被锁定误伤 ────────────────────────────────────────────


async def test_content_refresh_still_rebinds(tmp_path: Path) -> None:
    """inv-4：同 mode 但工作区片段内容变更，复用分支仍触发一次 rebind。"""
    (tmp_path / "PANDAPAL.md").write_text("FRAGMENT-V1", encoding="utf-8")
    assembler = PromptAssembler(
        base_prompts={"coding": "CODING_BASE", "office": "OFFICE_BASE"},
        env_block="## ENV\nws=/tmp",
        work_dir=tmp_path,
    )
    bp = FakeBlueprint()
    pool = _make_pool(
        bp,
        prompt_by_mode={},
        default_mode="coding",
        prompt_assembler=assembler,
    )
    await pool.start()
    try:
        async with pool.acquire("s1", "u", mode="coding") as a1:
            pass
        assert a1.prompts == []  # 初始绑定已等于 coding prompt，跳过

        (tmp_path / "PANDAPAL.md").write_text("FRAGMENT-V2", encoding="utf-8")
        async with pool.acquire("s1", "u", mode="coding"):
            pass

        entry = pool._agents["s1"]
        assert len(a1.prompts) == 1
        assert "FRAGMENT-V2" in a1.prompts[-1]
        assert "FRAGMENT-V1" not in a1.prompts[-1]
        assert entry.bound_mode == "coding"
        assert entry.bound_prompt == a1.prompts[-1]
    finally:
        await pool.stop()


# ─── inv-5 非法 mode 早退 ──────────────────────────────────────────────────


async def test_unknown_mode_early_returns_keeps_default() -> None:
    """inv-5：非法 mode 早退，不 crash、不误 rebind，保持 default 绑定。"""
    bp = FakeBlueprint()
    pool = _make_pool(bp, prompt_by_mode=CODING_OFFICE, default_mode="office")
    await pool.start()
    try:
        async with pool.acquire("s1", "u", mode="bogus") as a1:
            pass

        entry = pool._agents["s1"]
        assert entry.bound_mode == "office"
        assert entry.bound_prompt == "OFFICE_PROMPT"
        assert a1.prompts == []
    finally:
        await pool.stop()


# ─── inv-6 竞态复用锁定 ────────────────────────────────────────────────────


# inv-6 + R-5 期望：两协程并发 materialize 同一 session，后到者复用 existing、传 None、
# 弃用自身新造实例。现状：materialize() 为同步调用，首检到建 entry 之间无 await 调度点，
# 后到协程走复用分支（materialize 仅 1 次），竞态弃用分支为不可达死代码。
@pytest.mark.xfail(
    strict=True,
    reason=(
        "inv-6 竞态复用分支不可达：materialize() 同步无 await，"
        "_get_or_materialize 首检到建 entry 之间无调度点，"
        "后到协程走复用分支（materialize 仅 1 次），竞态弃用分支为死代码"
    ),
)
async def test_race_reuse_locks_mode() -> None:
    bp = FakeBlueprint()
    pool = _make_pool(bp, prompt_by_mode=CODING_OFFICE, default_mode="office")
    await pool.start()
    try:
        task_a = asyncio.create_task(pool._get_or_materialize("s1", "u", "coding"))
        task_b = asyncio.create_task(pool._get_or_materialize("s1", "u", "office"))
        agent_a = await task_a
        agent_b_result = await task_b
        await asyncio.sleep(0)  # 推进弃用实例 aclose task

        entry = pool._agents["s1"]
        assert agent_b_result is agent_a
        assert entry.bound_mode == "coding"
        assert entry.bound_prompt == "CODING_PROMPT"
        assert agent_a.prompts == ["CODING_PROMPT"]
        # 期望竞态分支触发 2 次 materialize；现状仅 1 次（竞态分支死代码）
        assert bp._counter == 2, "竞态分支未触发：materialize 只被调用一次"
        agent_b = bp.agents[1]
        assert agent_b.closed is True
    finally:
        await pool.stop()


# ─── inv-7 rebind 失败保底 ─────────────────────────────────────────────────


async def test_rebind_failure_keeps_old_binding() -> None:
    """inv-7：rebind 抛异常被 _apply_mode 捕获，不传播，保持旧 bound_mode / bound_prompt。"""
    bp = FakeBlueprint(rebind_error=RuntimeError("rebind boom"))
    pool = _make_pool(bp, prompt_by_mode=CODING_OFFICE, default_mode="office")
    await pool.start()
    try:
        async with pool.acquire("s1", "u", mode="coding") as a1:
            pass

        entry = pool._agents["s1"]
        assert entry.bound_mode == "office"
        assert entry.bound_prompt == "OFFICE_PROMPT"
        assert a1.prompts == []  # 未成功替换
        assert a1.rebind_attempts == ["CODING_PROMPT"]  # 记录了一次尝试
    finally:
        await pool.stop()
