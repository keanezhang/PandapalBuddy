"""SQLite SummaryBackend — 实现 pandaren SDK 的 SummaryBackend Protocol。

注意：SDK Protocol 方法全部是同步的，因此使用原生 sqlite3。
user_id 在构造时绑定，不出现在 Protocol 签名中。

依赖的 SDK 类型：
- pandaren.memory.models.EntryMetadata
- pandaren.memory.models.SearchResult
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pandaren.memory.models import EntryMetadata, SearchResult


class SQLiteSummaryBackend:
    """pandaren SDK SummaryBackend 的 SQLite 实现。

    同步方法，独立 sqlite3 连接（WAL 模式保证与 aiosqlite 并发安全）。
    搜索使用 LIKE 关键词匹配（未来可升级为 FTS5）。
    """

    def __init__(self, db_path: str, user_id: str) -> None:
        self._db_path = db_path
        self._user_id = user_id
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        """关闭连接。"""
        self._conn.close()

    def store(
        self, content: str, metadata: "EntryMetadata", session_id: str
    ) -> str:
        """存储一条摘要，返回 entry_id。"""
        entry_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        metadata_json = json.dumps(metadata, ensure_ascii=False)

        self._conn.execute(
            "INSERT INTO session_summaries "
            "(id, session_id, user_id, summary_text, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (entry_id, session_id, self._user_id, content, metadata_json, now),
        )
        self._conn.commit()
        return entry_id

    def search(
        self, query: str, top_k: int, session_id: str
    ) -> list["SearchResult"]:
        """关键词搜索，返回最相关的 top_k 条结果。

        当前实现使用 LIKE 匹配。未来可升级为 FTS5 全文搜索。
        Fix #2: 对 query 中的 LIKE 通配符 % 和 _ 进行转义，防止意外匹配。
        """
        # 转义 LIKE 通配符
        escaped_query = (
            query.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        # 按关键词匹配 + 时间排序
        cursor = self._conn.execute(
            "SELECT id, session_id, summary_text, metadata_json, created_at "
            "FROM session_summaries "
            "WHERE user_id = ? AND session_id = ? AND summary_text LIKE ? ESCAPE '\\' "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (self._user_id, session_id, f"%{escaped_query}%", top_k),
        )

        results: list["SearchResult"] = []
        for row in cursor:
            metadata = json.loads(row[3]) if row[3] else {}
            results.append({
                "entry_id": row[0],
                "content": row[2],
                "metadata": metadata,
                "score": 1.0,  # LIKE 匹配无实际分数
            })
        return results

    def get_recent(
        self, top_k: int, session_id: str
    ) -> list["SearchResult"]:
        """按时间倒序返回最新的 top_k 条摘要。"""
        cursor = self._conn.execute(
            "SELECT id, session_id, summary_text, metadata_json, created_at "
            "FROM session_summaries "
            "WHERE user_id = ? AND session_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (self._user_id, session_id, top_k),
        )

        results: list["SearchResult"] = []
        for row in cursor:
            metadata = json.loads(row[3]) if row[3] else {}
            results.append({
                "entry_id": row[0],
                "content": row[2],
                "metadata": metadata,
            })
        return results

    def delete(self, entry_id: str, session_id: str) -> None:
        """删除指定 entry_id 的摘要条目（幂等）。"""
        self._conn.execute(
            "DELETE FROM session_summaries WHERE id = ? AND user_id = ?",
            (entry_id, self._user_id),
        )
        self._conn.commit()
