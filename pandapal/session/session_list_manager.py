"""SessionListManager — UI 会话列表管理（v004）。

职责：
- 会话元数据 CRUD（title/preview/is_favorite/is_deleted/group_id 回填）
- 会话列表的排序/分页/筛选查询
- "当前会话"视图切换的路由决策
- 空会话节流复用
- 50 上限的容量校验和淘汰触发
- 启动引导（清 is_empty 遗留 + 建新空 session + 加载列表）
- 会话删除时通过 on_session_removed 回调通知分组层同步正向记录

⚠️ 与 SessionManager 的区别：
- SessionManager 管"消息 session"生命周期（超时/心跳）
- SessionListManager 管"UI 会话"元数据（用户视角话题）
- 共用 sessions 表但语义正交

⚠️ 与 SessionGroupManager 的区别（v004 拆分）：
- 分组 CRUD / 1:1 关联 / 正向记录 / 组内会话列表 → SessionGroupManager
- 本类保留 group_id 字段的只读回填（session_to_info 的 group_name）

设计约束：
- BL1: 单一职责 —— 只做元数据 + 路由，不做并发/STM/审批状态机
- BL2: 无状态 —— 实例字段仅存注入依赖，无请求上下文（currentSessionId 在前端）
- BL4: 依赖注入 —— 依赖全部构造参数注入
- BL5: 业务异常语义化 —— SessionQuotaExceeded 等独立类型
- D3: 禁止 N+1 —— list_visible_sessions 单次 SQL
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal, Protocol

from pandapal.broadcast.broadcaster import MessageBroadcast
from pandapal.config.system.manager import ConfigManager
from pandapal import session_id as session_id_mod
from pandapal.events.normalized import NormalizedEvent
from pandapal.session.exceptions import (
    InvalidPageSize,
    SessionNotFoundError,
    SessionQuotaExceeded,
)
from pandapal.storage.models import (
    ApprovalDecision,
    Session,
    SessionGroup,
)

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════
# 常量（编译期）
# ═════════════════════════════════════════════════════════════

DEFAULT_MAX_SESSIONS = 300
DEFAULT_MAX_GROUPS = 10
MAX_TITLE_LENGTH = 10
MAX_PREVIEW_LENGTH = 40
MAX_GROUP_NAME_LENGTH = 20
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 50


# ═════════════════════════════════════════════════════════════
# 返回类型
# ═════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class SessionInfo:
    """会话列表条目（IPC 传输格式）。"""

    session_id: str
    title: str
    preview: str
    message_count: int
    is_favorite: bool
    is_empty: bool
    group_id: str | None
    group_name: str | None
    updated_at: str  # ISO8601
    created_at: str  # ISO8601，列表排序键

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "preview": self.preview,
            "message_count": self.message_count,
            "is_favorite": self.is_favorite,
            "is_empty": self.is_empty,
            "group_id": self.group_id,
            "group_name": self.group_name,
            "updated_at": self.updated_at,
            "created_at": self.created_at,
        }


def session_to_info(session: Session, group_map: dict[str, str]) -> SessionInfo:
    """把 Session 转换为前端传输格式 SessionInfo（模块级，供 Manager 复用）。"""
    updated_at = session.updated_at or session.last_active
    return SessionInfo(
        session_id=session.session_id,
        title=session.title,
        preview=session.preview,
        message_count=session.message_count,
        is_favorite=session.is_favorite,
        is_empty=session.is_empty,
        group_id=session.group_id,
        group_name=group_map.get(session.group_id) if session.group_id else None,
        updated_at=updated_at.isoformat() if updated_at else "",
        created_at=session.created_at.isoformat() if session.created_at else "",
    )


@dataclass(frozen=True)
class SessionRoutingResult:
    """删除后前端应切到哪个会话的决策结果。"""

    action: Literal["no_change", "switch", "empty_state"]
    target_session_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "target_session_id": self.target_session_id,
        }


@dataclass(frozen=True)
class StartupPayload:
    """启动引导返回：初始空 session + 首屏列表 + 分组。"""

    initial_session_id: str
    sessions: list[SessionInfo]
    groups: list[SessionGroup]
    has_more: bool


# ═════════════════════════════════════════════════════════════
# 依赖协议（BL4 + 可测试性）
# ═════════════════════════════════════════════════════════════

class _AgentPoolLike(Protocol):
    async def cancel_session(self, session_id: str) -> None: ...


class _IdGeneratorLike(Protocol):
    def new_session_id(self) -> str: ...
    def new_group_id(self) -> str: ...


class _ClockLike(Protocol):
    def now(self) -> datetime: ...


class DefaultIdGenerator:
    """默认 UUID 生成器。测试可替换为 SequenceGenerator。"""

    def new_session_id(self) -> str:
        # ★ 经由命根子模块创建，勿在此散落 uuid 生成（见 session_id.py / CLAUDE.md 契约）。
        return session_id_mod.new_interactive()

    def new_group_id(self) -> str:
        return f"grp-{uuid.uuid4().hex[:16]}"


class SystemClock:
    """默认系统时钟。测试可替换为 FrozenClock。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


