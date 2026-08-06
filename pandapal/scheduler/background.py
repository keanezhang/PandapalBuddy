"""后台任务工具 —— 把长时间运行的 Agent run 从入站处理链路上摘下来。

★ 为什么需要它：
    stdin 读取循环（StdioIpcServer._stdin_loop）逐条 await 处理入站消息，
    处理完当前这条才读下一条。而 USER_INSTRUCTION / ask_user resume /
    HITL resume / Plan resume 这些消息的 handler 若内联 `await executor.execute(...)`，
    就会把整条 Agent run（可能几分钟）压在 handler 里不返回，导致后续所有入站消息
    （SESSION_HISTORY_REQUEST、会话切换、STOP…）在 stdin 管道里排队读不出来。

    解决办法：handler 只做快速前置检查，真正的 Agent run 用 spawn_background 丢到
    后台任务里 fire-and-forget，handler 立即返回，stdin 循环马上能读下一条。
    并发/串行由 SessionAgentPool（per-session 锁 + semaphore）负责，这正是它的设计意图。

★ 附带收益：Agent run 不再被路由层 asyncio.wait_for(handler, timeout=600s) 包住，
    真跑超 600s 的长任务不会再被 wait_for 误杀。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine

logger = logging.getLogger(__name__)

# 持有运行中的后台任务引用，防止被 GC 提前回收（asyncio 只保弱引用）。
# 单线程事件循环内操作，无需加锁。
_background_tasks: set[asyncio.Task[Any]] = set()


def spawn_background(
    coro: Coroutine[Any, Any, Any], *, label: str,
) -> asyncio.Task[Any]:
    """把协程丢到后台执行，持有引用防 GC，完成后自动摘除并记录异常。

    Args:
        coro:  要在后台跑的协程（通常是 executor.execute(...) 或包了它的内层协程）。
        label: 日志标识，便于排查（如 "user_instruction:r-1a2b"）。

    Returns:
        创建出的 asyncio.Task（一般无需理会，调用方 fire-and-forget）。
    """
    task = asyncio.ensure_future(coro)
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task[Any]) -> None:
        _background_tasks.discard(t)
        if t.cancelled():
            logger.info("[background] %s cancelled", label)
            return
        exc = t.exception()
        if exc is not None:
            # Agent run 内部已自行吞异常并广播 ERROR（O3），这里兜底记录未预期异常。
            logger.error(
                "[background] %s raised: %s", label, exc, exc_info=exc,
            )

    task.add_done_callback(_on_done)
    return task
