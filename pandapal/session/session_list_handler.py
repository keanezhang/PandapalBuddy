"""SessionListHandler — 会话列表 IPC 处理器。

不走 MessageRouter 路径，因为会话列表操作是纯副作用查询/更新，与 Agent 执行无关。
参考 REQUEST_SCHEDULED_TASKS / SKILL_LIST 的直连 handler 模式。

由 app.py 注册为 InboundDispatcher 直通 handler（经 _session_list_dispatch 分派）。
所有 handler 方法 O3 Never Throw —— 内部消化异常，返回 ERROR 事件交 Dispatcher 转发。

事件出口约定（直通路径集中式转发改造）：
- 请求-响应事件（SESSION_LIST / SESSION_SWITCHED / 各 ERROR）：本类只构建并返回，
  由 InboundDispatcher 统一 broadcast.send() 并注入 origin_channel_id；
- 状态变更事件（SESSION_UPDATED / SESSION_DELETED / SESSION_HISTORY_LIST）：
  由 SessionListManager 内部自广播（豁免路径——manager 同时服务 executor 钩子与
  bootstrap 等非请求触发源），对应 handler 成功路径返回 None；
- 分组相关事件（SESSION_GROUP_LIST / 组内 SESSION_UPDATED）由 SessionGroupManager
  自广播，其 handler 见 session_group_handler.py（本类只负责会话列表与会话操作）。
"""

from __future__ import annotations

import logging
from typing import Any

from pandapal.events.normalized import NormalizedEvent
from pandapal.session.exceptions import (
    GroupNotFoundError,
    InvalidPageSize,
    SessionNotFoundError,
    SessionQuotaExceeded,
)
from pandapal.session.session_group_manager import SessionGroupManager
from pandapal.session.session_list_manager import SessionListManager

logger = logging.getLogger(__name__)


