"""DashboardHandler — 看板 IPC 处理器。

参考 SessionListHandler 的直连 handler 模式（不走 MessageRouter，纯只读查询）。
由 app.py 在 IPC 分派中根据 ipc_type == DASHBOARD_REQUEST 调用。
O3 Never Throw —— 内部消化异常，返回 ERROR 事件交 Dispatcher 转发。

事件出口约定（直通路径集中式转发改造）：
- handle_dashboard_request（请求-响应）：只构建并返回事件，由 Dispatcher 统一转发；
- push_if_active（run 结束后的自主重推，非请求触发）：豁免路径，仍自广播。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pandapal.broadcast.broadcaster import MessageBroadcast
from pandapal.dashboard import build_dashboard_source
from pandapal.events.normalized import NormalizedEvent

logger = logging.getLogger(__name__)


class DashboardHandler:
    """看板请求 handler（O3：内部消化异常）。

    数据源按 storage_mode 二分（markdown 扫 .md / sqlite 查库），由 build_dashboard_source
    统一构造——handler 对两态透明。数据源为只读短生命周期对象，每次请求现建现读，
    确保拿到最新快照。
    """

    def __init__(
        self, storage_path: str | Path, storage_mode: str, broadcast: MessageBroadcast,
    ) -> None:
        if broadcast is None:
            raise ValueError("DashboardHandler requires broadcast")
        # storage_path = StorageManager._storage_path：
        #   markdown → {data_dir}/pandapal_md/users/{uid}（目录）
        #   sqlite   → {data_dir}/users/{uid}/pandapal.db（文件，observability.db 同目录）
        self._storage_path = str(storage_path)
        self._storage_mode = storage_mode
        self._broadcast = broadcast  # 仅豁免路径 push_if_active 使用
        # 前端至少打开过一次看板才自动重推（避免用户没看时白扫）。
        self._active = False

    async def handle_dashboard_request(
        self, data: dict[str, Any]
    ) -> NormalizedEvent | None:
        """请求-响应路径：构建快照事件返回（Dispatcher 统一转发）。"""
        self._active = True
        return self._build_event()

    async def push_if_active(self, session_id: str = "", user_id: str = "") -> None:
        """P2 实时刷新：run 结束时重推快照（豁免路径，自广播）。

        仅在看板被打开过后才推，O3 内部消化异常。
        """
        if not self._active:
            return
        event = self._build_event()
        if event is not None:
            try:
                await self._broadcast.send(event)
            except Exception:
                logger.exception("[Dashboard] push_if_active broadcast failed")

    def _build_event(self) -> NormalizedEvent | None:
        """构建看板快照事件；失败时构建 ERROR 事件。永不抛异常（O3）。"""
        try:
            source = build_dashboard_source(self._storage_mode, self._storage_path)
            snapshot = source.build()
            return NormalizedEvent.dashboard_data(snapshot=snapshot.to_dict())
        except Exception:
            logger.exception("[Dashboard] build event failed")
            return NormalizedEvent.global_error(
                error_code="dashboard_request_failed",
                error_message="dashboard_request_failed",
                error_detail="",
            )
