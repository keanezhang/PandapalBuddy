"""pandapal.storage.repositories.sqlite_working_memory_backend — SQLite 工作记忆持久化后端

内置 WorkingMemoryBackend Protocol 实现，零外部依赖（stdlib ``sqlite3``）。

设计要点：
  - **user_id 在构造时绑定**——数据按用户隔离，不出现在 Protocol 签名中
  - **session_id 不在构造参数里**——它是运行时身份，每次 save/load 调用时传入
  - ``db_path`` 与 ``connection`` **严格互斥**：必须二选一
  - 拒绝 ``db_path=":memory:"``——内存 SQLite 没有持久化价值
  - 默认启用 WAL 模式，写性能更好；传 ``connection`` 时尊重外部 PRAGMA
  - 所有写操作走单条事务，保证原子性
  - KV 值以 JSON 序列化存储，支持任意 Python 可序列化对象

注意：此实现位于 pandapal 层（应用层），SDK 核心默认使用纯内存 WorkingMemory，
SQLite 持久化是可选功能，通过 StorageManager.get_working_memory_backend(user_id) 获取。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger("pandapal.storage.repositories.sqlite_working_memory_backend")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS working_memory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL,
    session_id  TEXT    NOT NULL,
    key         TEXT    NOT NULL,
    value_json  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wm_user_session_key
    ON working_memory(user_id, session_id, key);
CREATE INDEX IF NOT EXISTS idx_wm_user_session
    ON working_memory(user_id, session_id);
"""


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


class SQLiteWorkingMemoryBackend:
    """SQLite 工作记忆持久化后端（实现 WorkingMemoryBackend Protocol）。

    每个 KV 条目以 (user_id, session_id, key) 为唯一索引，value 以 JSON 序列化存储。
    支持增量更新（save 单条）和批量覆盖（save_all），以及按 key / session 删除。

    user_id 在构造时绑定，不出现在 Protocol 签名中（与 SQLiteRawLogBackend 一致）。

    Args:
        user_id:    用户标识，用于数据隔离。
        db_path:    数据库文件路径。与 connection 严格互斥。
                    禁止使用 ``":memory:"``——内存模式无持久化价值。
        connection: 已有的 ``sqlite3.Connection``（与应用层共享同一数据库）。
                    与 db_path 严格互斥。
        wal_mode:   是否启用 WAL 模式（默认 True，写性能更好）。
                    仅在传 db_path 时生效；传 connection 时尊重外部 PRAGMA。

    Raises:
        ValueError: db_path 与 connection 必须二选一（不能都传，也不能都不传）；
                    db_path == ":memory:" 时拒绝构造。
    """

    def __init__(
        self,
        user_id: str,
        db_path: str | Path | None = None,
        *,
        connection: sqlite3.Connection | None = None,
        wal_mode: bool = True,
    ) -> None:
        if not user_id:
            raise ValueError(
                "SQLiteWorkingMemoryBackend: user_id is required and cannot be empty."
            )
        if (db_path is None) == (connection is None):
            raise ValueError(
                "SQLiteWorkingMemoryBackend: provide exactly one of db_path or connection "
                "(got both or neither)."
            )
        if db_path is not None and str(db_path) == ":memory:":
            raise ValueError(
                "SQLiteWorkingMemoryBackend: db_path=':memory:' is not supported. "
                "Use a real file path (use pytest tmp_path fixture for ephemeral storage)."
            )

        self._user_id: str = user_id
        self._db_path: Path | None = Path(db_path) if db_path is not None else None
        self._owns_connection: bool = connection is None
        self._wal_mode: bool = wal_mode

        if connection is not None:
            self._conn = connection
        else:
            assert self._db_path is not None
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
                isolation_level="DEFERRED",
            )
            if wal_mode:
                try:
                    self._conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.Error as exc:
                    logger.warning(
                        "SQLiteWorkingMemoryBackend: failed to enable WAL mode: %s", exc,
                    )

        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # ─────────────────────────────────────────
    # 初始化
    # ─────────────────────────────────────────

    def _init_schema(self) -> None:
        """初始化 schema（IF NOT EXISTS，幂等）。"""
        with self._conn:  # 自动 commit
            self._conn.executescript(_SCHEMA)

    # ─────────────────────────────────────────
    # WorkingMemoryBackend Protocol 方法
    # ─────────────────────────────────────────

    def save(self, key: str, value: Any, session_id: str) -> None:
        """持久化单个 KV 条目（增量更新：INSERT OR REPLACE）。"""
        if not session_id:
            raise ValueError("SQLiteWorkingMemoryBackend.save: session_id is required.")
        value_json = json.dumps(value, ensure_ascii=False)
        now = _now_iso()
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO working_memory (user_id, session_id, key, value_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (self._user_id, session_id, key, value_json, now),
            )

    def load(self, session_id: str) -> dict[str, Any]:
        """加载指定 session 的所有 KV 条目，返回 {key: value} 字典。"""
        if not session_id:
            return {}
        cur = self._conn.execute(
            "SELECT key, value_json FROM working_memory WHERE user_id = ? AND session_id = ?",
            (self._user_id, session_id),
        )
        result: dict[str, Any] = {}
        for row in cur.fetchall():
            try:
                result[row["key"]] = json.loads(row["value_json"])
            except (json.JSONDecodeError, TypeError, ValueError):
                logger.warning(
                    "SQLiteWorkingMemoryBackend.load: failed to decode value for key=%s, skipping",
                    row["key"],
                )
        return result

    def delete_key(self, key: str, session_id: str) -> None:
        """删除单个 KV 条目。"""
        if not session_id:
            return
        with self._conn:
            self._conn.execute(
                "DELETE FROM working_memory WHERE user_id = ? AND session_id = ? AND key = ?",
                (self._user_id, session_id, key),
            )

    def delete_session(self, session_id: str) -> None:
        """删除指定 session 的所有条目。"""
        if not session_id:
            return
        with self._conn:
            self._conn.execute(
                "DELETE FROM working_memory WHERE user_id = ? AND session_id = ?",
                (self._user_id, session_id),
            )

    def save_all(self, data: dict[str, Any], session_id: str) -> None:
        """一次性保存整个 WorkingMemory 快照（覆盖写入）。

        先删除该 session 的所有旧条目，再批量插入新条目。
        整个操作在单个事务中完成，保证原子性。
        """
        if not session_id:
            raise ValueError("SQLiteWorkingMemoryBackend.save_all: session_id is required.")
        now = _now_iso()
        with self._conn:
            self._conn.execute(
                "DELETE FROM working_memory WHERE user_id = ? AND session_id = ?",
                (self._user_id, session_id),
            )
            self._conn.executemany(
                "INSERT INTO working_memory (user_id, session_id, key, value_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (self._user_id, session_id, key, json.dumps(value, ensure_ascii=False), now)
                    for key, value in data.items()
                ],
            )

    # ─────────────────────────────────────────
    # 资源管理
    # ─────────────────────────────────────────

    def close(self) -> None:
        """关闭内部持有的连接。

        - 自构造的 connection（``db_path`` 模式）：会被关闭
        - 外部传入的 connection：**不**关闭（生命周期由调用方掌控）
        """
        if self._owns_connection:
            try:
                self._conn.close()
            except sqlite3.Error as exc:
                logger.warning(
                    "SQLiteWorkingMemoryBackend.close: failed to close connection: %s", exc,
                )

    def __enter__(self) -> "SQLiteWorkingMemoryBackend":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
