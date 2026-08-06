"""pandaren/memory/backends/sqlite_raw_log.py — SQLite 原始日志后端

内置 RawLogBackend Protocol 实现，零外部依赖（stdlib ``sqlite3``）。

设计要点：
  - **session_id 不在构造参数里**——它是运行时身份，每次 append/load 调用时传入
  - ``db_path`` 与 ``connection`` **严格互斥**：必须二选一
  - 拒绝 ``db_path=":memory:"``——内存 SQLite 没有持久化价值，违背 raw_log 用途；
    单测应使用 ``pytest`` ``tmp_path`` fixture 落临时文件
  - 默认启用 WAL 模式，写性能更好；传 ``connection`` 时尊重外部 PRAGMA
  - 所有写操作走单条事务，保证原子性
  - 读操作返回 list[MessageDict]，按时间从旧到新排列
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models import MessageDict, CompactBoundaryDict
from ..protocols import TokenEstimator, CharBasedTokenEstimator

logger = logging.getLogger("pandaren.memory.backends.sqlite_raw_log")


_SCHEMA_RAW_MESSAGES = """
CREATE TABLE IF NOT EXISTS raw_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT    NOT NULL,
    seq          INTEGER NOT NULL,
    role         TEXT    NOT NULL,
    content      TEXT,
    tool_calls   TEXT,
    tool_call_id TEXT,
    ts           TEXT    NOT NULL,
    run_id       TEXT,
    step         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_raw_session_seq ON raw_messages(session_id, seq);
"""

_SCHEMA_COMPACT_BOUNDARIES = """
CREATE TABLE IF NOT EXISTS compact_boundaries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    seq         INTEGER NOT NULL,
    ts          TEXT    NOT NULL,
    metadata    TEXT
);
CREATE INDEX IF NOT EXISTS idx_boundary_session_seq ON compact_boundaries(session_id, seq);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_message(row: sqlite3.Row) -> MessageDict:
    """将 raw_messages 表的一行转回 MessageDict。"""
    msg: MessageDict = {
        "role": row["role"],
        "content": row["content"] if row["content"] is not None else "",
    }
    tc_str = row["tool_calls"]
    if tc_str:
        try:
            msg["tool_calls"] = json.loads(tc_str)
        except (TypeError, ValueError):
            # 历史数据兼容；存原文
            msg["tool_calls"] = []  # type: ignore[typeddict-item]
    tc_id = row["tool_call_id"]
    if tc_id:
        msg["tool_call_id"] = tc_id
    return msg


