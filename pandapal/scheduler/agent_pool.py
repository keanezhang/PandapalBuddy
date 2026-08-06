"""pandapal/scheduler/agent_pool.py — SessionAgentPool 多 Session 并发资源管控中心

上游需求：docs/design/multi-session-concurrency-reform.md §3.2 + §4.2.1
详细设计：docs/design/SessionAgentPool-详细设计方案.md

职责：
  - 按 session_id 动态 materialize Agent 实例（首次 acquire 时懒创建）
  - Semaphore 控制全局并发上限（max_concurrent，默认 5）
  - Per-session Lock 保证同一 session 消息顺序处理（G3）
  - HITL/Interaction/Plan 挂起时释放 slot（不占用并发额度）
  - 排队反馈：SESSION_CONCURRENCY 三态广播（queued/started/released）
  - Session 独立取消（cancel_session 不影响其他 session）
  - 空闲回收：LRU + TTL

锁持有顺序（防死锁 · 铁律）：
    _pool_lock（短临界区）< _semaphore < _session_locks[sid]

  绝对禁止：持有 _semaphore 时去等 _pool_lock。
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from pandaren.agent.agent import Agent
from pandaren.agent.blueprint import AgentBlueprint

from pandapal.broadcast.broadcaster import MessageBroadcast
from pandapal.events.normalized import NormalizedEvent

logger = logging.getLogger(__name__)


@dataclass
class _SessionAgentEntry:
    """Pool 内部 per-session 状态。

    - agent:           pandaren Agent 实例
    - user_id:         该 session 归属用户（日志/审计标签）
    - last_used:       最近一次 acquire/release 的 monotonic 时间戳
    - running_since:   当前正在执行的 acquire 开始时间；未 running 时为 None
    - acquire_count:   累计 acquire 次数（可观测指标）
    """

    agent: Agent
    user_id: str
    last_used: float
    running_since: float | None = None
    acquire_count: int = 0
    # 当前绑定到该 Agent Memory 的 prompt 模式；用于 delta-rebind（None = 尚未绑定）。
    bound_mode: str | None = None


class SessionAgentPool:
    """Session 级 Agent 池。

    - 并发上限：Semaphore(max_concurrent)
    - 同 Session 顺序：per-session Lock
    - 空闲回收：LRU + TTL
    - 排队反馈：SESSION_CONCURRENCY 三态

    用法::

        pool = SessionAgentPool(
            blueprint=blueprint,
            broadcast=broadcast,
            max_concurrent=5,
        )
        await pool.start()

        async with pool.acquire(session_id="s1", user_id="alice") as agent:
            async for ev in agent.run_stream(task="hi", session_id="s1"):
                ...

        await pool.stop()
    """

    def __init__(
        self,
        *,
        blueprint: AgentBlueprint,
        broadcast: MessageBroadcast,
        max_concurrent: int = 5,
        idle_ttl_seconds: float = 1800.0,
        prompt_by_mode: dict[str, str] | None = None,
        default_mode: str = "",
    ) -> None:
        if blueprint is None:
            raise ValueError("SessionAgentPool requires blueprint")
        if broadcast is None:
            raise ValueError("SessionAgentPool requires broadcast")
        if max_concurrent < 1:
            raise ValueError(
                f"SessionAgentPool.max_concurrent must be >= 1, got {max_concurrent}"
            )

        self._blueprint = blueprint
        self._broadcast = broadcast
        self._max_concurrent = max_concurrent
        self._idle_ttl_seconds = idle_ttl_seconds
        # 双层 Prompt：{mode: 完整prompt} + 缺省模式，由 run_local 装配注入。
        self._prompt_by_mode = prompt_by_mode or {}
        self._default_mode = default_mode

        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._agents: dict[str, _SessionAgentEntry] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._pool_lock = asyncio.Lock()

        # 追踪每 session 未完成的 acquire Task（支持 cancel_session 取消 pending）
        self._pending_acquires: dict[str, set[asyncio.Task]] = {}
        # 当前正在排队（等 semaphore）的 session 数，用于 queue_position 估算
        self._queued_count: int = 0

        self._evict_task: asyncio.Task | None = None
        self._started: bool = False
        self._shutdown: bool = False

    # ─── 费用记账源（会话末尾展示用）─────────────────────────────────
    @property
    def cost_source(self) -> object | None:
        """应用层费用记账源 = blueprint 注入的 step_guard（跨 session 共享单例）。

        本 pool 材料化的所有 Agent 共享这一个 `CostBudgetGuard`，它按 run_id 累加净费用。
        executor 在 REPLY_END 时读它的 `spent(run_id)` 展示本 run 花费。返回的对象只保证
        鸭子类型：可能有 `.spent(run_id)`（CostBudgetGuard），也可能是别的 StepGuard 或 None。
        """
        return self._blueprint.step_guard

    # ─── 生命周期 ───────────────────────────────────────────────────

    async def start(self) -> None:
        """启动后台 evict 任务（幂等）。"""
        if self._started:
            return
        self._started = True
        self._shutdown = False
        self._evict_task = asyncio.create_task(
            self._evict_loop(), name="agent-pool-evict"
        )
        logger.info(
            "[AgentPool] started (max_concurrent=%d, idle_ttl=%.0fs)",
            self._max_concurrent, self._idle_ttl_seconds,
        )

    async def stop(self) -> None:
        """优雅关闭：cancel evict + cancel pending + cancel + 释放所有 agent 实例（幂等）。

        ⚠️ 池子只管 **Agent 实例**：这里 cancel/丢弃 in-flight 实例，但**不关闭**
        跨 session 共享的 ``llm_client``——那是 blueprint（owner-of-record）的职责，
        由持有 blueprint 的一方在进程停机时关一次（见 PandaPalApp.stop / blueprint.aclose）。
        """
        if not self._started:
            return
        self._shutdown = True
        logger.info("[AgentPool] stopping...")

        # 1. 停 evict 循环
        if self._evict_task is not None:
            self._evict_task.cancel()
            try:
                await self._evict_task
            except (asyncio.CancelledError, Exception):
                pass
            self._evict_task = None

        # 2. 取消所有 pending acquires
        async with self._pool_lock:
            pending_tasks: list[asyncio.Task] = []
            for tasks in self._pending_acquires.values():
                pending_tasks.extend(tasks)
            self._pending_acquires.clear()

        for t in pending_tasks:
            t.cancel()

        # 3. cancel 所有 agent（协作式）
        agents_snapshot: list[tuple[str, _SessionAgentEntry]]
        async with self._pool_lock:
            agents_snapshot = list(self._agents.items())

        for sid, entry in agents_snapshot:
            try:
                entry.agent.cancel()
            except Exception as e:
                logger.warning(
                    "[AgentPool] agent.cancel error (session=%s): %s", sid, e,
                )

        # 4. 释放每个 agent 实例的独占资源（per-instance cleanup；带 5s 超时保护）
        #    注意：Agent.aclose() 只清理本实例独有资源，**不会**关闭共享 llm_client
        #    （共享 client 由 blueprint.aclose() 在 app 停机时统一关闭）。
        for sid, entry in agents_snapshot:
            try:
                await asyncio.wait_for(entry.agent.aclose(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "[AgentPool] agent.aclose timeout (session=%s)", sid,
                )
            except Exception as e:
                logger.warning(
                    "[AgentPool] agent.aclose error (session=%s): %s", sid, e,
                )

        # 5. 清空所有字典
        async with self._pool_lock:
            self._agents.clear()
            self._session_locks.clear()
            self._pending_acquires.clear()
            self._queued_count = 0

        self._started = False
        logger.info("[AgentPool] stopped")

    # ─── acquire / release ──────────────────────────────────────────

    @asynccontextmanager
    async def acquire(
        self, session_id: str, user_id: str = "", mode: str | None = None,
    ) -> AsyncIterator[Agent]:
        """取到 Session 专属 Agent（async context manager）。

        契约：
          - finally 保证释放 semaphore + session lock + 广播 released
          - 若 semaphore 需要等待，先广播 queued(queue_position=N)
          - 若被 cancel_session 触发，CancelledError 冒泡；finally 仍广播 released
          - 若 yield 期间 consumer 抛异常，finally 释放资源后重新 raise

        Args:
            session_id: 会话 ID（必填）
            user_id:    该 session 归属用户（用于日志/审计标签；可选）
            mode:       本次请求的 prompt 模式（coding/office）；仅当与该 session
                        当前绑定模式不同才 rebind（delta-rebind，保护 prompt cache）。

        Raises:
            RuntimeError: pool 处于 shutdown 状态
            ValueError:   session_id 为空
        """
        if not session_id:
            raise ValueError("SessionAgentPool.acquire requires session_id")
        if self._shutdown:
            raise RuntimeError("SessionAgentPool is shutting down")

        # ── 追踪当前 acquire Task（用于 cancel_session 取消 pending）──
        current_task = asyncio.current_task()
        if current_task is not None:
            async with self._pool_lock:
                self._pending_acquires.setdefault(session_id, set()).add(current_task)

        acquired_semaphore = False
        session_lock: asyncio.Lock | None = None
        session_lock_acquired = False
        was_queued = False
        start_wait_ms = time.monotonic()

        try:
            # ── 判断是否需要排队 ──
            # asyncio.Semaphore 内部计数：_value=0 表示已满
            needs_queue = self._semaphore.locked() or (
                getattr(self._semaphore, "_value", 0) <= 0
            )
            if needs_queue:
                was_queued = True
                async with self._pool_lock:
                    self._queued_count += 1
                    queue_position = self._queued_count - 1  # 0 = 队首
                    queue_length = self._queued_count
                running_count = self._max_concurrent - max(
                    0, getattr(self._semaphore, "_value", 0)
                )
                await self._broadcast_concurrency(
                    session_id=session_id,
                    status="queued",
                    running_count=running_count,
                    queue_position=queue_position,
                    queue_length=queue_length,
                )
                logger.info(
                    "[AgentPool] queued session=%s pos=%d len=%d",
                    session_id, queue_position, queue_length,
                )

            # ── 拿 semaphore（可能等待，可能被 cancel）──
            await self._semaphore.acquire()
            acquired_semaphore = True

            if was_queued:
                async with self._pool_lock:
                    self._queued_count = max(0, self._queued_count - 1)

            # ── per-session lock（保证同 session 顺序）──
            session_lock = await self._ensure_session_lock(session_id)
            await session_lock.acquire()
            session_lock_acquired = True

            # ── 复用或 materialize（含按 mode delta-rebind）──
            agent = await self._get_or_materialize(session_id, user_id, mode)

            # ── 广播 started ──
            wait_ms = (time.monotonic() - start_wait_ms) * 1000
            running_count = self._running_count_locked_estimate()
            await self._broadcast_concurrency(
                session_id=session_id,
                status="started",
                running_count=running_count,
            )
            logger.info(
                "[AgentPool] granted session=%s user=%s wait_ms=%.1f running=%d",
                session_id, user_id, wait_ms, running_count,
            )

            # ── 标记 running ──
            #    同时把 current_task 从 _pending_acquires 移除：cancel_session 只
            #    应该 cancel「还没拿到 slot 的 pending 任务」，对已经在跑的 Agent
            #    仅通过 agent.cancel() 协作式信号，不 kill 承载 task。
            async with self._pool_lock:
                entry = self._agents.get(session_id)
                if entry is not None:
                    entry.running_since = time.monotonic()
                    entry.acquire_count += 1
                if current_task is not None:
                    s = self._pending_acquires.get(session_id)
                    if s is not None:
                        s.discard(current_task)
                        if not s:
                            self._pending_acquires.pop(session_id, None)

            yield agent

        except asyncio.CancelledError:
            logger.info("[AgentPool] acquire cancelled session=%s", session_id)
            raise
        finally:
            # ── 清理：无论正常/异常/取消，都要保证释放 ──
            if was_queued and not acquired_semaphore:
                # 排队中被取消，需要修正计数
                async with self._pool_lock:
                    self._queued_count = max(0, self._queued_count - 1)

            # 标记 not running + 更新 last_used
            try:
                async with self._pool_lock:
                    entry = self._agents.get(session_id)
                    if entry is not None:
                        entry.running_since = None
                        entry.last_used = time.monotonic()
            except Exception:
                logger.exception("[AgentPool] finally: update entry failed")

            if session_lock_acquired and session_lock is not None:
                try:
                    session_lock.release()
                except Exception:
                    logger.exception("[AgentPool] finally: session_lock release failed")

            if acquired_semaphore:
                try:
                    self._semaphore.release()
                except Exception:
                    logger.exception("[AgentPool] finally: semaphore release failed")

            # 从 pending 集合移除
            if current_task is not None:
                try:
                    async with self._pool_lock:
                        s = self._pending_acquires.get(session_id)
                        if s is not None:
                            s.discard(current_task)
                            if not s:
                                self._pending_acquires.pop(session_id, None)
                except Exception:
                    logger.exception("[AgentPool] finally: pending cleanup failed")

            # 广播 released（无论 queued/started/异常，只要触发了 acquire 就广播）
            try:
                running_count = self._running_count_locked_estimate()
                await self._broadcast_concurrency(
                    session_id=session_id,
                    status="released",
                    running_count=running_count,
                )
            except Exception:
                logger.exception("[AgentPool] finally: released broadcast failed")

    # ─── cancel_session ─────────────────────────────────────────────

    async def cancel_session(
        self, session_id: str, expected_user_id: str = "",
    ) -> bool:
        """取消该 session 的 Agent + 所有 pending acquire（幂等）。

        双路径：
          - running Agent → agent.cancel()（协作式，AgentLoop 循环头检查）
          - pending acquires → 逐个 task.cancel()（触发 CancelledError）
          - 两者都无 → no-op

        Args:
            expected_user_id: 若非空，必须与该 session 的归属用户一致才执行取消；
                不一致则拒绝并记 warning（防跨用户/跨会话误杀）。

        Returns:
            是否实际发出了取消信号（归属校验失败或无目标时为 False）。
        """
        if not session_id:
            return False

        had_running = False
        cancelled_pending = 0

        async with self._pool_lock:
            entry = self._agents.get(session_id)
            pending_tasks = list(self._pending_acquires.get(session_id, ()))

        # ★ 归属校验：只对 running 的 entry 校验（pending acquire 尚无 entry，
        #   其归属由调用方的 session_id 隐含保证）。归属不符直接拒绝。
        if (
            expected_user_id
            and entry is not None
            and entry.user_id
            and entry.user_id != expected_user_id
        ):
            logger.warning(
                "[AgentPool] cancel_session 拒绝：session=%s 归属 user=%s，"
                "但请求方 user=%s，疑似跨会话误杀，已忽略。",
                session_id, entry.user_id, expected_user_id,
            )
            return False

        if entry is not None:
            try:
                entry.agent.cancel()
                had_running = True
            except Exception as e:
                logger.warning(
                    "[AgentPool] cancel_session: agent.cancel failed session=%s: %s",
                    session_id, e,
                )

        for t in pending_tasks:
            try:
                t.cancel()
                cancelled_pending += 1
            except Exception as e:
                logger.warning(
                    "[AgentPool] cancel_session: task.cancel failed session=%s: %s",
                    session_id, e,
                )

        logger.info(
            "[AgentPool] cancel_session session=%s had_running=%s cancelled_pending=%d",
            session_id, had_running, cancelled_pending,
        )
        return had_running or cancelled_pending > 0

    # ─── 可观测 ─────────────────────────────────────────────────────

    def get_stats(self) -> dict[str, int]:
        """返回状态快照。

        Returns:
            {running, queued, total, max_concurrent}
        """
        running = sum(
            1 for e in self._agents.values() if e.running_since is not None
        )
        return {
            "running": running,
            "queued": self._queued_count,
            "total": len(self._agents),
            "max_concurrent": self._max_concurrent,
        }

    # ─── 内部 ──────────────────────────────────────────────────────

    async def _ensure_session_lock(self, session_id: str) -> asyncio.Lock:
        """按需惰性创建 per-session Lock。"""
        async with self._pool_lock:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[session_id] = lock
            return lock

    async def _get_or_materialize(
        self, session_id: str, user_id: str, mode: str | None = None,
    ) -> Agent:
        """复用现有 Agent 或用 blueprint 新造，并按 mode delta-rebind system prompt。

        blueprint.materialize 抛异常时不留半初始化 entry（异常向上传播）。
        """
        async with self._pool_lock:
            entry = self._agents.get(session_id)
            if entry is not None:
                self._apply_mode(entry, mode)
                return entry.agent

        # materialize 在锁外执行（可能耗时几十 ms 且分配 Memory）
        agent = self._blueprint.materialize()

        async with self._pool_lock:
            existing = self._agents.get(session_id)
            if existing is not None:
                # 竞态：另一个协程刚 materialize 了 → 弃用新造的
                # （不常见，但保证只留一个）
                logger.debug(
                    "[AgentPool] materialize race session=%s, using existing", session_id,
                )
                # 释放弃用实例的独占资源（不 await 阻塞）。
                # Agent.aclose() 只清本实例，**不碰**共享 llm_client——
                # 否则丢弃一个竞态副本会连带关掉全进程共享的连接池。
                try:
                    asyncio.create_task(agent.aclose())
                except Exception:
                    pass
                self._apply_mode(existing, mode)
                return existing.agent

            entry = _SessionAgentEntry(
                agent=agent,
                user_id=user_id,
                last_used=time.monotonic(),
                # 新造 Agent 的 Memory 已烤入 default_mode 的 prompt（见 run_local 接线）。
                bound_mode=self._default_mode or None,
            )
            self._agents[session_id] = entry
            total = len(self._agents)
            self._apply_mode(entry, mode)

        logger.info(
            "[AgentPool] materialize session=%s user=%s (total=%d)",
            session_id, user_id, total,
        )
        return agent

    def _apply_mode(self, entry: _SessionAgentEntry, mode: str | None) -> None:
        """按 mode 对该 session Agent 做 delta-rebind system prompt。

        - mode 为 None / 非法 → **保持当前绑定**，不 rebind。
          （关键：HITL / ask_user / Plan 的 resume 不带 mode，必须沿用本 session 已绑定的
          模式，绝不能因缺省回退把中途会话切回 default。新 session 的初始绑定已是
          default_mode，故非桌面渠道（企微 / 小智）不带 mode 时天然落 office。）
        - mode 合法且与 entry.bound_mode 不同 → rebind + 更新 bound_mode（仅此一次失效缓存）。
        - mode 合法但与当前相同 → 跳过（保护 prompt cache：前缀字节不变）。

        rebind 失败不影响本次执行（沿用旧 prompt），仅记录日志。
        """
        if mode not in self._prompt_by_mode:  # None / 非法 → 保持当前绑定
            return
        if entry.bound_mode == mode:
            return
        prev = entry.bound_mode
        _label = {"coding": "编码模式", "office": "办公助手模式"}

        def _fmt(m: str | None) -> str:
            return _label.get(m, m) if m else "无"

        try:
            entry.agent.rebind_system_prompt(self._prompt_by_mode[mode])
            entry.bound_mode = mode
            logger.info(
                "[AgentPool] 🔄 模式切换：%s → 当前模式【%s】",
                _fmt(prev), _fmt(mode),
            )
        except Exception:
            logger.exception(
                "[AgentPool] rebind system_prompt failed (mode=%s), keeping previous",
                mode,
            )

    def _running_count_locked_estimate(self) -> int:
        """估算当前 running session 数（从 semaphore.value 反推）。

        max - _value = 已 acquire 的 slot 数。不加锁，仅读取快照。
        """
        value = getattr(self._semaphore, "_value", self._max_concurrent)
        return max(0, self._max_concurrent - value)

    async def _broadcast_concurrency(
        self,
        *,
        session_id: str,
        status: str,
        running_count: int,
        queue_position: int = 0,
        queue_length: int = 0,
    ) -> None:
        """统一封装 SESSION_CONCURRENCY 广播；try/except 隔离故障。"""
        try:
            await self._broadcast.send(
                NormalizedEvent.session_concurrency(
                    session_id=session_id,
                    status=status,
                    running_count=running_count,
                    max_concurrent=self._max_concurrent,
                    queue_position=queue_position,
                    queue_length=queue_length,
                )
            )
        except Exception as e:
            logger.warning(
                "[AgentPool] broadcast_failed session=%s status=%s: %s",
                session_id, status, e,
            )

    async def _evict_loop(self) -> None:
        """后台循环：每 60 秒扫描一次 idle 超时。"""
        while not self._shutdown:
            try:
                await asyncio.sleep(60)
                await self._evict_scan_once()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("[AgentPool] evict loop iteration failed")

    async def _evict_scan_once(self) -> None:
        """扫描一次候选 → 逐个 evict。"""
        now = time.monotonic()
        candidates: list[str] = []
        async with self._pool_lock:
            for sid, entry in self._agents.items():
                if entry.running_since is not None:
                    continue
                if (now - entry.last_used) <= self._idle_ttl_seconds:
                    continue
                if self._pending_acquires.get(sid):
                    continue
                candidates.append(sid)

        for sid in candidates:
            await self._evict_one(sid)

    async def _evict_one(self, session_id: str) -> None:
        """单 session 清理（幂等 + 二次检查）。"""
        entry_to_close: _SessionAgentEntry | None = None
        async with self._pool_lock:
            entry = self._agents.get(session_id)
            if entry is None:
                return
            if entry.running_since is not None:
                return
            if self._pending_acquires.get(session_id):
                return
            self._agents.pop(session_id, None)
            self._session_locks.pop(session_id, None)
            entry_to_close = entry

        if entry_to_close is not None:
            # 释放被驱逐实例的独占资源。Agent.aclose() 只清本实例，**不关**
            # 共享 llm_client——这正是历史事故根因：驱逐一个空闲 session 曾连带
            # 关掉全进程共享的连接池，使其余（含新建）session 全部「client has been closed」。
            try:
                await entry_to_close.agent.aclose()
            except Exception as e:
                logger.warning(
                    "[AgentPool] evict aclose error session=%s: %s", session_id, e,
                )
            logger.info("[AgentPool] evict idle session=%s", session_id)
