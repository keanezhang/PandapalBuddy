"""pandapal/quality/gate.py — CodeQualityGate

把"改完代码要检查"从软倡议变成框架强制：Agent 写完 .py，门控立刻用**与 CI 相同的
规则**跑 ruff，诊断随**同一条 tool 消息**回到 Agent 下一轮上下文。

── 一个类一个接口（偏离设计 §5B-a，理由见下）────────────────────
本类**只**实现 ToolFeedbackProvider（控制面）。它另有一个生命周期需求——run 结束时
清掉该 session 的熔断计数——由 `reclaim_hooks()` 产出的**适配器**承担：

    gate = CodeQualityGate(cfg)
    builder.behavior(tool_feedback_providers=[gate])   # 控制面：贡献反馈
    hooks.add(gate.reclaim_hooks())                    # 观测面：仅为回收状态

设计原方案是让本类直接继承 DefaultAgentHooks、自己实现 on_run_end。改掉的理由：
那样为了用 **1 个**方法要背上 **21 个**方法的接口，且 `isinstance(gate, AgentHooks)`
恒为 True —— 门控从此可被当成"观测者"对待，而它不是。

也**没有**退化成"抽出共享 state 给两个平级类"那种拆法：`_retry_counts` 依然私有，
适配器只是把「run 结束」这个观测事件翻译成一次 `evict_session()` 调用。
适配器由 gate 自己产出（而非装配层 new），所以"绑错状态"这种错误压根构造不出来。
先例：pandaren/observability/hooks_adapter.py 的 ObservabilityHooksAdapter。

职责边界仍然清晰：
  provide       → 贡献反馈 → **影响** Agent 行为（LLM 会读到）
  evict_session → 释放自己的内存 → **不影响**任何行为（纯自扫门前雪，不观测、不记录）
"""

from __future__ import annotations

import logging

from pandaren.hook import DefaultAgentHooks
from pandaren.tool.definition.context import ToolContext
from pandaren.tool.definition.tool_result import (
    FeedbackSeverity,
    ToolFeedback,
    ToolResult,
)
from pandaren.utils import expand_path

from .checker import Checker, RuffChecker
from .models import CircuitDecision, Diagnostic, GateConfig

logger = logging.getLogger("pandapal.quality.gate")

__all__ = ["CodeQualityGate"]

#: 反馈来源标识（留痕/分段/去重用）。stage 合并多源时各分段靠它溯源。
GATE_SOURCE = "code_quality_gate"

#: 受门控的工具。门控**不挂在这些工具上** —— 它们本体一行不改；
#: 触发点是"任何工具执行完"这个通用 stage，由本 provider 自己筛。
#: 所以将来加密钥扫描、加 .ts 支持，都不必碰任何工具。
_GATED_TOOLS = frozenset({"write_file", "edit_file"})