class SessionListHandler:
    """IPC handler 集合（O3：所有方法内部消化异常，返回事件交 Dispatcher 转发）。"""

    def __init__(
        self,
        manager: SessionListManager,
        group_manager: SessionGroupManager,
        user_id: str,
    ) -> None:
        if manager is None:
            raise ValueError("SessionListHandler requires manager")
        if group_manager is None:
            raise ValueError("SessionListHandler requires group_manager")
        self._mgr = manager
        self._group_mgr = group_manager
        self._user_id = user_id  # sidecar 单用户，启动时确定

    async def handle_session_list_request(
        self, data: dict[str, Any]
    ) -> NormalizedEvent | None:
        try:
            group_id_raw = data.get("group_id", "all")
            group_id: str | None
            if group_id_raw in (None, "all"):
                group_id = None
            elif group_id_raw == "":
                group_id = ""  # 无分组视图
            else:
                group_id = str(group_id_raw)

            page = int(data.get("page", 1))
            limit = int(data.get("limit", 10))

            # 具体分组 → 走正向记录快路径（SessionGroupManager）；
            # all / 无分组 → 走全量过滤（SessionListManager）。
            if group_id not in (None, ""):
                infos, has_more = await self._group_mgr.list_group_sessions(
                    user_id=self._user_id,
                    group_id=group_id,
                    page=page,
                    limit=limit,
                )
            else:
                infos, has_more = await self._mgr.list_sessions(
                    user_id=self._user_id,
                    group_id=group_id,
                    page=page,
                    limit=limit,
                )
            return NormalizedEvent.session_list(
                sessions=[s.to_dict() for s in infos],
                has_more=has_more,
                page=page,
                group_id=group_id if group_id is not None else "all",
            )
        except InvalidPageSize as e:
            return self._build_error_event(e.error_code, str(e))
        except GroupNotFoundError as e:
            # 分组已被并发删除：返回空列表（保持旧 list_sessions 的"组不存在→空"语义）
            return NormalizedEvent.session_list(
                sessions=[], has_more=False, page=int(data.get("page", 1)),
                group_id=str(data.get("group_id", "all")),
            )
        except Exception:
            logger.exception("[SessionList] handle_session_list_request failed")
            return self._build_error_event("session_list_request_failed", "")

    async def handle_session_create(
        self, data: dict[str, Any]
    ) -> NormalizedEvent | None:
        try:
            session_id = await self._mgr.create_empty_session(self._user_id)
            # 返回 SESSION_SWITCHED{fresh} 让前端切到新空 session
            return NormalizedEvent.session_switched(
                session_id=session_id,
                context_status="fresh",
            )
        except SessionQuotaExceeded as e:
            return self._build_error_event(e.error_code, str(e))
        except Exception:
            logger.exception("[SessionList] handle_session_create failed")
            return self._build_error_event("session_create_failed", "")

    async def handle_session_switch(
        self, data: dict[str, Any]
    ) -> NormalizedEvent | None:
        target = str(data.get("target_session_id", ""))
        if not target:
            return self._build_error_event(
                "session_not_found", "target_session_id required",
            )
        try:
            await self._mgr.on_switch_session(self._user_id, target)
            return None  # 成功事件由 manager 自广播（豁免路径）
        except SessionNotFoundError as e:
            return self._build_error_event(e.error_code, str(e))
        except Exception:
            logger.exception("[SessionList] handle_session_switch failed")
            return self._build_error_event("session_switch_failed", "")

    async def handle_session_delete(
        self, data: dict[str, Any]
    ) -> NormalizedEvent | None:
        session_id = str(data.get("session_id", ""))
        current_view = data.get("current_view_session_id")
        if not session_id:
            return self._build_error_event("session_not_found", "session_id required")
        try:
            await self._mgr.soft_delete_session(
                session_id, current_view_session_id=current_view,
            )
            return None  # 成功事件由 manager 自广播（豁免路径）
        except SessionNotFoundError as e:
            return self._build_error_event(e.error_code, str(e))
        except Exception:
            logger.exception("[SessionList] handle_session_delete failed")
            return self._build_error_event("session_delete_failed", "")

    async def handle_session_rename(
        self, data: dict[str, Any]
    ) -> NormalizedEvent | None:
        session_id = str(data.get("session_id", ""))
        title = str(data.get("title", "") or "")
        if not session_id:
            return self._build_error_event("session_not_found", "session_id required")
        try:
            await self._mgr.rename_session(session_id, title)
            return None  # 成功事件由 manager 自广播（豁免路径）
        except SessionNotFoundError as e:
            return self._build_error_event(e.error_code, str(e))
        except Exception:
            logger.exception("[SessionList] handle_session_rename failed")
            return self._build_error_event("session_rename_failed", "")

    async def handle_session_history_request(
        self, data: dict[str, Any]
    ) -> NormalizedEvent | None:
        session_id = str(data.get("session_id", ""))
        limit = int(data.get("limit", 50))
        try:
            offset = int(data.get("offset", 0))
        except (TypeError, ValueError):
            offset = 0
        if offset < 0:
            offset = 0
        if not session_id:
            return self._build_error_event("session_not_found", "session_id required")
        try:
            await self._mgr.get_session_history(
                self._user_id, session_id, limit=limit, offset=offset,
            )
            return None  # 成功事件由 manager 自广播（豁免路径）
        except SessionNotFoundError as e:
            return self._build_error_event(e.error_code, str(e))
        except Exception:
            logger.exception("[SessionList] handle_session_history_request failed")
            return self._build_error_event("session_history_failed", "")

    async def bootstrap(self) -> None:
        """启动引导（app.start 完成后调用一次）。"""
        try:
            await self._mgr.startup_bootstrap(self._user_id)
        except Exception:
            logger.exception("[SessionList] bootstrap failed")

    # ─────────────────────────────────────────────
    # 内部
    # ─────────────────────────────────────────────

    def _build_error_event(self, error_code: str, detail: str) -> NormalizedEvent:
        """构建 ERROR 事件（由 Dispatcher 统一转发）。"""
        return NormalizedEvent.error(
            error_code=error_code,
            error_message=error_code,
            error_detail=detail,
        )