class SQLiteRawLogBackend:
    """内置 SQLite 原始日志后端（实现 RawLogBackend Protocol）。

    Args:
        db_path:    数据库文件路径。与 connection 严格互斥。
                    禁止使用 ``":memory:"``——内存模式无持久化价值。
        connection: 已有的 ``sqlite3.Connection``（与应用层共享同一数据库）。
                    与 db_path 严格互斥。
        wal_mode:   是否启用 WAL 模式（默认 True，写性能更好）。
                    仅在传 db_path 时生效；传 connection 时尊重外部 PRAGMA。
        token_estimator: 用于 ``load_within_budget`` 的 token 估算器
                         （None = 默认 CharBasedTokenEstimator）。

    Raises:
        ValueError: db_path 与 connection 必须二选一（不能都传，也不能都不传）；
                    db_path == ":memory:" 时拒绝构造。
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        connection: sqlite3.Connection | None = None,
        wal_mode: bool = True,
        token_estimator: TokenEstimator | None = None,
    ) -> None:
        if (db_path is None) == (connection is None):
            raise ValueError(
                "SQLiteRawLogBackend: provide exactly one of db_path or connection "
                "(got both or neither)."
            )
        if db_path is not None and str(db_path) == ":memory:":
            raise ValueError(
                "SQLiteRawLogBackend: db_path=':memory:' is not supported. "
                "Use a real file path (use pytest tmp_path fixture for ephemeral storage)."
            )

        self._db_path: Path | None = Path(db_path) if db_path is not None else None
        self._owns_connection: bool = connection is None
        self._wal_mode: bool = wal_mode
        self._token_estimator: TokenEstimator = (
            token_estimator or CharBasedTokenEstimator()
        )

        if connection is not None:
            self._conn = connection
        else:
            assert self._db_path is not None
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self._db_path),
                # 允许多线程使用（FlushPolicy 可能在异步线程上下文调用）
                check_same_thread=False,
                isolation_level="DEFERRED",  # 显式事务
            )
            if wal_mode:
                try:
                    self._conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.Error as exc:
                    logger.warning(
                        "SQLiteRawLogBackend: failed to enable WAL mode: %s", exc,
                    )

        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # ─────────────────────────────────────────
    # 初始化
    # ─────────────────────────────────────────

    def _init_schema(self) -> None:
        """初始化 schema（IF NOT EXISTS，幂等）。"""
        with self._conn:  # 自动 commit
            self._conn.executescript(_SCHEMA_RAW_MESSAGES)
            self._conn.executescript(_SCHEMA_COMPACT_BOUNDARIES)

    def _next_seq(self, session_id: str) -> int:
        """计算指定 session 的下一个 seq（在 raw_messages 与 compact_boundaries 之间统一）。"""
        cur = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM raw_messages WHERE session_id = ?",
            (session_id,),
        )
        m1 = cur.fetchone()["m"]
        cur = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM compact_boundaries WHERE session_id = ?",
            (session_id,),
        )
        m2 = cur.fetchone()["m"]
        return max(m1, m2) + 1

    # ─────────────────────────────────────────
    # 写入（运行时）
    # ─────────────────────────────────────────

    def append_raw_message(
        self,
        message: MessageDict,
        session_id: str,
        run_id: str = "",
        step: int | None = None,
    ) -> None:
        """追加一条消息到原始日志。run_id/step 落独立列，供与 traces 按 (run_id, step) join。"""
        if not session_id:
            raise ValueError("SQLiteRawLogBackend.append_raw_message: session_id is required.")

        role = message.get("role", "")
        content = message.get("content", "")
        # content 可能是 list（多模态），统一序列化为 JSON 文本
        if isinstance(content, list):
            content_str = json.dumps(content, ensure_ascii=False)
        else:
            content_str = str(content) if content is not None else ""

        tool_calls = message.get("tool_calls")
        tc_str = json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
        tc_id = message.get("tool_call_id") or None

        with self._conn:
            seq = self._next_seq(session_id)
            self._conn.execute(
                "INSERT INTO raw_messages "
                "(session_id, seq, role, content, tool_calls, tool_call_id, ts, run_id, step) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, seq, role, content_str, tc_str, tc_id, _now_iso(), run_id or None, step),
            )

    def append_compact_boundary(
        self,
        boundary: CompactBoundaryDict,
        session_id: str,
    ) -> None:
        """追加一条压缩边界标记。"""
        if not session_id:
            raise ValueError(
                "SQLiteRawLogBackend.append_compact_boundary: session_id is required."
            )

        ts = boundary.get("timestamp") or _now_iso()
        # 把整个 boundary dict 作为 metadata 存（含 type / tokens_before / tokens_after / ...）
        metadata_str = json.dumps(dict(boundary), ensure_ascii=False)

        with self._conn:
            seq = self._next_seq(session_id)
            self._conn.execute(
                "INSERT INTO compact_boundaries "
                "(session_id, seq, ts, metadata) VALUES (?, ?, ?, ?)",
                (session_id, seq, ts, metadata_str),
            )

    # ─────────────────────────────────────────
    # 运行时恢复读
    # ─────────────────────────────────────────

    def load_within_budget(
        self,
        session_id: str,
        token_budget: int,
    ) -> list[MessageDict]:
        """从最新 compact_boundary 向前读取，直到 token_budget 用尽。

        语义：
          1. 找到指定 session 的最新 compact_boundary（如果有），从它之后的消息开始读
             （早于该 boundary 的消息已被压缩，对当前 STM 来说不再有意义）
          2. 按 seq 升序取消息，按 token 估算累加；超过 budget 立即停
          3. 返回的消息列表按时间从旧到新排列
        """
        if not session_id or token_budget <= 0:
            return []

        # 找最新 boundary 的 seq（如果有）
        cur = self._conn.execute(
            "SELECT MAX(seq) AS m FROM compact_boundaries WHERE session_id = ?",
            (session_id,),
        )
        row = cur.fetchone()
        boundary_seq = row["m"] if row and row["m"] is not None else 0

        # 取该 boundary 之后的所有消息（时间升序）
        cur = self._conn.execute(
            "SELECT role, content, tool_calls, tool_call_id "
            "FROM raw_messages "
            "WHERE session_id = ? AND seq > ? "
            "ORDER BY seq ASC",
            (session_id, boundary_seq),
        )
        rows = cur.fetchall()

        # 按 token budget 从尾向前选（保留最新部分），再翻回正序
        messages_in_order = [_row_to_message(r) for r in rows]

        accumulated = 0
        keep_count = 0
        for msg in reversed(messages_in_order):
            t = self._token_estimator.estimate([msg])
            if accumulated + t > token_budget and keep_count > 0:
                break
            accumulated += t
            keep_count += 1

        if keep_count == 0:
            return []
        return messages_in_order[-keep_count:]

    # ─────────────────────────────────────────
    # 离线分析读（应用层用）
    # ─────────────────────────────────────────

    def load_all(self, session_id: str) -> list[MessageDict]:
        """加载指定 session 的全部历史消息，按时间从旧到新排列。

        供应用层离线任务（User Model / Episodic Archive 提炼）使用。
        """
        if not session_id:
            return []
        cur = self._conn.execute(
            "SELECT role, content, tool_calls, tool_call_id "
            "FROM raw_messages "
            "WHERE session_id = ? "
            "ORDER BY seq ASC",
            (session_id,),
        )
        return [_row_to_message(r) for r in cur.fetchall()]

    def list_sessions(self) -> list[str]:
        """枚举所有已存在的 session_id。

        ``user_id`` 维度的过滤是应用层在 session_id 命名规则上的关注点
        （如 ``user-123:session-001``），SDK 不感知 user 概念。
        """
        cur = self._conn.execute(
            "SELECT DISTINCT session_id FROM raw_messages "
            "UNION "
            "SELECT DISTINCT session_id FROM compact_boundaries"
        )
        return [r["session_id"] for r in cur.fetchall() if r["session_id"]]

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
                    "SQLiteRawLogBackend.close: failed to close connection: %s", exc,
                )

    def __enter__(self) -> "SQLiteRawLogBackend":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