class CodeQualityGate:
    """编码质量门控：实现 ToolFeedbackProvider（控制面）。

    状态回收走 `reclaim_hooks()` 产出的适配器，本类自身**不是** AgentHooks。
    """

    #: 供框架 stage 的日志/留痕识别本 provider（executor 读 getattr(provider, "source", ...)）
    source = GATE_SOURCE

    def __init__(
        self,
        config: GateConfig | None = None,
        checkers: list[Checker] | None = None,
    ) -> None:
        self._config = config or GateConfig()
        # 检查器经构造注入：禁止在 provider 内直接 import ruff 具体实现，
        # 否则测试没法注入 FakeChecker（设计 §8.5）。
        self._checkers: list[Checker] = (
            list(checkers) if checkers is not None else [RuffChecker()]
        )
        # key=(session_id, file_path) —— per-session 物理隔离，A 的失败计数绝不熔断 B。
        # 计数**可越过阈值继续自增**：一个 int 装不下"是否已熔断"这一位，靠 n vs T 的
        # 三段关系表达 continue/fuse/silent 三态（详见 _apply_circuit）。
        self._retry_counts: dict[tuple[str, str], int] = {}

    # ══════════════════════════════════════════════
    #  控制面：ToolFeedbackProvider
    # ══════════════════════════════════════════════

    async def provide(
        self,
        tool_name: str,
        args: dict,
        result: ToolResult,
        ctx: ToolContext,
    ) -> ToolFeedback | None:
        """门控主入口。任何不确定 → 返回 None（E 原则：不打扰、不误导、不阻断）。

        注：**无需显式检查取消令牌** —— ToolContext 不携带 CancelToken，且本协程运行在
        工具执行的 task 内，run 被取消时 CancelledError 会穿透这里的 await，
        框架 stage 不吞它。取消语义由结构保证，不靠轮询标志位。
        """
        try:
            return await self._provide(tool_name, args, result, ctx)
        except Exception as e:  # noqa: BLE001 — 实现方自兜底，不赌框架一定会吞（O3）
            logger.error(
                "[quality_gate] 门控自身异常，降级为无反馈 | tool=%s | error=%s",
                tool_name, e, exc_info=True,
            )
            return None

    async def _provide(
        self, tool_name: str, args: dict, result: ToolResult, ctx: ToolContext,
    ) -> ToolFeedback | None:
        file_path = args.get("file_path") if isinstance(args, dict) else None
        if not self._should_gate(tool_name, file_path, result):
            return None
        assert isinstance(file_path, str)  # _should_gate 已保证

        # ── SESSION_ID 契约：无法安全归属熔断状态时，门控就没有安全运行的前提 ──
        # 空 session_id 本身是框架契约被破坏的信号。三条路都不能走：
        #   用 "" 当 key   → 所有空 session 共用一个计数，A 的失败熔断 B（红线11 物理隔离不坍缩）
        #   只回灌不计数   → 无熔断保护，且掩盖了这个必须被修的 bug（红线12 违反必留痕）
        #   抛异常         → 净效果与 return None 相同，却多一条 traceback 噪音
        # 采纳：ERROR 留痕（红线12）+ return None（E/O3）+ 不写任何 key（红线11）。
        session_id = ctx.session_id
        if not session_id:
            logger.error(
                "[quality_gate] session_id 为空 —— 熔断状态无法安全归属，本次不跑门控。"
                "这是框架契约被破坏的信号，需排查上游 | tool=%s | file=%s",
                tool_name, file_path,
            )
            return None

        # 与写文件工具用**同一个** expand_path 解析，保证检查的就是它刚写的那个文件。
        # 自造一套解析＝迟早与工具漂移，门控跑在错的文件上还毫无察觉。
        # 它在「工作区未选定」时会抛（resolve_project_root）—— 那是环境态，不是门控 bug，
        # 故按降级处理并记 WARN，而非落到外层 catch 里污染成 ERROR「门控自身异常」。
        try:
            abs_path = str(expand_path(file_path))
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[quality_gate.degraded] 路径解析失败，本次不跑门控 | file=%s | error=%s",
                file_path, e,
            )
            return None

        diagnostics = await self._run_checkers(abs_path)
        if diagnostics is None:
            # 降级：**不得动计数** —— 否则一次 ruff 崩溃就能让已熔断的文件"洗白"
            return None

        errors = [d for d in diagnostics if d.severity is FeedbackSeverity.ERROR]
        warnings = [d for d in diagnostics if d.severity is FeedbackSeverity.WARNING]
        if not self._config.feedback_warnings:
            warnings = []       # 本期 no-op：ruff 一律 error，此分支不可达

        decision = self._apply_circuit(session_id, abs_path, has_error=bool(errors))

        if decision is CircuitDecision.PASS:
            logger.info(
                "[quality_gate.checked] 通过 | session=%s | file=%s | error_count=0",
                session_id, file_path,
            )
            reportable = warnings
            if not reportable:
                # 场景2：对 LLM 静默放行（零打扰原样保持），但给用户一个绿灯。
                # 关键是**不能返回 None** —— 那与降级（ruff 没装/超时）同一个信号，
                # UI 分不出「查过了，干净」和「压根没查」，据此亮绿就是假绿灯。
                return self._passed_feedback()
            return self._format_feedback(reportable, severity=FeedbackSeverity.WARNING)

        if decision is CircuitDecision.SILENT:
            return None                         # 已熔断：仍跑了检查（上面），但不回灌

        if decision is CircuitDecision.FUSE:
            n = self._retry_counts.get((session_id, abs_path), 0)
            logger.warning(
                "[quality_gate.fused] 连续 %d 轮未通过，改为提示如实说明 | session=%s | file=%s",
                n, session_id, file_path,
            )
            return ToolFeedback(
                text=(
                    f"该文件已连续 {n} 轮未通过质量检查（仍有 {len(errors)} 个 error）。\n"
                    f"请停止反复尝试，向用户如实说明该文件仍存在的问题。"
                ),
                severity=FeedbackSeverity.WARNING,
                source=GATE_SOURCE,
            )

        logger.info(
            "[quality_gate.checked] 未通过 | session=%s | file=%s | error_count=%d",
            session_id, file_path, len(errors),
        )
        return self._format_feedback(errors + warnings, severity=FeedbackSeverity.ERROR)

    # ══════════════════════════════════════════════
    #  状态回收
    # ══════════════════════════════════════════════

    def evict_session(self, session_id: str) -> None:
        """清掉该 session 的全部熔断计数（主回收，由 reclaim_hooks() 在 run 结束时调用）。

        门控的修复循环本就发生在**单个 run 的 ReAct 循环内**（写→反馈→改→再写）；
        run 结束 = 本次编码任务收尾，计数天然该清零。新 run（用户新指令）应是全新
        一次尝试，不该继承上次的熔断态。

        不能只靠 GC：Agent 是 per-app 长驻的，session 不断进出，而 key 只在该文件某轮
        pass 时才删 —— 一个从未修好的文件永远等不到 pass，其 key 就永久驻留。

        sync 且只做 dict 删除，零 I/O，不违反 B 原则；用幂等的 pop 而非 read-then-delete
        （run 已结束，该 session 不会再有 in-flight 检查）。
        """
        if not session_id:
            return
        for key in [k for k in self._retry_counts if k[0] == session_id]:
            self._retry_counts.pop(key, None)

    def reclaim_hooks(self) -> "_GateStateReclaimer":
        """产出绑定到本实例的观测面适配器，注册进 `.hooks(...)` 即可自动回收状态。

        由 gate 自己产出而非装配层 new：适配器与它服务的 gate **不可能配错**。
        """
        return _GateStateReclaimer(self)

    # ══════════════════════════════════════════════
    #  内部（全为纯函数或轻状态操作，可独立单测）
    # ══════════════════════════════════════════════

    def _should_gate(self, tool_name: str, file_path: str | None, result: ToolResult) -> bool:
        """是否该对这次工具调用跑门控（场景3：不满足则 checker 根本不启动）。"""
        if not self._config.enabled:
            return False
        if tool_name not in _GATED_TOOLS:
            return False
        if not file_path or not isinstance(file_path, str):
            return False
        if not result.success:
            return False
        suffix = _suffix_of(file_path)
        return suffix in self._config.suffixes

    async def _run_checkers(self, abs_path: str) -> list[Diagnostic] | None:
        """跑所有注入的检查器。

        Returns:
            合并后的诊断（`[]` = 干净）；`None` = **全部**降级（无一给出可用结论）。
            部分降级时只丢降级的那个，其余结论照常合并 —— 与框架 stage 的隔离粒度一致。
        """
        merged: list[Diagnostic] = []
        any_usable = False
        for checker in self._checkers:
            try:
                out = await checker.check(
                    abs_path,
                    timeout=self._config.check_timeout_seconds,
                    cwd=self._config.project_root,
                )
            except Exception as e:  # noqa: BLE001 — 检查器不该抛，但不赌
                logger.warning(
                    "[quality_gate.degraded] 检查器抛异常 | checker=%s | error=%s",
                    getattr(checker, "name", "?"), e, exc_info=True,
                )
                continue
            if out is None:
                continue                        # 该检查器降级
            any_usable = True
            merged.extend(out)
        return merged if any_usable else None

    def _apply_circuit(
        self, session_id: str, file_path: str, *, has_error: bool,
    ) -> CircuitDecision:
        """按 (session, file) 计数裁决（设计 §5B-b 决策表）。

        设 n = 递增**后**的计数，T = circuit_threshold：
          not has_error → 删 key，PASS      （已熔断的文件借此**退出熔断**）
          n < T         → CONTINUE          （正常回灌诊断）
          n == T        → FUSE              （恰好达阈值，回灌一次熔断提示）
          n > T         → SILENT            （已熔断，不再回灌）

        计数越过阈值仍自增：状态载体是一个 int，装不下"是否已熔断"这一位。若到阈值就
        停增，"首次达阈值（发一次提示）"与"已熔断（静默）"在数据上无法区分。
        """
        key = (session_id, file_path)
        if not has_error:
            self._retry_counts.pop(key, None)
            return CircuitDecision.PASS

        n = self._retry_counts.get(key, 0) + 1
        self._retry_counts[key] = n
        self._evict_if_over_capacity()

        threshold = self._config.circuit_threshold
        if n < threshold:
            return CircuitDecision.CONTINUE
        if n == threshold:
            return CircuitDecision.FUSE
        return CircuitDecision.SILENT

    def _passed_feedback(self) -> ToolFeedback:
        """「查过了，干净」的绿灯 —— 只给用户，不给 LLM。

        llm_visible=False 是本方法的全部要害：让用户看见检查发生过，同时对 LLM
        一个 token 都不花。若哪天有人把它改成 True，代价是**每一次**干净的文件写入
        都往上下文里塞一句「检查通过」—— 编码 session 里这是最高频的事件。

        带上检查器名（"ruff"）而非只说「通过」：用户要知道**做了什么检查**，
        「通过」而不说通过了什么，等于没说。名字取自注入的 checker，不硬编码 ——
        将来加 mypy，这里自动变成「ruff、mypy」。
        """
        names = "、".join(getattr(c, "name", "?") for c in self._checkers)
        return ToolFeedback(
            text=f"Lint 检查通过（{names}），未发现问题。",
            severity=FeedbackSeverity.INFO,
            source=GATE_SOURCE,
            llm_visible=False,
        )

    def _format_feedback(
        self, diagnostics: list[Diagnostic], *, severity: FeedbackSeverity,
    ) -> ToolFeedback | None:
        """Diagnostic[] → ToolFeedback（含 file:line:col code message）。

        输出**不含** "[code_quality_gate]" 前缀 —— 那是渲染层（run_core）按 source 加的，
        这里再加一次就成了双重标签。
        """
        if not diagnostics:
            return None

        root = self._config.project_root
        shown = diagnostics[: self._config.max_diagnostics_shown]
        lines = [
            f"{_display_path(d.file, root)}:{d.line}:{d.column} {d.code} {d.message}"
            for d in shown
        ]
        hidden = len(diagnostics) - len(shown)
        if hidden > 0:
            # 不静默截断：明说还有多少条没列（设计原则「No silent caps」）
            lines.append(f"…另有 {hidden} 条未列出。")

        noun = "error" if severity is FeedbackSeverity.ERROR else "warning"
        # 头部点名「做了什么检查」（与 _passed_feedback 对称）：用户看到红灯时，
        # 得先知道是谁在报警、报的是哪一类问题，才谈得上「看得见」。
        names = "、".join(getattr(c, "name", "?") for c in self._checkers)
        head = f"Lint 检查（{names}）发现 {len(diagnostics)} 个 {noun}："
        return ToolFeedback(
            text=f"{head}\n" + "\n".join(lines) + "\n请修复后重新写入。",
            severity=severity,
            source=GATE_SOURCE,
        )

    def _evict_if_over_capacity(self) -> None:
        """容量兜底：超限按插入序淘汰最旧（dict 保序）。

        只防 reclaim_hooks 的回收未触发的异常路径导致的慢泄漏；正常不会触及。
        代价是可能误清活跃 session 的计数 → 退化为不熔断，非致命。
        """
        limit = self._config.max_state_entries
        while len(self._retry_counts) > limit:
            oldest = next(iter(self._retry_counts))
            self._retry_counts.pop(oldest, None)
            logger.warning(
                "[quality_gate] 熔断计数超容量上限 %d，淘汰最旧 key=%s。"
                "若频繁出现说明 reclaim_hooks 的回收未生效，需排查",
                limit, oldest,
            )

    # ── 测试用只读快照（设计 §8.5「可观测中间状态」）──

    def retry_counts_snapshot(self) -> dict[tuple[str, str], int]:
        return dict(self._retry_counts)