# ═════════════════════════════════════════════════════════════
# SessionListManager
# ═════════════════════════════════════════════════════════════

class SessionListManager:
    """UI 会话列表管理器（无状态 + 全局单例）。

    使用方式：
        mgr = SessionListManager(
            session_repo=..., group_repo=..., agent_pool=...,
            approval_repo=..., run_state_repo=...,
            broadcast=..., config_manager=...,
        )
        session_id = await mgr.create_empty_session(user_id="alice")
    """

    def __init__(
        self,
        session_repo: Any,          # SessionRepository
        group_repo: Any,            # SessionGroupRepository | None（markdown 模式为 None）
        agent_pool: _AgentPoolLike,
        approval_repo: Any,         # ApprovalRepository
        run_state_repo: Any,        # RunStateRepository
        broadcast: MessageBroadcast,
        config_manager: ConfigManager,
        raw_log_backend: Any = None,  # 可选：SQLiteRawLogBackend，用于历史消息回补
        working_memory_backend: Any = None,
        agent_task_repo: Any = None,
        clock: _ClockLike | None = None,
        id_generator: _IdGeneratorLike | None = None,
        on_session_removed: (
            Callable[[str, str | None], Awaitable[None]] | None
        ) = None,
    ) -> None:
        if session_repo is None:
            raise ValueError("SessionListManager requires session_repo")
        if agent_pool is None:
            raise ValueError("SessionListManager requires agent_pool")
        if approval_repo is None:
            raise ValueError("SessionListManager requires approval_repo")
        if run_state_repo is None:
            raise ValueError("SessionListManager requires run_state_repo")
        if broadcast is None:
            raise ValueError("SessionListManager requires broadcast")

        self._session_repo = session_repo
        self._group_repo = group_repo
        self._agent_pool = agent_pool
        self._approval_repo = approval_repo
        self._run_state_repo = run_state_repo
        self._broadcast = broadcast
        self._config_manager = config_manager
        self._raw_log_backend = raw_log_backend
        self._working_memory_backend = working_memory_backend
        self._agent_task_repo = agent_task_repo

        self._clock: _ClockLike = clock or SystemClock()
        self._id_gen: _IdGeneratorLike = id_generator or DefaultIdGenerator()
        self._on_session_removed = on_session_removed

    # ═══════════════════════════════════════════════════════════
    # 生命周期钩子
    # ═══════════════════════════════════════════════════════════

    async def shutdown(self) -> None:
        """SubsystemContainer 关闭钩子（无状态，无资源要释放）。"""
        logger.info("SessionListManager shutdown (stateless)")

    # ═══════════════════════════════════════════════════════════
    # 配置读取（实时 + 优雅降级）
    # ═══════════════════════════════════════════════════════════

    def _get_max_sessions(self) -> int:
        try:
            cfg = self._config_manager.get_system_config()
            v = getattr(cfg, "max_sessions", DEFAULT_MAX_SESSIONS)
            return int(v) if v else DEFAULT_MAX_SESSIONS
        except Exception:
            return DEFAULT_MAX_SESSIONS

    # ═══════════════════════════════════════════════════════════
    # 会话生命周期
    # ═══════════════════════════════════════════════════════════

    async def create_empty_session(
        self, user_id: str, device_id: str = ""
    ) -> str:
        """新建空会话（含节流复用 + 容量淘汰）。

        决策表：
          - find_current_empty_session 命中 → 返回该 id（节流复用）
          - miss + count < max → 新建
          - miss + count >= max → evict_oldest → 新建

        Args:
            user_id: 用户 ID
            device_id: 设备 ID（保留字段）

        Returns:
            新建或复用的空会话 session_id

        Raises:
            ValueError: user_id 为空
            SessionQuotaExceeded: 容量满且淘汰失败
        """
        if not user_id:
            raise ValueError("user_id is required")

        # 1. 节流复用
        existing_empty = await self._session_repo.find_current_empty_session(user_id)
        if existing_empty is not None:
            logger.info(
                "[SessionList] create_empty_session: reuse existing "
                "empty session=%s user=%s", existing_empty.session_id, user_id,
            )
            return existing_empty.session_id

        # 2. 容量校验
        max_sessions = self._get_max_sessions()
        count = await self._session_repo.count_visible_sessions(user_id)
        if count >= max_sessions:
            evicted = await self._evict_oldest(user_id)
            if evicted is None:
                raise SessionQuotaExceeded(user_id, count, max_sessions)

        # 3. 建新空 session
        now = self._clock.now()
        session_id = self._id_gen.new_session_id()
        session = Session(
            session_id=session_id,
            user_id=user_id,
            device_id=device_id,
            last_active=now,
            created_at=now,
            title="",
            preview="",
            message_count=0,
            is_empty=True,
            is_favorite=False,
            is_deleted=False,
            updated_at=now,
            group_id=None,
        )
        await self._session_repo.save_session(session)
        logger.info(
            "[SessionList] created empty session=%s user=%s", session_id, user_id,
        )
        return session_id

    async def on_first_message(
        self, session_id: str, first_user_text: str
    ) -> None:
        """AgentExecutor 前钩子：会话发出首条用户消息时调用。

        - 生成 title（前 10 字符 / 全空白则"新会话"）+ preview（前 40 字符）
        - is_empty=1 → 0
        - message_count 置为 1
        - 广播 SESSION_UPDATED{reason="first_message"}

        幂等：is_empty=0 时直接 no-op。
        Fail-safe：DB 写失败记 ERROR 但不阻塞 Agent 生成。
        """
        try:
            session = await self._session_repo.find_session(session_id)
            if session is None:
                logger.warning(
                    "[SessionList] on_first_message: session not found=%s",
                    session_id,
                )
                return
            if not session.is_empty:
                return  # 幂等

            title = self._generate_title(first_user_text)
            preview = self._generate_preview(first_user_text)

            ok = await self._session_repo.update_session_meta(
                session_id,
                title=title,
                preview=preview,
                message_count=1,
                is_empty=False,
                touch_updated_at=True,
            )
            if not ok:
                return
            updated = await self._session_repo.find_session(session_id)
            if updated is not None:
                await self._broadcast_session_updated(updated, "first_message")
            logger.info(
                "[SessionList] first_message session=%s title=%r",
                session_id, title,
            )
        except Exception:
            logger.exception(
                "[SessionList] on_first_message failed session=%s", session_id,
            )
            # 不 raise，避免阻塞 Agent 生成

    async def touch_activity(
        self, session_id: str, message_delta: int = 0
    ) -> None:
        """AgentExecutor 后钩子：Agent 回复完成后调用。

        - 增加 message_count（可为 0）
        - 更新 updated_at + last_active
        - 广播 SESSION_UPDATED{reason="activity"}（10% 采样，避免刷屏）

        Fail-safe：失败记 WARN 忽略。
        """
        try:
            if message_delta > 0:
                await self._session_repo.increment_message_count(
                    session_id, message_delta,
                )
            else:
                # 只刷时间
                await self._session_repo.update_session_last_active(
                    session_id, self._clock.now(),
                )
            # 单点广播（前端会自己 sort，避免每 token 都广播）
            session = await self._session_repo.find_session(session_id)
            if session is not None and not session.is_empty and not session.is_deleted:
                await self._broadcast_session_updated(session, "activity")
        except Exception as e:
            logger.warning(
                "[SessionList] touch_activity failed session=%s: %s",
                session_id, e,
            )

    async def soft_delete_session(
        self,
        session_id: str,
        current_view_session_id: str | None = None,
    ) -> SessionRoutingResult:
        """软删除会话（用户操作）。

        编排步骤（时序）：
          1. find_session（S3 越权检查：不存在/is_deleted=1 → SessionNotFoundError）
          2. Reject pending approvals（best-effort）
          3. cancel Agent（best-effort）
          4. 清 run_state / 会话附属数据（best-effort）
          5. soft delete DB
          6. 决定路由 SessionRoutingResult
          7. 广播 SESSION_DELETED
        """
        session = await self._session_repo.find_session(session_id)
        if session is None or session.is_deleted:
            raise SessionNotFoundError(session_id)

        # 2. 拒绝 pending approval
        try:
            pending = await self._approval_repo.find_pending_by_session(session_id)
            for req in pending:
                try:
                    await self._approval_repo.resolve_approval_request(
                        approval_id=req.approval_id,
                        decision=ApprovalDecision.REJECTED,
                        resolved_at=self._clock.now(),
                    )
                    # 广播关掉前端弹窗
                    await self._broadcast.send(
                        NormalizedEvent.approval_result(
                            approval_id=req.approval_id,
                            decision=ApprovalDecision.REJECTED.value,
                            reason="session_deleted",
                            reply_id=req.reply_id or req.run_id or "",
                            run_id=req.run_id or "",
                        ),
                    )
                except Exception as e:
                    logger.warning(
                        "[SessionList] reject pending approval failed "
                        "approval_id=%s: %s", req.approval_id, e,
                    )
        except Exception as e:
            logger.warning(
                "[SessionList] find_pending_by_session failed session=%s: %s",
                session_id, e,
            )

        # 3. cancel agent
        try:
            await self._agent_pool.cancel_session(session_id)
        except Exception as e:
            logger.warning(
                "[SessionList] agent_pool.cancel_session failed session=%s: %s",
                session_id, e,
            )

        # 4. 清 run_state
        try:
            await self._run_state_repo.delete_all_for_session(session_id)
        except Exception as e:
            logger.warning(
                "[SessionList] delete_all_for_session run_state failed=%s: %s",
                session_id, e,
            )
        await self._cleanup_session_payloads(session_id)

        # 5. soft delete
        await self._session_repo.soft_delete_session(session_id)

        # 5b. 通知 group 层同步正向记录（best-effort，不阻塞删除流程）
        if self._on_session_removed is not None:
            try:
                await self._on_session_removed(session_id, session.group_id)
            except Exception as e:
                logger.warning(
                    "[SessionList] on_session_removed failed session=%s: %s",
                    session_id, e,
                )

        # 6. 路由
        routing = await self._route_after_delete(
            deleted_session_id=session_id,
            user_id=session.user_id,
            current_view_session_id=current_view_session_id,
        )

        # 7. 广播 SESSION_DELETED
        try:
            await self._broadcast.send(
                NormalizedEvent.session_deleted(
                    session_id=session_id,
                    routing=routing.to_dict(),
                ),
            )
        except Exception as e:
            logger.warning(
                "[SessionList] broadcast SESSION_DELETED failed=%s: %s",
                session_id, e,
            )

        logger.info(
            "[SessionList] soft_delete session=%s routing=%s",
            session_id, routing.action,
        )
        return routing

    async def _cleanup_session_payloads(self, session_id: str) -> None:
        """Best-effort 清理会话附属数据。

        用户在会话列表删除一个会话时，前端语义是「这个会话不要了」。
        元数据仍走 soft delete 以保留路由/幂等语义，但 raw_log、
        working_memory、AgentTask 等 payload 应尽量物理清理，避免
        Markdown 模式下留下 sessions/{sid}/raw_log.md 目录。
        """
        raw_path: str | None = None
        if self._raw_log_backend is not None:
            try:
                raw_path = self._raw_log_backend._get_session_path(session_id)
            except Exception:
                raw_path = None
            try:
                self._raw_log_backend.delete_turns(session_id)
            except Exception as e:
                logger.warning(
                    "[SessionList] delete raw_log failed session=%s: %s",
                    session_id, e,
                )

        working_path: str | None = None
        if self._working_memory_backend is not None:
            try:
                working_path = self._working_memory_backend._get_session_path(
                    session_id,
                )
            except Exception:
                working_path = None
            try:
                self._working_memory_backend.delete_session(session_id)
            except Exception as e:
                logger.warning(
                    "[SessionList] delete working_memory failed session=%s: %s",
                    session_id, e,
                )

        if self._agent_task_repo is not None:
            try:
                await self._agent_task_repo.delete_session_tasks(session_id)
            except Exception as e:
                logger.warning(
                    "[SessionList] delete agent_tasks failed session=%s: %s",
                    session_id, e,
                )

        self._remove_empty_payload_dir(session_id, raw_path, working_path)

    def _remove_empty_payload_dir(
        self,
        session_id: str,
        raw_path: str | None,
        working_path: str | None,
    ) -> None:
        """删除 Markdown payload 的空 session 目录。"""
        dirs: set[str] = set()
        for path in (raw_path, working_path):
            if path:
                dirs.add(os.path.dirname(path))
        for session_dir in dirs:
            try:
                if os.path.isdir(session_dir) and not os.listdir(session_dir):
                    os.rmdir(session_dir)
            except Exception as e:
                logger.warning(
                    "[SessionList] remove empty payload dir failed "
                    "session=%s dir=%s: %s",
                    session_id, session_dir, e,
                )

    async def toggle_favorite(self, session_id: str) -> bool:
        """翻转收藏标记。返回翻转后值。"""
        session = await self._session_repo.find_session(session_id)
        if session is None or session.is_deleted:
            raise SessionNotFoundError(session_id)
        new_val = not session.is_favorite
        await self._session_repo.update_session_meta(
            session_id, is_favorite=new_val, touch_updated_at=False,
        )
        updated = await self._session_repo.find_session(session_id)
        if updated is not None:
            await self._broadcast_session_updated(updated, "favorite")
        return new_val

    # ═══════════════════════════════════════════════════════════
    # 列表查询
    # ═══════════════════════════════════════════════════════════

    async def list_sessions(
        self,
        user_id: str,
        group_id: str | None,
        page: int = 1,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> tuple[list[SessionInfo], bool]:
        """列出可见会话。

        Args:
            group_id: None 或 "all" 表示全部；"" 空字符串表示无分组；其他值表示具体分组
            page: 1-based
            limit: [1, MAX_PAGE_SIZE]

        Returns:
            (sessions, has_more)
        """
        if limit <= 0 or limit > MAX_PAGE_SIZE:
            raise InvalidPageSize(limit, MAX_PAGE_SIZE)

        # group_id 归一化
        effective_group_id: str | None
        if group_id is None or group_id == "all":
            effective_group_id = None
        else:
            effective_group_id = group_id

        sessions_raw, has_more = await self._session_repo.list_visible_sessions(
            user_id=user_id,
            group_id=effective_group_id,
            page=max(1, page),
            limit=limit,
        )

        # 分组名回填（一次查询整表 groups 字典）
        group_map: dict[str, str] = {}
        if self._group_repo is not None:
            try:
                groups = await self._group_repo.list_groups_by_user(user_id)
                group_map = {g.id: g.name for g in groups}
            except Exception as e:
                logger.warning(
                    "[SessionList] list_groups_by_user failed: %s", e,
                )

        infos = [session_to_info(s, group_map) for s in sessions_raw]
        return infos, has_more

    # ═══════════════════════════════════════════════════════════
    # 启动引导
    # ═══════════════════════════════════════════════════════════

    async def startup_bootstrap(
        self, user_id: str, device_id: str = ""
    ) -> StartupPayload:
        """启动引导（应用 start_all 完成后调用）。

        Steps:
          1. hard_delete_empty_sessions —— 清 is_empty=1 遗留
             （PRD 流转规则：空会话不持久化，下次启动清除）
          2. run_state 的 orphan 已由 pandapal 现有 cleanup_orphans 处理
          3. 建新空 session 作为初始视图
          4. 加载首屏列表 + 分组
        """
        if not user_id:
            raise ValueError("user_id is required")

        # 1. 清空 is_empty 遗留
        try:
            cleaned = await self._session_repo.hard_delete_empty_sessions(user_id)
            if cleaned > 0:
                logger.info(
                    "[SessionList] startup cleaned %d empty sessions user=%s",
                    cleaned, user_id,
                )
        except Exception as e:
            logger.warning(
                "[SessionList] hard_delete_empty_sessions failed: %s", e,
            )

        # 2. 建新空 session（走 create_empty_session 会节流复用；
        #    但此时刚清完 is_empty，肯定 miss，直接新建）
        try:
            initial_session_id = await self.create_empty_session(
                user_id=user_id, device_id=device_id,
            )
        except Exception as e:
            logger.error(
                "[SessionList] startup create_empty_session failed: %s", e,
            )
            initial_session_id = ""

        # 3. 加载首屏列表
        try:
            infos, has_more = await self.list_sessions(
                user_id, group_id=None, page=1, limit=DEFAULT_PAGE_SIZE,
            )
        except Exception as e:
            logger.warning("[SessionList] startup list_sessions failed: %s", e)
            infos, has_more = [], False

        # 4. 加载分组
        try:
            groups = (
                await self._group_repo.list_groups_by_user(user_id)
                if self._group_repo is not None
                else []
            )
        except Exception as e:
            logger.warning("[SessionList] startup list_groups failed: %s", e)
            groups = []

        payload = StartupPayload(
            initial_session_id=initial_session_id,
            sessions=infos,
            groups=groups,
            has_more=has_more,
        )
        # 广播首屏
        await self._broadcast_startup(payload)
        return payload

    # ═══════════════════════════════════════════════════════════
    # 切换（副作用 handler：只 touch_activity + 返回 context_status）
    # ═══════════════════════════════════════════════════════════

    async def on_switch_session(
        self, user_id: str, target_session_id: str
    ) -> str:
        """SESSION_SWITCH handler 副作用：判定 context_status 并广播 SESSION_SWITCHED。

        Returns:
            context_status: "fresh" | "restored" | "degraded"
        """
        session = await self._session_repo.find_session(target_session_id)
        if session is None or session.is_deleted or session.user_id != user_id:
            raise SessionNotFoundError(target_session_id)

        # 判定 context_status
        context_status = self._degrade_signal(session)

        await self._broadcast.send(
            NormalizedEvent.session_switched(
                session_id=target_session_id,
                context_status=context_status,
            ),
        )
        return context_status

    # ═══════════════════════════════════════════════════════════
    # 历史消息回补（Gap 1）
    # ═══════════════════════════════════════════════════════════

    async def get_session_history(
        self, user_id: str, session_id: str, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        """从 raw_log 拉取该 session 的历史消息（前端 LRU 淘汰后回补 + 向上翻页）。

        Args:
            limit: 本页返回的「折叠后」消息条数。
            offset: 已加载条数（0 = 最新一页）；翻页时取更早的切片。

        Returns:
            [{"role": "user"|"assistant"|..., "content": "...", "timestamp": iso}, ...]
        """
        session = await self._session_repo.find_session(session_id)
        if session is None or session.is_deleted or session.user_id != user_id:
            raise SessionNotFoundError(session_id)

        if self._raw_log_backend is None:
            return []
        try:
            messages = self._raw_log_backend.load_all(session_id) or []
        except Exception as e:
            logger.warning(
                "[SessionList] load_all raw_log failed session=%s: %s",
                session_id, e,
            )
            return []

        # 富投影：还原「思考/文本/工具」的交错时间线（前端 loadHistory 消费）。
        # tool 结果按 tool_call_id 建索引（从全量消息取，避免 limit 窗口切断 assistant 与其结果）。
        tool_results: dict[str, dict[str, Any]] = {}
        for m in messages:
            if m.get("role") == "tool":
                tcid = m.get("tool_call_id")
                if tcid:
                    out_text = _history_text(m.get("content", ""))
                    tool_results[str(tcid)] = {
                        "text": out_text,
                        "is_error": out_text.strip().startswith("❌"),
                    }

        simplified: list[dict[str, Any]] = []
        # 富投影必须在全量消息上完成（tool_results 索引本就全量），
        # 最后再按「折叠后条数」取 simplified[-limit:]。若在此处用 messages[-limit:]，
        # 单回合工具调用多时会把该回合的 user 提问整体切在窗口外，导致「丢最新消息」。
        for m in messages:
            role = m.get("role", "assistant")
            if role == "tool":
                continue  # tool 结果已折叠进对应 assistant 的 tool 段
            text = _history_text(m.get("content", ""))
            if role != "assistant":
                simplified.append({"role": role, "content": text, "timestamp": m.get("timestamp")})
                continue

            # assistant：按 reasoning → text → tools 的顺序拼装 timeline
            timeline: list[dict[str, Any]] = []
            reasoning = m.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning.strip():
                timeline.append({"kind": "reasoning", "text": reasoning})
            if text:
                timeline.append({"kind": "text", "content": text})

            tool_calls_out: list[dict[str, Any]] = []
            for tc in (m.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                tcid = str(tc.get("id", ""))
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                name = (fn or {}).get("name", "") or tc.get("name", "")
                args = _parse_tool_args((fn or {}).get("arguments"))
                res = tool_results.get(tcid)
                out_text = res["text"] if res else ""
                is_error = bool(res and res["is_error"])
                tool_calls_out.append({
                    "tool_call_id": tcid,
                    "tool_name": name,
                    "args": args,
                    "status": "error" if is_error else "done",
                    "result": {
                        "preview": out_text[:500],
                        "full": out_text[:20000],
                        "error": out_text if is_error else None,
                    },
                })
                timeline.append({"kind": "tool", "tool_call_id": tcid})

            simplified.append({
                "role": "assistant",
                "content": text,
                "timestamp": m.get("timestamp"),
                "timeline": timeline,
                "tool_calls": tool_calls_out,
            })
        # 分页切片：offset=0 取最新 limit 条；offset>0 取更早的 [-(offset+limit):-offset]
        if limit <= 0:
            page: list[dict[str, Any]] = []
        elif offset <= 0:
            page = simplified[-limit:]
        else:
            page = simplified[-(offset + limit):-offset]
        has_more = (offset + limit) < len(simplified) if limit > 0 else False

        # 广播
        try:
            await self._broadcast.send(
                NormalizedEvent.session_history_list(
                    session_id=session_id,
                    messages=page,
                    offset=offset,
                    has_more=has_more,
                ),
            )
        except Exception as e:
            logger.warning(
                "[SessionList] broadcast SESSION_HISTORY_LIST failed: %s", e,
            )
        return page

    # ═══════════════════════════════════════════════════════════
    # 全局搜索（命令面板 ⌘K）
    # ═══════════════════════════════════════════════════════════

    async def search(
        self,
        user_id: str,
        query: str,
        *,
        session_limit: int = 15,
        message_limit: int = 20,
    ) -> dict[str, list[dict[str, Any]]]:
        """命令面板全局搜索：会话标题命中 + 消息全文命中。

        返回 {"sessions": [...], "messages": [...]}；本方法不广播，
        由调用方（app.py 的 search handler）包成 SEARCH_RESULT 事件广播。
        """
        import json as _json

        q = query.strip()
        if not q:
            return {"sessions": [], "messages": []}
        ql = q.lower()

        # ── 1. 会话标题命中 ──
        session_results: list[dict[str, Any]] = []
        try:
            sessions = await self._session_repo.search_by_title(
                user_id, q, session_limit
            )
            for s in sessions:
                session_results.append({
                    "session_id": s.session_id,
                    "title": s.title or "新会话",
                    "preview": s.preview or "",
                    "is_favorite": bool(s.is_favorite),
                    "updated_at": s.updated_at or "",
                })
        except Exception as e:
            logger.warning("[SessionList] search titles failed: %s", e)

        # ── 2. 消息全文命中 ──
        message_results: list[dict[str, Any]] = []
        backend = self._raw_log_backend
        if backend is not None and hasattr(backend, "search_messages"):

            def _extract_text(cj: str) -> tuple[str, str]:
                """从 content_json 提取正文文本 + role。"""
                try:
                    msg = _json.loads(cj)
                except Exception:
                    return ("", "assistant")
                role = str(msg.get("role", "assistant"))
                content = msg.get("content", "")
                if isinstance(content, str):
                    return (content, role)
                parts: list[str] = []
                if isinstance(content, list):
                    for blk in content:
                        if isinstance(blk, dict):
                            t = blk.get("text")
                            if isinstance(t, str):
                                parts.append(t)
                        elif isinstance(blk, str):
                            parts.append(blk)
                return (" ".join(parts), role)

            def _snippet(text: str) -> str:
                """截取命中词周围窗口，两端加省略号。"""
                low = text.lower()
                idx = low.find(ql)
                if idx < 0:
                    return text[:80].strip()
                start = max(0, idx - 40)
                end = min(len(text), idx + len(q) + 40)
                snip = text[start:end].strip().replace("\n", " ")
                if start > 0:
                    snip = "…" + snip
                if end < len(text):
                    snip = snip + "…"
                return snip

            try:
                rows = backend.search_messages(q, message_limit * 4)
            except Exception as e:
                logger.warning("[SessionList] search messages failed: %s", e)
                rows = []

            # session_id → 标题（None 表示会话已删/越权，跳过其消息）
            title_cache: dict[str, str | None] = {}
            for sid, content_json, created_at in rows:
                text, role = _extract_text(content_json)
                # LIKE 命中可能落在 JSON 噪声（role / tool id）上，复核正文
                if ql not in text.lower():
                    continue
                if sid not in title_cache:
                    try:
                        sess = await self._session_repo.find_session(sid)
                    except Exception:
                        sess = None
                    if sess is None or sess.is_deleted or sess.user_id != user_id:
                        title_cache[sid] = None
                    else:
                        title_cache[sid] = sess.title or "新会话"
                title = title_cache[sid]
                if title is None:
                    continue
                message_results.append({
                    "session_id": sid,
                    "title": title,
                    "snippet": _snippet(text),
                    "role": role,
                    "timestamp": created_at or "",
                })
                if len(message_results) >= message_limit:
                    break

        return {"sessions": session_results, "messages": message_results}

    # ═══════════════════════════════════════════════════════════
    # 内部
    # ═══════════════════════════════════════════════════════════

    async def _evict_oldest(self, user_id: str) -> str | None:
        """查找最旧可见会话并软删除。返回被淘汰 id，None 表示无可淘汰。"""
        try:
            oldest = await self._session_repo.find_oldest_visible(user_id)
            if oldest is None:
                return None
            # 走完整 delete 流程（会 cancel agent + 清 approval + 清 run_state + 广播）
            await self.soft_delete_session(
                oldest.session_id,
                current_view_session_id=None,
            )
            logger.info(
                "[SessionList] evict_oldest session=%s user=%s",
                oldest.session_id, user_id,
            )
            return oldest.session_id
        except Exception as e:
            logger.error(
                "[SessionList] _evict_oldest failed user=%s: %s", user_id, e,
            )
            return None

    async def _route_after_delete(
        self,
        deleted_session_id: str,
        user_id: str,
        current_view_session_id: str | None,
    ) -> SessionRoutingResult:
        """决策删除后前端应切到哪个会话。见 5b-2-d 决策表。"""
        if current_view_session_id != deleted_session_id:
            return SessionRoutingResult(action="no_change")

        # 剩余可见会话
        infos, _ = await self._session_repo.list_visible_sessions(
            user_id=user_id, group_id=None, page=1, limit=1,
        )
        if infos:
            return SessionRoutingResult(
                action="switch", target_session_id=infos[0].session_id,
            )

        # 无可见会话 → 尝试用空 session
        empty = await self._session_repo.find_current_empty_session(user_id)
        if empty is not None:
            return SessionRoutingResult(
                action="switch", target_session_id=empty.session_id,
            )

        return SessionRoutingResult(action="empty_state")

    def _degrade_signal(self, session: Session) -> str:
        """判定切换的 context_status。

        规则（简化，raw_log 存储由 SDK 层保证按 session_id 隔离）：
          - is_empty=1 → "fresh"（新空 session）
          - message_count=0 但 is_empty=0 → "degraded"（异常状态）
          - message_count>0 → "restored"
        """
        if session.is_empty:
            return "fresh"
        if session.message_count == 0:
            return "degraded"
        return "restored"

    async def _broadcast_session_updated(
        self, session: Session, reason: str
    ) -> None:
        group_map: dict[str, str] = {}
        if self._group_repo is not None and session.group_id:
            try:
                g = await self._group_repo.find_group(session.group_id)
                if g is not None:
                    group_map[g.id] = g.name
            except Exception:
                pass
        info = session_to_info(session, group_map)
        try:
            await self._broadcast.send(
                NormalizedEvent.session_updated(
                    session_info=info.to_dict(),
                    reason=reason,
                ),
            )
        except Exception as e:
            logger.warning(
                "[SessionList] broadcast SESSION_UPDATED failed: %s", e,
            )

    async def _broadcast_group_list(self, user_id: str) -> None:
        try:
            groups = (
                await self._group_repo.list_groups_by_user(user_id)
                if self._group_repo is not None
                else []
            )
            payload = [
                {
                    "id": g.id,
                    "user_id": g.user_id,
                    "name": g.name,
                    "created_at": g.created_at.isoformat() if g.created_at else "",
                }
                for g in groups
            ]
            await self._broadcast.send(
                NormalizedEvent.session_group_list(groups=payload),
            )
        except Exception as e:
            logger.warning("[SessionList] broadcast group_list failed: %s", e)

    async def _broadcast_startup(self, payload: StartupPayload) -> None:
        """广播 SESSION_LIST + SESSION_GROUP_LIST + SESSION_SWITCHED（首屏）。"""
        try:
            await self._broadcast.send(
                NormalizedEvent.session_list(
                    sessions=[s.to_dict() for s in payload.sessions],
                    has_more=payload.has_more,
                    page=1,
                    group_id="all",
                ),
            )
            groups_payload = [
                {
                    "id": g.id, "user_id": g.user_id, "name": g.name,
                    "created_at": g.created_at.isoformat() if g.created_at else "",
                }
                for g in payload.groups
            ]
            await self._broadcast.send(
                NormalizedEvent.session_group_list(groups=groups_payload),
            )
            if payload.initial_session_id:
                await self._broadcast.send(
                    NormalizedEvent.session_switched(
                        session_id=payload.initial_session_id,
                        context_status="fresh",
                    ),
                )
        except Exception as e:
            logger.warning("[SessionList] broadcast startup failed: %s", e)

    # ═════════════════════════════════════════════════════════════
    # 纯函数（可独立单元测试）
    # ═════════════════════════════════════════════════════════════

    @staticmethod
    def _generate_title(first_user_text: str) -> str:
        """生成会话标题：前 10 个 Unicode 字符；全空白→"新会话"。"""
        if not first_user_text:
            return "新会话"
        stripped = first_user_text.strip()
        if not stripped:
            return "新会话"
        return stripped[:MAX_TITLE_LENGTH]

    @staticmethod
    def _generate_preview(first_user_text: str) -> str:
        """生成会话副标题：前 40 个 Unicode 字符。"""
        if not first_user_text:
            return ""
        stripped = first_user_text.strip().replace("\n", " ")
        return stripped[:MAX_PREVIEW_LENGTH]


# ═══════════════════════════════════════════════════════════
# 历史投影辅助（get_session_history 富 timeline 构建用）
# ═══════════════════════════════════════════════════════════

def _history_text(content: Any) -> str:
    """把 MessageDict.content（str | list 多模态块）归一化为纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for blk in content:
            if isinstance(blk, dict):
                t = blk.get("text") or blk.get("content")
                if isinstance(t, str):
                    parts.append(t)
            elif isinstance(blk, str):
                parts.append(blk)
        return "".join(parts)
    return ""


def _parse_tool_args(arguments: Any) -> dict[str, Any]:
    """OpenAI tool_call.function.arguments 是 JSON 字符串 → dict。"""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str) and arguments.strip():
        try:
            parsed = json.loads(arguments)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}
