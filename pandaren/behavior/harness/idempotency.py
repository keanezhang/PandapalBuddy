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
    """Turn 级幂等性去重。"""

    def __init__(self) -> None:
        self._cache: dict[str, ToolResult] = {}  # hash → ToolResult
        self._locks: dict[str, asyncio.Lock] = {}

    def _make_key(self, tool_name: str, args: dict) -> str:
        """生成去重 key：tool_name + args 的确定性 hash。"""
        args_str = json.dumps(args, sort_keys=True, ensure_ascii=False)
        content = f"{tool_name}:{args_str}"
        return hashlib.sha256(content.encode()).hexdigest()

    async def check(self, tool_name: str, args: dict) -> ToolResult | None:
        """检查是否命中幂等缓存。

        返回 None 表示未命中（需要执行），否则返回缓存的 ToolResult。
        并发安全：通过 asyncio.Lock 保证同一 key 的请求串行化。
        """
        key = self._make_key(tool_name, args)

        # 使用 setdefault 保证原子性，避免 TOCTOU 竞态
        lock = self._locks.setdefault(key, asyncio.Lock())

        async with lock:
            if key in self._cache:
                return self._cache[key]
            return None

    async def store(self, tool_name: str, args: dict, result: ToolResult) -> None:
        """存储执行结果到幂等缓存。"""
        key = self._make_key(tool_name, args)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            self._cache[key] = result

    def check_sync(self, tool_name: str, args: dict) -> ToolResult | None:
        """同步版本的幂等检查（用于非 async 场景）。"""
        key = self._make_key(tool_name, args)
        return self._cache.get(key)

    def store_sync(self, tool_name: str, args: dict, result: ToolResult) -> None:
        """同步版本的缓存存储。"""
        key = self._make_key(tool_name, args)
        self._cache[key] = result

    def reset_turn(self) -> None:
        """Turn 结束时清空缓存和锁。"""
        self._cache.clear()
        self._locks.clear()
