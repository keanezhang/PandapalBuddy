"""pandaren/behavior/harness/executor.py — HarnessExecutor

职责：
  将 R1-R4、S6 五道运行时安全检查包裹在 ToolRegistry.execute_tool() 外层。
  这是 behavior 层对 capability 层的正确包裹方向：behavior → capability。

调用链：
  engine/run_core.py
    └→ HarnessExecutor.execute_tools_concurrent()
         └→ HarnessExecutor.execute_tool()
              → R1 rate_limiter.check()
              → R3 circuit_manager.check()
              → R4 idempotency.check()
              → ToolRegistry.execute_tool()    ← capability 层（纯工具管理+执行）
              → R4 idempotency.store()
              → R3 circuit_manager.record_*()
              → R2 output_guard.check()
              → S6 halt_checker.should_halt()

依赖方向（正确）：
  behavior/harness/executor.py
    → tool/registry.py   (capability 层)
    → tool/models.py     (数据类型)
    → hooks.py           (统一 AgentHooks 协议，顶层模块)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import replace as dc_replace
from typing import Any, TYPE_CHECKING

from .rate_limiter import RateLimiter
from .output_guard import OutputGuard
from .circuit_breaker import CircuitBreakerManager
from .idempotency import IdempotencyGuard
from .halt import HaltChecker

from ...tool.definition.tool_result import COMPOSITE_SOURCE, ToolFeedback, ToolResult
from ...tool.definition.context import ToolContext
from ...hook import AgentHooks

if TYPE_CHECKING:
    from ...tool.registry import ToolRegistry
    from ...observability.audit import AuditLog
    from .tool_feedback import ToolFeedbackProvider

logger = logging.getLogger("pandaren.behavior.harness.executor")


class HarnessExecutor:
    """运行时安全执行器 — behavior 层包裹 capability 层。

    持有 R1-R4、S6 五个 harness 组件 + ToolRegistry 引用。
    engine 层通过本类的 execute_tool / execute_tools_concurrent 执行工具，
    ToolRegistry 退化为纯工具管理（注册、查询、schema 构建）+ 裸执行。

    并发执行的默认上限（DEFAULT_MAX_CONCURRENCY）由本类管理，
    因为并发控制是行为策略的一部分。
    """

    DEFAULT_MAX_CONCURRENCY: int = 5

    #: 框架对**每个** provider 的硬超时（秒）。与应用层自己的检查器超时是两层：
    #: 这层是纵深防御，防应用实现失控，不是给实现方当正常超时用的。
    PROVIDER_HARD_TIMEOUT_SECONDS: float = 10.0

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        audit_log: AuditLog | None = None,
        hooks: AgentHooks | None = None,
        max_concurrency: int | None = None,
        feedback_providers: list[ToolFeedbackProvider] | None = None,
    ) -> None:
        # ── capability 层引用 ──
        self._registry = registry

        # ── R1 — Turn 级调用频率控制器 ──
        self._rate_limiter = RateLimiter()

        # ── R2 — 输出大小控制器 ──
        self._output_guard = OutputGuard()

        # ── R3 — 熔断器管理器 ──
        self._circuit_manager = CircuitBreakerManager()

        # ── R4 — Turn 级幂等性去重守卫 ──
        self._idempotency = IdempotencyGuard()

        # ── S6 — 失败硬停止检查器 ──
        self._halt_checker = HaltChecker()

        # ── AuditLog（HC4）──
        self._audit_log: AuditLog | None = audit_log

        # ── Hooks（统一 AgentHooks 协议）──
        self._hooks: AgentHooks | None = None
        self._hooks_locked: bool = False
        if hooks is not None:
            self.set_hooks(hooks)

        # ── 控制面第四条链：工具执行后的反馈贡献者 ──
        # 默认空列表 → _run_feedback_stage 整个跳过，未注入的 Agent 零开销、零影响。
        self._feedback_providers: list[ToolFeedbackProvider] = list(feedback_providers or ())

        # ── 并发控制 ──
        concurrency = max_concurrency or self.DEFAULT_MAX_CONCURRENCY
        self._semaphore = asyncio.Semaphore(concurrency)

    def set_hooks(self, hooks: AgentHooks) -> None:
        """注入观测 hooks（只允许调用一次，HC4 原则）。"""
        if self._hooks_locked:
            raise RuntimeError(
                "HarnessExecutor.hooks 已注入，不允许二次替换。"
                "审计链一旦建立不可被运行时覆盖（HC4 原则）。"
            )
        self._hooks = hooks
        self._hooks_locked = True
        # 同步注入到需要 hooks 的 harness 组件
        self._circuit_manager.set_hooks(hooks)
        self._output_guard.set_hooks(hooks)

    # ════════════════════════════════════════════════
    #  Turn 级状态管理
    # ════════════════════════════════════════════════

    def reset_turn(self) -> None:
        """每轮开始时重置 turn 级状态。

        由 update_enabled_tools 调用，或由 engine 层在每轮开始时显式调用。
        """
        self._rate_limiter.reset_turn()
        self._idempotency.reset_turn()

    def register_circuit_breaker(self, tool_name: str, config: Any) -> None:
        """为工具注册熔断器（注册阶段调用）。"""
        self._circuit_manager.register(tool_name, config)

    def is_circuit_tripped(self, tool_name: str) -> bool:
        """检查工具是否处于熔断拒绝状态（供 update_enabled_tools 使用）。"""
        return self._circuit_manager.is_tripped(tool_name)

    # ════════════════════════════════════════════════
    #  工具执行（带 harness 检查）
    # ════════════════════════════════════════════════

    async def execute_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """执行单个工具，附带全部 harness 安全检查。

        流水线：
          Pre-harness:
            R1  频率检查
            R3  熔断检查
            R4  幂等检查（async）
          Core:
            → ToolRegistry.execute_tool()（纯 capability 层执行）
          Post-harness:
            R4  幂等缓存写入
            R3  熔断状态更新（失败已在 registry 内处理，这里补充成功路径）
            R2  输出截断
            S6  halt 检查
            HC4 审计写入
            Hook 通知
        """
        logger.debug(
            "[harness_execute] ▶ 进入 | tool=%s | args=%s | run_id=%s | step_n=%s",
            tool_name, args, context.run_id, context.step_n,
        )

        # ── 查找工具定义（用于读取配置参数）──
        tool = self._registry.get_tool(tool_name)
        if tool is None:
            # 工具不存在，直接委托给 registry（它会返回友好错误）
            return await self._registry.execute_tool(tool_name, args, context)

        # ── 统一使用 tool.full_name 确保与注册时的 key 一致 ──
        # tool_name 可能来自 LLM 返回的 safe_name（如 "skill.e4d7f2a1"），
        # 而熔断器/限流器等在注册时使用原始 full_name（如 "skill.天气预报"）。
        # 此处统一使用 tool.full_name 避免 key 不一致。
        resolved_name = tool.full_name

        # ── 交互型工具恢复：将用户回复注入 ctx.metadata ──
        if "_interaction_response" in args:
            import types as _types_mod
            response = args.pop("_interaction_response")
            new_meta = dict(context.metadata) if context.metadata else {}
            new_meta["interaction_response"] = response
            context = ToolContext(
                run_id=context.run_id,
                step_n=context.step_n,
                agent_id=context.agent_id,
                session_id=context.session_id,
                permissions=context.permissions,
                trust_level=context.trust_level,
                namespace=context.namespace,
                metadata=_types_mod.MappingProxyType(new_meta),
                working_memory=context.working_memory,
            )
            logger.debug(
                "[harness_execute] interaction_response injected for tool=%s",
                tool_name,
            )

        start_time = time.monotonic()

        # ── R1: 频率检查 ──
        rate_result = self._rate_limiter.check(resolved_name, tool.max_calls_per_turn)
        if rate_result is not None:
            logger.debug("[harness_execute] ✗ R1 频率限制 | tool=%s", tool_name)
            return rate_result

        # ── R3: 熔断检查 ──
        circuit_result = self._circuit_manager.check(resolved_name)
        if circuit_result is not None:
            logger.debug("[harness_execute] ✗ R3 熔断拦截 | tool=%s", tool_name)
            return circuit_result

        # ── R4: 幂等检查（async） ──
        if not tool.is_idempotent:
            cached = await self._idempotency.check(resolved_name, args)
            if cached:
                logger.debug("[harness_execute] R4 命中幂等缓存 | tool=%s", tool_name)
                return dc_replace(cached, deduplicated=True)

        # ── Hook: on_before_tool_call ──
        # 记录 on_before 是否已触发，供 finally 保证 on_after 必然配对（见下方 finally）。
        _before_fired = False
        if self._hooks:
            self._hooks.on_before_tool_call(
                tool_name, args, context.run_id,
                step_n=context.step_n, session_id=context.session_id,
            )
            _before_fired = True

        result: ToolResult | None = None
        _after_fired = False
        try:
            # ══════════════════════════════════════════
            #  Core: 委托给 ToolRegistry（纯 capability 层）
            # ══════════════════════════════════════════
            result = await self._registry.execute_tool(tool_name, args, context)

            # ── R4: 幂等缓存写入 ──
            if result.success and not tool.is_idempotent:
                await self._idempotency.store(resolved_name, args, result)

            # ── R3: 熔断状态更新 ──
            if result.success:
                self._circuit_manager.record_success(resolved_name)
            else:
                self._circuit_manager.record_failure(resolved_name)

            # ── R2: 输出截断 ──
            if result.success and tool.max_output_bytes:
                result = self._output_guard.check(result, tool.max_output_bytes)

            # ── S6: halt 检查 ──
            should_halt = self._halt_checker.should_halt(result.success, tool.halt_on_failure)
            if should_halt:
                result = dc_replace(result, halt=True)
                logger.error(
                    "工具 '%s' 执行失败且 halt_on_failure=True，触发硬停止: %s",
                    tool_name, result.error,
                )
                if self._hooks:
                    self._hooks.on_halt(
                        reason=f"Tool halt: {tool_name} — {result.error or 'unknown'}",
                        run_id=context.run_id,
                    )

            # ── HC4: 审计写入 ──
            if tool.audit_required:
                self._write_audit(tool=tool, args=args, result=result, context=context)

            # ── Hook: on_after_tool_call + 计时 ──
            duration_ms = (time.monotonic() - start_time) * 1000
            result = dc_replace(result, duration_ms=duration_ms)
            if self._hooks:
                self._hooks.on_after_tool_call(
                    tool_name, result, context.run_id,
                    step_n=context.step_n, duration_ms=duration_ms,
                    session_id=context.session_id,
                )
            _after_fired = True

            # ── 反馈 stage：控制面第四条链（与 R1-R4/审计并列）──
            # 位置固定在此：审计（HC4）已落、on_after 已发，此后才允许外部贡献反馈。
            # 挂载由本方法独占 —— provider 碰不到 result（见 ToolFeedbackProvider 契约）。
            result = await self._run_feedback_stage(tool_name, args, result, context)

            logger.debug(
                "[harness_execute] ◀ 完成 | tool=%s | success=%s | duration_ms=%.2f | "
                "halt=%s | truncated=%s | deduplicated=%s | feedback=%s",
                tool_name, result.success, result.duration_ms,
                result.halt, result.truncated, result.deduplicated,
                result.feedback.source if result.feedback else None,
            )
            return result
        finally:
            # on_before 触发后 on_after 必须配对触发，否则观测适配器的 FIFO 队列
            # （_tool_call_starts / _tool_spans）会泄漏一条 → 下一个同名工具 pop 到过期
            # start（duration 错乱）+ 悬挂 span 直到 run 结束。若 on_before→on_after
            # 之间任一步（idempotency/circuit/output_guard/audit）抛异常，此处补发一次
            # 配对的 on_after（携带真实/合成的失败结果），随后异常继续向上传播（不吞）。
            if _before_fired and not _after_fired and self._hooks:
                _dur = (time.monotonic() - start_time) * 1000
                _paired = (
                    dc_replace(result, duration_ms=_dur)
                    if result is not None else
                    ToolResult(
                        success=False, tool_name=tool_name,
                        error="tool execution raised before a result was produced",
                        duration_ms=_dur,
                    )
                )
                self._hooks.on_after_tool_call(
                    tool_name, _paired, context.run_id,
                    step_n=context.step_n, duration_ms=_dur,
                    session_id=context.session_id,
                )

    # ════════════════════════════════════════════════
    #  反馈 stage（控制面第四条链）
    # ════════════════════════════════════════════════

    async def _run_feedback_stage(
        self,
        tool_name: str,
        args: dict,
        result: ToolResult,
        context: ToolContext,
    ) -> ToolResult:
        """工具执行后运行 provider 链，把合并后的反馈挂到 ToolResult.feedback。

        失败隔离粒度 = **单个 provider**：一个崩溃/超时只丢它自己那条反馈，
        其余照常合并（O3——绝不因反馈自身问题把 run 炸断）。

        注意：本层的 try/except 是**新写**的，不能复用 engine 层的 _safe_hook
        （那在 RunCoreMixin 上，属 engine 层；本 stage 在 behavior 层）。execute_tool
        的 finally 块只保证 on_before→on_after 配对补发，**不吞异常**，指望不上。

        必须用 dc_replace 而非就地改字段：R4 幂等缓存存的是 store() 当时的**对象引用**，
        就地 mutation 会把反馈焊进缓存对象，导致后续命中时重放过期诊断。
        """
        if not self._feedback_providers:
            return result                                   # 默认路径：零开销跳过

        collected: list[ToolFeedback] = []
        for provider in self._feedback_providers:
            source = getattr(provider, "source", None) or type(provider).__name__
            # 权限边界：交给 provider 的是**防御性副本**，不是真结果。
            # ToolResult 是可变 dataclass，光靠 Protocol 文档写「只读」拦不住任何人——
            # 一行 `result.success = False` 就能让 Agent 看到的与审计日志（HC4，已在本
            # stage 之前落盘）分道扬镳：工具明明失败了却被改成成功。副本让这条路
            # **物理上走不通**，契约从此是机制而非君子协定。
            # 注意：浅拷贝只挡住字段改绑（success/data/error 三条设计点名的攻击面）；
            # data 内部若是可变容器仍可被就地改。深拷贝对任意工具载荷代价与风险都过高，
            # 故不做——真需要时应另开 ToolResultRedactor 链，单独设计、单独审。
            probe = dc_replace(result)
            try:
                fb = await asyncio.wait_for(
                    provider.provide(tool_name, args, probe, context),
                    timeout=self.PROVIDER_HARD_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "[tool_feedback] provider 超过硬上限 %.1fs，丢弃该条 | tool=%s | provider=%s",
                    self.PROVIDER_HARD_TIMEOUT_SECONDS, tool_name, source,
                )
                continue
            except asyncio.CancelledError:
                raise                                       # 取消优先级最高，不吞
            except Exception as e:                          # noqa: BLE001 — O3 兜底，必须全包
                logger.error(
                    "[tool_feedback] provider 抛异常，丢弃该条 | tool=%s | provider=%s | error=%s",
                    tool_name, source, e, exc_info=True,
                )
                continue

            if fb is not None:
                collected.append(fb)

        merged = self._merge_feedback(collected)
        if merged is None:
            return result                                   # 全 None → 零打扰不变

        logger.info(
            "[tool_feedback] 已挂载 | tool=%s | source=%s | severity=%s",
            tool_name, merged.source, merged.severity.name,
        )
        return dc_replace(result, feedback=merged)

    @staticmethod
    def _merge_feedback(items: list[ToolFeedback]) -> ToolFeedback | None:
        """多源反馈合并：**全部拼接**，不取首个。

        「这文件有 lint 错误」与「这文件泄漏了密钥」是正交事件，两条都必须到达 LLM。
        取首个非 None 意味着谁注册在前谁赢、另一条被静默丢弃 —— 静默丢掉密钥告警是安全事故。

        ── 混合可见性（llm_visible 有真有假）─────────────────────────────
        典型：门控「检查通过」（不可见状态播报）+ 密钥扫描「发现密钥」（可见告警）。
        合并结果只有**一个** text，而它同时喂两个受众，故必须取舍：

          全不可见 → 合并为不可见。纯状态播报，LLM 依旧一个 token 不花。
          有可见的 → 合并为可见，且 text **只拼可见的那些**。

        后者会丢掉不可见分段（用户少看到一个绿灯）。这是有意的取舍：状态播报是装饰，
        告警是载荷 —— 宁可少一个绿角标，也绝不能把「检查通过」混进 LLM 该读的告警里，
        那会让模型误以为一切正常。真需要同时呈现时，正解是让 ToolResult.feedback 变成
        **列表**（N 个 provider → N 条反馈，各自带受众），而非在这里把两种东西揉成一条。
        """
        if not items:
            return None
        if len(items) == 1:
            return items[0]

        visible = [fb for fb in items if fb.llm_visible]
        # 无可见分段：整条合并结果对 LLM 隐身，text 拼全部（只有 UI 会读到）
        parts = visible or items
        return ToolFeedback(
            # 各分段自带 [source] 标签（HC4 可溯源）；渲染层据 COMPOSITE_SOURCE 不再加外层标签
            text="\n\n".join(f"[{fb.source}] {fb.text}" for fb in parts),
            severity=max(fb.severity for fb in parts),      # 取最高，供 block 级门控判定
            source=COMPOSITE_SOURCE,
            llm_visible=bool(visible),
        )

    # ════════════════════════════════════════════════
    #  并发工具执行
    # ════════════════════════════════════════════════

    async def execute_tools_concurrent(
        self,
        calls: list[dict],
        context: ToolContext,
    ) -> list[ToolResult]:
        """并发执行同一步骤内的所有 tool_calls（C1 原则）。

        Semaphore 限流 + gather 并发 + 失败仲裁。
        结果顺序与 calls 严格对齐，永不抛异常（O3 原则）。
        """
        async def _guarded_execute(call: dict) -> ToolResult:
            async with self._semaphore:
                return await self.execute_tool(call["name"], call["args"], context)

        tasks = [_guarded_execute(call) for call in calls]
        results = list(await asyncio.gather(*tasks, return_exceptions=False))

        # C1 失败仲裁
        # 仅在**真正并发**（同一 step 派发了 >1 个 tool_call）且其中有失败时才触发。
        # 单工具失败已由 on_after_tool_call → tool_execute_total{status=error} 计入，
        # 不应再进 on_concurrent_execution_failure → error_total{concurrent_tool_failure}：
        # 否则普通的单工具校验错（如 ask_user options 数量非法）会被误记为「并发执行失败」，
        # 既名不副实又与工具级 error 双重计数，淹没真正的 C1 并发仲裁失败信号。
        has_failure = any(not r.success for r in results)
        if has_failure and len(calls) > 1 and self._hooks:
            failed_names = [r.tool_name for r in results if not r.success]
            self._hooks.on_concurrent_execution_failure(
                tool_names=failed_names,
                run_id=context.run_id,
                step_n=context.step_n,
                session_id=context.session_id,
            )

        return results

    # ════════════════════════════════════════════════
    #  审计写入（HC4）
    # ════════════════════════════════════════════════

    def _write_audit(
        self,
        tool: Any,
        args: dict,
        result: ToolResult,
        context: ToolContext,
    ) -> None:
        """同步审计记录写入（从 registry 平移过来，逻辑完全一致）。"""
        try:
            args_hash = hashlib.sha256(
                json.dumps(args, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
        except Exception:
            args_hash = "serialization_failed"

        if self._audit_log is not None:
            from ...observability.types import AuditEventType
            detail = (
                f"tool={tool.full_name} sensitivity={tool.sensitivity.name} "
                f"success={result.success} args_hash={args_hash}"
                + (f" error={result.error}" if result.error else "")
            )
            self._audit_log.write_sync(
                AuditEventType.TOOL_EXECUTED,
                agent_id=context.agent_id,
                run_id=context.run_id,
                detail=detail,
                session_id=context.session_id,
                step_n=context.step_n,
                tool_name=tool.full_name,
            )
        else:
            audit_logger = logging.getLogger("pandaren.tool.audit")
            audit_logger.info(
                "TOOL_AUDIT",
                extra={
                    "tool_name": tool.full_name,
                    "sensitivity": tool.sensitivity.name,
                    "agent_id": context.agent_id,
                    "run_id": context.run_id,
                    "step_n": context.step_n,
                    "success": result.success,
                    "args_hash": args_hash,
                    "error": result.error,
                },
            )
