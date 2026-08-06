"""pandapal.storage — 数据持久化层（Storage / Repository）。

提供统一的 SQLite 持久化接口，管理 Schema 迁移，
为上层业务模块暴露业务语言的 Repository 操作。

公开导出：
- StorageManager: 统一入口
- 所有数据模型（frozen dataclass）
- 所有异常类
- 所有 Repository 类
- SDK Backend 实现类

v1.4 变更：SummaryBackend 已从 SDK 删除，不再导出 SQLiteSummaryBackend。
"""

from pandapal.storage.exceptions import (
    StorageDuplicateError,
    StorageInitError,
    SchemaMigrationError,
    StorageTimeoutError,
)
from pandapal.storage.manager import StorageManager
from pandapal.storage.models import (
    AgentTask,
    AgentTaskStatus,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    AvatarConfig,
    DeviceRegistration,
    Session,
    TaskDefinition,
    TaskExecution,
    TaskExecutionStatus,
)
from pandapal.storage.repositories.sqlite_agent_task_repo import AgentTaskRepository
from pandapal.storage.repositories.markdown_agent_task_repo import MarkdownAgentTaskRepository
from pandapal.storage.repositories.sqlite_approval_repo import ApprovalRepository
from pandapal.storage.repositories.sqlite_avatar_config_repo import AvatarConfigRepository
from pandapal.storage.repositories.sqlite_device_repo import DeviceRepository
from pandapal.storage.repositories.sqlite_raw_log_backend import SQLiteRawLogBackend
from pandapal.storage.repositories.sqlite_run_state_repo import RunStateRepository
from pandapal.storage.repositories.sqlite_working_memory_backend import SQLiteWorkingMemoryBackend
from pandapal.storage.repositories.markdown_working_memory_backend import MarkdownWorkingMemoryBackend
from pandapal.storage.repositories.sqlite_session_repo import SessionRepository
from pandapal.storage.repositories.sqlite_task_repo import TaskRepository
from pandapal.storage.schema_manager import SchemaManager

__all__ = [
    # Manager
    "StorageManager",
    "SchemaManager",
    # Models
    "AgentTask",
    "AgentTaskStatus",
    "ApprovalDecision",
    "ApprovalRequest",
    "ApprovalStatus",
    "AvatarConfig",
    "DeviceRegistration",
    "Session",
    "TaskDefinition",
    "TaskExecution",
    "TaskExecutionStatus",
    # Exceptions
    "StorageDuplicateError",
    "StorageInitError",
    "SchemaMigrationError",
    "StorageTimeoutError",
    # Repositories
    "AgentTaskRepository",
    "MarkdownAgentTaskRepository",
    "ApprovalRepository",
    "AvatarConfigRepository",
    "DeviceRepository",
    "RunStateRepository",
    "SessionRepository",
    "TaskRepository",
    # SDK Backends
    "SQLiteRawLogBackend",
    "SQLiteWorkingMemoryBackend",
    "MarkdownWorkingMemoryBackend",
]
