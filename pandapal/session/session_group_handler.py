"""SessionGroupHandler — 会话分组 IPC 处理器。

拆分自 SessionListHandler（v004），负责 SESSION_GROUP_MUTATE 的 op 分派：
  create / rename / delete / assign。

级联软删（delete_sessions=True）在 Handler 层编排（§5.3）：
  group_mgr.list_group_session_ids → 逐个 list_mgr.soft_delete_session
  → group_mgr.delete_group，避免 Manager 层形成循环依赖。

所有 handler 方法 O3 Never Throw —— 内部消化异常，返回 ERROR 事件交 Dispatcher 转发。
"""

from __future__ import annotations

import logging
from typing import Any

from pandapal.events.normalized import NormalizedEvent
from pandapal.session.exceptions import (
    GroupNameConflict,
    GroupNameInvalid,
    GroupNotFoundError,
    GroupQuotaExceeded,
    SessionNotFoundError,
)
from pandapal.session.session_group_manager import SessionGroupManager
from pandapal.session.session_list_manager import SessionListManager

logger = logging.getLogger(__name__)


class SessionGroupHandler:
    """分组 IPC handler（O3：内部消化异常，返回事件交 Dispatcher 转发）。"""

    def __init__(
        self,
        group_manager: SessionGroupManager,
        session_list_manager: SessionListManager,
        user_id: str,
    ) -> None:
        if group_manager is None:
            raise ValueError("SessionGroupHandler requires group_manager")
        if session_list_manager is None:
            raise ValueError("SessionGroupHandler requires session_list_manager")
        self._group_mgr = group_manager
        self._list_mgr = session_list_manager
        self._user_id = user_id

    async def handle_group_mutate(
        self, data: dict[str, Any]
    ) -> NormalizedEvent | None:
        op = data.get("op", "")
        try:
            if op == "create":
                await self._group_mgr.create_group(
                    self._user_id, str(data.get("name", "")),
                )
            elif op == "rename":
                await self._group_mgr.rename_group(
                    self._user_id,
                    str(data.get("group_id", "")),
                    str(data.get("new_name", "")),
                )
            elif op == "delete":
                await self._delete_group(
                    str(data.get("group_id", "")),
                    delete_sessions=bool(data.get("delete_sessions", False)),
                )
            elif op == "assign":
                gid = data.get("group_id")
                await self._group_mgr.assign_to_group(
                    user_id=self._user_id,
                    session_id=str(data.get("session_id", "")),
                    group_id=(str(gid) if gid else None),
                )
            else:
                return self._build_error_event(
                    "group_op_invalid", f"unknown op: {op}",
                )
            return None  # 成功事件由 manager 自广播（豁免路径）
        except (
            GroupNameConflict, GroupQuotaExceeded, GroupNameInvalid,
            GroupNotFoundError, SessionNotFoundError,
        ) as e:
            return self._build_error_event(e.error_code, str(e))
        except Exception:
            logger.exception(
                "[SessionGroup] handle_group_mutate failed op=%s", op,
            )
            return self._build_error_event("group_mutate_failed", "")

    async def _delete_group(self, group_id: str, delete_sessions: bool) -> None:
        """删除分组（级联逻辑在 handler 层编排，不落入 Manager）。

        delete_sessions=False：仅删分组（组内会话保留，变「无分组」）。
        delete_sessions=True：先级联软删除组内会话，再删分组。
        """
        if not delete_sessions:
            await self._group_mgr.delete_group(self._user_id, group_id)
            return

        # 先拿组内会话 id 快照，再逐个走完整 soft_delete_session（拒绝待审批 /
        # 取消 Agent / 清理附属数据 / 广播 SESSION_DELETED + 路由 + 同步正向记录）。
        session_ids = await self._group_mgr.list_group_session_ids(
            self._user_id, group_id,
        )
        for sid in session_ids:
            try:
                await self._list_mgr.soft_delete_session(sid)
            except SessionNotFoundError:
                pass  # 并发已删除，忽略
            except Exception:
                logger.warning(
                    "[SessionGroup] cascade delete session failed sid=%s", sid,
                )
        # 组内会话已清空（soft_delete 会触发 on_session_removed 同步正向记录），
        # 再删分组本身。
        await self._group_mgr.delete_group(self._user_id, group_id)

    async def bootstrap(self) -> None:
        """启动引导：幂等重建分组正向记录（修复升级后/漂移）。"""
        try:
            await self._group_mgr.backfill_forward_index(self._user_id)
        except Exception:
            logger.exception("[SessionGroup] bootstrap backfill failed")

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
