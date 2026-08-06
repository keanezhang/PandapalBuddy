"""pandaren/cancellation.py — 协作式取消令牌（横切模块，零依赖）。

设计契约见 docs/design/取消语义-契约.md。

★ 为什么放在顶层而非 engine/：
  取消令牌需要贯穿 engine（Layer 4）/ behavior 执行器（Layer 3）/ tool（Layer 2）/
  sub_agent（编排层）四处。依赖方向严格单向 engine → behavior → capability，若放在
  engine/ 则 behavior/capability 无法向上 import（违反分层）。故与 constants.py / hook
  同列为「横切零依赖模块」，各层均可向下 import。

核心语义：
  - CancelToken 是「单向闸门」：一旦 cancel() 便永不复位。
  - 每次新 run 在 _run_stream_core 入口重建 token，确保干净起点、AgentLoop 可复用。
  - token 通过 ToolContext.metadata["cancel_token"] 下发给工具 / 子 Agent（P2/P3）。

为什么用自定义 CancelledSignal 而非 asyncio.CancelledError：
  - CancelledError 继承 BaseException，会绕过 O3 的 `except Exception` 兜底，有逃逸风险。
  - 取消是「协作式、可转事件」的语义，不应复用 asyncio 的强制取消。
  - CancelledSignal 继承 Exception，可在 step try 内被捕获并转成 AGENT_CANCELLED 事件。
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("pandaren.cancellation")


class CancelledSignal(Exception):
    """引擎内部协作式取消信号（继承 Exception，不逃逸出 run()）。

    由检查点（LLM 逐 chunk / 工具边界 / 子 Agent）抛出，
    在 _run_stream_core 的 per-step try 中捕获，转为 AGENT_CANCELLED + RUN_END。
    """


class CancelToken:
    """协作式取消令牌。协程安全的单向闸门。

    用法::

        token = CancelToken()
        token.cancel("Cancelled by user")   # 外部触发（另一个 task）
        token.raise_if_cancelled()          # 检查点：已取消则抛 CancelledSignal
        await token.wait()                  # 竞速：等待取消发生（P2 工具竞速用）
    """

    __slots__ = ("_event", "_reason")

    def __init__(self) -> None:
        # Python ≥3.12：asyncio.Event 可在无运行 loop 时创建，
        # loop 仅在 wait() 时惰性获取；set() 无 waiter 时不触碰 loop。
        # 因此在 AgentLoop.__init__（可能于 build 期、无 loop）创建是安全的。
        self._event = asyncio.Event()
        self._reason: str | None = None

    def cancel(self, reason: str = "Cancelled by user") -> None:
        """设置取消。幂等：多次调用只记录首个 reason。"""
        if not self._event.is_set():
            self._reason = reason
            self._event.set()
            logger.info("[cancel] token FIRED · reason=%r · id=%s", reason, hex(id(self)))

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    def raise_if_cancelled(self) -> None:
        """检查点辅助：已取消则抛 CancelledSignal（携带 reason）。"""
        if self._event.is_set():
            raise CancelledSignal(self._reason or "Cancelled")

    async def wait(self) -> None:
        """等待取消发生（供 P2 `asyncio.wait({tool_task, cancel_wait})` 竞速使用）。"""
        await self._event.wait()

    def link_parent(
        self, parent: "CancelToken", *, reason: str | None = None
    ) -> "asyncio.Task[None]":
        """建立父子取消链（P3 子 Agent 级联，见契约 §3.6 方案 B）。

        父取消 → 子同步取消：父的 Layer 0/1/2 检查点触发后，子的检查点也随之为真，
        实现多层委派的递归级联。

        返回后台监听 task。★ 调用方（委派处）必须在 finally 里 `task.cancel()` 解除链接，
        否则子 Agent 实例被复用时残留父引用 → 跨 run 误取消（契约 §10 风险项）。

        必须在有运行 event loop 的上下文调用（委派发生在 run 期间，满足此条件）。
        """
        # 父已取消 → 立即同步（快速路径，避免依赖调度）
        if parent.cancelled:
            logger.info(
                "[cancel] link_parent · parent ALREADY cancelled → child sync-cancel · "
                "child=%s parent=%s", hex(id(self)), hex(id(parent)),
            )
            self.cancel(parent.reason or reason or "Cancelled by parent")
        else:
            logger.debug(
                "[cancel] link_parent established · child=%s ← parent=%s",
                hex(id(self)), hex(id(parent)),
            )

        async def _monitor() -> None:
            await parent.wait()
            logger.info(
                "[cancel] CASCADE · parent cancelled → propagating to child · "
                "child=%s ← parent=%s reason=%r",
                hex(id(self)), hex(id(parent)), parent.reason,
            )
            self.cancel(parent.reason or reason or "Cancelled by parent")

        return asyncio.ensure_future(_monitor())
