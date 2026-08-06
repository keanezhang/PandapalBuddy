"""pandapal.storage.repositories — Repository 实现集合。"""

from pandapal.storage.repositories.sqlite_approval_repo import ApprovalRepository
from pandapal.storage.repositories.sqlite_avatar_config_repo import AvatarConfigRepository
from pandapal.storage.repositories.sqlite_device_repo import DeviceRepository
from pandapal.storage.repositories.sqlite_raw_log_backend import SQLiteRawLogBackend
from pandapal.storage.repositories.sqlite_run_state_repo import RunStateRepository
from pandapal.storage.repositories.sqlite_session_repo import SessionRepository
from pandapal.storage.repositories.sqlite_task_repo import TaskRepository
from pandapal.storage.repositories.markdown_raw_log_backend import MarkdownRawLogBackend

# WorkingMemoryBackend 实现（可选持久化）
from pandapal.storage.repositories.sqlite_working_memory_backend import SQLiteWorkingMemoryBackend
from pandapal.storage.repositories.markdown_working_memory_backend import MarkdownWorkingMemoryBackend

# ⚠️ v1.4 废弃已清理：SummaryBackend 实现已删除
# - SQLiteSummaryBackend
# - MarkdownSummaryBackend


__all__ = [
    "ApprovalRepository",
    "AvatarConfigRepository",
    "DeviceRepository",
    "RunStateRepository",
    "SessionRepository",
    "SQLiteRawLogBackend",
    "SQLiteWorkingMemoryBackend",
    "MarkdownWorkingMemoryBackend",
    "TaskRepository",
    "MarkdownRawLogBackend",
]