class _GateStateReclaimer(DefaultAgentHooks):
    """观测面适配器：把「run 结束」翻译成门控的一次状态回收调用。

    存在的唯一理由是**协议适配** —— 让 CodeQualityGate 不必为了 1 个方法背上
    AgentHooks 的 21 个方法、也不必伪装成观测者。

    完全符合观测面契约：接收生命周期事件、返回 None、不改变任何行为。
    继承 DefaultAgentHooks 拿到其余 20 个空实现（CompositeAgentHooks 会挨个调，
    少一个就 AttributeError）。
    """

    def __init__(self, gate: CodeQualityGate) -> None:
        self._gate = gate

    def on_run_end(
        self, run_id: str, success: bool, *,
        terminal_reason: str = "", session_id: str = "",
    ) -> None:
        self._gate.evict_session(session_id)


def _suffix_of(file_path: str) -> str:
    dot = file_path.rfind(".")
    slash = max(file_path.rfind("/"), file_path.rfind("\\"))
    return file_path[dot:].lower() if dot > slash else ""


def _display_path(abs_path: str, root: str) -> str:
    """给 LLM 看的路径：能转项目相对就转（短、可读、少泄漏绝对路径）。"""
    if not root:
        return abs_path
    try:
        import os
        rel = os.path.relpath(abs_path, root)
    except (ValueError, OSError):
        return abs_path
    return abs_path if rel.startswith("..") else rel.replace("\\", "/")
