"""pandaren/tool/registry/discovery.py — DEFERRED 工具发现状态的唯一管理者。

单一写入点，消除三写问题。discovered 状态通过 snapshot/restore 显式序列化。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("pandaren.tool.registry.discovery")


class DiscoveryManager:
    """DEFERRED 工具发现状态的唯一管理者。

    设计原则：
      - discover() 是唯一的写入方法
      - 不从消息历史反向扫描
      - 通过 snapshot/restore 显式序列化，与消息格式解耦
    """

    def __init__(self, max_discovered: int = 20) -> None:
        self._discovered: dict[str, int] = {}  # name → step_n
        self._max_discovered = max_discovered

    def discover(self, name: str, step_n: int) -> None:
        """唯一的写入方法。标记工具为已发现。"""
        self._discovered[name] = step_n
        logger.info(
            "工具已发现: '%s' (step=%d) | 当前已发现: %d/%d",
            name, step_n, len(self._discovered), self._max_discovered,
        )

        # LRU 淘汰
        if len(self._discovered) > self._max_discovered:
            self._evict_lru()

    def is_discovered(self, name: str) -> bool:
        """查询工具是否已被发现。"""
        found = name in self._discovered
        return found

    def get_step(self, name: str) -> int | None:
        """获取工具被发现时的 step_n。"""
        return self._discovered.get(name)

    def update_step(self, name: str, step_n: int) -> None:
        """更新已发现工具的 step（用于 LRU 排序）。"""
        if name in self._discovered:
            self._discovered[name] = step_n

    def snapshot(self) -> dict[str, int]:
        """导出当前状态（用于序列化/持久化）。"""
        return dict(self._discovered)

    def restore(self, state: dict[str, int]) -> None:
        """从快照恢复状态。"""
        self._discovered = dict(state)
        # 恢复后检查上限
        if len(self._discovered) > self._max_discovered:
            self._evict_lru()

    def clear(self) -> None:
        """清空所有发现状态。"""
        self._discovered.clear()

    def undiscover(self, name: str) -> bool:
        """移除单个工具的发现状态。

        Returns:
            True 表示成功移除，False 表示工具未被发现。
        """
        if name in self._discovered:
            del self._discovered[name]
            logger.debug("工具发现状态已移除: '%s'", name)
            return True
        return False

    def _evict_lru(self) -> None:
        """LRU 淘汰：移除最久未使用的工具。"""
        if len(self._discovered) <= self._max_discovered:
            return
        sorted_items = sorted(self._discovered.items(), key=lambda x: x[1])
        excess = len(self._discovered) - self._max_discovered
        for name, _ in sorted_items[:excess]:
            del self._discovered[name]
            logger.debug("LRU 淘汰: '%s'", name)

    def __len__(self) -> int:
        return len(self._discovered)

    def __contains__(self, name: str) -> bool:
        return name in self._discovered
