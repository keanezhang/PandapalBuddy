"""TaskScheduler — 任务调度层（#6）。

职责：
- 任务定义的注册/注销（持久化 + 调度引擎同步）
- 三类触发源（cron 定时 / event 事件 / manual 手动）的调度管理
- 每次执行的生命周期：创建 TaskExecution → 驱动 Agent 调度层 → 更新状态
- 并发保护：同 task_id already_running 时跳过本轮触发（BL3）
- 执行结果投递：task_notification（broadcast）/ sensitive_data（target_only）
- 重启恢复：startup 时检测 pending/running executions（场景 6）
- 优雅关闭：5s 宽限期后强制取消，状态更新为 cancelled（场景 8）

设计约束：
- BL1: 状态机单向转换（pending→running→completed/failed/cancelled）
- BL2: 不持有 AgentScheduler 直接引用，通过 router.inject_inbound_message 解耦
- BL3: 幂等触发（same task_id already_running → 跳过）
- BL4: 失败隔离（每个任务的 asyncio.Task 独立捕获异常）
- BL5: Fail-Safe Default（配置缺失时使用保守默认值）
- I1:  显式生命周期（initialize_scheduler / shutdown_scheduler）
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable

from pandapal.broadcast.broadcaster import MessageBroadcast
from pandapal.broadcast.channel_ids import LOCAL_SCHEDULER_CHANNEL_ID
from pandapal.events.normalized import EventType, NormalizedEvent
from pandapal.messages.types import RouterMessageType
from pandapal.router.models import InboundMessage
from pandapal.router.router import MessageRouter
from pandapal.storage.models import (
    TaskDefinition,
    TaskExecution,
    TaskExecutionStatus,
)
from pandapal.storage.repositories.sqlite_task_repo import TaskRepository
from pandapal.task_scheduler.models import TriggerType

from pandapal.degradation import DegradationEvent, report_degradation

logger = logging.getLogger(__name__)

# 每个用户最多可注册的定时任务数（防止无限堆积）
MAX_TASKS_PER_USER = 50

# croniter 可选依赖（BL5 fail-safe：未安装时 CRON 触发降级为不可用，手动/事件触发正常工作）
try:
    from croniter import croniter as CronIter  # type: ignore[import-untyped]
    _CRONITER_AVAILABLE = True
except ImportError:
    _CRONITER_AVAILABLE = False

_DEFAULT_TASK_TIMEOUT_MINUTES: float = 30.0
_SHUTDOWN_GRACE_PERIOD_S: float = 5.0

# sensitivity 字符串级别阈值（与 pandaren SDK SensitivityLevel 保持语义一致）
_SENSITIVE_LEVELS: frozenset[str] = frozenset({"high", "critical"})


class TaskScheduler:
    """任务调度层（#6）。

    使用方式：
        task_scheduler = TaskScheduler(
            task_repo=storage.get_task_repo(),
            broadcast=broadcast,
            router=router,
            config_manager=config_manager,
        )
        # 注册 task_result 路由处理器（对称路由：通过 Router 与 AgentScheduler 双向解耦）
        task_scheduler.register_route_handlers()

        # Bootstrap 调用
        await task_scheduler.initialize_scheduler()
        # ... 运行 ...
        await task_scheduler.shutdown_scheduler()
    """

    def __init__(
        self,
        task_repo: TaskRepository,
        broadcast: MessageBroadcast,
        router: MessageRouter,
        config_manager: Any,
    ) -> None:
        """E1: 必填依赖构造时校验。"""
        if task_repo is None:
            raise ValueError("task_repo cannot be None")
        if broadcast is None:
            raise ValueError("broadcast cannot be None")
        if router is None:
            raise ValueError("router cannot be None")
        if config_manager is None:
            raise ValueError("config_manager cannot be None")

        self._task_repo = task_repo
        self._broadcast = broadcast
        self._router = router
        self._config_manager = config_manager

        # ── 四张核心内存表 ──────────────────────────────────────────────
        # task_id → asyncio.Task（正在运行的执行协程）
        self._running_executions: dict[str, asyncio.Task] = {}
        # task_id → asyncio.TimerHandle（cron 下一次触发句柄）
        self._cron_handles: dict[str, asyncio.TimerHandle] = {}
        # event_name → [task_id]（事件触发映射，运行时构建）
        self._event_handlers: dict[str, list[str]] = {}
        # execution_id → asyncio.Future（等待 AgentScheduler 回调 resolve_task_execution）
        self._pending_futures: dict[str, asyncio.Future] = {}
        # task_id → execution_id（运行时反向映射，O(1) 查询 already_running 的 execution_id）
        self._task_execution_map: dict[str, str] = {}

        self._initialized: bool = False
        # 任务列表变更回调（注册/注销后通知前端刷新列表）
        self._on_task_list_changed: Callable[[], Awaitable[None]] | None = None

    # ──────────────────────────────────────────────
    # Public Methods — Lifecycle (I1)
    # ──────────────────────────────────────────────

    def set_task_list_changed_callback(
        self, cb: Callable[[], Awaitable[None]]
    ) -> None:
        """注册任务列表变更回调（注册/注销后触发，通知前端刷新）。"""
        self._on_task_list_changed = cb

    def register_route_handlers(self) -> None:
        """向路由层注册 task_result 处理器。

        调用时机：Bootstrap 中 TaskScheduler 创建后、initialize_scheduler 前。
        - 注册 TASK_RESULT → self._handle_task_result
        - AgentScheduler 通过 Router 注入 task_result 后，由本 handler 解析 Future

        对称路由设计：
        - AgentScheduler 不直接持有 TaskScheduler 引用
        - TaskScheduler 不直接持有 AgentScheduler 引用
        - 两者仅通过 Router 的 task_instruction / task_result 双向通信
        """
        self._router.register_route_handler(
            RouterMessageType.TASK_RESULT, self._handle_task_result
        )
        logger.info("TaskScheduler route handlers registered")

    async def initialize_scheduler(self) -> None:
        """启动调度引擎（I1: 显式生命周期）。

        1. 恢复中断的执行记录（重启恢复）
        2. 从 Storage 加载所有任务定义并注册到调度引擎
        """
        if self._initialized:
            logger.warning("TaskScheduler already initialized, skipping")
            return

        logger.info("TaskScheduler initializing...")

        # Step 1: 恢复中断执行（重启恢复）
        await self._restore_pending_executions()

        # Step 2: 全量加载任务定义并注册调度引擎
        await self._load_and_register_all_tasks()

        self._initialized = True
        logger.info(
            "TaskScheduler initialized: cron_jobs=%d, event_types=%d",
            len(self._cron_handles),
            len(self._event_handlers),
        )

    async def shutdown_scheduler(self) -> None:
        """优雅关闭调度引擎（I1: 显式生命周期）。

        1. 取消所有 cron 句柄（停止新触发）
        2. 等待 running executions 完成（5s 宽限期）
        3. 超时后强制取消，状态更新为 cancelled（BL1 失败情况 8）
        """
        logger.info("TaskScheduler shutting down...")

        # Step 1: 停止所有 cron 定时触发
        for task_id in list(self._cron_handles):
            self._unregister_cron_job(task_id)

        # Step 2: 等待 running executions 完成（宽限期）
        if self._running_executions:
            running_tasks = list(self._running_executions.values())
            logger.info(
                "Waiting %.1fs for %d running executions to complete...",
                _SHUTDOWN_GRACE_PERIOD_S,
                len(running_tasks),
            )
            done, pending = await asyncio.wait(
                running_tasks,
                timeout=_SHUTDOWN_GRACE_PERIOD_S,
            )
            if pending:
                logger.warning(
                    "Grace period expired, force-cancelling %d executions", len(pending)
                )
                for task in pending:
                    task.cancel()
                # 等待取消完成
                await asyncio.gather(*pending, return_exceptions=True)

        # Step 3: 将仍 running 的 TaskExecution 标为 cancelled（BL1: 失败情况 8）
        for task_id, execution_id in list(self._task_execution_map.items()):
            try:
                await self._task_repo.update_task_execution_status(
                    execution_id, TaskExecutionStatus.CANCELLED
                )
                logger.warning(
                    "shutdown: execution_id=%s (task_id=%s) marked CANCELLED",
                    execution_id, task_id,
                )
            except Exception as e:
                logger.error(
                    "shutdown: failed to mark execution %s as cancelled: %s",
                    execution_id, e,
                )

        self._running_executions.clear()
        self._pending_futures.clear()
        self._task_execution_map.clear()
        self._initialized = False
        logger.info("TaskScheduler shutdown complete")

    # ──────────────────────────────────────────────
    # Public Methods — Task Definition Management
    # ──────────────────────────────────────────────

    async def register_task_definition(self, definition: TaskDefinition) -> None:
        """注册任务定义（持久化 + 调度引擎同步，UPSERT 语义）。

        若同 task_id 已存在，先注销旧调度项再重新注册。
        若同 user_id + name 已存在其他 task_id，则跳过创建（防 LLM 重复调用）。

        校验顺序：先解析 + 校验 trigger_rule，**通过后**才动 storage 和调度表。
        这样失败时不会留下"持久化了但没调度"的孤儿记录，调用方拿到 ValueError
        即可向用户返回明确错误。

        Raises:
            ValueError: trigger_rule_json 无法解析、缺字段、或 cron 表达式语法非法。
        """
        # ── Step 1: 先解析 + 校验，失败立即抛错，不污染存储 ───────────────
        trigger_rule = self._parse_trigger_rule(definition.trigger_rule_json)
        if trigger_rule is None:
            raise ValueError(
                f"invalid trigger_rule_json for task_id={definition.task_id}: "
                f"{definition.trigger_rule_json!r}"
            )
        if trigger_rule.trigger_type in (TriggerType.RECURRING, TriggerType.ONESHOT):
            if not trigger_rule.cron_expression:
                raise ValueError(
                    f"{trigger_rule.trigger_type.value} trigger requires cron_expression "
                    f"(task_id={definition.task_id})"
                )
            self._validate_cron_expression(trigger_rule.cron_expression)
        elif trigger_rule.trigger_type == TriggerType.EVENT:
            if not trigger_rule.event_name:
                raise ValueError(
                    f"event trigger requires event_name (task_id={definition.task_id})"
                )

        # ── Step 2: 防重复 —— 同用户同名（不同 task_id）则跳过 ──────────
        try:
            existing_defs = await self._task_repo.find_task_definitions_by_user(
                definition.user_id
            )
            for existing in existing_defs:
                if existing.name == definition.name and existing.task_id != definition.task_id:
                    logger.warning(
                        "register_task_definition: duplicate task name detected — "
                        "existing task_id=%s, new task_id=%s, name=%s — skipping new registration",
                        existing.task_id, definition.task_id, definition.name,
                    )
                    return
            # 数量上限：仅对「新任务」拦截（同 task_id 视为更新，放行）
            is_new = all(e.task_id != definition.task_id for e in existing_defs)
            if is_new and len(existing_defs) >= MAX_TASKS_PER_USER:
                raise ValueError(
                    f"已达到定时任务数量上限（{MAX_TASKS_PER_USER} 个），"
                    f"不支持创建更多任务，请先删除部分旧任务"
                )
        except AttributeError:
            # Markdown repository 可能不支持 find_task_definitions_by_user 的某些路径
            pass

        # ── Step 3: 注销旧的（幂等）── 持久化 ── 同步调度引擎 ────────────
        self._unregister_cron_job(definition.task_id)
        self._unregister_event_handler(definition.task_id)

        await self._task_repo.save_task_definition(definition)

        if trigger_rule.trigger_type in (TriggerType.RECURRING, TriggerType.ONESHOT):
            assert trigger_rule.cron_expression  # Step 1 已校验
            self._register_cron_job(
                definition.task_id,
                trigger_rule.cron_expression,
                trigger_type=trigger_rule.trigger_type,
            )
        elif trigger_rule.trigger_type == TriggerType.EVENT:
            assert trigger_rule.event_name  # Step 1 已校验
            self._register_event_handler(definition.task_id, trigger_rule.event_name)
        # TriggerType.MANUAL — 无需注册调度引擎，由 execute_task_manually 按需触发

        # D2 Push 增量：广播单个任务变更
        await self._broadcast_task_changed(definition, change_type="updated")

        logger.info(
            "Task registered: task_id=%s, trigger_type=%s",
            definition.task_id, trigger_rule.trigger_type,
        )

        if self._on_task_list_changed is not None:
            await self._on_task_list_changed()

    async def unregister_task_definition(self, task_id: str) -> None:
        """注销任务定义（持久化删除 + 调度引擎注销）。

        若有 running 执行则先取消（BL1: completed/failed 状态静默幂等）。
        """
        # 取消正在运行的执行（BL1: 幂等）
        await self._cancel_running_execution(task_id)

        # 注销调度引擎
        self._unregister_cron_job(task_id)
        self._unregister_event_handler(task_id)

        # 持久化删除（D4: 级联删除关联 executions）
        await self._task_repo.delete_task_definition(task_id)

        # D2 Push 增量：广播删除
        await self._broadcast_task_changed(
            None, change_type="deleted", task_id=task_id,
        )

        logger.info("Task unregistered: task_id=%s", task_id)

        if self._on_task_list_changed is not None:
            await self._on_task_list_changed()

    # ──────────────────────────────────────────────
    # Public Methods — Trigger Sources
    # ──────────────────────────────────────────────

    async def execute_task_manually(
        self, task_id: str, source_channel_id: str | None = None
    ) -> str:
        """手动触发执行（场景 4）。

        Returns:
            execution_id — 新建的执行 ID；若 already_running，返回当前执行 ID（BL3 幂等）
        """
        # BL3: already_running 检查（幂等）
        if task_id in self._running_executions:
            existing_execution_id = self._task_execution_map.get(task_id, "")
            logger.warning(
                "execute_task_manually: task_id=%s already_running (execution_id=%s), skipping",
                task_id, existing_execution_id,
            )
            return existing_execution_id

        # 预先生成 execution_id（返回给调用方，执行在后台进行）
        execution_id = str(uuid.uuid4())
        asyncio.ensure_future(
            self._execute_task(task_id, execution_id, source_channel_id)
        )
        return execution_id

    async def trigger_event_execution(
        self, event_name: str, source_channel_id: str | None = None
    ) -> None:
        """事件触发执行（场景 3）。

        查找注册了该事件的所有任务并逐一触发；单任务失败不影响其他（BL4）。
        """
        task_ids = self._event_handlers.get(event_name, [])
        if not task_ids:
            logger.debug("trigger_event_execution: no tasks registered for event=%s", event_name)
            return

        for task_id in list(task_ids):
            try:
                await self._trigger_scheduled_execution(task_id, source_channel_id)
            except Exception as e:
                # BL4: 单任务失败不影响其他任务
                logger.error(
                    "trigger_event_execution: task_id=%s failed: %s", task_id, e
                )

    # ──────────────────────────────────────────────
    # Public Methods — Query & Callback
    # ──────────────────────────────────────────────

    async def get_task_execution_status(
        self, execution_id: str
    ) -> TaskExecution | None:
        """查询执行记录（无副作用）。"""
        return await self._task_repo.find_task_execution(execution_id)

    async def _handle_task_result(self, msg: InboundMessage) -> None:
        """处理 AgentScheduler 回传的 task_result（O3: 永不向外抛异常）。

        从 msg.content 中提取 execution_id 和 result，解析对应的 asyncio.Future。
        幂等：Future 已完成或不存在时静默。

        设计意图：
        - 对称路由：AgentScheduler 不直接回调 TaskScheduler，两者通过 Router 解耦
        - Future 由 TaskScheduler 自己解析，不将内部状态控制权交给外部模块
        """
        try:
            content = msg.content if isinstance(msg.content, dict) else {}
            execution_id = content.get("execution_id", "")
            result = content.get("result")

            if not execution_id:
                logger.warning("_handle_task_result: missing execution_id")
                return

            await self.resolve_task_execution(execution_id, result)
        except Exception as e:
            logger.error(
                "_handle_task_result: unexpected error execution_id=%s: %s",
                msg.content.get("execution_id", "?") if isinstance(msg.content, dict) else "?",
                e,
            )

    async def resolve_task_execution(
        self, execution_id: str, result: Any
    ) -> None:
        """AgentScheduler 执行完 task_instruction 后的回调（O3: 永不向外抛异常）。

        将 AgentResult 注入对应的 asyncio.Future，解除 _execute_task 的 await 等待。
        幂等：Future 已完成或不存在时静默。
        """
        future = self._pending_futures.get(execution_id)
        if future is None:
            logger.warning(
                "resolve_task_execution: no pending future for execution_id=%s (already resolved or timed out)",
                execution_id,
            )
            return
        if future.done():
            logger.debug(
                "resolve_task_execution: future already done for execution_id=%s", execution_id
            )
            return
        try:
            future.set_result(result)
            logger.debug(
                "resolve_task_execution: future resolved for execution_id=%s", execution_id
            )
        except asyncio.InvalidStateError:
            logger.warning(
                "resolve_task_execution: future in invalid state for execution_id=%s", execution_id
            )

    # ──────────────────────────────────────────────
    # Internal Methods — Execution Core
    # ──────────────────────────────────────────────

    async def _trigger_scheduled_execution(
        self, task_id: str, source_channel_id: str | None = None
    ) -> None:
        """cron / event 触发的执行入口（BL3: already_running 检查）。"""
        # BL3: 幂等触发检查
        if task_id in self._running_executions:
            existing_execution_id = self._task_execution_map.get(task_id, "")
            logger.warning(
                "_trigger_scheduled_execution: task_id=%s already_running (execution_id=%s), skipping",
                task_id, existing_execution_id,
            )
            return

        execution_id = str(uuid.uuid4())
        asyncio.ensure_future(
            self._execute_task(task_id, execution_id, source_channel_id)
        )

    async def _execute_task(
        self,
        task_id: str,
        execution_id: str,
        source_channel_id: str | None,
    ) -> None:
        """核心执行流程（BL1/BL4/全程 try/except 兜底）。

        阶段：
        1. 加载 TaskDefinition（不存在 → 中止）
        2. 创建 TaskExecution(PENDING) 持久化（失败 → 中止，防"幽灵执行"）
        3. pending → running，注册内存表
        4. 创建 asyncio.Future，注入 task_instruction
        5. await Future（asyncio.wait_for + asyncio.shield，防超时取消 future）
        6. 更新状态 + 发送通知
        """
        definition: TaskDefinition | None = None
        execution: TaskExecution | None = None
        # 提前解析 trigger_type，避免 ONESHOT 并发注销后 _dispatch_task_notification 查不到 definition
        preloaded_trigger_type: str | None = None

        try:
            # ── Step 1: 加载任务定义 ──────────────────────────────────
            definition = await self._task_repo.find_task_definition(task_id)
            if definition is None:
                logger.error(
                    "_execute_task: task_id=%s not found in storage, aborting execution_id=%s",
                    task_id, execution_id,
                )
                return

            # 提前缓存 trigger_type（ONESHOT 触发后并发 unregister 可能删除 definition）
            trigger_rule = self._parse_trigger_rule(definition.trigger_rule_json)
            if trigger_rule is not None:
                preloaded_trigger_type = trigger_rule.trigger_type.value

            # ── Step 2: 创建 TaskExecution(PENDING) ───────────────────
            now = datetime.now(timezone.utc)
            execution = TaskExecution(
                execution_id=execution_id,
                task_id=task_id,
                user_id=definition.user_id,
                status=TaskExecutionStatus.PENDING,
                started_at=now,
                source_channel_id=source_channel_id,
            )
            try:
                await self._task_repo.save_task_execution(execution)
            except Exception as e:
                # 失败情况 7: 持久化失败 → 中止，避免"幽灵执行"
                logger.error(
                    "_execute_task: failed to save TaskExecution execution_id=%s: %s",
                    execution_id, e,
                )
                return

            # ── Step 3: pending → running，注册内存表 ─────────────────
            await self._task_repo.update_task_execution_status(
                execution_id, TaskExecutionStatus.RUNNING
            )

            current_task = asyncio.current_task()
            if current_task is not None:
                self._running_executions[task_id] = current_task
            self._task_execution_map[task_id] = execution_id

            logger.info(
                "_execute_task: started execution_id=%s task_id=%s", execution_id, task_id
            )

            # ── Step 4: 创建 Future + 注入 task_instruction ───────────
            loop = asyncio.get_event_loop()
            future: asyncio.Future = loop.create_future()
            self._pending_futures[execution_id] = future

            await self._inject_task_instruction(definition, execution_id, source_channel_id)

            # ── Step 5: 等待 AgentScheduler 回调 resolve_task_execution ──
            timeout_s = self._get_task_timeout_minutes() * 60.0
            result = await asyncio.wait_for(
                asyncio.shield(future),
                timeout=timeout_s,
            )

            # ── Step 6: 更新状态 completed + 发送通知 ────────────────
            completed_execution = TaskExecution(
                execution_id=execution_id,
                task_id=task_id,
                user_id=definition.user_id,
                status=TaskExecutionStatus.COMPLETED,
                started_at=now,
                completed_at=datetime.now(timezone.utc),
                source_channel_id=source_channel_id,
                result_json=self._serialize_result(result),
            )
            await self._task_repo.save_task_execution(completed_execution)

            await self._dispatch_task_notification(completed_execution, result, preloaded_trigger_type)

            logger.info(
                "_execute_task: completed execution_id=%s task_id=%s",
                execution_id, task_id,
            )

        except asyncio.TimeoutError:
            # 失败情况 3: 执行超时 → FAILED
            logger.warning(
                "_execute_task: timeout execution_id=%s task_id=%s (timeout=%.1f min)",
                execution_id, task_id, self._get_task_timeout_minutes(),
            )
            if execution is not None and definition is not None:
                failed_exec = TaskExecution(
                    execution_id=execution_id,
                    task_id=task_id,
                    user_id=definition.user_id,
                    status=TaskExecutionStatus.FAILED,
                    started_at=execution.started_at,
                    completed_at=datetime.now(timezone.utc),
                    source_channel_id=source_channel_id,
                    error_message="Task execution timed out",
                )
                try:
                    await self._task_repo.save_task_execution(failed_exec)
                    await self._dispatch_task_notification(failed_exec, None, preloaded_trigger_type)
                except Exception as save_err:
                    logger.error(
                        "_execute_task: failed to save timeout status: %s", save_err
                    )

        except asyncio.CancelledError:
            # 失败情况 8: shutdown 取消 → CANCELLED（BL1: re-raise 让 Task 正确标为 cancelled）
            logger.warning(
                "_execute_task: cancelled execution_id=%s task_id=%s",
                execution_id, task_id,
            )
            if execution is not None and definition is not None:
                try:
                    await self._task_repo.update_task_execution_status(
                        execution_id, TaskExecutionStatus.CANCELLED
                    )
                except Exception as save_err:
                    logger.error(
                        "_execute_task: failed to mark cancelled status: %s", save_err
                    )
            raise  # 必须 re-raise，让 asyncio.Task 正确被标为 cancelled

        except Exception as e:
            # BL4: 失败隔离 — 单任务异常不向外传播
            logger.error(
                "_execute_task: unexpected error execution_id=%s task_id=%s: %s",
                execution_id, task_id, e,
            )
            if execution is not None and definition is not None:
                failed_exec = TaskExecution(
                    execution_id=execution_id,
                    task_id=task_id,
                    user_id=definition.user_id,
                    status=TaskExecutionStatus.FAILED,
                    started_at=execution.started_at,
                    completed_at=datetime.now(timezone.utc),
                    source_channel_id=source_channel_id,
                    error_message=str(e),
                )
                try:
                    await self._task_repo.save_task_execution(failed_exec)
                    await self._dispatch_task_notification(failed_exec, None, preloaded_trigger_type)
                except Exception as save_err:
                    logger.error(
                        "_execute_task: failed to save failed status: %s", save_err
                    )

        finally:
            # 清除所有内存表条目（无论成功/失败/取消）
            self._running_executions.pop(task_id, None)
            self._task_execution_map.pop(task_id, None)
            # 清除 Future（若超时 future 仍在 _pending_futures 中，需清理防内存泄漏）
            orphaned_future = self._pending_futures.pop(execution_id, None)
            if orphaned_future is not None and not orphaned_future.done():
                orphaned_future.cancel()

    # ──────────────────────────────────────────────
    # Internal Methods — Notification
    # ──────────────────────────────────────────────

    async def _inject_task_instruction(
        self,
        definition: TaskDefinition,
        execution_id: str,
        source_channel_id: str | None,
    ) -> None:
        """构造 InboundMessage(task_instruction) 并注入路由层（BL2: 不直接调用 Agent）。

        session_id 是可选元数据（标识哪个 session 创建了此任务），不作为执行硬约束。
        定时任务属于用户级别，用户登录后任何 session 都能执行；
        若 session_id 不存在，下游 _handle_task_instruction 会为本次执行自动创建新 session。
        """
        effective_channel = source_channel_id or LOCAL_SCHEDULER_CHANNEL_ID
        # 任务创建时的 session_id：仅用于 traceability（知道谁创建的），不阻塞执行
        session_id = definition.session_id or None

        msg = InboundMessage(
            msg_id=str(uuid.uuid4()),
            message_type="task_instruction",
            source_channel_id=effective_channel,
            user_id=definition.user_id,
            session_id=session_id,
            content={
                "task_id": definition.task_id,
                "execution_id": execution_id,
                "task_input": definition.task_prompt,
            },
        )
        await self._router.inject_inbound_message(msg)

        logger.debug(
            "_inject_task_instruction: injected execution_id=%s task_id=%s session_id=%s",
            execution_id, definition.task_id, session_id,
        )

    async def _dispatch_task_notification(
        self, execution: TaskExecution, result: Any, trigger_type: str | None = None
    ) -> None:
        """按 sensitivity 决定通知策略（BL2: 只构造消息，不管路由细节）。

        LOW/MEDIUM  → broadcast（所有在线设备）
        HIGH/CRITICAL + source_channel_id → target_only（仅源 channel）
        HIGH/CRITICAL + no source_channel_id → 降级为 broadcast，记 WARN（失败情况 4）

        通知 payload 使用 IPC 扁平格式：title / body / level，与前端 TaskNotificationModal 对齐。

        Args:
            trigger_type: 优先使用的触发类型（避免 ONESHOT 并发注销后查库失败）。
        """
        definition = await self._task_repo.find_task_definition(execution.task_id)
        sensitivity = (definition.sensitivity if definition is not None else "medium").lower()

        # 提取执行结果摘要
        result_summary = ""
        if result is not None:
            try:
                if hasattr(result, "output") and result.output:
                    result_summary = str(result.output)[:200]
                elif isinstance(result, dict):
                    result_summary = str(result.get("output", result.get("result", "")))[:200]
                else:
                    result_summary = str(result)[:200]
            except Exception:
                # result_summary 仅拼入通知正文（展示类），回落空串但留痕（§九展示类留痕）。
                report_degradation(
                    DegradationEvent.RESULT_SUMMARY_EXTRACT_FAILED,
                    category="display", source="task_scheduler.notify",
                    fallback="", exc_info=True,
                )
                result_summary = ""

        # 构造 body 文本
        task_prompt = definition.task_prompt if definition else ""
        body = task_prompt
        if result_summary:
            body = f"{task_prompt}\n\n执行结果：{result_summary}" if task_prompt else result_summary

        # level 由执行状态推导
        if execution.status == TaskExecutionStatus.FAILED:
            level = "error"
        elif execution.status == TaskExecutionStatus.CANCELLED:
            level = "warning"
        else:
            level = "info"

        # IPC 扁平 payload（与 ipc_transport.py TASK_NOTIFICATION 字段对齐）
        ipc_payload = {
            "task_id": execution.task_id,
            "title": definition.name if definition else execution.task_id,
            "body": body,
            "level": level,
        }

        try:
            if sensitivity in _SENSITIVE_LEVELS:
                if execution.source_channel_id:
                    # 敏感数据 → target_only（仅源 channel）
                    await self._broadcast.send(
                        NormalizedEvent(
                            event_type=EventType.TASK_NOTIFICATION,
                            payload=ipc_payload,
                            origin_channel_id="task_scheduler",
                        ),
                        target_channel_ids=(execution.source_channel_id,),
                    )
                    logger.info(
                        "_dispatch_task_notification: sensitive_data sent to channel=%s "
                        "execution_id=%s sensitivity=%s",
                        execution.source_channel_id, execution.execution_id, sensitivity,
                    )
                else:
                    # 降级广播（失败情况 4）
                    logger.warning(
                        "_dispatch_task_notification: sensitivity=%s but no source_channel_id, "
                        "downgrading to broadcast for execution_id=%s",
                        sensitivity, execution.execution_id,
                    )
                    await self._broadcast.send(
                        NormalizedEvent(
                            event_type=EventType.TASK_NOTIFICATION,
                            payload=ipc_payload,
                            origin_channel_id="task_scheduler",
                        )
                    )
            else:
                # LOW/MEDIUM → broadcast
                await self._broadcast.send(
                    NormalizedEvent(
                        event_type=EventType.TASK_NOTIFICATION,
                        payload=ipc_payload,
                        origin_channel_id=execution.source_channel_id or "task_scheduler",
                    )
                )
        except Exception as e:
            logger.error(
                "_dispatch_task_notification: failed to send notification "
                "execution_id=%s: %s",
                execution.execution_id, e,
            )

    # ──────────────────────────────────────────────
    # Internal Methods — Cron Scheduling
    # ──────────────────────────────────────────────

    def _register_cron_job(
        self,
        task_id: str,
        cron_expression: str,
        trigger_type: TriggerType = TriggerType.RECURRING,
    ) -> None:
        """计算下次 cron 触发时间，注册 asyncio 定时回调（I1）。

        时区约定：cron 字段统一按**本地时间**解释，与 scheduler_tools._resolve_cron 一致。

        ⚠️ 历史 bug（已修复）：
            原实现 `CronIter(cron_expression)` 不传 start_time，croniter 内部用
            `time.time()` 为基准且 tzinfo=None，会把 cron 字段当 UTC 解释。
            而 scheduler_tools.py 是用 `datetime.now()`（本地）生成字段，两端语义不一致，
            导致 cron "15:42" 实际触发漂到本地 23:42（UTC+8 偏移 8 小时）。
            修复：显式传本地 naive datetime 作为基准，让 croniter 走本地语义。

        BL5: croniter 未安装时记 ERROR 并返回，不阻断其他任务。
        """
        if not _CRONITER_AVAILABLE:
            logger.error(
                "_register_cron_job: croniter not installed, cron scheduling disabled "
                "for task_id=%s. Install with: pip install croniter",
                task_id,
            )
            return

        try:
            # 显式传本地 naive datetime 作为基准 → croniter 按本地时间解释 cron 字段。
            now_local = datetime.now()
            cron = CronIter(cron_expression, now_local)
            next_local: datetime = cron.get_next(datetime)  # 返回 naive datetime（本地）
            delay_s = (next_local - now_local).total_seconds()
            if delay_s < 0:
                delay_s = 0.0

            loop = asyncio.get_event_loop()
            handle = loop.call_later(
                delay_s,
                self._on_cron_fire,
                task_id,
                cron_expression,
                trigger_type,
            )
            self._cron_handles[task_id] = handle

            # INFO 级别：用户能直接在控制台看到下次触发时间，便于排查"没触发"问题
            logger.info(
                "_register_cron_job: task_id=%s type=%s expr=%r next_fire=%s (local) in %.1fs",
                task_id,
                trigger_type.value,
                cron_expression,
                next_local.strftime("%Y-%m-%d %H:%M:%S"),
                delay_s,
            )
        except Exception as e:
            # 失败情况 1: cron 表达式解析失败 → 跳过，BL4 失败隔离
            logger.error(
                "_register_cron_job: failed to parse cron_expression=%s for task_id=%s: %s",
                cron_expression, task_id, e,
            )

    def _unregister_cron_job(self, task_id: str) -> None:
        """取消并删除 _cron_handles[task_id]（幂等）。"""
        handle = self._cron_handles.pop(task_id, None)
        if handle is not None:
            handle.cancel()
            logger.debug("_unregister_cron_job: task_id=%s cancelled", task_id)

    def _on_cron_fire(
        self,
        task_id: str,
        cron_expression: str,
        trigger_type: TriggerType = TriggerType.RECURRING,
    ) -> None:
        """cron 定时回调（同步，由 asyncio.call_later 触发）。

        1. 移除当前句柄（已触发）
        2. 异步触发执行
        3. RECURRING → 注册下一次触发；ONESHOT → 注销任务定义（含持久化删除）
        """
        logger.info(
            "_on_cron_fire: task_id=%s type=%s fired at %s",
            task_id,
            trigger_type.value,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        self._cron_handles.pop(task_id, None)

        if trigger_type == TriggerType.ONESHOT:
            # 一次性任务：先执行完成，再自动注销（避免删除定义和加载定义的竞态）
            async def _execute_oneshot() -> None:
                if task_id in self._running_executions:
                    logger.warning(
                        "_on_cron_fire: ONESHOT task_id=%s already_running, skipping", task_id
                    )
                    return
                execution_id = str(uuid.uuid4())
                # BL4: _execute_task 内部做失败隔离
                await self._execute_task(task_id, execution_id, source_channel_id=None)
                await self.unregister_task_definition(task_id)
                logger.info(
                    "_on_cron_fire: ONESHOT task_id=%s auto-unregistered after fire", task_id
                )
            asyncio.ensure_future(_execute_oneshot())
        else:
            # 异步触发执行（BL4: 失败隔离，ensure_future 不 await）
            asyncio.ensure_future(self._trigger_scheduled_execution(task_id, source_channel_id=None))
            # 周期性 cron → 注册下一次触发
            self._register_cron_job(task_id, cron_expression, trigger_type=trigger_type)

    # ──────────────────────────────────────────────
    # Internal Methods — Event Handlers
    # ──────────────────────────────────────────────

    def _register_event_handler(self, task_id: str, event_name: str) -> None:
        """注册事件 → 任务映射（运行时构建）。"""
        if event_name not in self._event_handlers:
            self._event_handlers[event_name] = []
        if task_id not in self._event_handlers[event_name]:
            self._event_handlers[event_name].append(task_id)
        logger.debug(
            "_register_event_handler: event=%s → task_id=%s", event_name, task_id
        )

    def _unregister_event_handler(self, task_id: str) -> None:
        """从所有事件映射中移除 task_id（幂等）。"""
        for event_name, task_ids in self._event_handlers.items():
            if task_id in task_ids:
                task_ids.remove(task_id)
        # 清理空列表
        empty_events = [k for k, v in self._event_handlers.items() if not v]
        for k in empty_events:
            del self._event_handlers[k]

    # ──────────────────────────────────────────────
    # Internal Methods — Cancel & Recovery
    # ──────────────────────────────────────────────

    async def _cancel_running_execution(self, task_id: str) -> None:
        """取消正在运行的 asyncio.Task（BL1: 已完成则静默幂等）。"""
        running_task = self._running_executions.get(task_id)
        if running_task is None:
            return
        if running_task.done():
            return
        running_task.cancel()
        try:
            await running_task
        except (asyncio.CancelledError, Exception):
            pass  # 正常，取消完成
        logger.info("_cancel_running_execution: task_id=%s cancelled", task_id)

    async def _restore_pending_executions(self) -> None:
        """重启恢复（场景 6，BL4: 失败隔离）。

        - RUNNING 状态（上次崩溃中断）→ 直接标为 FAILED（AgentLoop 中间状态不可安全重建）
        - PENDING 状态（已创建未执行）→ 重新触发（新建 execution_id）
        """
        try:
            stale_executions = await self._task_repo.find_all_pending_task_executions()
        except Exception as e:
            logger.error("_restore_pending_executions: query failed: %s", e)
            return

        running_count = 0
        pending_count = 0

        for exec_record in stale_executions:
            if exec_record.status == TaskExecutionStatus.RUNNING:
                # 失败情况 6: 崩溃中断的 running → FAILED
                try:
                    await self._task_repo.update_task_execution_status(
                        exec_record.execution_id, TaskExecutionStatus.FAILED
                    )
                    running_count += 1
                    logger.warning(
                        "_restore_pending_executions: execution_id=%s task_id=%s "
                        "marked FAILED on restart",
                        exec_record.execution_id, exec_record.task_id,
                    )
                except Exception as e:
                    logger.error(
                        "_restore_pending_executions: failed to mark execution %s as failed: %s",
                        exec_record.execution_id, e,
                    )

            elif exec_record.status == TaskExecutionStatus.PENDING:
                # pending → 重新触发（新 execution_id）
                try:
                    asyncio.ensure_future(
                        self._execute_task(
                            exec_record.task_id,
                            str(uuid.uuid4()),  # 新 execution_id
                            exec_record.source_channel_id,
                        )
                    )
                    pending_count += 1
                    logger.info(
                        "_restore_pending_executions: re-triggering pending task_id=%s",
                        exec_record.task_id,
                    )
                except Exception as e:
                    logger.error(
                        "_restore_pending_executions: failed to re-trigger task_id=%s: %s",
                        exec_record.task_id, e,
                    )

        logger.info(
            "_restore_pending_executions: recovered_pending=%d, failed_running=%d",
            pending_count, running_count,
        )

    async def _load_and_register_all_tasks(self) -> None:
        """从 Storage 全量加载任务定义并注册调度引擎（BL4: 单任务注册失败不阻断整体）。"""
        try:
            definitions = await self._task_repo.find_all_task_definitions()
        except Exception as e:
            logger.error("_load_and_register_all_tasks: query failed: %s", e)
            return

        for definition in definitions:
            try:
                trigger_rule = self._parse_trigger_rule(definition.trigger_rule_json)
                if trigger_rule is None:
                    logger.error(
                        "_load_and_register_all_tasks: skipping task_id=%s "
                        "(invalid trigger_rule_json)",
                        definition.task_id,
                    )
                    continue

                if trigger_rule.trigger_type in (TriggerType.RECURRING, TriggerType.ONESHOT):
                    if trigger_rule.cron_expression:
                        self._register_cron_job(
                            definition.task_id,
                            trigger_rule.cron_expression,
                            trigger_type=trigger_rule.trigger_type,
                        )
                elif trigger_rule.trigger_type == TriggerType.EVENT:
                    if trigger_rule.event_name:
                        self._register_event_handler(
                            definition.task_id, trigger_rule.event_name
                        )
                # MANUAL — 无需注册
            except Exception as e:
                # BL4: 单任务注册失败不阻断其他任务（失败情况 1）
                logger.error(
                    "_load_and_register_all_tasks: failed to register task_id=%s: %s",
                    definition.task_id, e,
                )

        logger.info(
            "_load_and_register_all_tasks: loaded %d task definitions", len(definitions)
        )

    # ──────────────────────────────────────────────
    # Internal Methods — Helpers
    # ──────────────────────────────────────────────

    def _get_task_timeout_minutes(self) -> float:
        """读取任务超时配置（BL5: 失败时 fallback=30.0 min）。"""
        try:
            config = self._config_manager.get_system_config()
            value = getattr(config, "task_timeout_minutes", None)
            if value is not None and isinstance(value, (int, float)) and value > 0:
                return float(value)
        except Exception as e:
            logger.debug("_get_task_timeout_minutes: config read failed: %s", e)
        return _DEFAULT_TASK_TIMEOUT_MINUTES

    @staticmethod
    def _parse_trigger_rule(trigger_rule_json: str) -> Any | None:
        """将 JSON 字符串反序列化为 TriggerRule（BL5: 解析失败返回 None）。"""
        from pandapal.task_scheduler.models import TriggerRule

        try:
            data = json.loads(trigger_rule_json)
            raw_type = data.get("trigger_type", "")
            return TriggerRule(
                trigger_type=TriggerType(raw_type),
                cron_expression=data.get("cron_expression"),
                event_name=data.get("event_name"),
            )
        except Exception as e:
            logger.error(
                "_parse_trigger_rule: failed to parse trigger_rule_json=%r: %s",
                trigger_rule_json, e,
            )
            return None

    @staticmethod
    def _validate_cron_expression(cron_expression: str) -> None:
        """注册前的 cron 语法校验。

        Raises:
            ValueError: 表达式语法不合法。
            RuntimeError: croniter 未安装（部署/环境问题）。

        croniter 缺失会让整个 cron 子系统失效——这不是单个任务问题，
        必须让上层（tool）感知到，向用户回显部署提示，而不是悄悄存盘。
        """
        if not _CRONITER_AVAILABLE:
            raise RuntimeError(
                "croniter 未安装，定时任务子系统不可用。"
                "请联系运维：pip install croniter"
            )
        try:
            CronIter(cron_expression)
        except Exception as e:
            raise ValueError(
                f"invalid cron expression {cron_expression!r}: {e}"
            ) from e

    @staticmethod
    def _serialize_result(result: Any) -> str | None:
        """将 AgentResult 序列化为 JSON 字符串存入 TaskExecution.result_json。"""
        if result is None:
            return None
        try:
            if hasattr(result, "__dict__"):
                return json.dumps(result.__dict__, ensure_ascii=False, default=str)
            elif isinstance(result, dict):
                return json.dumps(result, ensure_ascii=False, default=str)
            else:
                return json.dumps({"result": str(result)}, ensure_ascii=False)
        except Exception:
            return json.dumps({"result": str(result)}, ensure_ascii=False)

    @staticmethod
    def _iso_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    async def _broadcast_task_changed(
        self,
        definition: TaskDefinition | None,
        *,
        change_type: str,
        task_id: str | None = None,
    ) -> None:
        """D2 Push 增量：广播单个定时任务的变更。

        Args:
            definition: 任务定义（deleted 时为 None）
            change_type: "created" | "updated" | "deleted"
            task_id: deleted 时必须提供（definition 为 None）
        """
        try:
            tid = task_id or (definition.task_id if definition else "")
            task_dict: dict[str, Any] = {"task_id": tid}

            if definition is not None:
                trigger_rule = self._parse_trigger_rule(definition.trigger_rule_json)
                trigger_type_val = trigger_rule.trigger_type.value if trigger_rule else "manual"
                cron_expr = trigger_rule.cron_expression if trigger_rule else ""
                task_dict.update({
                    "name": definition.name,
                    "trigger_type": trigger_type_val,
                    "cron_expression": cron_expr or "",
                    "task_prompt": definition.task_prompt or "",
                    "session_id": definition.session_id or "",
                    "sensitivity": definition.sensitivity or "medium",
                    "created_at": definition.created_at.isoformat() if definition.created_at else "",
                })

            event = NormalizedEvent.scheduled_task_changed(
                task=task_dict,
                change_type=change_type,
            )
            await self._broadcast.send(event)
            logger.debug(
                "_broadcast_task_changed: %s task_id=%s", change_type, tid,
            )
        except Exception as e:
            # D2 推送是增量优化，失败不影响主流程
            logger.warning(
                "_broadcast_task_changed: failed for task_id=%s: %s",
                task_id or (definition.task_id if definition else "?"), e,
            )
