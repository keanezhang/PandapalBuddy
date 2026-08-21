"""pandaren/behavior/harness/idempotency.py — R4 幂等性保护

is_idempotent=False 的工具，同 turn 相同参数只执行一次。
并发场景：asyncio.Lock(tool_name + args_hash) 保证先到先执行。
去重缓存生命周期为单个 turn，turn 结束后清空。
"""

from __future__ import annotations

import asyncio
import hashlib
import json

from ...tool.definition.tool_result import ToolResult


class IdempotencyGuard:
    """Turn 级幂等性去重。

    并发契约（inv-ID-7）：并发同 key 的调用中，仅第一个真正执行，
    其余等待 in-flight 结果并命中缓存（deduplicated 由调用方标记）。
    等待在锁**外**进行（锁内 await 会让执行方的 store 拿不到锁而死锁）。
    """

    def __init__(self) -> None:
        self._cache: dict[str, ToolResult] = {}  # hash → ToolResult
        self._locks: dict[str, asyncio.Lock] = {}
        self._inflight: dict[str, asyncio.Future] = {}  # hash → 首个执行者的结果 Future
        self._failed: dict[str, Exception] = {}  # hash → 执行者失败信号（异常路径留痕）

    def _make_key(self, tool_name: str, args: dict) -> str:
        """生成去重 key：tool_name + args 的确定性 hash。"""
        args_str = json.dumps(args, sort_keys=True, ensure_ascii=False)
        content = f"{tool_name}:{args_str}"
        return hashlib.sha256(content.encode()).hexdigest()

    async def check(self, tool_name: str, args: dict) -> ToolResult | None:
        """检查是否命中幂等缓存。

        返回 None 表示未命中且**登记为本 key 的执行者**（调用方应去执行），
        否则返回缓存的 ToolResult（含等待 in-flight 完成后拿到的结果）。
        """
        key = self._make_key(tool_name, args)

        # 使用 setdefault 保证原子性，避免 TOCTOU 竞态
        lock = self._locks.setdefault(key, asyncio.Lock())

        async with lock:
            if key in self._failed:
                # 本 key 执行者已失败收口（complete 留痕）：后续到达者不得重新执行
                # （inv-ID-7「仅第一个真正执行」在异常路径同样成立），直接传播失败。
                raise self._failed[key]
            if key in self._cache:
                return self._cache[key]
            fut = self._inflight.get(key)
            if fut is None:
                # 第一个到达者：登记 in-flight 后返回 None，指示调用方去执行
                fut = asyncio.get_running_loop().create_future()
                self._inflight[key] = fut
                return None
        # 后续并发者：锁外等待执行方 store/complete 的结果（锁内 await 会死锁）
        return await fut

    async def store(self, tool_name: str, args: dict, result: ToolResult) -> None:
        """存储执行结果到幂等缓存，并唤醒等待 in-flight 的并发调用者。"""
        key = self._make_key(tool_name, args)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            self._cache[key] = result
            fut = self._inflight.pop(key, None)
            if fut is not None and not fut.done():
                fut.set_result(result)

    def complete(self, tool_name: str, args: dict, result: ToolResult | None) -> None:
        """收口 in-flight 登记（同步版，供执行方 finally 调用）。

        result=None（执行方抛异常）→ 等待者收到 RuntimeError，绝不永久挂起；
        正常路径下 store 已消费 in-flight，此处为 no-op。
        失败留痕：执行方失败收口时在 _failed 登记本 key，后续并发到达者
        （在收口之后才进入 check）同样感知失败、不重复执行（inv-ID-7）。
        """
        key = self._make_key(tool_name, args)
        fut = self._inflight.pop(key, None)
        if fut is None or fut.done():
            return
        exc = RuntimeError(f"tool execution failed: {tool_name}")
        if result is not None:
            # 失败 ToolResult（executor 只在 success 时 store，此处必为失败结果）
            fut.set_result(result)
        else:
            fut.set_exception(exc)
        self._failed[key] = exc

    def check_sync(self, tool_name: str, args: dict) -> ToolResult | None:
        """同步版本的幂等检查（用于非 async 场景）。"""
        key = self._make_key(tool_name, args)
        return self._cache.get(key)

    def store_sync(self, tool_name: str, args: dict, result: ToolResult) -> None:
        """同步版本的缓存存储。"""
        key = self._make_key(tool_name, args)
        self._cache[key] = result

    def reset_turn(self) -> None:
        """Turn 结束时清空缓存、锁、in-flight 登记和失败留痕。"""
        self._cache.clear()
        self._locks.clear()
        self._inflight.clear()
        self._failed.clear()
