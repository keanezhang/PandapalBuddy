"""pandaren/engine/run_core.py — 统一执行内核

设计目标
--------
将 run.py（RunMixin）和 run_stream.py（RunStreamMixin）中平行的 8 Phase 逻辑
合并为单一 async generator `_run_stream_core()`。
run.py 和 run_stream.py 均已废弃，由本文件完全替代。

外部 API 不变：
  - run_stream() → passthrough，直接 yield 内核事件
  - run()        → 消费内核生成器，从最终 RUN_END 事件中取 AgentResult

架构原则
--------
HC3：permission_guard.check_permission() 和 hitl_controller.check_approval()
     在主路径中硬编码调用，不通过 hook。
HC4：audit_log.write_sync() 在所有关键节点同步写入，AuditWriteError 向外传播。
HC5：for range(max_steps) 有界循环 + StepCounter（只增不减）。
HC6：sensitivity >= CRITICAL 时无视 auto_confirm_high。
O3 ：_run_stream_core() 内部捕获所有异常并通过 RUN_END 事件传达；
     run() 额外用外层 try/except 兜底，永远返回 AgentResult。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

from .types import TerminalReason
from ..cancellation import CancelToken, CancelledSignal
from .models import AgentResult, RunState, StepRecord
from .stream import StreamEvent, StreamEventType
from .step_counter import StepCounter

from ..llm.exceptions import (
    LLMAuthError,
    LLMRequestError,
    LLMRateLimitError,
    LLMServerError,
    LLMNetworkError,
)
from ..llm.types import ModelSettings
from ..tool.definition.context import ToolContext
from ..tool.definition.tool_result import COMPOSITE_SOURCE, ToolFeedback, ToolResult
from ..observability.types import AuditEventType, AuditSeverity, generate_id
from ..observability.exceptions import AuditWriteError
from ..memory.models import MemorySnapshot
from ..behavior.hitl_controller import PendingApproval, PendingInteraction
from ..behavior.step_guard import StepUsage

logger = logging.getLogger("pandaren.engine.loop")

# Layer 2 工具取消宽限期（秒）：取消胜出后，给 cancel-aware 工具优雅收尾的时间。
# 太短 → 工具来不及释放锁/回滚；太长 → 用户感知「还没停」。见契约 §10。
CANCEL_GRACE_SECONDS = 2.0

# ──────────────────────────────────────────────────────────────────────────────
# RUN_END data 结构约定（供 run() 消费）
# {
#   "result": AgentResult,   ← 完整对象，run() 从此取值
#   "success": bool,         ← 冗余字段，方便流式消费方
#   "output": str | None,
#   "error": str | None,
#   "terminal_reason": str | None,
#   "total_steps": int,
#   "total_input_tokens": int,
#   "total_output_tokens": int,
#   "paused": bool,          ← HITL PAUSE 时为 True
#   "run_state": RunState | None,
# }
# ──────────────────────────────────────────────────────────────────────────────


def _tool_data_to_text(data: Any) -> str:
    """ToolResult.data → LLM 历史可读文本。

    处理 dict 时用 json.dumps 而非 str()，避免 \\n 转义问题。

    data 默认值为 ""。空/None 数据返回 "(空)" 而非空串：
    部分 LLM API 会拒绝 tool message 的空 content，非空占位符
    是兼容性保底。占位符用 "(空)" 而非 "[OK]"，避免误导 LLM。
    """
    if data is None:
        return "OK"
    if data == "":
        return "OK"
    if isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False)
    return str(data)


def render_tool_result_for_llm(result: ToolResult) -> str:
    """ToolResult → 写入 memory 的 tool 消息文本（工具结果的统一渲染口）。

    只用于**工具结果**的组装。halt / 超时 / plan exit_msg 等写的是错误或终止
    文本而非工具结果，不走此函数（见设计 §Step 6 消费方 2）。

    硬不变量：`result.feedback is None` 时，输出与重构前的内联表达式**逐字节
    相等**——由 engine/tests/test_render_tool_result.py 锁死。这是本函数唯一
    触及「所有工具输出」的地方，任何格式漂移都会改变每一个工具给 LLM 看的文本。

    有反馈时，反馈段**前置**于原始 result_text。这不是排版偏好：
    Memory.add_tool_result 入口的 MicroCompact 单条截断切的是**尾部**，反馈若
    放尾部会在长结果下被静默切掉，门控看起来「跑了但 Agent 没反应」。宁可丢掉
    diff 回显（Agent 自己刚写的内容，它已知）也不能丢掉诊断（它不知道的新信息）。

    `llm_visible=False` 的反馈在此**整段跳过**：那是给用户屏幕的状态播报
    （如「检查通过」），对 LLM 是纯噪音 —— 它不需要为「什么都没发生」付 token。
    跳过后输出与无反馈时逐字节相同，故上面的硬不变量对它同样成立。
    """
    base = (
        _tool_data_to_text(result.data) if result.success
        else (result.error or "Error")
    )
    fb = result.feedback
    if fb is None or not fb.llm_visible:
        return base
    # composite = 多源合并，text 里每段已自带 [source] 标签，再加外层就成了
    # `[composite] [code_quality_gate] ...` 的双重前缀，且 "composite" 对 LLM 不含信息。
    prefix = "" if fb.source == COMPOSITE_SOURCE else f"[{fb.source}] "
    return f"{prefix}{fb.text}\n---\n{base}"


def feedback_to_event_data(fb: ToolFeedback | None) -> dict[str, str] | None:
    """ToolFeedback → TOOL_CALL_END 事件的 `feedback` 字段（无反馈时透传 None）。

    与 render_tool_result_for_llm 是**并列的两个消费方**，服务不同受众：

        render_tool_result_for_llm → 拼进 tool 消息文本 → 给 **LLM** 读
        feedback_to_event_data     → 进 StreamEvent     → 给 **用户的屏幕** 看

    两条通路谁也不是谁的转发。此前只有前者，门控的结论进得了 LLM 的上下文却到不了
    用户眼前 —— 「管得住」有了，「看得见」缺了一半。本函数补的就是这一半。

    发**结构化三元组**而非拼好的串：UI 要按 severity 渲染角标、按 source 溯源，
    拿到一坨 "[code_quality_gate] 该文件有 15 个 error：..." 只能整段塞进 DOM。
    LLM 那边正相反 —— 纯文本才好读。同一份数据，两种形状，各给各的。

    severity 发**小写名字**而非 IntEnum 数值：跨进程 JSON 里 "error" 自解释，
    发 3 则要求读者手边有枚举定义才看得懂。
    """
    if fb is None:
        return None
    return {
        "text":     fb.text,
        "severity": fb.severity.name.lower(),
        "source":   fb.source,
    }


def tool_call_end_data(tool_call_id: str, args: dict, result: ToolResult) -> dict[str, Any]:
    """TOOL_CALL_END 事件的 data 载荷 —— run_core **两处**发射点（普通路径 / HITL
    resume 路径）共用的唯一构造口。

    提成函数而非在两处各写一遍字典字面量：这两处必须逐字段同步，而漏改一处的症状是
    「普通路径能看见、HITL 恢复后看不见」——只在特定路径复现的幽灵 bug。同时字面量
    散落时没有任何单测抓得住它们（测试只能复刻一份字典自己测自己，是假绿灯）。

    `result` 只读，本函数不改它。

    字段语义：
      result   —— 工具**自己**产出的数据。失败时为 None（错误走 error 字段）。
      feedback —— **第三方**（ToolFeedbackProvider）对这次调用的评价，或 None。
                  不按 success 门控（与 result 不同）：provider 对失败的工具同样
                  可以有话说（如密钥扫描）。挂没挂由 provider 定，不由这里猜。
    """
    return {
        "tool_call_id": tool_call_id,
        "result":       result.data if result.success else None,
        "tool_args":    args,
        "success":      result.success,
        "error":        result.error,
        "feedback":     feedback_to_event_data(result.feedback),
    }


class RunCoreMixin:
    """提供统一执行内核 _run_stream_core()，以及 run_stream() / run() / _safe_hook() 的完整实现。

    继承时需要 AgentLoop 已提供所有安全关键属性（_identity, _llm_client, etc.）。
    本 Mixin 不定义 __slots__，依赖 AgentLoop.__slots__ 覆盖。

    无外部 Mixin 依赖：_safe_hook() 直接定义在本类中，不再依赖 RunStreamMixin。
    """

    __slots__ = ()

    # ═══════════════════════════════════════════════════════════════════════════
    # 公共 API：run_stream()  ← passthrough
    # ═══════════════════════════════════════════════════════════════════════════

    async def run_stream(
        self,
        task: str,
        *,
        session_id: str,
        resume_state: RunState | None = None,
        metadata: dict | None = None,
        hitl_decision: str | None = None,
        interaction_response: str | None = None,
        skill_name: str | None = None,
        plan_action: str | None = None,
        edited_plan_content: str | None = None,
        settings: ModelSettings | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """流式入口（async generator）。逐个 yield StreamEvent。

        用法::

            async for event in loop.run_stream("帮我重构这个函数"):
                match event.type:
                    case StreamEventType.LLM_TOKEN:
                        print(event.data["delta"], end="", flush=True)
                    case StreamEventType.TOOL_CALL_START:
                        print(f"\\n🔧 {event.tool_name}")
                    case StreamEventType.PERMISSION_DENIED:
                        print(f"\\n⛔ 权限拒绝: {event.tool_name}")
                    case StreamEventType.HITL_REQUESTED:
                        run_state = event.data["run_state"]
                    case StreamEventType.INTERACTION_REQUESTED:
                        run_state = event.data["run_state"]
                    case StreamEventType.PLAN_APPROVAL_REQUESTED:
                        plan = event.data["plan_content"]
                    case StreamEventType.RUN_END:
                        result = event.data["result"]   # AgentResult

        resume 时传入 hitl_decision="approved"|"rejected" 以传递审批结果。
        传入 interaction_response="..." 以传递交互型工具的用户回复。
        Plan Mode 由 LLM 自主决策：调用 enter_plan_mode 进入规划，exit_plan_mode 提交计划。
        """
        async for event in self._run_stream_core(
            task=task,
            resume_state=resume_state,
            metadata=metadata,
            session_id=session_id,
            hitl_decision=hitl_decision,
            interaction_response=interaction_response,
            skill_name=skill_name,
            plan_action=plan_action,
            edited_plan_content=edited_plan_content,
            settings=settings,
        ):
            yield event

    # ═══════════════════════════════════════════════════════════════════════════
    # 公共 API：run()  ← drain 消费 _run_stream_core()
    # ═══════════════════════════════════════════════════════════════════════════

    async def run(
        self,
        task: str,
        *,
        session_id: str,
        resume_state: RunState | None = None,
        metadata: dict | None = None,
        hitl_decision: str | None = None,
        interaction_response: str | None = None,
        skill_name: str | None = None,
        settings: ModelSettings | None = None,
    ) -> AgentResult:
        """非流式入口。永远返回 AgentResult，不向外抛异常（O3）。

        resume 时传入 hitl_decision="approved"|"rejected" 以传递审批结果。
        传入 interaction_response="..." 以传递交互型工具的用户回复。
        Plan Mode 由 LLM 自主决策：调用 enter_plan_mode 进入规划，exit_plan_mode 提交计划。
        """
        run_id = resume_state.run_id if resume_state else generate_id()  # 兜底路径也需要合法 ID
        started_at = datetime.now(timezone.utc)
        start_mono = time.monotonic()

        try:
            gen = self._run_stream_core(
                task=task,
                resume_state=resume_state,
                metadata=metadata,
                session_id=session_id,
                hitl_decision=hitl_decision,
                interaction_response=interaction_response,
                skill_name=skill_name,
                settings=settings,
            )
            try:
                async for event in gen:
                    if event.type == StreamEventType.RUN_END:
                        # 正常路径：_run_stream_core 在每个终止点都发出 RUN_END，
                        # data["result"] 是完整 AgentResult。
                        return event.data["result"]
            finally:
                # 确保 generator 的 finally 块（on_run_end / memory flush）被执行，
                # 即使 run() 在收到 RUN_END 后提前 return 也能正确关闭。
                await gen.aclose()

            # 理论上走不到这里（_run_stream_core 保证每条路径都发 RUN_END）
            logger.error("_run_stream_core ended without RUN_END event — this is a bug")
            return self._build_result(
                success=False,
                run_id=run_id or generate_id(),
                error="Internal error: generator ended without RUN_END",
                terminal_reason=TerminalReason.LLM_ERROR,
                steps=[],
                started_at=started_at,
                start_mono=start_mono,
            )

        except Exception as e:
            # O3 最外层兜底（理论上 _run_stream_core 已经捕获了所有异常）
            logger.error("Unexpected error draining _run_stream_core: %s", e, exc_info=True)
            return self._build_result(
                success=False,
                run_id=run_id or generate_id(),
                error=f"Unexpected error: {e}",
                terminal_reason=TerminalReason.LLM_ERROR,
                steps=[],
                started_at=started_at,
                start_mono=start_mono,
            )

    # ═══════════════════════════════════════════════════════════════════════════
    # 私有：唯一执行内核
    # ═══════════════════════════════════════════════════════════════════════════

    def _audit(
        self,
        event_type: AuditEventType,
        *,
        agent_id: str,
        run_id: str,
        detail: str,
        step_n: int | None = None,
        tool_name: str | None = None,
        terminal_reason: str | None = None,
        severity: AuditSeverity | None = None,
    ) -> None:
        """审计写入包装：自动透传当前 run 的 session_id，供后端按 session 分片存储。

        session_id 从 self._current_session_id 读取（由 _run_stream_core 入口设置），
        保证一次 run 内所有审计事件的 session_id 一致；跨 run 复用同一 AgentLoop
        实例时（同 session 顺序执行）也天然正确。
        """
        self._audit_log.write_sync(
            event_type,
            agent_id=agent_id,
            run_id=run_id,
            detail=detail,
            session_id=getattr(self, "_current_session_id", "") or "",
            step_n=step_n,
            tool_name=tool_name,
            terminal_reason=terminal_reason,
            severity=severity,
        )

    async def _execute_tools_with_cancel_race(
        self,
        calls: list[dict],
        ctx: Any,
        remaining: float,
        step_n: int,
    ) -> list:
        """Layer 2：工具执行 vs 取消闸门竞速（见取消语义-契约.md §3.5）。

        主路径 / HITL resume / Interaction resume 三处共用的单一实现，防止各自
        散写导致漂移（本方法抽取前，resume 两路走的是裸 asyncio.wait_for，
        取消在工具执行期间从不生效——STOP 无法打断 in-flight 工具）。

        语义：
          - 工具先完成 → 返回 tool_results（即便取消也已到，此步结果有效，
            收尾交给下一步循环头 Layer 0）。
          - 取消先到 → grace 期等 cancel-aware 工具优雅收尾，仍不返回则强杀
            task + 打 orphaned 标记 + 审计留痕，统一抛 CancelledSignal(phase='tool')，
            由本 step 的 except CancelledSignal 分支收口（in-flight 结果不入 memory）。
          - 两者都没完成（step_timeout 兜底）→ 抛 asyncio.TimeoutError。

        Args:
            calls: [{"name": ..., "args": ...}, ...]，透传给 execute_tools_concurrent。
            ctx: ToolContext（其 metadata 必须已注入 cancel_token）。
            remaining: 本 step 剩余可用时间（秒），由 step_timeout 派生（HC5）。
            step_n: 当前 step 序号（仅用于日志）。

        Returns:
            tool_results 列表（与 calls 一一对应）。
        """
        tools_task = asyncio.ensure_future(
            self._harness_executor.execute_tools_concurrent(calls, ctx)
        )
        cancel_wait = asyncio.ensure_future(self._cancel_token.wait())
        try:
            done, _pending = await asyncio.wait(
                {tools_task, cancel_wait},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            # cancel_wait 若未完成必须清理，避免协程泄漏 / "Task was destroyed
            # but it is pending" 告警（契约 §10）。
            if not cancel_wait.done():
                cancel_wait.cancel()

        if not done:
            # step_timeout 兜底：两者都没完成 → 取消工具 task 后走原 TimeoutError 路径
            tools_task.cancel()
            try:
                await tools_task
            except (asyncio.CancelledError, Exception):
                pass
            raise asyncio.TimeoutError()

        if tools_task in done:
            # 正常路径：工具先完成（即便取消也已到，此步结果有效，
            # 收尾在下一步头/本轮 Layer 0）
            return tools_task.result()

        # 取消先到、工具仍在跑：给 grace 期等 cancel-aware 工具优雅返回
        logger.info(
            "[cancel] Layer2 · cancel WON tool race @step=%d · tools=%s · entering %.1fs grace",
            step_n, [c["name"] for c in calls], CANCEL_GRACE_SECONDS,
        )
        _orphaned_tools: list[str] = []
        try:
            await asyncio.wait_for(tools_task, timeout=CANCEL_GRACE_SECONDS)
            logger.info("[cancel] tools finished gracefully within grace period")
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # grace 内未返回 → 强杀 task（cancel-aware 支持；纯阻塞工具无效）
            tools_task.cancel()
            try:
                await tools_task
            except (asyncio.CancelledError, Exception):
                pass
            _orphaned_tools = [c["name"] for c in calls]
            logger.warning(
                "[cancel] tools did not finish within %.1fs grace, "
                "marking orphaned=%s (may still run in background)",
                CANCEL_GRACE_SECONDS, _orphaned_tools,
            )
        # 无论 grace 是否成功，取消已发生 → 统一抛 CancelledSignal，
        # 由 except CancelledSignal 分支收口。in-flight 工具结果不入 memory。
        _cancel_sig = CancelledSignal(
            self._cancel_token.reason or "Cancelled by user"
        )
        _cancel_sig.phase = "tool"          # type: ignore[attr-defined]
        _cancel_sig.orphaned_tools = _orphaned_tools  # type: ignore[attr-defined]
        raise _cancel_sig

    async def _commit_tool_step_atomically(
        self,
        assistant_content: str | None,
        tool_calls: list[dict],
        reasoning_content: str | None,
        results: list[tuple[str, str, str]],
    ) -> None:
        """原子提交一个 tool step：assistant(tool_calls) + 全部 tool 结果按序写入。

        API 硬约束：assistant 带 tool_calls 时，其后必须跟足 tool 消息响应每个
        tool_call_id。主路径若"先写 assistant、后边执行边写结果"，任何中断
        （STOP / step 超时 / step 异常）都会在 memory 留下孤儿 tool_call，
        下一轮 LLM 调用直接 400。本方法把写入收敛为一步：
        **所有结果齐备后才提交**，中断发生在提交前 → memory 干净。
        与 HITL/Interaction resume 路径（暂停后原子写入）语义一致。

        Args:
            assistant_content: LLM 本轮的 content。
            tool_calls: 归一化后的完整 tool_calls（每个元素必须含非空 id）。
            reasoning_content: LLM 推理内容（可选）。
            results: [(tool_call_id, tool_name, result_text), ...]，必须覆盖
                     tool_calls 中全部 id（缺失由调用方补占位）。
        """
        await self._memory.add_assistant_message(
            assistant_content,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
        )
        for tc_id, tc_name, text in results:
            await self._memory.add_tool_result(tc_id, tc_name, text)

    def _resolve_run_llm(
        self, settings: "ModelSettings | None"
    ) -> "tuple[Any, str, str]":
        """解析本 run 生效的 LLM settings / model_name / provider（per-run 覆盖）。

        字段级叠加：per-run `settings` 只覆盖 `target_model`（LLMRouter 路由键），
        其余调参一律继承构造期的 `self._llm_settings`。settings 为 None 或未带
        target_model 时，返回值与改造前完全一致（无回归）。

        model_name / provider 取「路由后实际生效的 client」——使 LLM_CALL 事件、
        Tracer span、审计与预算归属反映真正被路由到的模型，而非 Router 的 default。
        单 client（非 Router）场景无 resolve 方法，退回读 self._llm_client 自身。
        """
        base = self._llm_settings
        target_model = getattr(settings, "target_model", None) if settings is not None else None
        if target_model:
            effective = settings if base is None else dataclasses.replace(base, target_model=target_model)
        else:
            effective = base

        client = self._llm_client
        resolve = getattr(self._llm_client, "resolve", None)
        if callable(resolve):
            try:
                client = resolve(effective)
            except Exception:  # noqa: BLE001 — 解析失败退回默认 client，绝不炸断 run（O3）
                client = self._llm_client
        model = getattr(client, "model_name", "") or ""
        provider = getattr(client, "provider", "") or ""
        return effective, model, provider

    async def _run_stream_core(
        self,
        task: str,
        *,
        session_id: str,
        resume_state: RunState | None = None,
        metadata: dict | None = None,
        hitl_decision: str | None = None,
        interaction_response: str | None = None,
        skill_name: str | None = None,
        plan_action: str | None = None,
        edited_plan_content: str | None = None,
        settings: "ModelSettings | None" = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """8 Phase 单一执行体。

        所有终止路径均保证 yield RUN_END，data["result"] = AgentResult。
        异常不逃逸（O3）——AuditWriteError / Exception 都被捕获并转为事件。

        yield 顺序（正常单步）::

            RUN_START
            STEP_START
              LLM_CALL_START
              LLM_TOKEN × N          ← 真增量路径
              LLM_CALL_END
              [PERMISSION_DENIED × M]
              [TOOL_CALL_START × K]
              [TOOL_CALL_END   × K]
            STEP_END
            ...（循环）
            RUN_END                  ← 携带完整 AgentResult
        """
        # ─── 重建取消令牌：确保每次新 run 从干净状态开始 ───
        # 如果上一个 run 被 cancel() 后、finally 块尚未执行完时新一轮
        # run 就开始了，旧 token 可能仍为 cancelled，导致新 run 立即退出。
        # 重建全新 token（而非复位）可天然规避跨 run 的取消泄漏。
        self._cancel_token = CancelToken()

        # ─── Layer 3：子 Agent 级联取消（见契约 §3.6 方案 B）───
        # 委派方（sub_agent registry）通过 metadata["parent_cancel_token"] 把父 token
        # 传入。★ 必须在上面「重建 token」之后 link，否则 link 会被重建覆盖。
        # 父取消 → 子 token 同步取消 → 子 Layer 0/1/2 检查点全部生效 → 子协作式退出。
        # 监听 task 在本函数 finally 中取消，解除父子链，防止实例复用时跨 run 误取消。
        _parent_cancel_token = (metadata or {}).get("parent_cancel_token")
        _cascade_monitor_task = (
            self._cancel_token.link_parent(_parent_cancel_token)
            if _parent_cancel_token is not None
            else None
        )
        if _cascade_monitor_task is not None:
            logger.info(
                "[cancel] Layer3 · sub-agent linked to parent cancel token · agent_id=%s",
                getattr(self._identity, "agent_id", "?"),
            )

        # ─── 刷新 static_context：每次 run 入口重新从 Registry 拉取 ───
        # 确保 Skill/Tool 增删后下次对话自动生效，无需重建 Agent 实例。
        self._static_context_str = self._build_static_context()

        # ─── per-run 模型选择：解析本 run 生效的 settings/model/provider ───
        # 字段级叠加（只覆盖 target_model），并按路由后的实际 client 读 model/provider，
        # 使后续 LLM 调用、LLM_CALL 事件、Tracer、审计、预算归属全部反映所选模型。
        # settings 为 None 时三者与改造前完全一致（无回归）。
        _effective_settings, _effective_model, _effective_provider = self._resolve_run_llm(settings)

        # ─── session_id 校验 ───
        if not session_id or not session_id.strip():
            raise ValueError(
                "session_id is required and cannot be empty. "
                "Please provide a session identifier for multi-turn state management."
            )
        # ★ 数据隔离：本次 run 绑定 session_id，_audit/_tracer/_logger 从这里读
        #   多 Session 并发下，每个 session 各自的 AgentLoop 实例互不干扰。
        self._current_session_id = session_id
        run_id = resume_state.run_id if resume_state else generate_id()
        # raw_log 的 run 上下文：run 起始阶段（含 task/user 消息）step=None；
        # 每个 step 开始时再更新为 step_n。使 raw_log 每条带 (run_id, step)，
        # 供离线分析与 traces 的 llm_call 按 key join（多 run/多会话不错位）。
        self._memory.set_run_context(run_id, None)
        started_at = datetime.now(timezone.utc)
        start_mono = time.monotonic()
        step_records: list[StepRecord] = []
        total_input_tokens = 0
        total_output_tokens = 0
        consecutive_failures = 0
        consecutive_permission_denied_rounds = 0
        run_success = False
        run_terminal_reason: str = ""  # 传给 on_run_end，空字符串=正常完成

        # HITL resume 路径：approved 时保存待执行的 PendingApproval，
        # 进入循环后在第一个 step 直接执行，不需要等 LLM 重新生成 tool_call
        _hitl_resume_pending: PendingApproval | None = None

        # Interaction resume 路径：用户回复后保存待执行的 PendingInteraction
        _interaction_resume_pending: PendingInteraction | None = None
        _interaction_user_response: str | None = None

        # 模型循环检测状态（场景 10）
        recent_tool_calls: list[tuple[str, str]] = []
        loop_correction_count = 0

        agent_id = self._identity.agent_id

        # ── 内部辅助：构造 StreamEvent ──────────────────────────────────────
        def _mk(
            evt_type: StreamEventType,
            data: Any = None,
            step_n: int = -1,
            tool_name: str | None = None,
        ) -> StreamEvent:
            return StreamEvent(
                type=evt_type,
                data=data,
                run_id=run_id,
                agent_id=agent_id,
                step_n=step_n,
                tool_name=tool_name,
            )

        # ── 内部辅助：构造携带 AgentResult 的 RUN_END 事件 ─────────────────
        def _run_end(result: AgentResult, paused: bool = False) -> StreamEvent:
            return _mk(
                StreamEventType.RUN_END,
                data={
                    "result": result,
                    # 以下冗余字段方便流式消费方直接读取，无需拆 AgentResult
                    "success": result.success,
                    "output": result.output,
                    "error": result.error,
                    "terminal_reason": result.terminal_reason.value if result.terminal_reason else None,
                    "total_steps": result.total_steps,
                    "total_input_tokens": result.total_input_tokens,
                    "total_output_tokens": result.total_output_tokens,
                    "paused": paused,
                    "run_state": result.run_state,
                },
            )

        try:
            # ── HC4：审计 run_started ──────────────────────────────────────
            self._audit(
                AuditEventType.RUN_STARTED,
                agent_id=agent_id,
                run_id=run_id,
                detail=f"Task: {(task or '')[:200]}",
            )
            self._safe_hook("on_run_start", task, run_id)

            yield _mk(StreamEventType.RUN_START, data={"task": (task or "")[:200], "run_id": run_id})

            # ── 初始化上下文 ───────────────────────────────────────────────
            if resume_state:
                # ═══ HITL Resume 路径 ═══
                # 从 RunState.metadata 恢复 PendingApproval
                pending = self._rebuild_pending_approval(resume_state)

                if hitl_decision and pending:
                    resume_decision = self._hitl_controller.resolve_resume(
                        hitl_decision, pending,
                    )

                    if resume_decision.action == "reject_and_halt":
                        # 恢复 Memory session 状态，确保 finally 中 end_session() 能正常落盘
                        pause_snapshot = MemorySnapshot(
                            messages=tuple(resume_state.messages),
                        )
                        self._memory.resume_context(
                            pause_snapshot,
                            session_id=resume_state.session_id,
                        )
                        # 拒绝：写审计 → 终止
                        self._audit(
                            AuditEventType.HITL_REJECTED,
                            agent_id=agent_id,
                            run_id=run_id,
                            detail=f"HITL rejected: {pending.tool_name}",
                            terminal_reason=TerminalReason.HITL_REJECTED.value,
                        )
                        self._audit(
                            AuditEventType.RUN_FINISHED,
                            agent_id=agent_id,
                            run_id=run_id,
                            detail=f"Terminated: HITL rejected for {pending.tool_name}",
                            step_n=resume_state.step_n,
                            terminal_reason=TerminalReason.HITL_REJECTED.value,
                        )
                        # 审批「结果」计入指标（补齐 on_hitl_requested 只记 need_approval 的缺口）
                        self._safe_hook("on_hitl_resolved", pending.tool_name, "rejected", run_id)
                        self._safe_hook("on_halt", f"HITL rejected: {pending.tool_name}", run_id)
                        run_terminal_reason = TerminalReason.HITL_REJECTED.value
                        result = self._build_result(
                            success=False, run_id=run_id,
                            error=f"HITL approval rejected for: {pending.tool_name}",
                            terminal_reason=TerminalReason.HITL_REJECTED,
                            steps=step_records, started_at=started_at, start_mono=start_mono,
                        )
                        yield _mk(
                            StreamEventType.AGENT_HALTED,
                            data={
                                "terminal_reason": TerminalReason.HITL_REJECTED.value,
                                "error": result.error,
                            },
                        )
                        yield _run_end(result)
                        return

                    elif resume_decision.action == "execute_pending":
                        # 批准：写审计，保存 pending 供第一个 step 直接执行
                        self._audit(
                            AuditEventType.HITL_APPROVED,
                            agent_id=agent_id,
                            run_id=run_id,
                            detail=f"HITL approved: {pending.tool_name}",
                        )
                        # 审批「结果」计入指标（补齐 on_hitl_requested 只记 need_approval 的缺口）
                        self._safe_hook("on_hitl_resolved", pending.tool_name, "approved", run_id)
                        _hitl_resume_pending = pending

                # ═══ Interaction Resume 路径 ═══
                # 从 RunState.metadata 恢复 PendingInteraction
                raw_interaction = resume_state.metadata.get("pending_interaction")
                if interaction_response is not None and raw_interaction:
                    _interaction_resume_pending = PendingInteraction(
                        tool_call=raw_interaction["tool_call"],
                        tool_name=raw_interaction["tool_name"],
                        tool_args=raw_interaction["tool_args"],
                        step_n=raw_interaction["step_n"],
                        approved_calls_before=tuple(
                            raw_interaction.get("approved_calls_before", [])
                        ),
                    )
                    _interaction_user_response = interaction_response
                    logger.info(
                        "Interaction resume: tool=%s, response=%s",
                        raw_interaction.get("tool_name"),
                        interaction_response[:100],
                    )

                # ─── 会话隔离定位（用 user_id + session_id 联合确认） ───
                # resume 时用 (user_id, session_id) 联合定位 paused state，
                # 自然隔离不同用户的暂停快照。user_id 非必传（默认 "default"），
                # 无需额外 if 校验——定位不到就直接报错。
                if resume_state.session_id != session_id:
                    raise PermissionError(
                        f"HITL resume session_id mismatch: "
                        f"paused_session_id={resume_state.session_id!r}, "
                        f"resume_session_id={session_id!r}. "
                        f"Cannot resume a different session's paused run."
                    )

                # 恢复 discovered 状态（DEFERRED 工具在暂停前已被 discover 的状态）
                saved_discovered = resume_state.metadata.get("discovered_set")
                if saved_discovered:
                    for tool_name, step in saved_discovered.items():
                        self._tool_registry.promote_to_discovered(tool_name, step)

                # 恢复 Memory 上下文
                pause_snapshot = MemorySnapshot(
                    messages=tuple(resume_state.messages),
                )
                self._memory.resume_context(
                    pause_snapshot,
                    session_id=resume_state.session_id,
                )

                # 恢复暂停前的运行统计（token/cost/step_records/step_n 偏移）
                total_input_tokens = resume_state.metadata.get("paused_total_input_tokens", 0)
                total_output_tokens = resume_state.metadata.get("paused_total_output_tokens", 0)
                _saved_records = resume_state.metadata.get("paused_step_records", [])
                for _sr in _saved_records:
                    step_records.append(StepRecord(
                        step_n=_sr.get("step_n", 0),
                        duration_ms=_sr.get("duration_ms", 0.0),
                        llm_input_tokens=_sr.get("llm_input_tokens", 0),
                        llm_output_tokens=_sr.get("llm_output_tokens", 0),
                        tool_calls=_sr.get("tool_calls", []),
                        error=_sr.get("error"),
                        permission_denied=_sr.get("permission_denied", False),
                        hitl_requested=_sr.get("hitl_requested", False),
                    ))

                # 恢复跨 HITL 的循环检测状态（防止 HITL 重入时计数器归零导致无限循环）
                _saved_recent = resume_state.metadata.get("paused_recent_tool_calls", [])
                if _saved_recent:
                    recent_tool_calls = [tuple(item) for item in _saved_recent]
                loop_correction_count = resume_state.metadata.get("paused_loop_correction_count", 0)
            else:
                self._memory.init_from_restore(task, session_id=session_id)

            # ── 【Phase 0.5】手动 Skill 预验证 ────────────────────────────
            # 当接入层显式传入 skill_name 时（用户通过 /skill_name 指令触发），
            # 验证 Skill 存在性并标记 _manually_requested，
            # 然后注入 hint 引导 LLM 在 step 循环中调用 search_skills 完成加载。
            # Skill 的实际加载（渲染、Tool 提升、审计）全部复用 search_skills 路径。
            if skill_name and self._skill_registry:
                _skill_result = self._skill_registry.invoke_skill_manually(
                    name=skill_name,
                )

                if _skill_result.success:
                    # ✅ Skill 存在 → 将 hint 作为 user 消息注入 STM + raw_log
                    #    引导 LLM 在 Phase 2+ 调用 search_skills 加载
                    self._memory.inject_user_hint(_skill_result.content)
                    logger.info(
                        "Phase 0.5: Skill '%s' 验证通过，hint 已注入 STM + raw_log",
                        _skill_result.skill_name,
                    )
                else:
                    # ❌ 没找到 → 降级处理，不阻塞
                    # task 已在 Phase 0 作为 user message 进了 memory，
                    # LLM 会看到原始文本自行理解
                    logger.warning(
                        "Phase 0.5: Skill '%s' 未找到，降级为普通 user message: %s",
                        skill_name,
                        _skill_result.error,
                    )

            # ── Plan Mode 初始化（PlanManager 管理）──────────────────
            # PlanManager 封装所有 planning 状态：phase / turns / reminder / 工具过滤
            # LLM 通过 enter_plan_mode 工具自主进入，exit_plan_mode 提交后等用户决策
            from ..plan import PlanManager
            from ..plan.files import write_initial_plan_file

            plan_manager = PlanManager()

            # ── 跨 run 恢复：从 session_meta 读取状态 ──
            session_phase = self._memory.get_session_meta("plan_phase")
            submitted_at = self._memory.get_session_meta("plan_submitted_at")
            plan_file_path_meta = self._memory.get_session_meta("plan_file_path")

            # 情况 A: 继续进行中的规划（enter_plan_mode 已入 session_meta，未提交）
            if session_phase == "planning" and not submitted_at:
                plan_manager = PlanManager.restore_from_session_meta({
                    "plan_phase": session_phase,
                    "plan_file_path": plan_file_path_meta,
                })

            # 情况 B: 用户决策（前端传入 plan_action）
            if plan_action:
                plan_manager = PlanManager.restore_from_session_meta({
                    "plan_phase": session_phase if session_phase else "planning",
                    "plan_file_path": plan_file_path_meta,
                })
                _handle_plan_action(
                    self, plan_manager,
                    plan_action=plan_action,
                    message=task,
                    plan_file_path=plan_file_path_meta,
                    edited_plan_content=edited_plan_content,
                )

            # ── HC5：for 有界循环 + StepCounter ───────────────────────────
            # resume 时从暂停的 step_n + 1 开始，确保 max_steps 是全局上限
            _step_start_offset = (resume_state.step_n + 1) if resume_state else 0
            _remaining_steps = self._limits.max_steps - _step_start_offset
            if _remaining_steps <= 0:
                _remaining_steps = 1  # 至少允许执行 resume 的 pending call
            step_counter = StepCounter(_remaining_steps)
            for _step_idx in range(_remaining_steps):
                step_n = _step_start_offset + _step_idx

                # HC5 总超时检查
                elapsed = time.monotonic() - start_mono
                if elapsed >= self._limits.total_timeout:
                    self._audit(
                        AuditEventType.AGENT_TERMINATED,
                        agent_id=agent_id,
                        run_id=run_id,
                        detail=f"Total timeout: {elapsed:.1f}s >= {self._limits.total_timeout}s",
                        terminal_reason=TerminalReason.TOTAL_TIMEOUT.value,
                    )
                    self._safe_hook("on_halt", f"Total timeout: {elapsed:.1f}s", run_id)
                    result = self._build_result(
                        success=False, run_id=run_id,
                        error=f"Total timeout exceeded: {elapsed:.1f}s >= {self._limits.total_timeout}s",
                        terminal_reason=TerminalReason.TOTAL_TIMEOUT,
                        steps=step_records, started_at=started_at, start_mono=start_mono,
                        total_input_tokens=total_input_tokens,
                        total_output_tokens=total_output_tokens,
                    )
                    yield _mk(
                        StreamEventType.AGENT_HALTED,
                        data={"terminal_reason": TerminalReason.TOTAL_TIMEOUT.value, "error": result.error},
                        step_n=step_n,
                    )
                    yield _run_end(result)
                    return

                # 取消信号检查
                if self._cancelled:
                    self._audit(
                        AuditEventType.AGENT_TERMINATED,
                        agent_id=agent_id,
                        run_id=run_id,
                        detail="Cancelled by user",
                        step_n=step_n,
                        terminal_reason=TerminalReason.CANCELLED.value,
                    )
                    self._safe_hook("on_halt", "Cancelled by user", run_id)
                    result = self._build_result(
                        success=False, run_id=run_id,
                        error="Cancelled by user",
                        terminal_reason=TerminalReason.CANCELLED,
                        steps=step_records, started_at=started_at, start_mono=start_mono,
                        total_input_tokens=total_input_tokens,
                        total_output_tokens=total_output_tokens,
                    )
                    yield _mk(StreamEventType.AGENT_CANCELLED, data={"error": "Cancelled by user"}, step_n=step_n)
                    yield _run_end(result)
                    return

                step_counter.increment()
                step_start = time.monotonic()
                # HC5：step_timeout 在循环外确定，循环内不可变
                step_timeout = self._limits.step_timeout
                step_record = StepRecord(step_n=step_n)

                try:
                    # 更新 raw_log run 上下文到当前 step：本 step 内写入的
                    # assistant / tool 消息都会带 (run_id, step_n)。
                    self._memory.set_run_context(run_id, step_n)
                    self._safe_hook("on_step_start", step_n, run_id)
                    yield _mk(StreamEventType.STEP_START, data={"step_n": step_n}, step_n=step_n)

                    # ═══ HITL Resume: 直接执行 pending tool call ═══
                    # approved 后第一个 step 直接执行暂停时保存的工具调用，
                    # 不需要 LLM 重新生成（确定性保证）。
                    if _hitl_resume_pending is not None:
                        pending = _hitl_resume_pending
                        _hitl_resume_pending = None  # 消费一次后清除

                        # 合并需要执行的 tool calls：
                        # 1. 暂停前已通过预检的 calls
                        # 2. 被审批通过的 pending call
                        # 注意：unchecked_calls_after 不执行，让 LLM 在下一轮重新决策
                        resume_calls: list[dict] = []
                        for prev_call in pending.approved_calls_before:
                            resume_calls.append(prev_call)
                        resume_calls.append({
                            "id": pending.tool_call.get("id", generate_id()),
                            "name": pending.tool_name,
                            "args": pending.tool_args,
                        })

                        # HITL 审批通过的工具强制加入 discovered_set，
                        # 确保 _pre_execute_checks 门-D 不会拦截已被人工批准的调用
                        for rc in resume_calls:
                            self._tool_registry.promote_to_discovered(rc["name"], step_n)

                        # 构建 HITL resume 路径的 metadata（与主路径一致）
                        import types as _types_mod_hitl
                        _hitl_metadata_dict: dict = {
                            "tool_store": self._tool_registry.store,
                            "discovery_manager": self._tool_registry.discovery,
                            # Layer 2/3 取消：必须与主路径（见下方工具执行处
                            # ctx.metadata["cancel_token"] 注入）保持一致，否则经 HITL
                            # 审批后 resume 执行的 call_agent 拿不到父 cancel_token，
                            # 子 Agent 级联链无法武装 → 用户 STOP 对子 Agent 失效。
                            "cancel_token": self._cancel_token,
                        }
                        if self._skill_registry is not None:
                            _hitl_metadata_dict["skill_registry"] = self._skill_registry
                        if self._agent_registry is not None:
                            _hitl_metadata_dict["agent_registry"] = self._agent_registry
                        if metadata:
                            _hitl_metadata_dict.update(metadata)

                        ctx = ToolContext(
                            run_id=run_id,
                            step_n=step_n,
                            agent_id=agent_id,
                            session_id=session_id,
                            permissions=frozenset(
                                p.value
                                for p in self._identity.sensitive_permissions
                            ),
                            trust_level=self._identity.trust_level,
                            working_memory=self._memory.working_memory_accessor,
                            metadata=_types_mod_hitl.MappingProxyType(_hitl_metadata_dict) if _hitl_metadata_dict else _types_mod_hitl.MappingProxyType({}),
                        )

                        for rc in resume_calls:
                            progress_label = self._resolve_progress_label(rc["name"], rc.get("args", {}))
                            yield _mk(
                                StreamEventType.TOOL_CALL_START,
                                data={
                                    "tool_call_id": rc["id"],
                                    # 5.2: 透传完整 args dict（前端 ToolStartMsg.tool_args: dict）
                                    "tool_args":     rc.get("args", {}),
                                    "args_preview":  str(rc["args"])[:200],
                                    "progress_label": progress_label,
                                },
                                step_n=step_n,
                                tool_name=rc["name"],
                            )

                        step_timeout_remaining = self._limits.step_timeout - (time.monotonic() - step_start)
                        if step_timeout_remaining <= 0:
                            raise asyncio.TimeoutError()
                        # Layer 2 竞速：与主路径一致，STOP 能打断 in-flight 工具
                        # （ctx.metadata 已注入 cancel_token，见上方 _hitl_metadata_dict）。
                        tool_results = await self._execute_tools_with_cancel_race(
                            [{"name": rc["name"], "args": rc["args"]} for rc in resume_calls],
                            ctx, step_timeout_remaining, step_n,
                        )

                        # 收集暂停时被推迟的 denied 结果 + resume 执行结果，
                        # 等全部收集完毕后一次性原子写入 memory
                        _resume_collected: list[tuple[str, str, str]] = []  # [(tc_id, name, text), ...]
                        _paused_original_calls = resume_state.metadata.get("paused_original_tool_calls", [])
                        _paused_content = resume_state.metadata.get("paused_assistant_content", "")
                        _paused_reasoning = resume_state.metadata.get("paused_reasoning_content")
                        _paused_deferred = resume_state.metadata.get("paused_deferred_results", [])

                        for rc, tool_result in zip(resume_calls, tool_results):
                            tc_id = rc["id"]
                            tc_name = rc["name"]
                            yield _mk(
                                StreamEventType.TOOL_CALL_END,
                                data=tool_call_end_data(tc_id, rc.get("args", {}), tool_result),
                                step_n=step_n,
                                tool_name=tc_name,
                            )

                            if tool_result.halt:
                                result_text = tool_result.error or "Error"
                                _resume_collected.append((tc_id, tc_name, result_text))

                                # 原子写入：暂停时 assistant(tool_calls) 未写入 memory，
                                # 现在工具 halt，所有已收集结果齐备，一次性写入
                                await self._memory.add_assistant_message(
                                    _paused_content, tool_calls=_paused_original_calls,
                                    reasoning_content=_paused_reasoning,
                                )
                                for _dtc_id, _dtc_name, _dtc_text in _paused_deferred:
                                    await self._memory.add_tool_result(_dtc_id, _dtc_name, _dtc_text)
                                for _rtc_id, _rtc_name, _rtc_text in _resume_collected:
                                    await self._memory.add_tool_result(_rtc_id, _rtc_name, _rtc_text)

                                self._audit(
                                    AuditEventType.AGENT_TERMINATED,
                                    agent_id=agent_id,
                                    run_id=run_id,
                                    detail=f"Tool halt: {tc_name} - {tool_result.error}",
                                    step_n=step_n,
                                    tool_name=tc_name,
                                    terminal_reason=TerminalReason.TOOL_HALT.value,
                                )
                                self._safe_hook("on_halt", f"Tool halt: {tc_name}", run_id)
                                step_record.duration_ms = (time.monotonic() - step_start) * 1000
                                step_records.append(step_record)
                                self._safe_hook("on_step_end", step_n, run_id)
                                yield _mk(
                                    StreamEventType.STEP_END,
                                    data={"step_n": step_n, "duration_ms": step_record.duration_ms},
                                    step_n=step_n,
                                )
                                result = self._build_result(
                                    success=False, run_id=run_id,
                                    error=f"Tool halt: {tool_result.error}",
                                    terminal_reason=TerminalReason.TOOL_HALT,
                                    steps=step_records, started_at=started_at, start_mono=start_mono,
                                )
                                yield _mk(
                                    StreamEventType.AGENT_HALTED,
                                    data={"terminal_reason": TerminalReason.TOOL_HALT.value, "error": result.error},
                                    step_n=step_n,
                                    tool_name=tc_name,
                                )
                                yield _run_end(result)
                                return

                            result_text = render_tool_result_for_llm(tool_result)
                            _resume_collected.append((tc_id, tc_name, result_text))

                        # 原子写入：暂停时 assistant(tool_calls) 未写入 memory，
                        # 现在恢复执行完毕，所有 tool 结果齐备，一次性写入。
                        # paused_original_tool_calls 仅含到 HITL 边界为止的调用，
                        # unchecked_calls_after 不在此列（LLM 下一轮自行重新决策）。
                        await self._memory.add_assistant_message(
                            _paused_content, tool_calls=_paused_original_calls,
                            reasoning_content=_paused_reasoning,
                        )
                        for _dtc_id, _dtc_name, _dtc_text in _paused_deferred:
                            await self._memory.add_tool_result(_dtc_id, _dtc_name, _dtc_text)
                        for _rtc_id, _rtc_name, _rtc_text in _resume_collected:
                            await self._memory.add_tool_result(_rtc_id, _rtc_name, _rtc_text)

                        # resume step 结束，记录并 continue 到下一步让 LLM 继续思考
                        step_record.tool_calls = [rc["name"] for rc in resume_calls]
                        step_record.duration_ms = (time.monotonic() - step_start) * 1000
                        step_records.append(step_record)

                        # 将 HITL resume 执行的工具也加入循环检测（跨 HITL 累计）
                        for rc in resume_calls:
                            tc_args_str = json.dumps(rc["args"], sort_keys=True, ensure_ascii=False)
                            recent_tool_calls.append((rc["name"], tc_args_str))

                        self._safe_hook("on_step_end", step_n, run_id)
                        yield _mk(
                            StreamEventType.STEP_END,
                            data={"step_n": step_n, "duration_ms": step_record.duration_ms},
                            step_n=step_n,
                        )
                        continue  # 进入下一个 step，让 LLM 根据工具结果继续

                    # ═══ Interaction Resume 路径 ═══
                    # 交互型工具恢复：执行交互工具之前已通过的 calls + 交互工具本身
                    if _interaction_resume_pending is not None:
                        pending = _interaction_resume_pending
                        _interaction_resume_pending = None

                        tc_id = pending.tool_call.get("id", generate_id())
                        tc_name = pending.tool_name
                        tc_args = pending.tool_args

                        # 将用户回复注入交互工具参数
                        args_with_response = {
                            **tc_args,
                            "_interaction_response": _interaction_user_response,
                        }
                        _interaction_user_response = None

                        # 构建 ToolContext
                        import types as _types_mod_int
                        _int_meta: dict = {
                            "tool_store": self._tool_registry.store,
                            "discovery_manager": self._tool_registry.discovery,
                            # 与主路径 / HITL resume 一致：下发取消令牌，令交互工具
                            # 内部 checkpoint 与子 Agent 级联链在 STOP 后能生效。
                            "cancel_token": self._cancel_token,
                        }
                        if self._skill_registry is not None:
                            _int_meta["skill_registry"] = self._skill_registry
                        if self._agent_registry is not None:
                            _int_meta["agent_registry"] = self._agent_registry
                        if metadata:
                            _int_meta.update(metadata)

                        int_ctx = ToolContext(
                            run_id=run_id,
                            step_n=step_n,
                            agent_id=agent_id,
                            session_id=session_id,
                            permissions=frozenset(
                                p.value for p in self._identity.sensitive_permissions
                            ),
                            trust_level=self._identity.trust_level,
                            working_memory=self._memory.working_memory_accessor,
                            metadata=_types_mod_int.MappingProxyType(_int_meta) if _int_meta else _types_mod_int.MappingProxyType({}),
                        )

                        # 合并需要执行的 tool_calls：
                        # 1. 交互工具之前已通过预检的 calls
                        # 2. 交互工具本身（注入用户回复）
                        _approved_before: list[dict] = list(pending.approved_calls_before)
                        _resume_calls = _approved_before + [{
                            "id": tc_id,
                            "name": tc_name,
                            "args": args_with_response,
                        }]

                        # HITL 审批通过的工具强制加入 discovered_set（与 HITL resume 一致）
                        for _rc in _approved_before:
                            self._tool_registry.promote_to_discovered(_rc["name"], step_n)

                        # 静默执行：不 yield TOOL_CALL_START/END（用户已看到交互结果，
                        # 前置 approved calls 在原始 step 中已向用户展示）
                        step_timeout_remaining = self._limits.step_timeout - (time.monotonic() - step_start)
                        if step_timeout_remaining <= 0:
                            raise asyncio.TimeoutError()
                        # Layer 2 竞速：与主路径一致，STOP 能打断 in-flight 工具。
                        _tool_results = await self._execute_tools_with_cancel_race(
                            _resume_calls, int_ctx, step_timeout_remaining, step_n,
                        )

                        # 收集恢复时已执行的工具结果 + 暂停时被推迟的 denied 结果，
                        # 等全部收集完毕后一次性原子写入 memory
                        _resume_collected: list[tuple[str, str, str]] = []  # [(tc_id, name, text), ...]
                        _has_halt = False
                        _halt_error: str | None = None
                        for _rc, _tr in zip(_resume_calls, _tool_results):
                            _rtc_id = _rc["id"]
                            _rtc_name = _rc["name"]
                            if _tr.halt:
                                _result_text = _tr.error or "Error"
                                _resume_collected.append((_rtc_id, _rtc_name, _result_text))
                                _has_halt = True
                                _halt_error = _tr.error
                            else:
                                _result_text = render_tool_result_for_llm(_tr)
                                _resume_collected.append((_rtc_id, _rtc_name, _result_text))

                        # 原子写入：暂停时 assistant(tool_calls) 未写入 memory，
                        # 现在所有 tool 结果齐备，一次性写入保证 API 合约完整性
                        _int_original_calls = resume_state.metadata.get("paused_original_tool_calls", [])
                        _int_content = resume_state.metadata.get("paused_assistant_content", "")
                        _int_reasoning = resume_state.metadata.get("paused_reasoning_content")
                        _int_deferred = resume_state.metadata.get("paused_deferred_results", [])
                        await self._memory.add_assistant_message(
                            _int_content, tool_calls=_int_original_calls,
                            reasoning_content=_int_reasoning,
                        )
                        for _itc_id, _itc_name, _itc_text in _int_deferred:
                            await self._memory.add_tool_result(_itc_id, _itc_name, _itc_text)
                        for _rtc_id, _rtc_name, _rtc_text in _resume_collected:
                            await self._memory.add_tool_result(_rtc_id, _rtc_name, _rtc_text)

                        if _has_halt:
                            self._audit(
                                AuditEventType.AGENT_TERMINATED,
                                agent_id=agent_id,
                                run_id=run_id,
                                detail=f"Tool halt in interaction resume: {_halt_error}",
                                step_n=step_n,
                                terminal_reason=TerminalReason.TOOL_HALT.value,
                            )
                            self._safe_hook("on_halt", f"Tool halt in interaction resume: {_halt_error}", run_id)
                            step_record.tool_calls = [rc["name"] for rc in _resume_calls]
                            step_record.duration_ms = (time.monotonic() - step_start) * 1000
                            step_records.append(step_record)
                            self._safe_hook("on_step_end", step_n, run_id)
                            yield _mk(
                                StreamEventType.STEP_END,
                                data={"step_n": step_n, "duration_ms": step_record.duration_ms},
                                step_n=step_n,
                            )
                            result = self._build_result(
                                success=False, run_id=run_id,
                                error=f"Tool halt in interaction resume: {_halt_error}",
                                terminal_reason=TerminalReason.TOOL_HALT,
                                steps=step_records, started_at=started_at, start_mono=start_mono,
                            )
                            yield _mk(
                                StreamEventType.AGENT_HALTED,
                                data={"terminal_reason": TerminalReason.TOOL_HALT.value, "error": result.error},
                                step_n=step_n,
                            )
                            yield _run_end(result)
                            return

                        logger.info(
                            "Interaction resume executed: tool=%s (with %d pre-approved calls), success=%s",
                            tc_name, len(_approved_before),
                            all(not _tr.halt for _tr in _tool_results),
                        )

                        # resume step 结束，继续下一步让 LLM 根据工具结果继续
                        step_record.tool_calls = [rc["name"] for rc in _resume_calls]
                        step_record.duration_ms = (time.monotonic() - step_start) * 1000
                        step_records.append(step_record)
                        self._safe_hook("on_step_end", step_n, run_id)
                        yield _mk(
                            StreamEventType.STEP_END,
                            data={"step_n": step_n, "duration_ms": step_record.duration_ms},
                            step_n=step_n,
                        )
                        continue  # 进入下一个 step，让 LLM 根据工具结果继续

                    # ═══ Phase 0.5: Update enabled tools ═══
                    tool_ctx_for_update = ToolContext(
                        run_id=run_id,
                        step_n=step_n,
                        agent_id=agent_id,
                        session_id=session_id,
                        permissions=frozenset(
                            p.value
                            for p in self._identity.sensitive_permissions
                        ),
                        trust_level=self._identity.trust_level,
                        working_memory=self._memory.working_memory_accessor,
                    )
                    # 计算工具可用性，这里用await是为了让is_enable的回调可以跑异步，所以这里用了异步
                    # 先重置 HarnessExecutor 的 turn 级状态（R1 频率 / R4 幂等）
                    self._harness_executor.reset_turn()
                    await self._tool_registry.update_enabled_tools(
                        tool_ctx_for_update,
                        is_circuit_tripped=self._harness_executor.is_circuit_tripped,
                    )

                    # ═══ Phase 1: Prepare ═══
                    # compact_if_needed 返回 None 表示正常，返回 int 表示压缩后仍溢出
                    overflow_tokens = await self._memory.compact_if_needed()
                    if overflow_tokens is not None:
                        step_record.duration_ms = (time.monotonic() - step_start) * 1000
                        step_records.append(step_record)
                        self._audit(
                            AuditEventType.AGENT_TERMINATED,
                            agent_id=agent_id,
                            run_id=run_id,
                            detail=f"Context overflow after compaction: {overflow_tokens} tokens",
                            step_n=step_n,
                            terminal_reason=TerminalReason.CONTEXT_OVERFLOW.value,
                        )
                        self._safe_hook("on_halt", f"Context overflow: {overflow_tokens} tokens", run_id)
                        self._safe_hook("on_step_end", step_n, run_id)
                        yield _mk(
                            StreamEventType.STEP_END,
                            data={"step_n": step_n, "duration_ms": step_record.duration_ms},
                            step_n=step_n,
                        )
                        result = self._build_result(
                            success=False, run_id=run_id,
                            error=f"Context overflow: {overflow_tokens} tokens after compaction",
                            terminal_reason=TerminalReason.CONTEXT_OVERFLOW,
                            steps=step_records, started_at=started_at, start_mono=start_mono,
                            total_input_tokens=total_input_tokens,
                            total_output_tokens=total_output_tokens,
                        )
                        yield _mk(
                            StreamEventType.AGENT_HALTED,
                            data={"terminal_reason": TerminalReason.CONTEXT_OVERFLOW.value, "error": result.error},
                            step_n=step_n,
                        )
                        yield _run_end(result)
                        return

                    current_messages = self._memory.get_messages()

                    skill_allowed_tools = None
                    if self._skill_registry is not None:
                        skill_allowed_tools = self._skill_registry.get_active_skill_tools()

                    # 从 ContextWindowBudget 获取工具 schema token 预算（如已配置）
                    _tool_schema_tokens: int | None = None
                    if getattr(self, "_context_window_budget", None) is not None:
                        _tool_schema_tokens = self._context_window_budget.get_slot_tokens("tool_schema")

                    tool_schemas = self._tool_registry.build_tool_schemas(
                        agent_id=agent_id,
                        messages=current_messages,
                        skill_allowed_tools=skill_allowed_tools,
                        tool_schema_tokens=_tool_schema_tokens,
                    )

                    # ── Plan Mode 工具过滤（委托 PlanManager）──
                    from ..plan.tools import EXIT_PLAN_MODE_NAME, ENTER_PLAN_MODE_NAME
                    if plan_manager.is_planning():
                        # 规划阶段：委托 PlanManager.filter_tools
                        all_registry_tools = self._tool_registry.list_tools()
                        filtered_tools = plan_manager.filter_tools(all_registry_tools)
                        # 使用 safe_name 确保与 ToolSchema.name 一致（兼容中文工具名）
                        from ..tool.safe_name import to_safe_name
                        allowed_tool_names = {
                            to_safe_name(t.full_name) for t in filtered_tools
                        }
                        tool_schemas = [
                            ts for ts in tool_schemas
                            if ts.name in allowed_tool_names
                        ]
                        logger.info(
                            "[plan-filter] ✅ phase=planning | "
                            "before=%d, after=%d, allowed=%s",
                            len(all_registry_tools), len(tool_schemas),
                            sorted(allowed_tool_names),
                        )
                        # 补回 Plan Mode 内置工具的 schema（filter_tools 允许但可能不在当前 schemas 中）
                        from ..plan.tools import PLAN_MODE_BUILTIN_TOOLS
                        from ..tool.definition.tool_schema import ToolSchema as _ToolSchema
                        for _bt_name in PLAN_MODE_BUILTIN_TOOLS:
                            if _bt_name in allowed_tool_names and not any(
                                ts.name == _bt_name for ts in tool_schemas
                            ):
                                _bt = self._tool_registry.get_tool(_bt_name)
                                if _bt:
                                    tool_schemas.append(_ToolSchema(
                                        name=_bt.name,
                                        description=_bt.description,
                                        parameters=dict(_bt.input_schema) if hasattr(_bt.input_schema, 'items') else _bt.input_schema,
                                    ))
                        logger.debug(
                            "[plan-filter] phase=planning, exposed tools: %s",
                            [ts.name for ts in tool_schemas],
                        )
                    else:
                        # 非规划阶段：过滤掉 exit_plan_mode，保留 enter_plan_mode
                        logger.info(
                            "[plan-filter] ⚠️ phase=executing | "
                            "before=%d, plan_manager._plan_phase=%s",
                            len(tool_schemas), plan_manager._plan_phase,
                        )
                        tool_schemas = [
                            ts for ts in tool_schemas
                            if ts.name != EXIT_PLAN_MODE_NAME
                        ]
                        _has_enter = any(ts.name == ENTER_PLAN_MODE_NAME for ts in tool_schemas)
                        if not _has_enter:
                            _enter_tool = self._tool_registry.get_tool(ENTER_PLAN_MODE_NAME)
                            if _enter_tool:
                                from ..tool.definition.tool_schema import ToolSchema
                                tool_schemas.append(ToolSchema(
                                    name=_enter_tool.name,
                                    description=_enter_tool.description,
                                    parameters=dict(_enter_tool.input_schema) if hasattr(_enter_tool.input_schema, 'items') else _enter_tool.input_schema,
                                ))
                        logger.debug(
                            "[plan-filter] phase=executing, exposed tools (%d): %s",
                            len(tool_schemas),
                            [ts.name for ts in tool_schemas],
                        )

                    # ── Prefix Cache v1.0 ──
                    # 静态前缀（<available_tools> / <available_skills> / <available_agents>）
                    # 已在 AgentLoop.__init__ 中一次性序列化进 self._static_context_str，
                    # 本轮 build() 直接复用以保证 PC1 字节级一致。
                    #
                    # 动态提醒（plan 阶段方法论 / 当前 plan 状态等）走独立的
                    # <system-reminder> user 消息尾插（PC3 / 方案 B1）。
                    # Skill 正文通过 search_skills 工具的 ToolResult 下发给 LLM
                    # （详见 docs/.../13_prefix_cache.md 5d2）。
                    # v1.4 重构：去 summary 化后，跨 session recall 整条路径已废弃，
                    # 此处不再注入 recall_text。

                    # --reminder管理逻辑--
                    # 场景 1：用户刚发消息，LLM 调用 enter_plan_mode
                    # plan_manager.enter() 
                    #      → _plan_phase = "planning"
                    #      → _plan_mode_turns = 0

                    # 场景 2：LLM 在 Phase 2 探索代码（第 7 轮）
                    # is_planning() → True
                    # 每 5 轮注入："仍在规划模式（6-Phase: Interview→Explore→Design→Review→Write→Submit）"

                    # 场景 3：用户批准了计划
                    # _handle_plan_action("approve"):
                    # plan_manager.exit(approved=True)
                    #      → _plan_phase = "executing"
                    # plan_manager.set_context_reminder(PLAN_CONTEXT_REMINDER)

                    # 场景 4：用户点了"完善"
                    # _handle_plan_action("refine"):
                    # plan_manager.reenter()
                    #     → _plan_phase = "planning"       ← 回到 planning
                    #     → _plan_mode_is_reentry = True
                    dynamic_reminder = self._message_builder.build_dynamic_reminder()

                    # ── Plan Mode dynamic_reminder 注入（委托 PlanManager）──
                    if plan_manager.is_planning():
                        _planning_reminder = plan_manager.get_reminder()
                        if _planning_reminder:
                            if dynamic_reminder:
                                dynamic_reminder += "\n" + _planning_reminder
                            else:
                                dynamic_reminder = _planning_reminder
                            logger.debug(
                                "[plan-reminder] injected <planning-mode> block (turn=%d)",
                                plan_manager.turns,
                            )
                    elif plan_manager.context_reminder:
                        # 批准后注入 plan-context
                        _ctx_reminder = plan_manager.context_reminder
                        if dynamic_reminder:
                            dynamic_reminder += f"\n{_ctx_reminder}"
                        else:
                            dynamic_reminder = _ctx_reminder
                        logger.info(
                            "[plan-reminder] injected <plan-context> block (len=%d)",
                            len(_ctx_reminder),
                        )

                    messages, tools_for_llm = self._message_builder.build(
                        messages=current_messages,
                        tool_schemas=tool_schemas,
                        static_context_str=self._static_context_str,
                        dynamic_reminder=dynamic_reminder,
                    )

                    # ═══ Phase 2: Think（LLM 调用，含重试）═══
                    max_llm_retries = self._error_policy.max_retries
                    llm_response = None
                    nudge_count = 0
                    has_streamed_tokens = False

                    yield _mk(
                        StreamEventType.LLM_CALL_START,
                        data={"model": _effective_model},
                        step_n=step_n,
                    )

                    for llm_attempt in range(max_llm_retries + 1):
                        _llm_call_start = time.monotonic()
                        self._safe_hook(
                            "on_before_llm_call", messages, run_id,
                            model=_effective_model,
                            tools=tools_for_llm,
                            call_type="main",
                            provider=_effective_provider,
                        )
                        try:
                            if self._stream and hasattr(self._llm_client, "stream_response"):
                                # ── 真增量路径 ────────────────────────────
                                accumulated_content = ""
                                accumulated_reasoning = ""
                                accumulated_refusal = ""
                                # tool_calls 增量累积器：index → {id, type, function: {name, arguments}}
                                # client 层只分发 tool_call_delta，完整列表由本层组装
                                tc_acc: dict[int, dict[str, Any]] = {}
                                stream_finish_reason: str | None = None
                                stream_usage: dict | None = None

                                async for chunk in self._llm_client.stream_response(
                                    messages, tools=tools_for_llm, settings=_effective_settings,
                                    always_tools_count=self._tool_registry.always_tools_count,
                                ):
                                    # 流式路径主动超时检查
                                    if time.monotonic() - step_start >= step_timeout:
                                        raise asyncio.TimeoutError()

                                    # Layer 1：逐 chunk 取消检查（协作式取消，见取消语义-契约.md）
                                    # 点停止后 ≤1 chunk 内停止 yield token，抛 CancelledSignal
                                    # 由本 step 的 except CancelledSignal 分支捕获转事件。
                                    self._cancel_token.raise_if_cancelled()

                                    if chunk.delta_content:
                                        accumulated_content += chunk.delta_content
                                        has_streamed_tokens = True
                                        yield _mk(
                                            StreamEventType.LLM_TOKEN,
                                            data={
                                                "delta": chunk.delta_content,
                                                "snapshot": accumulated_content,
                                            },
                                            step_n=step_n,
                                        )

                                    if chunk.delta_reasoning_content:
                                        accumulated_reasoning += chunk.delta_reasoning_content
                                        yield _mk(
                                            StreamEventType.LLM_REASONING_TOKEN,
                                            data={
                                                "delta": chunk.delta_reasoning_content,
                                                "snapshot": accumulated_reasoning,
                                            },
                                            step_n=step_n,
                                        )

                                    # 模型拒答增量（content_filter / safety 等）
                                    # 当前版本仅在引擎内累积；后续可在 stream.py 新增
                                    # LLM_REFUSAL 事件类型向外透传（方案 P2）
                                    if chunk.refusal_delta:
                                        accumulated_refusal += chunk.refusal_delta

                                    # tool_call 增量：按 index 累积 id / name / arguments
                                    if chunk.tool_call_delta is not None:
                                        d = chunk.tool_call_delta
                                        idx = d["index"]
                                        slot = tc_acc.get(idx)
                                        if slot is None:
                                            slot = {
                                                "id": d["id"],
                                                "type": "function",
                                                "function": {
                                                    "name": d["name"],
                                                    "arguments": d["arguments_delta"],
                                                },
                                            }
                                            tc_acc[idx] = slot
                                        else:
                                            # 首次非空覆盖 id / name；arguments 总是串接
                                            if d["id"] and not slot["id"]:
                                                slot["id"] = d["id"]
                                            if d["name"] and not slot["function"]["name"]:
                                                slot["function"]["name"] = d["name"]
                                            if d["arguments_delta"]:
                                                slot["function"]["arguments"] += d["arguments_delta"]

                                    if chunk.finish_reason is not None:
                                        stream_finish_reason = chunk.finish_reason
                                    if chunk.usage is not None:
                                        stream_usage = dict(chunk.usage)

                                # 流结束后统一组装完整 tool_calls 列表
                                accumulated_tool_calls = (
                                    [tc_acc[i] for i in sorted(tc_acc)] if tc_acc else None
                                )

                                # 拒答作为 content 的降级兜底：
                                # 当模型拒答且未产生正文/工具调用时，把 refusal 作为 content
                                # 向下传递，使 OutputParser 能走 FINAL 分支、有明确输出，
                                # 而非被当作"空响应"触发 nudge/retry
                                final_content = accumulated_content or None
                                if not final_content and not accumulated_tool_calls and accumulated_refusal:
                                    final_content = accumulated_refusal

                                llm_response = {
                                    "content": final_content,
                                    "tool_calls": accumulated_tool_calls,
                                    "usage": stream_usage or {},
                                    "finish_reason": stream_finish_reason,
                                    "reasoning_content": accumulated_reasoning or None,
                                }
                            else:
                                # ── 降级非流式路径（主动超时）────────────────────
                                remaining = step_timeout - (time.monotonic() - step_start)
                                if remaining <= 0:
                                    raise asyncio.TimeoutError()
                                llm_response = await asyncio.wait_for(
                                    self._llm_client.call(
                                        messages, tools=tools_for_llm, settings=_effective_settings,
                                        always_tools_count=self._tool_registry.always_tools_count,
                                    ),
                                    timeout=remaining,
                                )

                        except (LLMAuthError, LLMRequestError) as llm_err:
                            self._safe_hook(
                                "on_after_llm_call", {"usage": {}, "error": str(llm_err)}, run_id,
                                model=_effective_model,
                                duration_ms=(time.monotonic() - _llm_call_start) * 1000,
                                call_type="main",
                            )
                            self._safe_hook("on_error", llm_err, run_id)
                            self._audit(
                                AuditEventType.AGENT_TERMINATED,
                                agent_id=agent_id,
                                run_id=run_id,
                                detail=f"LLM non-retryable error ({type(llm_err).__name__}): {llm_err}",
                                step_n=step_n,
                                terminal_reason=TerminalReason.LLM_ERROR.value,
                            )
                            self._safe_hook("on_halt", f"LLM non-retryable error: {llm_err}", run_id)
                            step_record.duration_ms = (time.monotonic() - step_start) * 1000
                            step_records.append(step_record)
                            self._safe_hook("on_step_end", step_n, run_id)
                            yield _mk(
                                StreamEventType.STEP_END,
                                data={"step_n": step_n, "duration_ms": step_record.duration_ms},
                                step_n=step_n,
                            )
                            result = self._build_result(
                                success=False, run_id=run_id,
                                error=f"LLM non-retryable error ({type(llm_err).__name__}): {llm_err}",
                                terminal_reason=TerminalReason.LLM_ERROR,
                                steps=step_records, started_at=started_at, start_mono=start_mono,
                                total_input_tokens=total_input_tokens,
                                total_output_tokens=total_output_tokens,
                            )
                            yield _mk(
                                StreamEventType.AGENT_HALTED,
                                data={"terminal_reason": TerminalReason.LLM_ERROR.value, "error": str(llm_err)},
                                step_n=step_n,
                            )
                            yield _run_end(result)
                            return

                        except LLMRateLimitError as llm_err:
                            self._safe_hook(
                                "on_after_llm_call", {"usage": {}, "error": str(llm_err)}, run_id,
                                model=_effective_model,
                                duration_ms=(time.monotonic() - _llm_call_start) * 1000,
                                call_type="main",
                            )
                            self._safe_hook("on_error", llm_err, run_id)
                            if llm_attempt < max_llm_retries:
                                retry_after = (
                                    llm_err.retry_after if llm_err.retry_after is not None
                                    else self._error_policy.calculate_delay(llm_attempt)
                                )
                                logger.warning(
                                    "LLM rate limited (attempt %d/%d), retrying in %.1fs",
                                    llm_attempt + 1, max_llm_retries + 1, retry_after,
                                )
                                await asyncio.sleep(retry_after)
                                continue
                            self._audit(
                                AuditEventType.AGENT_TERMINATED,
                                agent_id=agent_id,
                                run_id=run_id,
                                detail=f"LLM rate limit exceeded after {llm_attempt + 1} attempts: {llm_err}",
                                step_n=step_n,
                                terminal_reason=TerminalReason.LLM_ERROR.value,
                            )
                            self._safe_hook("on_halt", f"LLM rate limit exceeded: {llm_err}", run_id)
                            step_record.duration_ms = (time.monotonic() - step_start) * 1000
                            step_records.append(step_record)
                            self._safe_hook("on_step_end", step_n, run_id)
                            yield _mk(
                                StreamEventType.STEP_END,
                                data={"step_n": step_n, "duration_ms": step_record.duration_ms},
                                step_n=step_n,
                            )
                            result = self._build_result(
                                success=False, run_id=run_id,
                                error=f"LLM rate limit exceeded after {llm_attempt + 1} attempts: {llm_err}",
                                terminal_reason=TerminalReason.LLM_ERROR,
                                steps=step_records, started_at=started_at, start_mono=start_mono,
                                total_input_tokens=total_input_tokens,
                                total_output_tokens=total_output_tokens,
                            )
                            yield _mk(
                                StreamEventType.AGENT_HALTED,
                                data={"terminal_reason": TerminalReason.LLM_ERROR.value, "error": str(llm_err)},
                                step_n=step_n,
                            )
                            yield _run_end(result)
                            return

                        except (LLMServerError, LLMNetworkError) as llm_err:
                            self._safe_hook(
                                "on_after_llm_call", {"usage": {}, "error": str(llm_err)}, run_id,
                                model=_effective_model,
                                duration_ms=(time.monotonic() - _llm_call_start) * 1000,
                                call_type="main",
                            )
                            self._safe_hook("on_error", llm_err, run_id)
                            if llm_attempt < max_llm_retries:
                                retry_after = self._error_policy.calculate_delay(llm_attempt)
                                logger.warning(
                                    "LLM call failed (attempt %d/%d), retrying in %.1fs",
                                    llm_attempt + 1, max_llm_retries + 1, retry_after,
                                )
                                await asyncio.sleep(retry_after)
                                continue
                            self._audit(
                                AuditEventType.AGENT_TERMINATED,
                                agent_id=agent_id,
                                run_id=run_id,
                                detail=f"LLM call failed after {llm_attempt + 1} attempts: {llm_err}",
                                step_n=step_n,
                                terminal_reason=TerminalReason.LLM_ERROR.value,
                            )
                            self._safe_hook("on_halt", f"LLM call failed: {llm_err}", run_id)
                            step_record.duration_ms = (time.monotonic() - step_start) * 1000
                            step_records.append(step_record)
                            self._safe_hook("on_step_end", step_n, run_id)
                            yield _mk(
                                StreamEventType.STEP_END,
                                data={"step_n": step_n, "duration_ms": step_record.duration_ms},
                                step_n=step_n,
                            )
                            result = self._build_result(
                                success=False, run_id=run_id,
                                error=f"LLM call failed after {llm_attempt + 1} attempts: {llm_err}",
                                terminal_reason=TerminalReason.LLM_ERROR,
                                steps=step_records, started_at=started_at, start_mono=start_mono,
                                total_input_tokens=total_input_tokens,
                                total_output_tokens=total_output_tokens,
                            )
                            yield _mk(
                                StreamEventType.AGENT_HALTED,
                                data={"terminal_reason": TerminalReason.LLM_ERROR.value, "error": str(llm_err)},
                                step_n=step_n,
                            )
                            yield _run_end(result)
                            return

                        self._safe_hook(
                            "on_after_llm_call", llm_response, run_id,
                            model=_effective_model,
                            duration_ms=(time.monotonic() - _llm_call_start) * 1000,
                            call_type="main",
                            provider=_effective_provider,
                        )

                        # 空响应 → nudge 重试
                        response_content = llm_response.get("content") if isinstance(llm_response, dict) else ""
                        has_tool_calls_flag = bool(llm_response.get("tool_calls")) if isinstance(llm_response, dict) else False
                        is_empty = not response_content and not has_tool_calls_flag

                        if is_empty and nudge_count < 3:
                            nudge_count += 1
                            logger.warning("LLM empty response (nudge %d/3)", nudge_count)
                            # 用 user 消息提示，不再伪造 tool 消息（nudge_N 无对应
                            # assistant tool_call 声明 → 会让 API 400：孤儿 tool 消息）。
                            # inject_user_hint 是同步方法（memory.py:551），不可 await。
                            self._memory.inject_user_hint(
                                "Empty response detected. Please provide a valid response or use a tool."
                            )
                            current_messages = self._memory.get_messages()
                            # nudge 场景下动态 reminder 与本轮同源（recall_text 不会因 nudge 变化），
                            # 直接复用外层已构造的 dynamic_reminder 即可；tool_schemas /
                            # static_context_str 亦保持不变以维持 PC1 序列化唯一性。
                            messages, tools_for_llm = self._message_builder.build(
                                messages=current_messages,
                                tool_schemas=tool_schemas,
                                static_context_str=self._static_context_str,
                                dynamic_reminder=dynamic_reminder,
                            )
                            continue

                        break  # 有效响应，退出重试循环

                    # 重试耗尽仍空响应
                    _final_content = llm_response.get("content") if isinstance(llm_response, dict) else None
                    _final_tool_calls = llm_response.get("tool_calls") if isinstance(llm_response, dict) else None
                    if llm_response is None or (not _final_content and not _final_tool_calls):
                        self._audit(
                            AuditEventType.AGENT_TERMINATED,
                            agent_id=agent_id,
                            run_id=run_id,
                            detail="LLM returned empty response after all retries",
                            step_n=step_n,
                            terminal_reason=TerminalReason.LLM_ERROR.value,
                        )
                        self._safe_hook("on_halt", "LLM empty response after retries", run_id)
                        step_record.duration_ms = (time.monotonic() - step_start) * 1000
                        step_records.append(step_record)
                        self._safe_hook("on_step_end", step_n, run_id)
                        yield _mk(
                            StreamEventType.STEP_END,
                            data={"step_n": step_n, "duration_ms": step_record.duration_ms},
                            step_n=step_n,
                        )
                        result = self._build_result(
                            success=False, run_id=run_id,
                            error="LLM returned empty response after all retries",
                            terminal_reason=TerminalReason.LLM_ERROR,
                            steps=step_records, started_at=started_at, start_mono=start_mono,
                            total_input_tokens=total_input_tokens,
                            total_output_tokens=total_output_tokens,
                        )
                        yield _mk(
                            StreamEventType.AGENT_HALTED,
                            data={"terminal_reason": TerminalReason.LLM_ERROR.value, "error": result.error},
                            step_n=step_n,
                        )
                        yield _run_end(result)
                        return

                    # 记录 token 用量
                    usage = llm_response.get("usage", {}) if isinstance(llm_response, dict) else {}
                    step_input_tokens = usage.get("prompt_tokens", 0)
                    step_output_tokens = usage.get("completion_tokens", 0)
                    step_record.llm_input_tokens = step_input_tokens
                    step_record.llm_output_tokens = step_output_tokens

                    yield _mk(
                        StreamEventType.LLM_CALL_END,
                        data={
                            "input_tokens": step_input_tokens,
                            "output_tokens": step_output_tokens,
                        },
                        step_n=step_n,
                    )

                    # 通用停机守卫：SDK 不知道停机理由——把本步用量事实交给应用层 StepGuard，
                    # 由它据自身策略（如按净费用累加判断超预算）裁决 halt/继续 + 理由（纯机制）。
                    if self._step_guard is not None:
                        _ptd = usage.get("prompt_tokens_details", {}) or {}
                        _ctd = usage.get("completion_tokens_details", {}) or {}
                        _step_cached = _ptd.get("cached_tokens", 0) or 0
                        _step_creation = _ptd.get("cache_creation_input_tokens", 0) or 0
                        _step_reasoning = _ctd.get("reasoning_tokens", 0) or 0
                        _decision = self._step_guard.should_halt(
                            run_id=run_id,
                            usage=StepUsage(
                                model=_effective_model,
                                input_tokens=step_input_tokens,
                                output_tokens=step_output_tokens,
                                cached_tokens=_step_cached,
                                step=step_n,
                                cache_creation_tokens=_step_creation,
                                reasoning_tokens=_step_reasoning,
                                # provider 事实透传给应用层守卫（按 provider 分账）；
                                # 客户端未暴露/无能力声明时为 ""（守卫据此降级为不分账）。
                                provider=_effective_provider,
                            ),
                        )
                        if _decision.halt:
                            _halt_reason = _decision.reason or "应用层 StepGuard 裁决停机"
                            self._safe_hook("on_halt", _halt_reason, run_id)
                            step_record.duration_ms = (time.monotonic() - step_start) * 1000
                            step_records.append(step_record)
                            self._safe_hook("on_step_end", step_n, run_id)
                            yield _mk(
                                StreamEventType.STEP_END,
                                data={"step_n": step_n, "duration_ms": step_record.duration_ms},
                                step_n=step_n,
                            )
                            result = self._build_result(
                                success=False, run_id=run_id,
                                error=_halt_reason,
                                terminal_reason=TerminalReason.HALTED_BY_GUARD,
                                steps=step_records, started_at=started_at, start_mono=start_mono,
                                total_input_tokens=total_input_tokens + step_input_tokens,
                                total_output_tokens=total_output_tokens + step_output_tokens,
                            )
                            yield _mk(
                                StreamEventType.AGENT_HALTED,
                                data={"terminal_reason": TerminalReason.HALTED_BY_GUARD.value, "error": result.error},
                                step_n=step_n,
                            )
                            yield _run_end(result)
                            return

                    # ═══ Phase 3: Parse ═══
                    parsed = self._output_parser.parse(llm_response)

                    if parsed.is_final:
                        if parsed.is_empty:
                            step_record.duration_ms = (time.monotonic() - step_start) * 1000
                            step_records.append(step_record)
                            self._audit(
                                AuditEventType.AGENT_TERMINATED,
                                agent_id=agent_id,
                                run_id=run_id,
                                detail="LLM returned empty final response",
                                step_n=step_n,
                                terminal_reason=TerminalReason.LLM_ERROR.value,
                            )
                            self._safe_hook("on_halt", "LLM empty final response", run_id)
                            step_record.duration_ms = (time.monotonic() - step_start) * 1000
                            step_records.append(step_record)
                            self._safe_hook("on_step_end", step_n, run_id)
                            yield _mk(
                                StreamEventType.STEP_END,
                                data={"step_n": step_n, "duration_ms": step_record.duration_ms},
                                step_n=step_n,
                            )
                            result = self._build_result(
                                success=False, run_id=run_id,
                                error="LLM returned empty final response",
                                terminal_reason=TerminalReason.LLM_ERROR,
                                steps=step_records, started_at=started_at, start_mono=start_mono,
                                total_input_tokens=total_input_tokens + step_input_tokens,
                                total_output_tokens=total_output_tokens + step_output_tokens,
                            )
                            yield _mk(
                                StreamEventType.AGENT_HALTED,
                                data={"terminal_reason": TerminalReason.LLM_ERROR.value, "error": result.error},
                                step_n=step_n,
                            )
                            yield _run_end(result)
                            return

                        # 最终答案
                        # 真增量路径：文本已逐 chunk yield，此处不重复
                        # 降级非流式路径：单次 yield 完整内容
                        if not has_streamed_tokens:
                            yield _mk(
                                StreamEventType.LLM_TOKEN,
                                data={"delta": parsed.content, "snapshot": parsed.content},
                                step_n=step_n,
                            )

                        await self._memory.add_assistant_message(
                            parsed.content,
                            reasoning_content=llm_response.get("reasoning_content") if isinstance(llm_response, dict) else None,
                        )

                        self._audit(
                            AuditEventType.RUN_FINISHED,
                            agent_id=agent_id,
                            run_id=run_id,
                            detail="Completed normally",
                            step_n=step_n,
                        )
                        run_success = True
                        run_terminal_reason = TerminalReason.COMPLETED.value
                        step_record.duration_ms = (time.monotonic() - step_start) * 1000
                        step_records.append(step_record)
                        self._safe_hook("on_step_end", step_n, run_id)

                        # 就地累计最终一步的 token（final-answer 在此直接 return，不走循环尾部）。
                        # 花费不再由 SDK 计算/报告：金额/预算全归应用层（见 pandapal.config.llm_pricing）。
                        total_input_tokens += step_input_tokens
                        total_output_tokens += step_output_tokens

                        result = self._build_result(
                            success=True, run_id=run_id,
                            output=parsed.content,
                            terminal_reason=TerminalReason.COMPLETED,
                            steps=step_records, started_at=started_at, start_mono=start_mono,
                            total_input_tokens=total_input_tokens,
                            total_output_tokens=total_output_tokens,
                        )
                        yield _mk(
                            StreamEventType.STEP_END,
                            data={
                                "step_n": step_n,
                                "duration_ms": step_record.duration_ms,
                                "input_tokens": step_input_tokens,
                                "output_tokens": step_output_tokens,
                            },
                            step_n=step_n,
                        )
                        yield _run_end(result)
                        return

                    # 有 tool_calls → 预扫描 + 串行预检 + 并发执行
                    #
                    # Phase 3.5: 预扫描所有 tool_calls，识别 HITL/交互边界
                    # 核心原则：assistant(tool_calls) 只有在所有 tool 结果齐备时
                    # 才能原子写入 memory，保证 API 合约完整性。
                    # 暂停时跳过写入，将所有信息存入 RunState，恢复后再原子写入。

                    # id 归一化：LLM 返回的 tool_calls 缺 id 时原地补齐（generate_id），
                    # 保证写入 assistant 的 id 与执行/结果写入使用的 id 完全一致
                    # （否则 tool 消息的 tool_call_id 在 assistant 的 tool_calls 中
                    #  找不到对应 → OpenAI 兼容 API 400；HITL 暂停持久化的
                    #  paused_original_tool_calls 同样依赖此归一化保持一致）。
                    for _tc_raw in parsed.tool_calls:
                        if isinstance(_tc_raw, dict):
                            _tc_raw["id"] = _tc_raw.get("id") or generate_id()

                    _prescan_normalized: list[dict] = []
                    _prescan_denied: dict[str, tuple[str, str]] = {}  # tc_id -> (name, error_text)
                    _prescan_first_hitl_idx: int | None = None
                    _prescan_first_interaction_idx: int | None = None

                    for _pi, _ptc in enumerate(parsed.tool_calls):
                        _ptc_id = _ptc.get("id", generate_id())
                        _ptc_func = _ptc.get("function", _ptc)
                        _ptc_name = _ptc_func.get("name", "")
                        _ptc_args_raw = _ptc_func.get("arguments", "{}")
                        try:
                            _ptc_args = json.loads(_ptc_args_raw) if isinstance(_ptc_args_raw, str) else _ptc_args_raw
                        except json.JSONDecodeError:
                            _ptc_args = {}

                        _pdef = self._tool_registry.get_tool(_ptc_name)
                        if not _pdef:
                            _prescan_denied[_ptc_id] = (_ptc_name, f"Error: Tool '{_ptc_name}' is not registered")
                            _prescan_normalized.append({"id": _ptc_id, "name": _ptc_name, "args": _ptc_args})
                            continue

                        _pguard = self._permission_guard.check_permission(
                            self._identity.sensitive_permissions,
                            _pdef.sensitivity,
                            _pdef.sensitive_permission,
                        )
                        if _pguard == "deny":
                            _prescan_denied[_ptc_id] = (_ptc_name, f"Error: Permission denied for tool '{_ptc_name}'")
                            _prescan_normalized.append({"id": _ptc_id, "name": _ptc_name, "args": _ptc_args})
                            continue

                        if _prescan_first_hitl_idx is None:
                            _phitl = self._hitl_controller.check_approval(_pdef.sensitivity.value, _ptc_name)
                            if _phitl == "need_approval":
                                _prescan_first_hitl_idx = _pi

                        if _prescan_first_interaction_idx is None and _pdef.requires_user_interaction:
                            _prescan_first_interaction_idx = _pi

                        _prescan_normalized.append({"id": _ptc_id, "name": _ptc_name, "args": _ptc_args})

                    _will_pause = _prescan_first_hitl_idx is not None or _prescan_first_interaction_idx is not None

                    # 主路径不提前写 assistant(tool_calls)：
                    # 改为"所有 tool 结果齐备后与 assistant 一起原子提交"
                    # （见收尾循环后的 _commit_tool_step_atomically 调用），
                    # 中断（STOP/超时/异常）发生在提交前 → memory 无孤儿。
                    # 暂停路径（HITL/Interaction）由 resume 侧原子写入，保持现状。

                    # 暂停时推迟写入的 tool 结果缓冲区
                    _deferred_tool_results: list[tuple[str, str, str]] = []  # [(tc_id, tc_name, result_text), ...]

                    # 原子提交缓冲区：本轮全部 tool 结果（denied + approved），
                    # 所有结果齐备后一次性写入（_commit_tool_step_atomically）
                    _pending_tool_results: list[tuple[str, str, str]] = []

                    # ═══ Phase 4-5: Guard + HITL 串行预检（HC3 硬编码）═══
                    approved_calls: list[dict] = []

                    for tc in parsed.tool_calls:
                        tc_id = tc.get("id", generate_id())
                        tc_func = tc.get("function", tc)
                        tc_name = tc_func.get("name", "")
                        tc_args_raw = tc_func.get("arguments", "{}")
                        try:
                            tc_args = json.loads(tc_args_raw) if isinstance(tc_args_raw, str) else tc_args_raw
                        except json.JSONDecodeError:
                            tc_args = {}

                        step_record.tool_calls.append(tc_name)

                        # Phase 4: Guard（HC3）
                        tool_def = self._tool_registry.get_tool(tc_name)
                        if tool_def:
                            guard_result = self._permission_guard.check_permission(
                                self._identity.sensitive_permissions,
                                tool_def.sensitivity,
                                tool_def.sensitive_permission,
                            )
                            if guard_result == "deny":
                                step_record.permission_denied = True
                                self._audit(
                                    AuditEventType.PERMISSION_DENIED,
                                    agent_id=agent_id,
                                    run_id=run_id,
                                    detail=f"Permission denied for tool: {tc_name}",
                                    step_n=step_n,
                                    tool_name=tc_name,
                                )
                                yield _mk(
                                    StreamEventType.PERMISSION_DENIED,
                                    data={"sensitive_permission": tool_def.sensitive_permission.value if tool_def.sensitive_permission else None},
                                    step_n=step_n,
                                    tool_name=tc_name,
                                )
                                if _will_pause:
                                    _deferred_tool_results.append((tc_id, tc_name, f"Error: Permission denied for tool '{tc_name}'"))
                                else:
                                    _pending_tool_results.append((tc_id, tc_name, f"Error: Permission denied for tool '{tc_name}'"))
                                continue

                            # Phase 5: HITL（HC3）
                            hitl_result = self._hitl_controller.check_approval(
                                tool_def.sensitivity.value, tc_name,
                            )
                            if hitl_result == "need_approval":
                                step_record.hitl_requested = True
                                self._safe_hook("on_hitl_requested", tc_name, run_id)
                                self._audit(
                                    AuditEventType.HITL_REQUESTED,
                                    agent_id=agent_id,
                                    run_id=run_id,
                                    detail=f"HITL approval required for: {tc_name}",
                                    step_n=step_n,
                                    tool_name=tc_name,
                                )

                                # 收集批次中尚未检查的 tool_calls
                                current_tc_index = parsed.tool_calls.index(tc)
                                unchecked_after = tuple(
                                    {"id": t.get("id", generate_id()), "name": t.get("function", t).get("name", ""), "args": json.loads(t.get("function", t).get("arguments", "{}")) if isinstance(t.get("function", t).get("arguments", "{}"), str) else t.get("function", t).get("arguments", {})}
                                    for t in parsed.tool_calls[current_tc_index + 1:]
                                )

                                # 构建 PendingApproval 并序列化到 RunState.metadata
                                pending_approval = PendingApproval(
                                    tool_call=tc,
                                    tool_name=tc_name,
                                    tool_args=tc_args,
                                    sensitivity=tool_def.sensitivity.value,
                                    step_n=step_n,
                                    approved_calls_before=tuple(approved_calls),
                                    unchecked_calls_after=unchecked_after,
                                )

                                pause_snapshot = self._memory.snapshot_for_pause()

                                # 序列化 step_records 用于 resume 恢复
                                _serialized_step_records = [
                                    {
                                        "step_n": sr.step_n,
                                        "duration_ms": sr.duration_ms,
                                        "llm_input_tokens": sr.llm_input_tokens,
                                        "llm_output_tokens": sr.llm_output_tokens,
                                        "tool_calls": sr.tool_calls,
                                        "error": sr.error,
                                        "permission_denied": sr.permission_denied,
                                        "hitl_requested": sr.hitl_requested,
                                    }
                                    for sr in step_records
                                ]

                                run_state = RunState(
                                    run_id=run_id,
                                    agent_id=agent_id,
                                    step_n=step_n,
                                    session_id=session_id,
                                    messages=list(pause_snapshot.messages),
                                    pending_tool_call=tc,
                                    working={},
                                    metadata={
                                        "discovered_set": self._tool_registry.discovery.snapshot(),
                                        "pending_approval": {
                                            "tool_call": tc,
                                            "tool_name": tc_name,
                                            "tool_args": tc_args,
                                            "sensitivity": tool_def.sensitivity.value,
                                            "step_n": step_n,
                                            "approved_calls_before": list(approved_calls),
                                            "unchecked_calls_after": list(unchecked_after),
                                        },
                                        # HITL 暂停时持久化已解析到边界的 tool_calls，
                                        # 不含 unchecked_calls_after（它们从未被执行/处理，
                                        # 不应出现在 conversation 中，LLM 下一轮重新决策即可）
                                        "paused_original_tool_calls": parsed.tool_calls[:current_tc_index + 1],
                                        "paused_assistant_content": parsed.content or "",
                                        "paused_reasoning_content": llm_response.get("reasoning_content") if isinstance(llm_response, dict) else None,
                                        "paused_deferred_results": _deferred_tool_results,
                                        # HITL 暂停时持久化运行统计，resume 时恢复
                                        "paused_step_n": step_n,
                                        "paused_total_input_tokens": total_input_tokens + step_input_tokens,
                                        "paused_total_output_tokens": total_output_tokens + step_output_tokens,
                                        "paused_step_records": _serialized_step_records,
                                        # 跨 HITL 循环检测状态持久化
                                        "paused_recent_tool_calls": recent_tool_calls,
                                        "paused_loop_correction_count": loop_correction_count,
                                    },
                                )
                                step_record.duration_ms = (time.monotonic() - step_start) * 1000
                                step_record.hitl_requested = True
                                step_records.append(step_record)
                                self._safe_hook("on_step_end", step_n, run_id)
                                yield _mk(
                                    StreamEventType.STEP_END,
                                    data={"step_n": step_n, "duration_ms": step_record.duration_ms},
                                    step_n=step_n,
                                )

                                result = self._build_result(
                                    success=False, run_id=run_id,
                                    error=f"HITL approval required for: {tc_name}",
                                    terminal_reason=TerminalReason.HITL_PAUSED,
                                    run_state=run_state,
                                    steps=step_records, started_at=started_at, start_mono=start_mono,
                                    total_input_tokens=total_input_tokens + step_input_tokens,
                                    total_output_tokens=total_output_tokens + step_output_tokens,
                                )
                                # ★ 暂停记账/审计必须在 yield 请求事件之前：消费方一看到请求事件即
                                #   return + stream.aclose()，会在下面的 yield 处抛 GeneratorExit，
                                #   yield 之后的语句永不执行 → 记账丢失、审计漏写（历史 bug）。
                                run_terminal_reason = TerminalReason.HITL_PAUSED.value
                                # HC4：本 run 段落因等待人工审批而结束（resume 会再写一条 run_started）。
                                self._audit(
                                    AuditEventType.RUN_FINISHED,
                                    agent_id=agent_id,
                                    run_id=run_id,
                                    detail=f"Paused: HITL approval required for {tc_name}",
                                    step_n=step_n,
                                    terminal_reason=TerminalReason.HITL_PAUSED.value,
                                )
                                yield _mk(
                                    StreamEventType.HITL_REQUESTED,
                                    data={
                                        "approval_id": f"appr-{uuid.uuid4().hex[:12]}",
                                        "sensitivity": tool_def.sensitivity.value,
                                        "run_state": run_state,
                                        "pending_tool_name": tc_name,
                                        "pending_tool_args": tc_args,
                                    },
                                    step_n=step_n,
                                    tool_name=tc_name,
                                )
                                yield _run_end(result, paused=True)
                                return

                            # ═══ 交互型工具检测（Phase 5）═══
                            if tool_def.requires_user_interaction:
                                step_record.tool_calls.append(tc_name)

                                # 构建 PendingInteraction
                                pending = PendingInteraction(
                                    tool_call=tc,
                                    tool_name=tc_name,
                                    tool_args=tc_args,
                                    step_n=step_n,
                                )

                                pause_snapshot = self._memory.snapshot_for_pause()

                                # 序列化 step_records
                                _serialized_step_records_int = [
                                    {
                                        "step_n": sr.step_n,
                                        "duration_ms": sr.duration_ms,
                                        "llm_input_tokens": sr.llm_input_tokens,
                                        "llm_output_tokens": sr.llm_output_tokens,
                                        "tool_calls": sr.tool_calls,
                                        "error": sr.error,
                                        "permission_denied": sr.permission_denied,
                                        "hitl_requested": sr.hitl_requested,
                                    }
                                    for sr in step_records
                                ]

                                run_state = RunState(
                                    run_id=run_id,
                                    agent_id=agent_id,
                                    step_n=step_n,
                                    session_id=session_id,
                                    messages=list(pause_snapshot.messages),
                                    pending_tool_call=tc,
                                    working={},
                                    metadata={
                                        "discovered_set": self._tool_registry.discovery.snapshot(),
                                        "pending_interaction": {
                                            "tool_call": tc,
                                            "tool_name": tc_name,
                                            "tool_args": tc_args,
                                            "step_n": step_n,
                                            # 交互工具之前已通过预检的 calls（resume 时需一起执行）
                                            "approved_calls_before": list(approved_calls),
                                        },
                                        # 交互暂停时持久化已解析到边界的 tool_calls
                                        # （不含后面未检查的），恢复后原子写入保证 API 合约完整性
                                        "paused_original_tool_calls": parsed.tool_calls[:parsed.tool_calls.index(tc) + 1],
                                        "paused_assistant_content": parsed.content or "",
                                        "paused_reasoning_content": llm_response.get("reasoning_content") if isinstance(llm_response, dict) else None,
                                        "paused_deferred_results": _deferred_tool_results,
                                        "paused_step_n": step_n,
                                        "paused_total_input_tokens": total_input_tokens + step_input_tokens,
                                        "paused_total_output_tokens": total_output_tokens + step_output_tokens,
                                        "paused_step_records": _serialized_step_records_int,
                                    },
                                )

                                step_record.duration_ms = (time.monotonic() - step_start) * 1000
                                step_records.append(step_record)
                                self._safe_hook("on_step_end", step_n, run_id)
                                yield _mk(
                                    StreamEventType.STEP_END,
                                    data={"step_n": step_n, "duration_ms": step_record.duration_ms},
                                    step_n=step_n,
                                )

                                result = self._build_result(
                                    success=False, run_id=run_id,
                                    error=f"Interaction requested for: {tc_name}",
                                    terminal_reason=TerminalReason.INTERACTION_PAUSED,
                                    run_state=run_state,
                                    steps=step_records, started_at=started_at, start_mono=start_mono,
                                    total_input_tokens=total_input_tokens + step_input_tokens,
                                    total_output_tokens=total_output_tokens + step_output_tokens,
                                )
                                # ★ 暂停记账/审计必须在 yield 请求事件之前：消费方（executor）一看到请求
                                #   事件即 return + stream.aclose()，会在下面的 yield 处抛 GeneratorExit，
                                #   yield 之后的语句永不执行 → 记账丢失、审计漏写（历史 bug）。
                                run_terminal_reason = TerminalReason.INTERACTION_PAUSED.value
                                # HC4：本 run 段落因等待用户回复而结束（resume 会再写一条 run_started）。
                                self._audit(
                                    AuditEventType.RUN_FINISHED,
                                    agent_id=agent_id,
                                    run_id=run_id,
                                    detail=f"Paused: interaction required for {tc_name}",
                                    step_n=step_n,
                                    terminal_reason=TerminalReason.INTERACTION_PAUSED.value,
                                )
                                yield _mk(
                                    StreamEventType.INTERACTION_REQUESTED,
                                    data={
                                        "request_id": f"req-{uuid.uuid4().hex[:12]}",
                                        "run_state": run_state,
                                        "tool_name": tc_name,
                                        "tool_args": tc_args,
                                    },
                                    step_n=step_n,
                                    tool_name=tc_name,
                                )
                                yield _run_end(result, paused=True)
                                return

                        else:
                            # 未注册工具：不跳过 Guard，记录警告并拒绝执行
                            # （修复 run_stream.py 的 `if tool_def:` 问题，HC3 红线）
                            logger.warning(
                                "Tool '%s' not found in registry — skipping execution (permission denied by default)",
                                tc_name,
                            )
                            step_record.permission_denied = True
                            self._audit(
                                AuditEventType.PERMISSION_DENIED,
                                agent_id=agent_id,
                                run_id=run_id,
                                detail=f"Tool not found in registry: {tc_name}",
                                step_n=step_n,
                                tool_name=tc_name,
                            )
                            yield _mk(
                                StreamEventType.PERMISSION_DENIED,
                                data={"sensitive_permission": "unknown — tool not registered"},
                                step_n=step_n,
                                tool_name=tc_name,
                            )
                            if _will_pause:
                                _deferred_tool_results.append((tc_id, tc_name, f"Error: Tool '{tc_name}' is not registered"))
                            else:
                                _pending_tool_results.append((tc_id, tc_name, f"Error: Tool '{tc_name}' is not registered"))
                            continue

                        approved_calls.append({"id": tc_id, "name": tc_name, "args": tc_args})

                    # ═══ Phase 6: Act ═══
                    if not approved_calls:
                        # 无 approved_calls：权限全部拒绝时，continue 给 LLM 自我纠正机会，
                        # 由跨轮累计 consecutive_permission_denied_rounds >= 3 触发 PERMISSION_EXHAUSTED
                        pass
                    else:
                        # ── 构建 ToolContext.metadata ──
                        # 内置工具 executor 通过 ctx.metadata 获取依赖（无闭包捕获）：
                        #   search_tools  → metadata["tool_store"] + metadata["discovery_manager"]
                        #   search_skills → metadata["skill_registry"]
                        import types as _types_mod
                        _ctx_metadata_dict: dict = {
                            "tool_store": self._tool_registry.store,
                            "discovery_manager": self._tool_registry.discovery,
                            # Layer 2/3：下发取消令牌。cancel-aware 工具在关键点
                            #   ctx.metadata["cancel_token"].raise_if_cancelled()；
                            #   子 Agent 委派处 link 父子 token（见 registry）。
                            "cancel_token": self._cancel_token,
                        }

                        # 注入 skill_registry（可选，仅在有 SkillRegistry 时注入）
                        if self._skill_registry is not None:
                            _ctx_metadata_dict["skill_registry"] = self._skill_registry

                        # 注入 agent_registry（可选，仅在有 SubAgentRegistry 时注入）
                        if self._agent_registry is not None:
                            _ctx_metadata_dict["agent_registry"] = self._agent_registry

                        if metadata:
                            _ctx_metadata_dict.update(metadata)

                        ctx = ToolContext(
                            run_id=run_id,
                            step_n=step_n,
                            agent_id=agent_id,
                            session_id=session_id,
                            permissions=frozenset(
                                p.value
                                for p in self._identity.sensitive_permissions
                            ),
                            trust_level=self._identity.trust_level,
                            working_memory=self._memory.working_memory_accessor,
                            metadata=_types_mod.MappingProxyType(_ctx_metadata_dict) if _ctx_metadata_dict else _types_mod.MappingProxyType({}),
                        )

                        for ac in approved_calls:
                            progress_label = self._resolve_progress_label(ac["name"], ac.get("args", {}))
                            yield _mk(
                                StreamEventType.TOOL_CALL_START,
                                data={
                                    "tool_call_id": ac["id"],
                                    # 5.2: 透传完整 args dict（前端 ToolStartMsg.tool_args: dict）
                                    "tool_args":     ac.get("args", {}),
                                    "args_preview":  str(ac["args"])[:200],
                                    "progress_label": progress_label,
                                },
                                step_n=step_n,
                                tool_name=ac["name"],
                            )

                        remaining_for_tools = step_timeout - (time.monotonic() - step_start)
                        if remaining_for_tools <= 0:
                            raise asyncio.TimeoutError()

                        # ═══ Layer 2：工具执行 vs 取消闸门 竞速（见契约 §3.5）═══
                        # 主路径 / HITL resume / Interaction resume 三处共用
                        # _execute_tools_with_cancel_race（单一实现防漂移）。
                        # step_timeout 兜底语义不变（HC5：remaining 仍由 step_timeout 派生）。
                        tool_results = await self._execute_tools_with_cancel_race(
                            [{"name": ac["name"], "args": ac["args"]} for ac in approved_calls],
                            ctx, remaining_for_tools, step_n,
                        )

                        # ═══ Phase 7-8: Halt + Observe ═══
                        # 本 step 统一原子提交的信号与缓冲区：
                        #  - _tool_halt_signal: 工具要求停机（收集后统一提交再停机）
                        #  - _plan_complete_signal: exit_plan_mode 提交（同上）
                        #  - _pending_tool_results: 全部 tool 结果（denied + approved）
                        _tool_halt_signal: tuple[str, Any] | None = None
                        _plan_complete_signal: tuple[Any, ...] | None = None
                        for ac, tool_result in zip(approved_calls, tool_results):
                            tc_id = ac["id"]
                            tc_name = ac["name"]

                            yield _mk(
                                StreamEventType.TOOL_CALL_END,
                                data=tool_call_end_data(tc_id, ac.get("args", {}), tool_result),
                                step_n=step_n,
                                tool_name=tc_name,
                            )

                            if tool_result.halt:
                                # halt 信号：收集 halt 错误文本 + 记录信号，break 出收尾循环。
                                # 其余已并发执行完的工具 id 由统一提交前的占位防御补
                                # "no result"（本 step 即将停机，不再执行剩余工具分支）。
                                _pending_tool_results.append(
                                    (tc_id, tc_name, tool_result.error or "Error")
                                )
                                if _tool_halt_signal is None:
                                    _tool_halt_signal = (tc_name, tool_result)
                                break

                            # ── Plan 工具结果日志（辅助诊断）──
                            if tc_name in ("enter_plan_mode", "write_plan", "exit_plan_mode"):
                                logger.info(
                                    "[plan-tool] tool=%s success=%s phase=%s",
                                    tc_name, tool_result.success,
                                    plan_manager._plan_phase,
                                )

                            # ═══ Enter Plan Mode 信号检测（委托 PlanManager）═══
                            # LLM 自主调用 enter_plan_mode → PlanManager 接管状态
                            if (
                                tc_name == "enter_plan_mode"
                                and tool_result.success
                                and not plan_manager.is_planning()
                            ):
                                _entered_path = tool_result.plan_path or ""
                                logger.info(
                                    "[plan-enter] enter_plan_mode success: path=%s, "
                                    "phase: %s → planning",
                                    _entered_path, plan_manager._plan_phase,
                                )
                                plan_manager.enter(
                                    file_path=_entered_path,
                                )
                                # 写入计划文件初始内容（用户原始需求）
                                if _entered_path and task:
                                    try:
                                        write_initial_plan_file(_entered_path, task)
                                    except OSError:
                                        logger.warning(
                                            "[plan-enter] failed to write initial plan: %s",
                                            _entered_path,
                                        )
                                # 📝 plan 是 session 级的，写入 session_meta
                                self._memory.set_session_meta("plan_phase", "planning")
                                self._memory.set_session_meta("plan_file_path", _entered_path)
                                self._memory.set_session_meta("plan_submitted_at", None)
                                self._memory.set_session_meta("plan_summary", None)

                            # ═══ exit_plan_mode 信号检测（委托 PlanManager）═══
                            # exit_plan_mode 成功后：写 session_meta + emit 事件 + 终止 run
                            # 注：不要求 is_planning() == True，因为 phase 可能在
                            # 边缘情况下被提前重置。LLM 调用 exit_plan_mode 本身
                            # 就是明确的提交意图，应该无条件触发审批流程。
                            if (
                                tc_name == "exit_plan_mode"
                                and tool_result.success
                            ):
                                logger.info(
                                    "[plan-exit] exit_plan_mode success: phase=%s, "
                                    "plan_path=%s",
                                    plan_manager._plan_phase,
                                    tool_result.plan_path if hasattr(tool_result, 'plan_path') else "?",
                                )
                                # PlanManager.handle_tool_result 消费信号，构建消息
                                consumed, exit_msg = plan_manager.handle_tool_result(
                                    tc_name, tool_result
                                )
                                if consumed and exit_msg:
                                    _pending_tool_results.append((tc_id, tc_name, exit_msg))

                                # 从 result.data 提取关键信息
                                _pc_plan_path = ""
                                _pc_plan_content = ""
                                if isinstance(tool_result.data, dict):
                                    _pc_plan_path = tool_result.data.get("plan_path", "")
                                    _pc_plan_content = tool_result.data.get("plan_content", "")

                                logger.info(
                                    "[plan-submit] plan_path=%s, content_len=%d",
                                    _pc_plan_path, len(_pc_plan_content),
                                )

                                # 📝 计算 plan hash（session_meta 写入移到收尾循环后的
                                #    plan_complete 分支：统一原子提交之后再写）
                                _pc_hash = plan_manager.compute_plan_hash(_pc_plan_content)
                                # ⛔ 规划完成信号：break 出收尾循环，统一提交后走
                                #    plan_complete 分支（session_meta/审计/事件/终止）。
                                _plan_complete_signal = (
                                    tc_name, tool_result,
                                    _pc_plan_path, _pc_plan_content, _pc_hash,
                                )
                                break

                            result_text = render_tool_result_for_llm(tool_result)
                            _pending_tool_results.append((tc_id, tc_name, result_text))

                            # SKILL_ACTIVATED: search_skills 成功后通过 hook 通知 pandapal
                            if (tc_name == "search_skills" and tool_result.success
                                    and self._skill_registry is not None):
                                skill_name = self._skill_registry.get_active_skill_name()
                                if skill_name:
                                    skill = self._skill_registry.get_skill(skill_name)
                                    skill_type = "ACTION" if (skill and skill.is_action) else "KNOWLEDGE"
                                    tools: list[str] = []
                                    if skill and skill.is_action:
                                        action_name = self._skill_registry.get_action_tool_name(skill_name)
                                        if action_name:
                                            tools = [action_name]
                                    self._safe_hook(
                                        "on_skill_activated",
                                        skill_name=skill_name,
                                        skill_type=skill_type,
                                        tools=tools,
                                        run_id=run_id,
                                        step_n=step_n,
                                    )

                        # ═══ 统一原子提交（本 step 全部 tool 结果齐备后一次性写入）═══
                        # 防御：approved_calls 结果缺失（zip 截断等异常）补占位，保证每个 id 都有结果
                        _written_ids = {r[0] for r in _pending_tool_results}
                        for _tc in parsed.tool_calls:
                            _tid = _tc.get("id")
                            if _tid and _tid not in _written_ids:
                                _pending_tool_results.append(
                                    (_tid, _tc.get("function", _tc).get("name", ""),
                                     "Error: Tool execution produced no result")
                                )
                        await self._commit_tool_step_atomically(
                            parsed.content, parsed.tool_calls,
                            llm_response.get("reasoning_content") if isinstance(llm_response, dict) else None,
                            _pending_tool_results,
                        )

                        # ═══ halt 分支处理（原子提交之后停机）═══
                        if _tool_halt_signal is not None:
                            _halt_name, _halt_result = _tool_halt_signal
                            _halt_error = _halt_result.error or "Error"
                            self._audit(
                                AuditEventType.AGENT_TERMINATED,
                                agent_id=agent_id,
                                run_id=run_id,
                                detail=f"Tool halt: {_halt_name} - {_halt_error}",
                                step_n=step_n,
                                tool_name=_halt_name,
                                terminal_reason=TerminalReason.TOOL_HALT.value,
                            )
                            self._safe_hook("on_halt", f"Tool halt: {_halt_name}", run_id)
                            step_record.duration_ms = (time.monotonic() - step_start) * 1000
                            step_records.append(step_record)
                            self._safe_hook("on_step_end", step_n, run_id)
                            yield _mk(
                                StreamEventType.STEP_END,
                                data={"step_n": step_n, "duration_ms": step_record.duration_ms},
                                step_n=step_n,
                            )
                            result = self._build_result(
                                success=False, run_id=run_id,
                                error=f"Tool halt: {_halt_error}",
                                terminal_reason=TerminalReason.TOOL_HALT,
                                steps=step_records, started_at=started_at, start_mono=start_mono,
                                total_input_tokens=total_input_tokens + step_input_tokens,
                                total_output_tokens=total_output_tokens + step_output_tokens,
                            )
                            yield _mk(
                                StreamEventType.AGENT_HALTED,
                                data={"terminal_reason": TerminalReason.TOOL_HALT.value, "error": result.error},
                                step_n=step_n,
                                tool_name=_halt_name,
                            )
                            yield _run_end(result)
                            return

                        # ═══ plan_complete 分支处理（原子提交之后提交审批）═══
                        if _plan_complete_signal is not None:
                            _pc_name, _pc_result, _pc_plan_path, _pc_plan_content, _pc_hash = _plan_complete_signal
                            # 📝 写入 session_meta（跨 run 恢复）
                            self._memory.set_session_meta("plan_phase", "planning")
                            self._memory.set_session_meta("plan_file_path", _pc_plan_path)
                            self._memory.set_session_meta("plan_submitted_at", datetime.now(timezone.utc).isoformat())
                            self._memory.set_session_meta("plan_summary", {
                                "title": _pc_plan_path,
                                "content_hash": _pc_hash[:16],
                            })
                            # ⛔ 规划完成，终止本次 run（等待审批）。
                            # ★ 记账/审计/step 收尾必须在 yield 请求事件之前：消费方一看到
                            #   PLAN_APPROVAL_REQUESTED 即 return + stream.aclose()，下面 yield 处抛
                            #   GeneratorExit，之后的语句永不执行 → 记账丢失、审计漏写（历史 bug）。
                            run_success = True
                            run_terminal_reason = TerminalReason.PLAN_COMPLETE.value
                            step_record.duration_ms = (time.monotonic() - step_start) * 1000
                            step_records.append(step_record)
                            self._safe_hook("on_step_end", step_n, run_id)
                            # HC4：本 run 段落因提交计划、等待审批而结束（resume 会再写 run_started）。
                            self._audit(
                                AuditEventType.RUN_FINISHED,
                                agent_id=agent_id,
                                run_id=run_id,
                                detail=f"Plan submitted: {_pc_plan_path}",
                                step_n=step_n,
                                terminal_reason=TerminalReason.PLAN_COMPLETE.value,
                            )
                            # 📡 发出审批请求事件
                            #    user_id 由应用层通过 metadata 传入；SDK 不做字符串解析
                            yield _mk(
                                StreamEventType.PLAN_APPROVAL_REQUESTED,
                                data={
                                    "plan_path": _pc_plan_path,
                                    "plan_content": _pc_plan_content,
                                    "session_id": session_id,
                                    "user_id": (metadata or {}).get("user_id", ""),
                                },
                                step_n=step_n,
                                tool_name=_pc_name,
                            )
                            yield _mk(
                                StreamEventType.STEP_END,
                                data={"step_n": step_n, "duration_ms": step_record.duration_ms},
                                step_n=step_n,
                            )
                            result = self._build_result(
                                success=True, run_id=run_id,
                                output=f"Plan submitted: {_pc_plan_path}",
                                terminal_reason=TerminalReason.PLAN_COMPLETE,
                                steps=step_records, started_at=started_at, start_mono=start_mono,
                                total_input_tokens=total_input_tokens + step_input_tokens,
                                total_output_tokens=total_output_tokens + step_output_tokens,
                                plan_path=_pc_plan_path,
                            )
                            yield _run_end(result)
                            return

                        if self._skill_registry is not None:
                            # SKILL_CLEARED: 清除前通过 hook 通知 pandapal
                            skill_name = self._skill_registry.get_active_skill_name()
                            if skill_name:
                                self._safe_hook(
                                    "on_skill_cleared",
                                    skill_name=skill_name,
                                    run_id=run_id,
                                )
                            self._skill_registry.clear_active_skill()

                        # ═══ TOOLS_EXHAUSTED 检测 ═══
                        # 所有已批准工具执行都失败时，不立即终止 run，
                        # 而是将错误返回给 LLM，让 LLM 有机会自我纠正
                        # （例如先调 search_tools 加载 schema 再重试）。
                        # max_steps 兜底防止无限循环。
                        if tool_results and all(not tr.success for tr in tool_results):
                            failed_names = [ac["name"] for ac in approved_calls]
                            logger.info(
                                "All tools failed in step %d: %s — returning to LLM for retry",
                                step_n, failed_names,
                            )

                    # 更新累计 token（花费不再由 SDK 计算，见上方花费熔断注释）
                    total_input_tokens += step_input_tokens
                    total_output_tokens += step_output_tokens

                    # 重置连续失败计数
                    consecutive_failures = 0

                    # ═══ 模型循环检测（场景 10）═══
                    if approved_calls:
                        for ac in approved_calls:
                            tc_args_str = json.dumps(ac["args"], sort_keys=True, ensure_ascii=False)
                            recent_tool_calls.append((ac["name"], tc_args_str))
                        if len(recent_tool_calls) > 18:
                            recent_tool_calls = recent_tool_calls[-18:]
                        call_counts = Counter(recent_tool_calls)
                        for (lname, largs), count in call_counts.items():
                            if count >= 5 and len(recent_tool_calls) >= 6:
                                loop_correction_count += 1
                                logger.warning(
                                    "Loop detected: tool '%s' with same args called %d times (correction #%d)",
                                    lname, count, loop_correction_count,
                                )
                                # 用 user 消息提示，不再伪造 tool 消息（loop_detect_N 无对应
                                # assistant tool_call 声明 → 会让 API 400：孤儿 tool 消息）。
                                # 此时本 step 的原子提交已完成，追加 user 消息顺序安全。
                                # inject_user_hint 是同步方法（memory.py:551），不可 await。
                                self._memory.inject_user_hint(
                                    f"WARNING: You are calling tool '{lname}' repeatedly with the same arguments. "
                                    "Please try a different approach or different parameters."
                                )
                                if loop_correction_count >= 5:
                                    step_record.duration_ms = (time.monotonic() - step_start) * 1000
                                    step_records.append(step_record)
                                    self._audit(
                                        AuditEventType.AGENT_TERMINATED,
                                        agent_id=agent_id,
                                        run_id=run_id,
                                        detail=f"LLM loop detected: tool '{lname}' with same args called {count} times",
                                        step_n=step_n,
                                        terminal_reason=TerminalReason.LLM_LOOP_DETECTED.value,
                                    )
                                    self._safe_hook("on_halt", f"LLM loop: tool '{lname}'", run_id)
                                    self._safe_hook("on_step_end", step_n, run_id)
                                    yield _mk(
                                        StreamEventType.STEP_END,
                                        data={"step_n": step_n, "duration_ms": step_record.duration_ms},
                                        step_n=step_n,
                                    )
                                    result = self._build_result(
                                        success=False, run_id=run_id,
                                        error=f"LLM loop detected: tool '{lname}' with same args called {count} times after corrections",
                                        terminal_reason=TerminalReason.LLM_LOOP_DETECTED,
                                        steps=step_records, started_at=started_at, start_mono=start_mono,
                                        total_input_tokens=total_input_tokens,
                                        total_output_tokens=total_output_tokens,
                                    )
                                    yield _mk(
                                        StreamEventType.AGENT_HALTED,
                                        data={
                                            "terminal_reason": TerminalReason.LLM_LOOP_DETECTED.value,
                                            "error": result.error,
                                        },
                                        step_n=step_n,
                                    )
                                    yield _run_end(result)
                                    return
                                break

                    # ═══ 权限全拒绝跨轮累计 ═══
                    if step_record.permission_denied:
                        consecutive_permission_denied_rounds += 1
                        if consecutive_permission_denied_rounds >= 3:
                            step_record.duration_ms = (time.monotonic() - step_start) * 1000
                            step_records.append(step_record)
                            self._audit(
                                AuditEventType.AGENT_TERMINATED,
                                agent_id=agent_id,
                                run_id=run_id,
                                detail=f"Permission exhausted: {consecutive_permission_denied_rounds} consecutive rounds all denied",
                                step_n=step_n,
                                terminal_reason=TerminalReason.PERMISSION_EXHAUSTED.value,
                            )
                            self._safe_hook("on_halt", "Permission exhausted", run_id)
                            self._safe_hook("on_step_end", step_n, run_id)
                            yield _mk(
                                StreamEventType.STEP_END,
                                data={"step_n": step_n, "duration_ms": step_record.duration_ms},
                                step_n=step_n,
                            )
                            result = self._build_result(
                                success=False, run_id=run_id,
                                error=f"Permission exhausted: {consecutive_permission_denied_rounds} consecutive rounds all denied",
                                terminal_reason=TerminalReason.PERMISSION_EXHAUSTED,
                                steps=step_records, started_at=started_at, start_mono=start_mono,
                                total_input_tokens=total_input_tokens,
                                total_output_tokens=total_output_tokens,
                            )
                            yield _mk(
                                StreamEventType.AGENT_HALTED,
                                data={
                                    "terminal_reason": TerminalReason.PERMISSION_EXHAUSTED.value,
                                    "error": result.error,
                                },
                                step_n=step_n,
                            )
                            yield _run_end(result)
                            return
                    else:
                        consecutive_permission_denied_rounds = 0

                    # ─ Step 正常结束：显式发出 STEP_END ─
                    step_record.duration_ms = (time.monotonic() - step_start) * 1000
                    if step_record not in step_records:
                        step_records.append(step_record)
                    self._safe_hook("on_step_end", step_n, run_id)

                    # Plan Mode: 每轮末尾递增 turn 计数器
                    if plan_manager.is_planning():
                        plan_manager.increment_turn()

                    yield _mk(
                        StreamEventType.STEP_END,
                        data={"step_n": step_n, "duration_ms": step_record.duration_ms},
                        step_n=step_n,
                    )

                except asyncio.TimeoutError:
                    # HC5：step_timeout 主动中断
                    # 如果超时发生在 LLM 调用阶段，补记 on_after_llm_call 保持 before/after 对称
                    # _llm_call_start 在 for llm_attempt 循环入口赋值，此处一定已存在
                    self._safe_hook(
                        "on_after_llm_call", {"usage": {}, "error": "timeout"}, run_id,
                        model=_effective_model,
                        duration_ms=(
                            (time.monotonic() - _llm_call_start) * 1000
                            if "_llm_call_start" in locals()
                            else (time.monotonic() - step_start) * 1000
                        ),
                        call_type="main",
                    )
                    step_elapsed = time.monotonic() - step_start
                    step_record.duration_ms = step_elapsed * 1000
                    step_records.append(step_record)
                    self._audit(
                        AuditEventType.AGENT_TERMINATED,
                        agent_id=agent_id,
                        run_id=run_id,
                        detail=f"Step timeout (asyncio.wait_for): {step_elapsed:.1f}s >= {step_timeout}s",
                        step_n=step_n,
                        terminal_reason=TerminalReason.STEP_TIMEOUT.value,
                    )
                    self._safe_hook("on_halt", f"Step {step_n} timeout", run_id)
                    self._safe_hook("on_step_end", step_n, run_id)
                    yield _mk(
                        StreamEventType.STEP_END,
                        data={"step_n": step_n, "duration_ms": step_record.duration_ms},
                        step_n=step_n,
                    )
                    result = self._build_result(
                        success=False, run_id=run_id,
                        error=f"Step {step_n} timeout: {step_elapsed:.1f}s >= {step_timeout}s",
                        terminal_reason=TerminalReason.STEP_TIMEOUT,
                        steps=step_records, started_at=started_at, start_mono=start_mono,
                        total_input_tokens=total_input_tokens,
                        total_output_tokens=total_output_tokens,
                    )
                    yield _mk(
                        StreamEventType.AGENT_HALTED,
                        data={"terminal_reason": TerminalReason.STEP_TIMEOUT.value, "error": result.error},
                        step_n=step_n,
                    )
                    yield _run_end(result)
                    return

                except CancelledSignal as cancel_sig:
                    # 协作式取消统一收口（Layer 1 LLM 逐 chunk / Layer 2 工具边界）。
                    # 与 Layer 0（step 头检查）语义一致，emit AGENT_CANCELLED。
                    # ★ 半截 accumulated_content / in-flight 工具结果均不入 memory
                    #   （避免半句话或无配对 tool_call 污染多轮历史），仅审计留痕。
                    cancel_reason = str(cancel_sig) or "Cancelled by user"
                    # 信号自带的相位标注：Layer 2（工具阶段）此前已正常触发 on_after_llm_call，
                    # 不可重复补记；Layer 1（LLM 阶段）则 before 已触发、after 未触发，需补记对称。
                    _is_tool_phase = getattr(cancel_sig, "phase", None) == "tool"
                    _orphaned = list(getattr(cancel_sig, "orphaned_tools", None) or [])
                    logger.info(
                        "[cancel] run CANCELLED @step=%d · phase=%s · orphaned=%s · reason=%r",
                        step_n, "tool" if _is_tool_phase else "llm", _orphaned, cancel_reason,
                    )
                    if not _is_tool_phase:
                        # Layer 1：补记 on_after_llm_call 保持 before/after 对称
                        self._safe_hook(
                            "on_after_llm_call", {"usage": {}, "error": "cancelled"}, run_id,
                            model=_effective_model,
                            duration_ms=(
                                (time.monotonic() - _llm_call_start) * 1000
                                if "_llm_call_start" in locals()
                                else (time.monotonic() - step_start) * 1000
                            ),
                            call_type="main",
                        )
                    if _is_tool_phase:
                        _detail = (
                            "Cancelled by user (during tool execution"
                            + (f", orphaned tools={_orphaned}" if _orphaned else "")
                            + ")"
                        )
                    else:
                        partial_tokens = len(locals().get("accumulated_content", "") or "")
                        _detail = f"Cancelled by user (mid-LLM-stream, discarded ~{partial_tokens} chars)"
                    step_record.duration_ms = (time.monotonic() - step_start) * 1000
                    step_records.append(step_record)
                    self._audit(
                        AuditEventType.AGENT_TERMINATED,
                        agent_id=agent_id,
                        run_id=run_id,
                        detail=_detail,
                        step_n=step_n,
                        terminal_reason=TerminalReason.CANCELLED.value,
                        # orphaned 工具可能仍在后台跑 → WARN 级留痕（HC4）
                        severity=AuditSeverity.WARN if _orphaned else AuditSeverity.INFO,
                    )
                    self._safe_hook("on_halt", cancel_reason, run_id)
                    self._safe_hook("on_step_end", step_n, run_id)
                    yield _mk(
                        StreamEventType.STEP_END,
                        data={"step_n": step_n, "duration_ms": step_record.duration_ms},
                        step_n=step_n,
                    )
                    result = self._build_result(
                        success=False, run_id=run_id,
                        error=cancel_reason,
                        terminal_reason=TerminalReason.CANCELLED,
                        steps=step_records, started_at=started_at, start_mono=start_mono,
                        total_input_tokens=total_input_tokens,
                        total_output_tokens=total_output_tokens,
                    )
                    # orphaned_tools 透传给前端（可选提示「部分操作可能仍在后台完成」）。
                    _cancel_data: dict[str, Any] = {"error": cancel_reason}
                    if _orphaned:
                        _cancel_data["orphaned_tools"] = _orphaned
                    yield _mk(
                        StreamEventType.AGENT_CANCELLED,
                        data=_cancel_data,
                        step_n=step_n,
                    )
                    yield _run_end(result)
                    return

                except AuditWriteError:
                    # HC4：审计失败向外传播，由外层 except 处理
                    raise

                except Exception as e:
                    consecutive_failures += 1
                    step_record.error = str(e)
                    logger.warning("Step %d error: %s", step_n, e)
                    self._safe_hook("on_error", e, run_id)

                    if consecutive_failures >= 3:
                        step_record.duration_ms = (time.monotonic() - step_start) * 1000
                        step_records.append(step_record)
                        self._audit(
                            AuditEventType.AGENT_TERMINATED,
                            agent_id=agent_id,
                            run_id=run_id,
                            detail=f"Circuit breaker: {consecutive_failures} consecutive failures",
                            step_n=step_n,
                            terminal_reason=TerminalReason.CIRCUIT_BREAKER.value,
                        )
                        self._safe_hook("on_halt", f"Circuit breaker: {consecutive_failures} failures", run_id)
                        self._safe_hook("on_step_end", step_n, run_id)
                        yield _mk(
                            StreamEventType.STEP_END,
                            data={"step_n": step_n, "duration_ms": step_record.duration_ms},
                            step_n=step_n,
                        )
                        result = self._build_result(
                            success=False, run_id=run_id,
                            error=f"Circuit breaker: {consecutive_failures} consecutive failures",
                            terminal_reason=TerminalReason.CIRCUIT_BREAKER,
                            steps=step_records, started_at=started_at, start_mono=start_mono,
                            total_input_tokens=total_input_tokens,
                            total_output_tokens=total_output_tokens,
                        )
                        yield _mk(
                            StreamEventType.AGENT_HALTED,
                            data={
                                "terminal_reason": TerminalReason.CIRCUIT_BREAKER.value,
                                "error": result.error,
                            },
                            step_n=step_n,
                        )
                        yield _run_end(result)
                        return
                    else:
                        # 非致命异常：step 以失败结束，记录并进入下一个 step 重试
                        step_record.duration_ms = (time.monotonic() - step_start) * 1000
                        if step_record not in step_records:
                            step_records.append(step_record)
                        self._safe_hook("on_step_end", step_n, run_id)
                        yield _mk(
                            StreamEventType.STEP_END,
                            data={"step_n": step_n, "duration_ms": step_record.duration_ms},
                            step_n=step_n,
                        )


            # for 循环自然结束 → MAX_STEPS_EXCEEDED
            self._audit(
                AuditEventType.AGENT_TERMINATED,
                agent_id=agent_id,
                run_id=run_id,
                detail=f"Max steps exceeded: {step_counter.count}/{step_counter.max_steps}",
                terminal_reason=TerminalReason.MAX_STEPS_EXCEEDED.value,
            )
            self._safe_hook("on_halt", f"Max steps exceeded: {step_counter.count}/{step_counter.max_steps}", run_id)
            result = self._build_result(
                success=False, run_id=run_id,
                error=f"Max steps exceeded: {step_counter.count}/{step_counter.max_steps}",
                terminal_reason=TerminalReason.MAX_STEPS_EXCEEDED,
                steps=step_records, started_at=started_at, start_mono=start_mono,
                total_input_tokens=total_input_tokens,
                total_output_tokens=total_output_tokens,
            )
            yield _mk(
                StreamEventType.AGENT_HALTED,
                data={
                    "terminal_reason": TerminalReason.MAX_STEPS_EXCEEDED.value,
                    "error": result.error,
                },
            )
            yield _run_end(result)

        except AuditWriteError as e:
            # HC4：审计失败 → 停止执行，仍通过事件传达（不逃逸）
            logger.error("AuditWriteError: %s", e)
            self._safe_hook("on_halt", f"Audit write failed: {e}", run_id)
            result = self._build_result(
                success=False, run_id=run_id,
                error=f"Audit write failed: {e}",
                terminal_reason=TerminalReason.AUDIT_FAILURE,
                steps=step_records, started_at=started_at, start_mono=start_mono,
                total_input_tokens=total_input_tokens,
                total_output_tokens=total_output_tokens,
            )
            yield _mk(
                StreamEventType.AGENT_HALTED,
                data={"terminal_reason": TerminalReason.AUDIT_FAILURE.value, "error": str(e)},
            )
            yield _run_end(result)

        except Exception as e:
            # O3：任何意外异常 → 事件化，不逃逸
            logger.error("Unexpected error in _run_stream_core(): %s", e, exc_info=True)
            try:
                self._audit(
                    AuditEventType.AGENT_TERMINATED,
                    agent_id=agent_id,
                    run_id=run_id,
                    detail=f"Unexpected error: {e}",
                    terminal_reason=TerminalReason.LLM_ERROR.value,
                )
            except Exception:
                logger.debug("terminal AGENT_TERMINATED audit write failed", exc_info=True)
            self._safe_hook("on_halt", f"Unexpected error: {e}", run_id)
            result = self._build_result(
                success=False, run_id=run_id,
                error=str(e),
                terminal_reason=TerminalReason.LLM_ERROR,
                steps=step_records, started_at=started_at, start_mono=start_mono,
                total_input_tokens=total_input_tokens,
                total_output_tokens=total_output_tokens,
            )
            yield _mk(
                StreamEventType.AGENT_HALTED,
                data={"terminal_reason": TerminalReason.LLM_ERROR.value, "error": str(e)},
            )
            yield _run_end(result)

        finally:
            # Layer 3：解除父子取消链（取消监听 task），防止实例复用时跨 run 误取消（契约 §10）。
            if _cascade_monitor_task is not None and not _cascade_monitor_task.done():
                _cascade_monitor_task.cancel()
            self._cancel_token = CancelToken()  # 重建取消令牌，保证 AgentLoop 可复用
            # ★ on_run_end 要收尾 RUN span + 写 run-end 日志，_safe_hook 会把
            #   self._current_session_id 注入 hook 用于分片；故必须先 fire、再清空，
            #   顺序不能颠倒，否则 RUN span / run-end 日志会掉进 _no_session。
            self._safe_hook("on_run_end", run_id, run_success, terminal_reason=run_terminal_reason)
            self._current_session_id = ""  # ★ 数据隔离：清空 session 绑定，防止跨 run 泄漏
            await self._memory.flush_raw_messages()
            await self._memory.end_session()
            # WorkingMemory 是 session 级语义：跨 run 自然保留，
            # 切 session 时由 WorkingMemory.set_session_id() 清空内存 + 重载。
            # 这里不再自动 clear_working()——run 级临时状态不应进入 KV。

    # ═══════════════════════════════════════════════════════════════════════════
    # 内部工具：hook 安全调用
    # ═══════════════════════════════════════════════════════════════════════════

    def _safe_hook(self, method_name: str, *args: Any, **kwargs: Any) -> None:
        """安全调用 hook，异常不传播到主流程。

        _hooks 由 AgentLoop.__slots__ 提供，通过 getattr 访问以避免静态分析误报。

        ★ session_id 一等透传：引擎经 _safe_hook 派发的 hook 全是 run 级
        （on_run_start/end、on_step_*、on_before/after_llm_call、on_hitl_requested、
        on_error、on_halt、on_skill_*），它们的协议签名都声明了关键字参数 session_id。
        这里从引擎自身权威字段 self._current_session_id 注入一次，避免在 40+ 个
        _safe_hook 调用点逐一手传（run_id 是局部变量才逐点传；session_id 是实例字段，
        单点注入更 DRY 且杜绝漏传 → 观测按会话分片不留死角）。非 run 级 hook
        （on_tool_register / on_tool_circuit_* / on_tool_output_truncated）不经此派发，
        天然保持全局（_no_session），不受影响。
        """
        try:
            hooks = getattr(self, "_hooks", None)
            if hooks is None:
                return
            method = getattr(hooks, method_name, None)
            if method:
                kwargs.setdefault("session_id", getattr(self, "_current_session_id", "") or "")
                method(*args, **kwargs)
        except Exception as e:
            logger.warning("Hook %s raised exception (suppressed): %s", method_name, e)

    # ═══════════════════════════════════════════════════════════════════════════
    # 内部工具：progress_label 解析
    # ═══════════════════════════════════════════════════════════════════════════

    def _resolve_progress_label(self, tool_name: str, args: dict | str) -> str | None:
        """从 ToolRegistry 查找工具的 progress_label 并替换占位符。

        用于 TOOL_CALL_START 事件中生成用户可读的进度文本。
        支持占位符 {arg_name} 从 tool_call arguments 中取值（截断到 30 字符）。
        """
        try:
            tool_def = self._tool_registry.get_tool(tool_name)
            if tool_def is None or tool_def.progress_label is None:
                return None

            label = tool_def.progress_label

            # 解析 args（可能是 JSON 字符串）
            parsed_args = args
            if isinstance(args, str):
                try:
                    parsed_args = json.loads(args)
                except Exception:
                    parsed_args = {}

            # 替换占位符 {arg_name} → 实际值（截断到 30 字符）
            if isinstance(parsed_args, dict):
                for k, v in parsed_args.items():
                    label = label.replace(f"{{{k}}}", str(v)[:30])

            return label
        except Exception:
            return None

    # ═══════════════════════════════════════════════════════════════════════════
    # 内部工具：HITL 状态重建
    # ═══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _rebuild_pending_approval(resume_state: RunState) -> PendingApproval | None:
        """从 RunState.metadata 重建 PendingApproval。

        如果 metadata 中没有 pending_approval 信息（兼容旧版 RunState），
        则从 pending_tool_call 字段回退构建。
        """
        meta = resume_state.metadata or {}
        pa_data = meta.get("pending_approval")

        if pa_data:
            return PendingApproval(
                tool_call=pa_data["tool_call"],
                tool_name=pa_data["tool_name"],
                tool_args=pa_data["tool_args"],
                sensitivity=pa_data["sensitivity"],
                step_n=pa_data["step_n"],
                approved_calls_before=tuple(pa_data.get("approved_calls_before", ())),
                unchecked_calls_after=tuple(pa_data.get("unchecked_calls_after", ())),
            )

        # 兼容旧版 RunState（只有 pending_tool_call 字段）
        tc = resume_state.pending_tool_call
        if tc:
            tc_func = tc.get("function", tc)
            tc_name = tc_func.get("name", "")
            tc_args_raw = tc_func.get("arguments", "{}")
            import json as _json
            try:
                tc_args = _json.loads(tc_args_raw) if isinstance(tc_args_raw, str) else tc_args_raw
            except (_json.JSONDecodeError, TypeError):
                tc_args = {}
            return PendingApproval(
                tool_call=tc,
                tool_name=tc_name,
                tool_args=tc_args,
                sensitivity=0,  # 旧版没有存储，用 0 表示未知
                step_n=resume_state.step_n,
            )

        return None


# ═══════════════════════════════════════════════════════════════════════════
# Plan Mode: 用户决策处理（模块级辅助函数）
# ═══════════════════════════════════════════════════════════════════════════

def _handle_plan_action(
    self: Any,  # AgentLoop (RunCoreMixin)
    plan_manager: Any,  # PlanManager
    *,
    plan_action: str,
    message: str | None,
    plan_file_path: str | None,
    edited_plan_content: str | None,
) -> None:
    """处理用户对计划的三向决策（批准 / 完善 / 放弃）。

    由 run_core 在检测到 plan_action 参数时调用。
    """
    from ..plan.files import write_plan_content as _write_plan

    if plan_action == "approve":
        # 批准实施 → 退出 Plan Mode
        plan_manager.exit(approved=True)

        # 用户编辑了计划文本 → 先写回文件
        if edited_plan_content and plan_file_path:
            try:
                _write_plan(plan_file_path, edited_plan_content)
            except OSError:
                pass

        from ..plan.prompt import PLAN_CONTEXT_REMINDER
        plan_manager.set_context_reminder(
            PLAN_CONTEXT_REMINDER.format(plan_file_path=plan_file_path or "")
        )

        # 🧹 清理 session_meta
        self._memory.set_session_meta("plan_phase", None)
        self._memory.set_session_meta("plan_file_path", None)
        self._memory.set_session_meta("plan_submitted_at", None)
        self._memory.set_session_meta("plan_summary", None)
        logger.info("[plan-action] user approved plan")

    elif plan_action == "refine":
        # 方案需完善 → 必须提供具体修改指令，防止 LLM 猜测
        if not message or not message.strip():
            raise ValueError(
                "请描述你希望如何完善方案。例如: "
                "'JWT 也应该在第一阶段支持'、'SSO 扩展点需要预留'、'步骤3的验证方式需要补充'"
            )

        # 方案需完善 → Re-entry
        plan_manager.reenter()
        self._memory.set_session_meta("plan_submitted_at", None)

        refine_reminder = (
            "[系统通知] 你已重新进入 Plan Mode（完善模式）。\n\n"
            f"用户查看了你的计划，认为还需要完善。\n"
            f"用户的具体要求:\n---\n{message}\n---\n\n"
        )
        refine_reminder += (
            "你的任务:\n"
            "1. 仔细阅读用户的具体修改指令\n"
            "2. **只改动用户要求你改的部分** — 不要主动修改未被提及的内容\n"
            "3. 如果指令有歧义，使用 ask_user 澄清\n"
            "4. 修改完成后使用 write_plan 更新计划文件\n"
            "5. 使用 exit_plan_mode 重新提交审批\n\n"
            "仍在 Plan Mode 中。只能使用只读工具 + write_plan + ask_user。"
        )
        plan_manager.set_context_reminder(refine_reminder)
        logger.info("[plan-action] user wants to refine plan: %s", (message or "")[:80])

    elif plan_action == "abandon":
        # 放弃计划 → 清理退出
        plan_manager.exit(approved=False)
        self._memory.set_session_meta("plan_phase", None)
        self._memory.set_session_meta("plan_file_path", None)
        self._memory.set_session_meta("plan_submitted_at", None)
        self._memory.set_session_meta("plan_summary", None)
        from ..plan.prompt import ABANDON_REMINDER
        plan_manager.set_context_reminder(ABANDON_REMINDER)
        logger.info("[plan-action] user abandoned the plan")
