"""StorageManager — 数据持久化层的统一入口（门面模式/Facade Pattern）。

核心职责：
1. 生命周期管理：初始化/关闭数据库连接，确保资源正确释放
2. 连接管理：管理 SQLite 连接（WAL 模式，单连接 + asyncio 序列化）
3.  Schema 管理：协调 SchemaManager 执行数据库迁移（版本升级）
4. Repository 工厂：提供所有 Repository 的访问器（get_*_repo）

设计约束与原则：
- I1 (Fail Fast): initialize_storage() 失败时立即抛出 StorageInitError，不隐藏错误
- 所有 get_* 方法在未初始化时 raise RuntimeError，防止误用
- 支持两种存储模式：
  - "sqlite": 使用 SQLite 数据库（生产环境，高性能）
  - "markdown": 使用 Markdown 文件存储（调试模式，人类可读）
- WAL 模式：Write-Ahead Logging，支持并发读写，提升性能
- 外键约束：确保数据完整性
- 数据库完整性检查：启动时执行 PRAGMA quick_check

注意：SDK Memory Backend（RawLogBackend、WorkingMemoryBackend）也由 StorageManager 统一管理，
通过 get_raw_log_backend(user_id) / get_working_memory_backend(user_id) 获取，
自动根据 storage_mode 选择对应实现。

v1.4 变更：SummaryBackend 已从 SDK 删除（去 summary 化），get_summary_backend() 已移除。

典型使用流程：
1. 创建 StorageManager 实例（指定数据库路径和存储模式）
2. 调用 initialize_storage() 初始化（创建连接、执行迁移、实例化 Repository）
3. 通过 get_*_repo() 获取各种 Repository 进行数据操作
4. 调用 shutdown_storage() 优雅关闭（关闭连接、释放资源）

模式切换示例：
```python
# SQLite 模式（生产环境）
manager = StorageManager(storage_path="data/pandapal.db", storage_mode="sqlite")
await manager.initialize_storage()

# Markdown 模式（调试模式，数据保存为 .md 文件）
# 注意：Markdown 模式的 storage_path 必须是目录路径，不做隐式转换
manager = StorageManager(storage_path="data/pandapal_md", storage_mode="markdown")
await manager.initialize_storage()
```
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal

import aiosqlite

from pandapal.storage.exceptions import StorageInitError
from pandapal.storage.repositories.sqlite_agent_task_repo import AgentTaskRepository
from pandapal.storage.repositories.sqlite_approval_repo import ApprovalRepository
from pandapal.storage.repositories.sqlite_avatar_config_repo import AvatarConfigRepository
from pandapal.storage.repositories.sqlite_device_repo import DeviceRepository
from pandapal.storage.repositories.sqlite_run_state_repo import RunStateRepository
from pandapal.storage.repositories.sqlite_session_group_repo import SessionGroupRepository
from pandapal.storage.repositories.sqlite_session_repo import SessionRepository
from pandapal.storage.repositories.sqlite_task_repo import TaskRepository
from pandapal.storage.schema_manager import SchemaManager

# Markdown Repository 导入
from pandapal.storage.repositories.markdown_session_repo import MarkdownSessionRepository
from pandapal.storage.repositories.markdown_task_repo import MarkdownTaskRepository
from pandapal.storage.repositories.markdown_device_repo import MarkdownDeviceRepository
from pandapal.storage.repositories.markdown_approval_repo import MarkdownApprovalRepository
from pandapal.storage.repositories.markdown_avatar_config_repo import MarkdownAvatarConfigRepository
from pandapal.storage.repositories.markdown_run_state_repo import MarkdownRunStateRepository
from pandapal.storage.repositories.markdown_agent_task_repo import MarkdownAgentTaskRepository
from pandapal.storage.repositories.markdown_session_group_repo import MarkdownSessionGroupRepository

# Memory Backend 导入（SDK 层 Protocol 实现）
from pandapal.storage.repositories.sqlite_raw_log_backend import SQLiteRawLogBackend
from pandapal.storage.repositories.markdown_raw_log_backend import MarkdownRawLogBackend
from pandapal.storage.repositories.sqlite_working_memory_backend import SQLiteWorkingMemoryBackend
from pandapal.storage.repositories.markdown_working_memory_backend import MarkdownWorkingMemoryBackend

logger = logging.getLogger(__name__)


class StorageManager:
    """数据持久化层统一管理器。

    使用方式：
        manager = StorageManager(storage_path="data/pandapal.db")
        await manager.initialize_storage()
        # ... 使用 get_*_repo() 获取 repository 实例 ...
        await manager.shutdown_storage()
    """

    def __init__(
        self,
        storage_path: str,
        query_timeout_s: float = 5.0,
        storage_mode: Literal["sqlite", "markdown"] = "markdown",
        user_id: str = "",
    ) -> None:
        """
        初始化 StorageManager 实例。
        
        Args:
            storage_path: 存储路径（不做任何隐式转换，用户自行保证正确性）
                - SQLite 模式：数据库文件路径，如 "data/pandapal.db"
                - Markdown 模式：目录路径，如 "data/pandapal_md/"
            query_timeout_s: 单次查询超时秒数（I5 设计原则）
                - 防止慢查询阻塞整个应用
                - 默认 5 秒，可根据实际需求调整
            storage_mode: 存储模式（"sqlite" 或 "markdown"）
                - "sqlite": 使用 SQLite 数据库（生产环境，高性能）
                - "markdown": 使用 Markdown 文件存储（调试模式，人类可读）
        
        初始化状态：
            - _initialized = False：表示存储层尚未初始化，所有 get_* 方法会抛出异常
            - 所有 Repository 和连接都为 None，等待 initialize_storage() 调用
        
        模式说明：
            SQLite 模式：
            - 数据存储在单个 .db 文件中
            - 支持高并发读写（WAL 模式）
            - 适合生产环境
            
            Markdown 模式：
            - 数据存储在多个 .md 文件中（每个实体一个文件）
            - 使用 YAML front matter 存储结构化数据
            - 人类可读可编辑，适合调试
            - 性能较低，不适合生产环境
        """
        # 存储模式
        self._storage_mode = storage_mode  # "sqlite" 或 "markdown"

        # ★ 多 Session 并发 · 数据隔离：
        #   sidecar 进程被 Rust 用 `--user-id alice` 启动，一进程一用户。
        #   user_id 在此层埋进 storage_path，下游 backend/repo 无需再感知 user_id。
        #   应用层责任：SDK 只按 session_id 分片，跨用户隔离由 pandapal 完成。
        self._user_id = user_id
        self._storage_path = self._make_user_scoped_path(storage_path, user_id, storage_mode)
        
        self._query_timeout_s = query_timeout_s
        
        # 核心资源（延迟初始化）
        self._connection: aiosqlite.Connection | None = None  # SQLite 异步连接
        self._schema_manager: SchemaManager | None = None      # Schema 迁移管理器
        self._initialized = False                               # 初始化标志位
        
        # Repository 实例（初始化后赋值）
        # 使用 Union 类型以支持 SQLite 和 Markdown 两种实现
        self._session_repo: SessionRepository | MarkdownSessionRepository | None = None
        self._task_repo: TaskRepository | MarkdownTaskRepository | None = None
        self._device_repo: DeviceRepository | MarkdownDeviceRepository | None = None
        self._approval_repo: ApprovalRepository | MarkdownApprovalRepository | None = None
        self._avatar_config_repo: AvatarConfigRepository | MarkdownAvatarConfigRepository | None = None
        self._run_state_repo: RunStateRepository | MarkdownRunStateRepository | None = None
        self._agent_task_repo: AgentTaskRepository | MarkdownAgentTaskRepository | None = None
        self._session_group_repo: SessionGroupRepository | MarkdownSessionGroupRepository | None = None

    @staticmethod
    def _make_user_scoped_path(
        storage_path: str, user_id: str, storage_mode: str,
    ) -> str:
        """把 storage_path 改造为 user-scoped 路径。

        Markdown 模式：{storage_path}/users/{safe_uid}
        SQLite 模式：  {dirname(storage_path)}/users/{safe_uid}/{basename(storage_path)}
        user_id 为空时直接返回原路径（向后兼容 / 老单用户场景）。
        """
        if not user_id:
            return storage_path
        # 清洗 user_id：与其他 backend 的 sanitize 保持一致
        safe_uid = "".join(
            c for c in user_id.replace("/", "_").replace("\\", "_").replace(":", "-")
            if c.isalnum() or c in "-_."
        ) or "unknown"
        if storage_mode == "markdown":
            return os.path.join(storage_path, "users", safe_uid)
        # sqlite: 每 user 一个 db 文件 → {parent}/users/{safe_uid}/{basename}
        parent = os.path.dirname(storage_path) or "."
        basename = os.path.basename(storage_path) or "pandapal.db"
        return os.path.join(parent, "users", safe_uid, basename)

    async def initialize_storage(self) -> None:
        """初始化存储层（I1 Fail Fast 原则：失败立即抛出，不隐藏错误）。

        根据 storage_mode 选择初始化逻辑：
        - "sqlite": 初始化 SQLite 数据库连接和 Schema 迁移
        - "markdown": 创建 Markdown 存储目录，实例化 Markdown Repository

        初始化流程（SQLite 模式）：
        Step 1-2: 环境检查 - 确保数据库目录存在且可写
        Step 3:   连接建立 - 打开 SQLite 连接并配置优化参数
        Step 4:   Schema 迁移 - 执行数据库版本升级（如有必要）
        Step 5:   组件实例化 - 创建所有 Repository 实例

        初始化流程（Markdown 模式）：
        Step 1:   创建 Markdown 存储目录
        Step 2:   实例化所有 Markdown Repository

        错误处理策略：
        - 任何步骤失败都会抛出 StorageInitError
        - 失败时自动清理已分配的资源（连接、内存等）
        - 保证对象处于一致状态（要么完全初始化，要么完全未初始化）

        Raises:
            StorageInitError: 任何初始化步骤失败时抛出，包含详细错误信息
        """
        # 防止重复初始化
        if self._initialized:
            logger.debug("StorageManager already initialized, skipping")
            return

        # ============================================================
        # 根据 storage_mode 选择初始化逻辑
        # ============================================================
        if self._storage_mode == "sqlite":
            await self._initialize_sqlite()
        elif self._storage_mode == "markdown":
            self._initialize_markdown()
        else:
            raise StorageInitError(
                f"Unsupported storage mode: {self._storage_mode}",
                self._storage_path,
            )

        # 标记初始化完成
        self._initialized = True
        
        if self._storage_mode == "sqlite":
            logger.info("StorageManager initialized (db=%s)", self._storage_path)
        else:
            logger.info("StorageManager initialized (markdown, dir=%s)", self._storage_path)

    async def _initialize_sqlite(self) -> None:
        """初始化 SQLite 存储（原有逻辑）。"""
        # ============================================================
        # Step 1-2: 环境检查 - 确保数据库目录存在且可写
        # ============================================================
        self._ensure_db_directory()

        # ============================================================
        # Step 3: 建立数据库连接并配置 SQLite 参数
        # ============================================================
        try:
            # 打开异步 SQLite 连接
            self._connection = await aiosqlite.connect(self._storage_path)
            
            # 配置 WAL 模式（Write-Ahead Logging）
            # 优势：
            #   1. 读写可以同时进行（读不阻塞写，写不阻塞读）
            #   2. 崩溃恢复更快
            #   3. 性能更好（特别是写入密集型场景）
            await self._connection.execute("PRAGMA journal_mode=WAL")
            
            # 启用外键约束（SQLite 默认关闭）
            # 作用：保证引用完整性，如删除被引用的记录时会报错或级联删除
            await self._connection.execute("PRAGMA foreign_keys=ON")
            
            # 设置 Row factory：查询结果可以通过列名访问
            # 例如：row["session_id"] 而不是 row[0]
            self._connection.row_factory = aiosqlite.Row

            # Fix #9: 数据库完整性检查（I1 Fail Fast，设计文档要求）
            # 目的：检测数据库文件是否损坏（如磁盘错误、异常关闭等）
            # PRAGMA quick_check：快速检查数据库完整性（比 full_check 快）
            cursor = await self._connection.execute("PRAGMA quick_check")
            result = await cursor.fetchone()
            # 检查结果：正常情况下返回 ("ok",)
            if result is None or result[0] != "ok":
                check_result = result[0] if result else "no response"
                raise StorageInitError(
                    f"Database integrity check failed: {check_result}",
                    self._storage_path,
                )
        except StorageInitError:
            # 已经是 StorageInitError，直接清理并重新抛出
            await self._safe_close_connection()
            raise
        except Exception as e:
            # 其他异常（如文件权限错误、磁盘满等）包装为 StorageInitError
            raise StorageInitError(
                f"Cannot open database: {e}", self._storage_path
            ) from e

        # ============================================================
        # Step 4: Schema 迁移 - 升级数据库结构到最新版本
        # ============================================================
        try:
            # 创建 SchemaManager 并执行迁移
            # SchemaManager 会检查当前数据库版本，执行必要的迁移脚本
            self._schema_manager = SchemaManager(self._connection)
            migration_count = await self._schema_manager.run_migrations()
            
            # 记录迁移信息（如有）
            if migration_count > 0:
                logger.info(
                    "Applied %d schema migration(s)", migration_count
                )
        except Exception as e:
            # 迁移失败：关闭连接，清理资源
            await self._safe_close_connection()
            if isinstance(e, StorageInitError):
                raise
            # 包装其他异常为 StorageInitError
            raise StorageInitError(
                f"Schema migration failed: {e}", self._storage_path
            ) from e

        # ============================================================
        # Step 5: 实例化所有 Repository（数据访问层）
        # ============================================================
        # Repository 模式：每个 Repository 封装对特定表的 CRUD 操作
        conn = self._connection
        timeout = self._query_timeout_s

        # 创建各个 Repository 实例（SQLite 版本）
        self._session_repo = SessionRepository(conn, timeout)         # 对话会话管理
        self._task_repo = TaskRepository(conn, timeout)               # Agent 任务管理
        self._device_repo = DeviceRepository(conn, timeout)            # 设备信息管理
        self._approval_repo = ApprovalRepository(conn, timeout)      # 人工审批流程
        self._avatar_config_repo = AvatarConfigRepository(conn, timeout) # Agent 角色配置
        self._run_state_repo = RunStateRepository(conn, timeout)     # 执行状态跟踪
        self._agent_task_repo = AgentTaskRepository(conn, timeout)   # AI 自驱任务管理
        self._session_group_repo = SessionGroupRepository(conn, timeout)  # UI 会话分组

    def _initialize_markdown(self) -> None:
        """初始化 Markdown 存储（调试模式）。"""
        # ============================================================
        # Step 1: 创建 Markdown 存储目录
        # ============================================================
        try:
            os.makedirs(self._storage_path, exist_ok=True)
            logger.info("Markdown storage directory: %s", self._storage_path)
        except OSError as e:
            raise StorageInitError(
                f"Cannot create markdown directory: {e}",
                self._storage_path,
            ) from e

        # ============================================================
        # Step 2: 实例化所有 Markdown Repository
        # ============================================================
        # Markdown Repository 是同步的，不需要连接对象
        # 每个 Repository 管理一个子目录（如 sessions/, tasks/ 等）
        timeout = self._query_timeout_s

        # 创建各个 Repository 实例（Markdown 版本）
        self._session_repo = MarkdownSessionRepository(self._storage_path, timeout)
        self._task_repo = MarkdownTaskRepository(self._storage_path, timeout)
        self._device_repo = MarkdownDeviceRepository(self._storage_path, timeout)
        self._approval_repo = MarkdownApprovalRepository(self._storage_path, timeout)
        self._avatar_config_repo = MarkdownAvatarConfigRepository(self._storage_path, timeout)
        self._run_state_repo = MarkdownRunStateRepository(self._storage_path, timeout)
        self._agent_task_repo = MarkdownAgentTaskRepository(self._storage_path, timeout)
        self._session_group_repo = MarkdownSessionGroupRepository(self._storage_path, timeout)  # UI 会话分组

    async def shutdown_storage(self) -> None:
        """优雅关闭存储层，释放所有资源。

        关闭流程：
        1. 检查是否已初始化（防止重复关闭）
        2. 关闭主数据库连接（仅 SQLite 模式）
        3. 重置初始化标志位
        """
        # 防止重复关闭（幂等性）
        if not self._initialized:
            return

        # ============================================================
        # 步骤 1: 关闭主数据库连接（仅 SQLite 模式）
        # ============================================================
        # Markdown 模式不需要关闭数据库连接（因为是文件操作）
        if self._storage_mode == "sqlite":
            await self._safe_close_connection()

        # ============================================================
        # 步骤 2: 重置状态
        # ============================================================
        self._initialized = False
        
        if self._storage_mode == "sqlite":
            logger.info("StorageManager shut down (sqlite)")
        else:
            logger.info("StorageManager shut down (markdown)")

    # ──────────────────────────────────────────────
    # Repository Accessors（访问器方法）
    # ──────────────────────────────────────────────
    # 设计模式：懒加载 + 门面模式
    # - 所有 Repository 在 initialize_storage() 时一次性创建
    # - 通过 get_*_repo() 方法提供访问（统一入口）
    # - 未初始化时抛出异常（Fail Fast，防止误用）
    #
    # Repository 说明：
    # - SessionRepository: 管理对话会话（session），记录每次对话的元信息
    # - TaskRepository: 管理 Agent 任务（task），记录任务执行状态和结果
    # - DeviceRepository: 管理设备信息（device），用于多设备场景
    # - ApprovalRepository: 管理人工审批流程（approval），用于需要人工确认的操作
    # - AvatarConfigRepository: 管理 Agent 角色配置（avatar_config），定义 Agent 的行为和人格
    # - RunStateRepository: 管理运行状态（run_state），记录 Agent 执行状态

    def get_session_repo(self) -> SessionRepository:
        """获取 Session Repository（对话会话管理）。

        Returns:
            SessionRepository 实例，用于操作 session 表

        Raises:
            RuntimeError: 如果存储层未初始化
        """
        self._check_initialized()
        return self._session_repo  # type: ignore[return-value]

    def get_task_repo(self) -> TaskRepository:
        """获取 Task Repository（Agent 任务管理）。

        Returns:
            TaskRepository 实例，用于操作 task 表

        Raises:
            RuntimeError: 如果存储层未初始化
        """
        self._check_initialized()
        return self._task_repo  # type: ignore[return-value]

    def get_device_repo(self) -> DeviceRepository:
        """获取 Device Repository（设备信息管理）。

        Returns:
            DeviceRepository 实例，用于操作 device 表

        Raises:
            RuntimeError: 如果存储层未初始化
        """
        self._check_initialized()
        return self._device_repo  # type: ignore[return-value]

    def get_approval_repo(self) -> ApprovalRepository:
        """获取 Approval Repository（人工审批流程管理）。

        Returns:
            ApprovalRepository 实例，用于操作 approval 表

        Raises:
            RuntimeError: 如果存储层未初始化
        """
        self._check_initialized()
        return self._approval_repo  # type: ignore[return-value]

    def get_avatar_config_repo(self) -> AvatarConfigRepository:
        """获取 AvatarConfig Repository（Agent 角色配置管理）。

        Returns:
            AvatarConfigRepository 实例，用于操作 avatar_config 表

        Raises:
            RuntimeError: 如果存储层未初始化
        """
        self._check_initialized()
        return self._avatar_config_repo  # type: ignore[return-value]

    def get_run_state_repo(self) -> RunStateRepository:
        """获取 RunState Repository（运行状态管理）。

        Returns:
            RunStateRepository 实例，用于操作 run_state 表

        Raises:
            RuntimeError: 如果存储层未初始化
        """
        self._check_initialized()
        return self._run_state_repo  # type: ignore[return-value]

    def get_agent_task_repo(self) -> AgentTaskRepository | MarkdownAgentTaskRepository:
        """获取 AgentTask Repository（AI 自驱任务管理）。

        Returns:
            AgentTaskRepository 或 MarkdownAgentTaskRepository 实例

        Raises:
            RuntimeError: 如果存储层未初始化
        """
        self._check_initialized()
        return self._agent_task_repo  # type: ignore[return-value]

    def get_session_group_repo(
        self,
    ) -> SessionGroupRepository | MarkdownSessionGroupRepository | None:
        """获取 SessionGroup Repository（UI 会话分组）。

        SQLite / Markdown 两种模式均返回具体实例，接口一致。
        （未初始化或异常路径下可能为 None。）

        Returns:
            SessionGroupRepository / MarkdownSessionGroupRepository 实例
        """
        self._check_initialized()
        return self._session_group_repo

    # ──────────────────────────────────────────────
    # Memory Backend Accessors（SDK Memory Backend 工厂方法）
    # ──────────────────────────────────────────────
    # 这些方法为 pandaren SDK 的 Memory 模块提供 Backend 实现。
    # 根据 storage_mode 自动选择 SQLite 或 Markdown 实现。
    #
    # ★ 数据隔离改造后：user_id 由 StorageManager 构造时绑定（埋进 storage_path），
    #   Backend 内部不再感知 user_id。方法签名保留 user_id 参数是为了向后兼容，
    #   如果传入的 user_id 与构造时的不一致，会记录 warning 但不 fail。
    def get_raw_log_backend(self, user_id: str = "") -> SQLiteRawLogBackend | MarkdownRawLogBackend:
        """获取 RawLog Backend（原始对话日志存储）。

        根据当前 storage_mode 自动选择实现：
        - "sqlite": 返回 SQLiteRawLogBackend
        - "markdown": 返回 MarkdownRawLogBackend

        Args:
            user_id: 兼容参数。数据隔离改造后，实际使用的 user_id 由 StorageManager
                    构造时绑定。传入值若与构造 user_id 不一致会记 warning。

        Returns:
            RawLogBackend 实现（满足 pandaren SDK 的 RawLogBackend Protocol）

        Raises:
            RuntimeError: 如果存储层未初始化
        """
        self._check_initialized()
        if user_id and self._user_id and user_id != self._user_id:
            logger.warning(
                "get_raw_log_backend: user_id 参数 (%r) 与 StorageManager 构造 user_id (%r) 不一致；"
                "以构造 user_id 为准（数据隔离改造后 user_id 已埋进 storage_path）",
                user_id, self._user_id,
            )

        # 会话历史回补上限：环境变量 PANDAPAL_RAW_LOG_MAX_ROWS 覆盖，默认 5000（backend 内部默认）。
        # 非法值 → None（回落 backend 默认），不因拼错而停机。
        max_load_rows: int | None = None
        try:
            _env = os.getenv("PANDAPAL_RAW_LOG_MAX_ROWS")
            if _env:
                max_load_rows = max(1, int(_env))
        except (TypeError, ValueError):
            max_load_rows = None

        if self._storage_mode == "markdown":
            # base_dir 已经是 {data_dir}/users/{uid}，backend 内部按 sessions/{sid}/raw_log.md
            return MarkdownRawLogBackend(
                self._storage_path, max_load_messages=max_load_rows,
            )
        else:
            # SQLite 每 user 一个 db 文件，backend 内部仍需 user_id 用于 schema 兼容（Phase D 再改）
            return SQLiteRawLogBackend(
                self._storage_path, self._user_id or user_id,
                max_load_rows=max_load_rows,
            )

    # ⚠️ v1.4 废弃：get_summary_backend() 已移除
    # SummaryBackend 已从 SDK 删除（去 summary 化）。
    # 跨 session 召回 / 知识抽取已迁到应用层定时任务，
    # 应用层应通过 RawLogBackend.load_all() 读取原始对话日志后自行提炼。

    def get_working_memory_backend(self, user_id: str = "") -> SQLiteWorkingMemoryBackend | MarkdownWorkingMemoryBackend:
        """获取 WorkingMemory Backend（工作记忆持久化）。

        根据当前 storage_mode 自动选择实现：
        - SQLite 模式：返回 SQLiteWorkingMemoryBackend
        - Markdown 模式：返回 MarkdownWorkingMemoryBackend

        每次调用创建新实例。

        Args:
            user_id: 兼容参数（同 get_raw_log_backend）。

        Returns:
            WorkingMemoryBackend 实现

        Raises:
            RuntimeError: 如果存储层未初始化
        """
        self._check_initialized()
        if user_id and self._user_id and user_id != self._user_id:
            logger.warning(
                "get_working_memory_backend: user_id 参数 (%r) 与 StorageManager 构造 user_id (%r) 不一致",
                user_id, self._user_id,
            )
        if self._storage_mode == "markdown":
            return MarkdownWorkingMemoryBackend(self._storage_path)
        else:
            return SQLiteWorkingMemoryBackend(
                user_id=self._user_id or user_id, db_path=self._storage_path,
            )

    # ──────────────────────────────────────────────
    # Internal Helpers（内部辅助方法）
    # ──────────────────────────────────────────────
    # 这些方法都是私有的（以 _ 开头），只在类内部使用
    # 目的：代码复用、集中错误处理、保证一致性

    def _check_initialized(self) -> None:
        """检查存储层是否已初始化（Fail Fast 原则）。

        设计目的：
        - 防止在存储层未初始化时就调用 get_* 方法
        - 及早发现问题，抛出清晰的错误信息
        - 避免更难调试的 None 指针异常

        Raises:
            RuntimeError: 如果 initialize_storage() 尚未调用
        """
        if not self._initialized:
            raise RuntimeError(
                "StorageManager not initialized. "
                "Call initialize_storage() before using get_*_repo() methods."
            )

    def _ensure_db_directory(self) -> None:
        """确保数据库文件所在目录存在且可写。

        检查步骤：
        1. 创建目录（如果不存在）
        2. 检查目录是否可写

        错误处理：
        - 目录创建失败：抛出 StorageInitError（权限不足、路径非法等）
        - 目录不可写：抛出 StorageInitError（权限问题）

        Raises:
            StorageInitError: 目录创建失败或不可写
        """
        # 获取数据库文件的父目录
        # 例如：/data/db/pandapal.db -> /data/db
        db_dir = Path(self._storage_path).parent
        
        # 步骤 1: 创建目录（包括必要的父目录）
        # parents=True: 创建所有必要的父目录
        # exist_ok=True: 目录已存在时不报错
        try:
            db_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # 捕获操作系统错误（权限不足、路径非法等）
            raise StorageInitError(
                f"Cannot create database directory: {e}",
                str(db_dir),
            ) from e

        # 步骤 2: 检查目录是否可写
        # os.access(path, os.W_OK): 检查写权限
        if not os.access(str(db_dir), os.W_OK):
            raise StorageInitError(
                "Database directory is not writable",
                str(db_dir),
            )

    async def _safe_close_connection(self) -> None:
        """安全关闭数据库连接（异常处理 + 资源清理）。

        安全策略：
        - 检查连接是否存在（避免重复关闭）
        - 捕获关闭时的异常（不中断清理流程）
        - 使用 finally 确保连接引用被清空

        设计考虑：
        - 关闭失败不应该阻止其他清理操作
        - 记录警告日志便于调试
        - 清空连接引用防止悬空引用
        """
        # 检查连接是否存在（避免重复关闭或关闭未初始化的连接）
        if self._connection is not None:
            try:
                # 异步关闭数据库连接
                await self._connection.close()
            except Exception as e:
                # 记录警告但不抛出异常（确保清理流程继续）
                logger.warning("Error closing database connection: %s", e)
            finally:
                # 无论如何都要清空连接引用（防止悬空引用）
                self._connection = None
