"""SessionManager — 会话生命周期管理。

================================================================================
⚠️  重要：Session 与对话记忆的关系（容易混淆，请仔细阅读）
================================================================================

一、Session 只负责"会话生命周期管理"，**不存储对话内容本身**：

  Session（本文件）
  ├── 存储内容：session_id, user_id, device_id, last_active, created_at
  ├── 职责：创建/验证/刷新/过期/清理会话
  └── 不存储：对话消息内容

  对话内容存储在 SDK 层的 Memory 模块：
  ├── RawLogBackend  → 存储原始对话日志（离线分析数据源）
  └── DropSummarizer → 被丢弃消息的 LLM 脉络摘要（v1.4 新增，应用层注入）

二、关键澄清：RawLog **永远有 session_id**，不存在"没有 session_id 就不知道是谁的"情况

  session_id 由后端 SessionListManager 创建（canonical 格式 sess-{uuid}；
  Rust 冷启动兜底 mint 亦同格式）。
  Scheduler 层不创建 session_id，仅从上游获取。
  └── 结果：RawLogBackend.append_raw_message() 调用时，session_id 一定存在

  因此：
  ❌ 错误理解：Session 的存在是为了"给 RawLog 提供身份标识"
  ✅ 正确理解：Session 的价值是「会话生命周期管理」，RawLog 的身份标识由 Scheduler 保证

三、Session 的真正价值（4 个核心作用）：

  1. 会话过期判断：通过 last_active 判断会话是否超时
  2. 多设备/多渠道隔离：不同 device_id 可拥有独立 Session（当前策略是同一用户共享）
  3. 消息路由：确保消息能关联到正确的会话上下文
  4. v1.4 变更：会话过期/结束不再触发 SessionSummaryPolicy（已删除），
     应用层可通过 RawLogBackend.load_all() 离线提炼

四、两者通过 session_id 关联，但存储完全分离：

典型流程：
  1. 用户发消息 → Scheduler 使用上游传入的 session_id（不自动生成）
  2. SessionManager.ensure_session() → 幂等确保会话记录存在（非破坏性，不造 session_id 身份）
  3. 对话进行中 → 每条消息追加到 RawLogBackend（按 session_id 隔离）
  4. 会话过期/结束 → end_session() 只做 flush + reset（v1.4 简化，不再调 LLM）
  5. 应用层离线任务通过 RawLogBackend.load_all() 提炼 User Model / Episodic Archive

设计好处：
  - 关注点分离：会话管理 vs 对话存储互不影响
  - 可独立扩展：换存储后端只需换 Backend 实现
  - 多设备支持：不同 device_id 可有独立 Session，共享同一 RawLog
  - Scheduler 保证 session_id 永远存在，降低各层的空值处理负担

================================================================================

职责：
- 创建/验证/刷新/过期/清理 Session
- 确保每条消息绑定到正确的会话上下文
- 提供超时策略（从 ConfigManager 实时读取，不缓存）

设计约束：
- BL1 (Single Responsibility): 仅管理会话生命周期
- BL2 (Stateless): 实例字段仅存储注入依赖，无请求上下文
- BL4 (DI): SessionRepository + ConfigManager 注入
- BL5 (Semantic Exceptions): SessionNotFoundError / SessionExpiredError
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from pandapal.config.system.manager import ConfigManager
from pandapal.session.exceptions import SessionExpiredError, SessionNotFoundError
from pandapal.storage.models import Session
from pandapal.storage.repositories.sqlite_agent_task_repo import AgentTaskRepository
from pandapal.storage.repositories.sqlite_session_repo import SessionRepository
from pandapal.storage.repositories.markdown_session_repo import MarkdownSessionRepository

logger = logging.getLogger(__name__)


class SessionManager:
    """会话生命周期管理器（BL2 Stateless）。

    ⚠️  重要：Session 与对话记忆的关系（容易混淆，请仔细阅读）
    ═══════════════════════════════════════════════════════════════════════

    一、Session 只负责"会话生命周期管理"，**不存储对话内容本身**：

      Session（本类）
      ├── 存储内容：session_id, user_id, device_id, last_active, created_at
      ├── 职责：创建/验证/刷新/过期/清理会话
      └── 不存储：对话消息内容

      对话内容存储在 SDK 层的 Memory 模块：
      ├── RawLogBackend  → 存储原始对话日志（离线分析数据源）
      └── DropSummarizer → 被丢弃消息的 LLM 脉络摘要（v1.4 新增，应用层注入）

    二、关键澄清：RawLog **永远有 session_id**，不存在"没有 session_id 就不知道是谁的"情况

      session_id 由后端 SessionListManager 创建（canonical 格式 sess-{uuid}；
  Rust 冷启动兜底 mint 亦同格式）。
      Scheduler 层不创建 session_id，仅从上游获取。
      └── 结果：RawLogBackend.append_raw_message() 调用时，session_id 一定存在

      因此：
      ❌ 错误理解：Session 的存在是为了"给 RawLog 提供身份标识"
      ✅ 正确理解：Session 的价值是「会话生命周期管理」，RawLog 的身份标识由 Scheduler 保证

    三、Session 的真正价值（4 个核心作用）：

      1. 会话过期判断：通过 last_active 判断会话是否超时
      2. 多设备/多渠道隔离：不同 device_id 可拥有独立 Session（当前策略是同一用户共享）
      3. 消息路由：确保消息能关联到正确的会话上下文
      4. v1.4 变更：会话过期/结束不再触发 SessionSummaryPolicy（已删除）

    四、典型交互流程：

      1. 用户发消息 → Scheduler 使用上游传入的 session_id（不自动生成）
      2. SessionManager.ensure_session() → 幂等确保会话记录存在（非破坏性，不造 session_id 身份）
      3. 对话进行中 → 每条消息追加到 RawLogBackend（按 session_id 隔离）
      4. 会话过期/结束 → end_session() 只做 flush + reset（v1.4 简化，不再调 LLM）
      5. 应用层离线任务通过 RawLogBackend.load_all() 提炼 User Model / Episodic Archive

    设计好处：
      - 关注点分离：会话管理 vs 对话存储互不影响
      - 可独立扩展：换存储后端只需换 Backend 实现
      - 多设备支持：不同 device_id 可有独立 Session，共享同一 RawLog
      - Scheduler 保证 session_id 永远存在，降低各层的空值处理负担

    ═══════════════════════════════════════════════════════════════════════

    职责说明：
        - 管理用户会话的整个生命周期（创建、验证、刷新、过期、清理）
        - 确保每条消息都能关联到正确的会话上下文
        - 提供灵活的超时策略，支持从配置动态读取

    设计模式：
        - 依赖注入（DI）：通过构造函数注入 SessionRepository 和 ConfigManager
        - 无状态设计（Stateless）：实例本身不保存会话状态，所有状态都在数据库中

    使用示例：
        session_manager = SessionManager(
            session_repo=storage_manager.get_session_repo(),  # 注入会话仓储
            config_manager=config_manager,  # 注入配置管理器
        )
        # 幂等确保会话记录存在
        session = await session_manager.ensure_session(
            session_id="s1", user_id="u1", device_id="d1"
        )
    """

    # 默认会话超时时间（分钟）
    # Fix #3: 提取为类常量，与 SystemConfig 默认值保持一致
    # 如果配置读取失败，将使用此默认值
    _DEFAULT_TIMEOUT_MINUTES = 60

    def __init__(
        self,
        session_repo: SessionRepository | MarkdownSessionRepository,
        config_manager: ConfigManager,
        agent_task_repo: AgentTaskRepository | None = None,
    ) -> None:
        """初始化会话管理器。

        依赖注入说明：
            - session_repo: 会话数据访问对象，负责会话的 CRUD 操作
            - config_manager: 配置管理器，用于读取会话超时等配置
            - agent_task_repo: （可选）AgentTask 数据仓库，用于会话过期时取消关联任务

        设计约束（BL2 Stateless）：
            - 实例只存储注入的依赖，不保存任何请求上下文状态
            - 每次方法调用都是独立的，不依赖实例的状态
        """
        # 注入会话仓储（数据访问层）
        self._session_repo = session_repo
        # 注入配置管理器（配置读取层）
        self._config_manager = config_manager
        # 注入 AgentTask 仓储（可选的跨模块联动）
        self._agent_task_repo = agent_task_repo

    # ──────────────────────────────────────────────
    # Public Methods (6)
    # ──────────────────────────────────────────────

    async def ensure_session(
        self, session_id: str, user_id: str, device_id: str = ""
    ) -> Session:
        """幂等确保「该 session_id 对应的会话记录」存在，返回它。

        ★ 这是 ensure（幂等 upsert）而非 create：session_id 由调用方（发起方）决定并传入，
          本方法只在持久层「有则返回、无则建记录」，**绝不创建/伪造 session_id 身份**
          —— 与 Rust 侧已拆分的 create_session_id（造身份）语义完全不同，见 SESSION_ID 契约。

        ★ 非破坏性：即使已存在的会话已过期也**原样返回，绝不删表重建**。sessions 表由
          SessionListManager 共管（title/preview/group 等元数据），此处删表会
          静默清空用户的会话元数据（数据丢失）。过期判定交给 validate_session 显式处理，
          由调用方决策，不在 ensure 里夹带破坏性副作用。

        业务逻辑（两种情况）：
        1. session_id 已存在 → 直接返回（幂等复用，不管是否过期）
        2. session_id 不存在 → 新建一条记录并返回

        参数说明：
            session_id: 会话ID（由发起方创建并传入，本方法不生成）
            user_id: 用户ID
            device_id: 设备ID（可选）

        返回值：
            该 session_id 对应的 Session 记录
        """
        # 1. 有则返回（幂等，非破坏性——过期与否不在此处理）
        existing = await self._session_repo.find_session(session_id)
        if existing is not None:
            return existing

        # 2. 无则新建记录
        now = datetime.now(timezone.utc)
        new_session = Session(
            session_id=session_id,
            user_id=user_id,
            device_id=device_id,
            last_active=now,
            created_at=now,
            is_empty=False,
        )
        await self._session_repo.save_session(new_session)
        logger.info(
            "会话记录已创建: session_id=%s, user_id=%s, device_id=%s",
            session_id, user_id, device_id,
        )
        return new_session

    async def validate_session(self, session_id: str) -> Session:
        """验证会话是否有效（存在且未过期）。

        业务场景：
            - 在处理用户消息前，验证会话是否有效
            - 如果会话无效，抛出异常，由调用方处理（如要求重新创建会话）

        参数：
            session_id: 要验证的会话ID

        返回值：
            如果会话有效，返回 Session 对象

        异常：
            SessionNotFoundError: 会话不存在（可能已被删除）
            SessionExpiredError: 会话已超时（长时间未活跃）
        """
        # 1. 从数据库查找会话
        session = await self._session_repo.find_session(session_id)

        # 2. 检查会话是否存在
        if session is None:
            logger.warning(
                "会话验证失败: session_id=%s, 原因=不存在",
                session_id,
            )
            # 抛出语义化异常（BL5: Semantic Exceptions）
            raise SessionNotFoundError(session_id)

        # 3. 检查会话是否过期
        if self._is_session_expired(session):
            logger.warning(
                "会话验证失败: session_id=%s, 原因=已过期",
                session_id,
            )
            # 抛出语义化异常，包含最后活跃时间
            raise SessionExpiredError(session_id, session.last_active)

        # 4. 会话有效，返回会话对象
        return session

    async def refresh_session_activity(self, session_id: str) -> None:
        """刷新会话的最后活跃时间（心跳机制）。

        业务场景：
            - 用户发送消息时，调用此方法来延长会话生命周期
            - 防止用户在活跃时会话过期

        工作原理：
            - 更新 last_active 为当前时间
            - 会话过期时间 = last_active + timeout_minutes
            - 所以刷新 last_active 就等于延长了过期时间

        参数：
            session_id: 要刷新的会话ID

        异常：
            SessionNotFoundError: 会话不存在（可能已被删除）

        注意：
            - 此方法只更新时间，不验证会话是否过期
            - 如果会话已过期，调用此方法会报错（因为会话不存在）
        """
        # 1. 查找会话（验证是否存在）
        session = await self._session_repo.find_session(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)

        # 2. 获取当前UTC时间
        now = datetime.now(timezone.utc)
        # 3. 更新数据库中的最后活跃时间
        await self._session_repo.update_session_last_active(session_id, now)

    async def expire_session(self, session_id: str) -> None:
        """强制使会话过期（立即删除会话）。

        业务场景：
            - 用户主动退出登录
            - 管理员强制下线用户
            - 检测到异常行为，强制终止会话

        联动：如果注入了 agent_task_repo，先取消该会话的所有未完成任务。

        幂等性设计：
            - 如果会话不存在，不会报错（删除操作本身是幂等的）
            - 可以多次调用，结果一致

        参数：
            session_id: 要过期的会话ID
        """
        # AgentTask 会话生命周期联动：过期前取消所有未完成任务
        if self._agent_task_repo is not None:
            try:
                cancelled = await self._agent_task_repo.cancel_session_tasks(session_id)
                if cancelled > 0:
                    logger.info(
                        "会话过期时取消了 %d 个 AgentTask: session_id=%s",
                        cancelled, session_id,
                    )
            except Exception as e:
                logger.warning(
                    "会话过期时取消 AgentTask 失败: session_id=%s, error=%s",
                    session_id, e,
                )

        # 直接从数据库删除会话（强制过期）
        await self._session_repo.delete_session(session_id)
        logger.info(
            "会话已强制过期: session_id=%s", session_id
        )

    async def delete_expired_sessions(self) -> int:
        """批量清理所有过期会话（定时任务调用）。

        业务场景：
            - 定时任务（如每小时）调用此方法清理过期会话
            - 防止数据库中存在大量过期会话，占用存储空间

        清理逻辑：
            - 计算阈值时间：当前时间 - 超时时间
            - 删除所有 last_active < 阈值时间 的会话

        返回值：
            返回实际删除的会话数量
            如果返回 0，表示没有过期会话需要清理
        """
        # 1. 从配置读取超时时间（分钟）
        timeout_minutes = self._get_timeout_minutes()
        # 2. 获取当前UTC时间
        now = datetime.now(timezone.utc)
        # 3. 计算阈值时间（超时分界线）
        #    任何 last_active 早于这个时间的会话都算过期
        threshold = now - timedelta(minutes=timeout_minutes)

        # 4. 调用仓储层批量删除过期会话
        deleted_count = await self._session_repo.delete_expired_sessions(threshold)

        # 5. 如果有删除操作，记录日志
        if deleted_count > 0:
            logger.info(
                "已清理过期会话: 删除数量=%d, 阈值时间=%s",
                deleted_count, threshold.isoformat(),
            )
        # 6. 返回删除数量
        return deleted_count

    async def get_active_sessions_by_user(self, user_id: str) -> list[Session]:
        """获取指定用户的所有活跃会话（未过期）。

        业务场景：
            - 查看用户当前有哪些活跃会话
            - 多设备登录场景，显示所有在线设备

        性能优化：
            Fix #1: 预先读取一次超时配置，避免对每个会话重复读取配置
            Fix #2: 当前在内存中过滤，未来可优化为在数据库层过滤（SQL WHERE）

        参数：
            user_id: 用户ID

        返回值：
            返回该用户所有未过期会话的列表
            如果用户在数据库中没有任何会话，返回空列表
        """
        # 1. 从数据库查询该用户的所有会话（包括过期的）
        all_sessions = await self._session_repo.find_sessions_by_user(user_id)

        # 2. 预先读取超时配置（性能优化：避免重复读取）
        timeout_minutes = self._get_timeout_minutes()

        # 3. 计算截止时间（当前时间 - 超时时间）
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=timeout_minutes)
        # 含义：任何 last_active 在 cutoff 之后的会话都是活跃的

        # 4. 在内存中过滤出活跃会话（last_active > cutoff）
        return [s for s in all_sessions if s.last_active > cutoff]

    # ──────────────────────────────────────────────
    # 私有方法（内部使用，不对外暴露）
    # ──────────────────────────────────────────────

    def _get_timeout_minutes(self) -> int:
        """从配置管理器实时读取会话超时时间（分钟）。

        设计要点：
            - 实时读取，不缓存（保证配置变更立即生效）
            - 如果配置读取失败，返回默认值（优雅降级）

        异常容错：
            - 捕获所有异常（配置缺失、配置格式错误、配置服务不可用等）
            - 出现异常时，记录警告日志，返回默认超时时间

        返回值：
            会话超时时间（分钟）
            默认值为 _DEFAULT_TIMEOUT_MINUTES (60分钟)
        """
        try:
            # 从配置管理器获取系统配置
            sys_config = self._config_manager.get_system_config()
            # 返回配置的超时时间
            return sys_config.session_timeout_minutes
        except Exception as e:
            # 配置读取失败，记录警告并返回默认值
            logger.warning(
                "超时配置读取失败，使用默认值: key=session_timeout_minutes, error=%s",
                e,
            )
            # 优雅降级：返回默认超时时间
            return self._DEFAULT_TIMEOUT_MINUTES

    def _is_session_expired(self, session: Session) -> bool:
        """判断会话是否已过期（纯函数，无副作用）。

        判断逻辑：
            - 计算当前时间与最后活跃时间的差值
            - 如果差值 > 超时时间，则认为会话已过期

        公式：
            过期 = (当前时间 - 最后活跃时间) > 超时时间

        参数：
            session: 要检查的会话对象

        返回值：
            True: 会话已过期
            False: 会话仍然有效

        注意：
            - 这是纯函数，不修改任何状态，不依赖外部状态
            - 每次调用都会重新读取配置（通过 _get_timeout_minutes）
        """
        # 1. 获取当前UTC时间
        now = datetime.now(timezone.utc)
        # 2. 读取超时配置
        timeout_minutes = self._get_timeout_minutes()
        # 3. 计算会话空闲时间（当前时间 - 最后活跃时间）
        elapsed = now - session.last_active
        # 4. 判断是否在超时时间
        return elapsed > timedelta(minutes=timeout_minutes)
