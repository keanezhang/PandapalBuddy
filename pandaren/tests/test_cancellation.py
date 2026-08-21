"""pandaren/tests/test_cancellation.py — CancelToken 协作式取消令牌测试。

风险映射（设计契约 docs/design/取消语义-契约.md）：
  - 单向闸门：cancel 后永不复位
  - cancel 幂等：多次取消保留首个 reason
  - raise_if_cancelled：已取消抛 CancelledSignal（继承 Exception，不逃逸 run()）
  - wait()：可被取消唤醒（P2 工具竞速）
  - link_parent：父已取消 → 子同步取消；父后取消 → 级联；解除链接后不级联（契约 §10 防跨 run 误取消）
"""

from __future__ import annotations

import asyncio

import pytest

from pandaren.cancellation import CancelledSignal, CancelToken


# ════════════════════════════════════════════════════════════════
# Group A — 单向闸门与幂等
# ════════════════════════════════════════════════════════════════

class TestOneWayGate:

    def test_default_not_cancelled(self):
        token = CancelToken()
        assert token.cancelled is False
        assert token.reason is None

    def test_cancel_sets_state(self):
        token = CancelToken()
        token.cancel("user stop")
        assert token.cancelled is True
        assert token.reason == "user stop"

    def test_cancel_idempotent_keeps_first_reason(self):
        """幂等：多次 cancel 保留首个 reason"""
        token = CancelToken()
        token.cancel("first")
        token.cancel("second")
        assert token.reason == "first"

    def test_default_reason(self):
        token = CancelToken()
        token.cancel()
        assert token.reason == "Cancelled by user"


# ════════════════════════════════════════════════════════════════
# Group B — raise_if_cancelled
# ════════════════════════════════════════════════════════════════

class TestRaiseIfCancelled:

    def test_not_cancelled_no_raise(self):
        CancelToken().raise_if_cancelled()  # 不抛即通过

    def test_cancelled_raises_signal(self):
        token = CancelToken()
        token.cancel("stop now")
        with pytest.raises(CancelledSignal) as ei:
            token.raise_if_cancelled()
        assert str(ei.value) == "stop now"

    def test_signal_inherits_exception(self):
        """CancelledSignal 继承 Exception：能被 O3 的 except Exception 兜底捕获"""
        assert issubclass(CancelledSignal, Exception)


# ════════════════════════════════════════════════════════════════
# Group C — wait() 竞速
# ════════════════════════════════════════════════════════════════

class TestWait:

    async def test_wait_returns_after_cancel(self):
        token = CancelToken()
        waiter = asyncio.create_task(token.wait())
        await asyncio.sleep(0)
        assert not waiter.done()
        token.cancel("wake")
        await asyncio.wait_for(waiter, 1.0)  # 被取消唤醒，不超时

    async def test_wait_cancelled_token_immediate(self):
        token = CancelToken()
        token.cancel()
        await asyncio.wait_for(token.wait(), 1.0)


# ════════════════════════════════════════════════════════════════
# Group D — link_parent 级联
# ════════════════════════════════════════════════════════════════

class TestLinkParent:

    async def test_parent_already_cancelled_sync_cancel(self):
        """父已取消 → 子同步取消（快速路径）"""
        parent = CancelToken()
        parent.cancel("parent done")
        child = CancelToken()
        task = child.link_parent(parent)
        try:
            assert child.cancelled is True
            assert child.reason == "parent done"
        finally:
            task.cancel()

    async def test_parent_cancel_cascades_to_child(self):
        """父后续取消 → 后台 task 级联取消子"""
        parent = CancelToken()
        child = CancelToken()
        task = child.link_parent(parent)
        try:
            assert child.cancelled is False
            parent.cancel("cascade now")
            await asyncio.wait_for(task, 1.0)
            assert child.cancelled is True
            assert child.reason == "cascade now"
        finally:
            task.cancel()

    async def test_unlink_prevents_cascade(self):
        """解除链接（task.cancel）后父取消不再级联 — 契约 §10 防跨 run 误取消"""
        parent = CancelToken()
        child = CancelToken()
        task = child.link_parent(parent)
        task.cancel()  # 解除
        parent.cancel("too late")
        await asyncio.sleep(0.05)  # 给残留监听一个机会（若有）
        assert child.cancelled is False

    async def test_child_cancel_does_not_affect_parent(self):
        """级联是单向的：子取消不影响父"""
        parent = CancelToken()
        child = CancelToken()
        task = child.link_parent(parent)
        try:
            child.cancel("child only")
            await asyncio.sleep(0.05)
            assert parent.cancelled is False
        finally:
            task.cancel()

    async def test_parent_reason_takes_precedence(self):
        """父 reason 非空时级联 reason 用父的（link 参数 fallback 仅防御性）"""
        parent = CancelToken()
        child = CancelToken()
        task = child.link_parent(parent, reason="from child context")
        try:
            parent.cancel("parent reason wins")
            await asyncio.wait_for(task, 1.0)
            assert child.reason == "parent reason wins"
        finally:
            task.cancel()
