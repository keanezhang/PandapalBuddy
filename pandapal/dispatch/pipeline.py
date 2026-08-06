"""pandapal/dispatch/pipeline.py — adapter + dispatcher 的绑定胶水。

gate（StdioIpcServer / Gateway）唯一持有的入站入口：
归一在渠道入口第一刻发生（adapter.normalize），分类在唯一决策点发生
（dispatcher.dispatch）。
"""

from __future__ import annotations

from typing import Any

from pandapal.dispatch.adapter import InboundChannelAdapter
from pandapal.dispatch.dispatcher import InboundDispatcher


class InboundPipeline:
    """gate 唯一持有的入口：adapter + dispatcher 的绑定胶水。"""

    def __init__(
        self,
        adapter: InboundChannelAdapter,
        dispatcher: InboundDispatcher,
    ) -> None:
        if adapter is None:
            raise ValueError("adapter cannot be None")
        if dispatcher is None:
            raise ValueError("dispatcher cannot be None")
        self._adapter = adapter
        self._dispatcher = dispatcher

    async def handle(self, raw: dict[str, Any]) -> None:
        """方言帧 → 归一 → 分发。normalize 返回 None 表示已被适配器拦截（已留痕）。"""
        env = self._adapter.normalize(raw)
        if env is None:
            return
        await self._dispatcher.dispatch(self._adapter, env)
