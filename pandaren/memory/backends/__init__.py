"""pandaren/memory/backends — 内置后端实现集合

公共导出：
  - SQLiteRawLogBackend          (推荐：stdlib sqlite3，事务原子写)

应用层若需 WorkingMemory 持久化（SQLite）、Markdown 文本日志、PostgreSQL 等其他后端，
可自行实现对应 Protocol 注入；SDK 不内置非 RawLog 的实现。
"""
from .sqlite_raw_log import SQLiteRawLogBackend

__all__ = [
    "SQLiteRawLogBackend",
]
