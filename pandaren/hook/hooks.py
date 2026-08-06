"""pandaren/hooks.py — AgentHooks 统一生命周期协议（21 个扩展点）

合并原 LoopHooks（engine 层事件）和 ToolHooks（tool/harness 层事件），
消除重叠（before_tool_call / after_tool_call / halt），统一为一套 hook。

分区：
  A. Run 生命周期（2）   — on_run_start, on_run_end
  B. Step 生命周期（2）  — on_step_start, on_step_end
  C. LLM 调用（2）       — on_before_llm_call, on_after_llm_call
  D. Tool 执行（2）      — on_before_tool_call, on_after_tool_call
  E. Tool 管理（3）      — on_tool_register, on_tool_discover, on_tool_disabled
  F. Harness 事件（4）   — on_tool_circuit_open, on_tool_circuit_close,
                           on_tool_output_truncated, on_concurrent_execution_failure
  G. 控制流事件（4）     — on_hitl_requested, on_hitl_resolved, on_error, on_halt
  H. Skill 生命周期（2） — on_skill_activated, on_skill_cleared

所有方法有空默认实现（pass），不强制覆写。
消费者（engine/run_core、ToolRegistry、HarnessExecutor、CircuitBreakerManager、OutputGuard）
共享同一个实例。

── session_id 一等透传（数据隔离）────────────────────────────────────────
所有 **run 级** hook（凡带 run_id 者）签名都声明关键字参数 `session_id: str = ""`，
与 run_id 同为「本次 run 的归属凭证」，供观测后端（logs.md / traces.md）按会话分片：
  - 引擎侧：run_core._safe_hook 从 self._current_session_id 单点注入。
  - Harness / Tool 侧：executor / facade 从 ToolContext.session_id 显式传入。
  - CompositeAgentHooks 原样转发给每个子 hook。
自定义 AgentHooks 实现必须接受该关键字参数（可忽略）。

**非 run 级** hook（on_tool_register 建期、on_tool_circuit_* / on_tool_output_truncated
为跨 run 的 harness 级状态）**不带** session_id——它们天然无会话归属，观测落 _no_session，
属明确的「全局级」而非污染。
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Protocol, runtime_checkable, TYPE_CHECKING

if TYPE_CHECKING:
    from .tool.types import ToolTier, SensitivityLevel

_logger = logging.getLogger(__name__)


@runtime_checkable
class AgentHooks(Protocol):
    """统一生命周期扩展点。21 个 hook，全部 optional（空默认实现）。

    run 级 hook 均声明关键字参数 session_id（见模块文档）。
    """

    # ═══ A. Run 生命周期 ═══

    def on_run_start(self, task: str, run_id: str, *, session_id: str = "") -> None: ...
    def on_run_end(self, run_id: str, success: bool, *, terminal_reason: str = "", session_id: str = "") -> None: ...

    # ═══ B. Step 生命周期 ═══

    def on_step_start(self, step_n: int, run_id: str, *, session_id: str = "") -> None: ...
    def on_step_end(self, step_n: int, run_id: str, *, session_id: str = "") -> None: ...

    # ═══ C. LLM 调用 ═══

    def on_before_llm_call(
        self, messages: list[dict], run_id: str,
        model: str = "", tools: list[dict] | None = None,
        *, call_type: str = "main", session_id: str = "", provider: str = "",
    ) -> None: ...
    def on_after_llm_call(
        self, response: Any, run_id: str,
        model: str = "",
        *, duration_ms: float | None = None, call_type: str | None = None,
        session_id: str = "", provider: str = "",
    ) -> None: ...

    # ═══ D. Tool 执行 ═══

    def on_before_tool_call(
        self, tool_name: str, args: dict, run_id: str,
        *, step_n: int = 0, session_id: str = "",
    ) -> None: ...
    """工具执行开始。
    step_n 由 HarnessExecutor 传入（engine 层可不传，默认 0）。
    """

    def on_after_tool_call(
        self, tool_name: str, result: Any, run_id: str,
        *, step_n: int = 0, duration_ms: float = 0.0, session_id: str = "",
    ) -> None: ...
    """工具执行结束。
    step_n 和 duration_ms 由 HarnessExecutor 传入。
    """

    # ═══ E. Tool 管理 ═══

    def on_tool_register(
        self, tool_name: str,
        tier: ToolTier, sensitivity: SensitivityLevel,
        namespace: str | None,
    ) -> None: ...

    def on_tool_discover(self, tool_name: str, query: str, run_id: str, *, session_id: str = "") -> None: ...

    def on_tool_disabled(self, tool_name: str, reason: str, run_id: str, *, session_id: str = "") -> None: ...

    # ═══ F. Harness 事件（跨 run 的 harness 级，无会话归属）═══

    def on_tool_circuit_open(
        self, tool_name: str,
        failure_count: int, recovery_timeout: float,
    ) -> None: ...

    def on_tool_circuit_close(self, tool_name: str) -> None: ...

    def on_tool_output_truncated(
        self, tool_name: str,
        original_size: int, max_size: int,
    ) -> None: ...

    def on_concurrent_execution_failure(
        self, tool_names: list[str],
        run_id: str, step_n: int, *, session_id: str = "",
    ) -> None: ...

    # ═══ G. 控制流事件 ═══

    def on_hitl_requested(self, tool_name: str, run_id: str, *, session_id: str = "") -> None: ...
    def on_hitl_resolved(self, tool_name: str, decision: str, run_id: str, *, session_id: str = "") -> None: ...
    """HITL 审批被人工裁决后触发（与 on_hitl_requested 配对，在 resume 段落里）。

    decision: "approved" | "rejected"。用于把审批「结果」计入指标
    （hitl_approval_total{result=approved|rejected}），补齐 on_hitl_requested
    只记 need_approval 的观测缺口——否则审批通过率/拒绝数在指标层不可观测。
    """
    def on_error(self, error: Exception, run_id: str, *, session_id: str = "") -> None: ...
    def on_halt(self, reason: str, run_id: str, *, session_id: str = "") -> None: ...

    # ═══ H. Skill 生命周期（2）═══

    def on_skill_activated(
        self, skill_name: str, skill_type: str,
        tools: list[str], run_id: str, step_n: int, *, session_id: str = "",
    ) -> None: ...
    """Skill 在 search_skills 中成功激活后触发。

    skill_type: "KNOWLEDGE" | "ACTION"
    tools: Action Skill 注册的 Tool 名列表（Knowledge Skill 为空列表）。
    """

    def on_skill_cleared(
        self, skill_name: str, run_id: str, *, session_id: str = "",
    ) -> None: ...
    """Turn 结束时 clear_active_skill() 调用后触发。"""


class DefaultAgentHooks:
    """空默认实现。全部 hook 为 pass。

    run 级 hook 均接受关键字参数 session_id（默认空串），自定义子类覆写时
    须保留该关键字参数（可忽略），否则会被 _safe_hook / Composite 的注入打断。
    """

    # ═══ A. Run 生命周期 ═══

    def on_run_start(self, task: str, run_id: str, *, session_id: str = "") -> None:
        pass

    def on_run_end(self, run_id: str, success: bool, *, terminal_reason: str = "", session_id: str = "") -> None:
        pass

    # ═══ B. Step 生命周期 ═══

    def on_step_start(self, step_n: int, run_id: str, *, session_id: str = "") -> None:
        pass

    def on_step_end(self, step_n: int, run_id: str, *, session_id: str = "") -> None:
        pass

    # ═══ C. LLM 调用 ═══

    def on_before_llm_call(
        self, messages: list[dict], run_id: str,
        model: str = "", tools: list[dict] | None = None,
        *, call_type: str = "main", session_id: str = "", provider: str = "",
    ) -> None:
        pass

    def on_after_llm_call(
        self, response: Any, run_id: str,
        model: str = "",
        *, duration_ms: float | None = None, call_type: str | None = None,
        session_id: str = "", provider: str = "",
    ) -> None:
        pass

    # ═══ D. Tool 执行 ═══

    def on_before_tool_call(
        self, tool_name: str, args: dict, run_id: str,
        *, step_n: int = 0, session_id: str = "",
    ) -> None:
        pass

    def on_after_tool_call(
        self, tool_name: str, result: Any, run_id: str,
        *, step_n: int = 0, duration_ms: float = 0.0, session_id: str = "",
    ) -> None:
        pass

    # ═══ E. Tool 管理 ═══

    def on_tool_register(
        self, tool_name: str,
        tier: ToolTier, sensitivity: SensitivityLevel,
        namespace: str | None,
    ) -> None:
        pass

    def on_tool_discover(self, tool_name: str, query: str, run_id: str, *, session_id: str = "") -> None:
        pass

    def on_tool_disabled(self, tool_name: str, reason: str, run_id: str, *, session_id: str = "") -> None:
        pass

    # ═══ F. Harness 事件（跨 run 的 harness 级，无会话归属）═══

    def on_tool_circuit_open(
        self, tool_name: str,
        failure_count: int, recovery_timeout: float,
    ) -> None:
        pass

    def on_tool_circuit_close(self, tool_name: str) -> None:
        pass

    def on_tool_output_truncated(
        self, tool_name: str,
        original_size: int, max_size: int,
    ) -> None:
        pass

    def on_concurrent_execution_failure(
        self, tool_names: list[str],
        run_id: str, step_n: int, *, session_id: str = "",
    ) -> None:
        pass

    # ═══ G. 控制流事件 ═══

    def on_hitl_requested(self, tool_name: str, run_id: str, *, session_id: str = "") -> None:
        pass

    def on_hitl_resolved(self, tool_name: str, decision: str, run_id: str, *, session_id: str = "") -> None:
        pass

    def on_error(self, error: Exception, run_id: str, *, session_id: str = "") -> None:
        pass

    def on_halt(self, reason: str, run_id: str, *, session_id: str = "") -> None:
        pass

    # ═══ H. Skill 生命周期（2）═══

    def on_skill_activated(
        self, skill_name: str, skill_type: str,
        tools: list[str], run_id: str, step_n: int, *, session_id: str = "",
    ) -> None:
        pass

    def on_skill_cleared(
        self, skill_name: str, run_id: str, *, session_id: str = "",
    ) -> None:
        pass


class CompositeAgentHooks:
    """组合多个 AgentHooks 实例，按注册顺序链式调用。

    Builder 层用此类来组合 ObservabilityHooksAdapter（框架内置观测）
    和用户自定义 Hooks（如 SkillAwareHooks），让两者共存而非互斥。

    容错：单个 hook 抛异常不中断后续 hook 的执行。

    run 级 hook 会把 session_id 原样转发给每个子 hook；子 hook 须遵循新契约
    （接受关键字参数 session_id）。
    """

    def __init__(self) -> None:
        self._hooks: list = []
        # 签名内省缓存：(id(hook), method_name, param) → 该 hook 的方法是否接受 param。
        # 用于向后兼容——on_before/after_llm_call 新增了 provider 关键字，外部旧签名的
        # AgentHooks 实现（无 provider）不应因 TypeError 被静默吞成 debug 而回调失效。
        self._sig_cache: dict[tuple[int, str, str], bool] = {}

    def add(self, hooks) -> None:
        """按顺序追加 hook 实例（先加的优先执行）。"""
        if hooks is not None:
            self._hooks.append(hooks)

    def _accepts(self, hook, method_name: str, param: str) -> bool:
        """该 hook 的 method_name 是否接受关键字参数 param（含 **kwargs 兜底）。

        无法内省（C 扩展等）→ 假定接受，走正常路径。结果按 (id(hook),method,param) 缓存，
        避免每次 LLM 调用都做 inspect（hook 生命周期内签名不变）。
        """
        key = (id(hook), method_name, param)
        cached = self._sig_cache.get(key)
        if cached is not None:
            return cached
        try:
            sig = inspect.signature(getattr(hook, method_name))
            ok = param in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
        except (TypeError, ValueError, AttributeError):
            ok = True
        self._sig_cache[key] = ok
        return ok

    def clone(self) -> "CompositeAgentHooks":
        """浅拷贝：产出一份新的 CompositeAgentHooks，内部 _hooks 列表复制引用。

        用于 AgentBlueprint.materialize()：每个 session 拿到独立的 CompositeAgentHooks
        实例，避免共享 buffer 状态；但内部各 hook 元素仍是共享的
        （ObservabilityHooksAdapter 是无内部 buffer 的单例）。

        如果未来某个 hook 元素本身有内部 buffer 需要独立化，需在该元素上
        单独实现 clone()，并在此处递归调用。本期不做（YAGNI）。
        """
        new_composite = CompositeAgentHooks()
        new_composite._hooks = list(self._hooks)
        return new_composite

    # ═══ A. Run 生命周期 ═══

    def on_run_start(self, task: str, run_id: str, *, session_id: str = "") -> None:
        for h in self._hooks:
            try:
                h.on_run_start(task, run_id, session_id=session_id)
            except Exception:
                _logger.debug("CompositeAgentHooks: on_run_start failed", exc_info=True)

    def on_run_end(self, run_id: str, success: bool, *, terminal_reason: str = "", session_id: str = "") -> None:
        for h in self._hooks:
            try:
                h.on_run_end(run_id, success, terminal_reason=terminal_reason, session_id=session_id)
            except Exception:
                _logger.debug("CompositeAgentHooks: on_run_end failed", exc_info=True)

    # ═══ B. Step 生命周期 ═══

    def on_step_start(self, step_n: int, run_id: str, *, session_id: str = "") -> None:
        for h in self._hooks:
            try:
                h.on_step_start(step_n, run_id, session_id=session_id)
            except Exception:
                _logger.debug("CompositeAgentHooks: on_step_start failed", exc_info=True)

    def on_step_end(self, step_n: int, run_id: str, *, session_id: str = "") -> None:
        for h in self._hooks:
            try:
                h.on_step_end(step_n, run_id, session_id=session_id)
            except Exception:
                _logger.debug("CompositeAgentHooks: on_step_end failed", exc_info=True)

    # ═══ C. LLM 调用 ═══

    def on_before_llm_call(
        self,
        messages: list[dict], run_id: str,
        model: str = "", tools: list[dict] | None = None,
        *, call_type: str = "main", session_id: str = "", provider: str = "",
    ) -> None:
        for h in self._hooks:
            try:
                kwargs: dict[str, Any] = dict(
                    model=model, tools=tools, call_type=call_type, session_id=session_id
                )
                # 向后兼容：仅对声明了 provider 的 hook 传入，旧签名 hook 降级为不传（不失效）。
                if self._accepts(h, "on_before_llm_call", "provider"):
                    kwargs["provider"] = provider
                h.on_before_llm_call(messages, run_id, **kwargs)
            except Exception:
                _logger.debug("CompositeAgentHooks: on_before_llm_call failed", exc_info=True)

    def on_after_llm_call(
        self, response: Any, run_id: str,
        model: str = "",
        *, duration_ms: float | None = None, call_type: str | None = None,
        session_id: str = "", provider: str = "",
    ) -> None:
        for h in self._hooks:
            try:
                kwargs: dict[str, Any] = dict(
                    model=model, duration_ms=duration_ms, call_type=call_type, session_id=session_id
                )
                # 向后兼容：仅对声明了 provider 的 hook 传入，旧签名 hook 降级为不传（不失效）。
                if self._accepts(h, "on_after_llm_call", "provider"):
                    kwargs["provider"] = provider
                h.on_after_llm_call(response, run_id, **kwargs)
            except Exception:
                _logger.debug("CompositeAgentHooks: on_after_llm_call failed", exc_info=True)

    # ═══ D. Tool 执行 ═══

    def on_before_tool_call(
        self, tool_name: str, args: dict, run_id: str,
        *, step_n: int = 0, session_id: str = "",
    ) -> None:
        for h in self._hooks:
            try:
                h.on_before_tool_call(tool_name, args, run_id, step_n=step_n, session_id=session_id)
            except Exception:
                _logger.debug("CompositeAgentHooks: on_before_tool_call failed", exc_info=True)

    def on_after_tool_call(
        self, tool_name: str, result: Any, run_id: str,
        *, step_n: int = 0, duration_ms: float = 0.0, session_id: str = "",
    ) -> None:
        for h in self._hooks:
            try:
                h.on_after_tool_call(tool_name, result, run_id, step_n=step_n, duration_ms=duration_ms, session_id=session_id)
            except Exception:
                _logger.debug("CompositeAgentHooks: on_after_tool_call failed", exc_info=True)

    # ═══ E. Tool 管理 ═══

    def on_tool_register(
        self, tool_name: str,
        tier: Any, sensitivity: Any,
        namespace: str | None,
    ) -> None:
        for h in self._hooks:
            try:
                h.on_tool_register(tool_name, tier, sensitivity, namespace)
            except Exception:
                _logger.debug("CompositeAgentHooks: on_tool_register failed", exc_info=True)

    def on_tool_discover(self, tool_name: str, query: str, run_id: str, *, session_id: str = "") -> None:
        for h in self._hooks:
            try:
                h.on_tool_discover(tool_name, query, run_id, session_id=session_id)
            except Exception:
                _logger.debug("CompositeAgentHooks: on_tool_discover failed", exc_info=True)

    def on_tool_disabled(self, tool_name: str, reason: str, run_id: str, *, session_id: str = "") -> None:
        for h in self._hooks:
            try:
                h.on_tool_disabled(tool_name, reason, run_id, session_id=session_id)
            except Exception:
                _logger.debug("CompositeAgentHooks: on_tool_disabled failed", exc_info=True)

    # ═══ F. Harness 事件（跨 run 的 harness 级，无会话归属）═══

    def on_tool_circuit_open(
        self, tool_name: str,
        failure_count: int, recovery_timeout: float,
    ) -> None:
        for h in self._hooks:
            try:
                h.on_tool_circuit_open(tool_name, failure_count, recovery_timeout)
            except Exception:
                _logger.debug("CompositeAgentHooks: on_tool_circuit_open failed", exc_info=True)

    def on_tool_circuit_close(self, tool_name: str) -> None:
        for h in self._hooks:
            try:
                h.on_tool_circuit_close(tool_name)
            except Exception:
                _logger.debug("CompositeAgentHooks: on_tool_circuit_close failed", exc_info=True)

    def on_tool_output_truncated(
        self, tool_name: str,
        original_size: int, max_size: int,
    ) -> None:
        for h in self._hooks:
            try:
                h.on_tool_output_truncated(tool_name, original_size, max_size)
            except Exception:
                _logger.debug("CompositeAgentHooks: on_tool_output_truncated failed", exc_info=True)

    def on_concurrent_execution_failure(
        self, tool_names: list[str],
        run_id: str, step_n: int, *, session_id: str = "",
    ) -> None:
        for h in self._hooks:
            try:
                h.on_concurrent_execution_failure(tool_names, run_id, step_n, session_id=session_id)
            except Exception:
                _logger.debug("CompositeAgentHooks: on_concurrent_execution_failure failed", exc_info=True)

    # ═══ G. 控制流事件 ═══

    def on_hitl_requested(self, tool_name: str, run_id: str, *, session_id: str = "") -> None:
        for h in self._hooks:
            try:
                h.on_hitl_requested(tool_name, run_id, session_id=session_id)
            except Exception:
                _logger.debug("CompositeAgentHooks: on_hitl_requested failed", exc_info=True)

    def on_hitl_resolved(self, tool_name: str, decision: str, run_id: str, *, session_id: str = "") -> None:
        for h in self._hooks:
            try:
                h.on_hitl_resolved(tool_name, decision, run_id, session_id=session_id)
            except Exception:
                _logger.debug("CompositeAgentHooks: on_hitl_resolved failed", exc_info=True)

    def on_error(self, error: Exception, run_id: str, *, session_id: str = "") -> None:
        for h in self._hooks:
            try:
                h.on_error(error, run_id, session_id=session_id)
            except Exception:
                _logger.debug("CompositeAgentHooks: on_error failed", exc_info=True)

    def on_halt(self, reason: str, run_id: str, *, session_id: str = "") -> None:
        for h in self._hooks:
            try:
                h.on_halt(reason, run_id, session_id=session_id)
            except Exception:
                _logger.debug("CompositeAgentHooks: on_halt failed", exc_info=True)

    # ═══ H. Skill 生命周期 ═══

    def on_skill_activated(
        self, skill_name: str, skill_type: str,
        tools: list[str], run_id: str, step_n: int, *, session_id: str = "",
    ) -> None:
        for h in self._hooks:
            try:
                h.on_skill_activated(skill_name, skill_type, tools, run_id, step_n, session_id=session_id)
            except Exception:
                _logger.debug("CompositeAgentHooks: on_skill_activated failed", exc_info=True)

    def on_skill_cleared(
        self, skill_name: str, run_id: str, *, session_id: str = "",
    ) -> None:
        for h in self._hooks:
            try:
                h.on_skill_cleared(skill_name, run_id, session_id=session_id)
            except Exception:
                _logger.debug("CompositeAgentHooks: on_skill_cleared failed", exc_info=True)


# ── 向后兼容别名（过渡期，后续可删除）──
LoopHooks = AgentHooks
DefaultLoopHooks = DefaultAgentHooks
ToolHooks = AgentHooks
