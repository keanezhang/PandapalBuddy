"""SQLite RawLogBackend — 实现 pandaren SDK 的 RawLogBackend Protocol。

注意：SDK Protocol 方法全部是同步的，因此使用原生 sqlite3 而非 aiosqlite。
user_id 在构造时绑定，不出现在 Protocol 签名中。

依赖的 SDK 类型：
- pandaren.memory.models.MessageDict
- pandaren.memory.models.CompactBoundaryDict
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pandaren.memory.models import CompactBoundaryDict, MessageDict

# 防止极端情况 OOM 的安全上限（默认 5000，可由构造参数 / 环境变量覆盖）
_DEFAULT_MAX_LOAD_ROWS = 5000


class SQLiteRawLogBackend:
    """pandaren SDK RawLogBackend 的 SQLite 实现。

    同步方法，独立 sqlite3 连接（WAL 模式保证与 aiosqlite 并发安全）。
    """

    def __init__(
        self, db_path: str, user_id: str, max_load_rows: int | None = None,
    ) -> None:
        self._db_path = db_path
        self._user_id = user_id
        self._max_load_rows = max_load_rows or _DEFAULT_MAX_LOAD_ROWS
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        """关闭连接。"""
        self._conn.close()

    def append_raw_message(
        self, message: "MessageDict", session_id: str,
        run_id: str = "", step: int | None = None,
    ) -> None:
        """追加一条消息到原始日志。

        Fix #3: 使用显式事务包裹 SELECT MAX + INSERT，保证原子性。
        run_id/step 落独立列，供与 traces 按 (run_id, step) join；content_json 保持纯净。
        """
        now = datetime.now(timezone.utc).isoformat()
        content_json = json.dumps(message, ensure_ascii=False)

        # 显式事务保证 SELECT MAX + INSERT 原子性
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            # 计算 turn_index：当前 session 的最大 turn_index + 1
            cursor = self._conn.execute(
                "SELECT COALESCE(MAX(turn_index), -1) FROM raw_log "
                "WHERE session_id = ? AND user_id = ?",
                (session_id, self._user_id),
            )
            max_index = cursor.fetchone()[0]
            next_index = max_index + 1

            self._conn.execute(
                "INSERT INTO raw_log "
                "(user_id, session_id, entry_type, content_json, turn_index, created_at, run_id, step) "
                "VALUES (?, ?, 'message', ?, ?, ?, ?, ?)",
                (self._user_id, session_id, content_json, next_index, now, run_id or None, step),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def append_compact_boundary(
        self, boundary: "CompactBoundaryDict", session_id: str
    ) -> None:
        """追加一条压缩边界标记。

        Fix #3: 使用显式事务包裹 SELECT MAX + INSERT，保证原子性。
        """
        now = datetime.now(timezone.utc).isoformat()
        content_json = json.dumps(boundary, ensure_ascii=False)

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._conn.execute(
                "SELECT COALESCE(MAX(turn_index), -1) FROM raw_log "
                "WHERE session_id = ? AND user_id = ?",
                (session_id, self._user_id),
            )
            max_index = cursor.fetchone()[0]
            next_index = max_index + 1

            self._conn.execute(
                "INSERT INTO raw_log "
                "(user_id, session_id, entry_type, content_json, turn_index, created_at) "
                "VALUES (?, ?, 'compact_boundary', ?, ?, ?)",
                (self._user_id, session_id, content_json, next_index, now),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def load_within_budget(
        self, session_id: str, token_budget: int
    ) -> list["MessageDict"]:
        """从最新 compact_boundary 向前读取，直到 token_budget 用尽。

        返回的消息列表按时间从旧到新排列。

        Fix #8: 添加 LIMIT 防止极端长会话 OOM。
        Token 估算：每个字符约 1.5 token（中文场景近似值，
        纯英文场景会高估；未来可注入 TokenEstimator Protocol 做精确计数）。
        """
        # 找到最新的 compact_boundary 位置
        cursor = self._conn.execute(
            "SELECT turn_index FROM raw_log "
            "WHERE session_id = ? AND user_id = ? AND entry_type = 'compact_boundary' "
            "ORDER BY turn_index DESC LIMIT 1",
            (session_id, self._user_id),
        )
        boundary_row = cursor.fetchone()
        boundary_index = boundary_row[0] if boundary_row else -1

        # 从 boundary 之后读取 message 条目（带安全 LIMIT）
        cursor = self._conn.execute(
            "SELECT content_json FROM raw_log "
            "WHERE session_id = ? AND user_id = ? "
            "AND entry_type = 'message' AND turn_index > ? "
            "ORDER BY turn_index ASC LIMIT ?",
            (session_id, self._user_id, boundary_index, self._max_load_rows),
        )

        messages: list["MessageDict"] = []
        token_used = 0

        for row in cursor:
            content_json = row[0]
            msg: "MessageDict" = json.loads(content_json)

            # 简化 token 估算（中文场景近似 1 字符 ≈ 1.5 token）
            content = msg.get("content", "")
            if isinstance(content, str):
                estimated_tokens = int(len(content) * 1.5)
            else:
                estimated_tokens = int(len(json.dumps(content)) * 1.5)

            if token_used + estimated_tokens > token_budget and messages:
                # 已超预算且已有消息，停止
                break

            messages.append(msg)
            token_used += estimated_tokens

        return messages

    def delete_turns(self, session_id: str) -> None:
        """删除指定 session 的所有日志条目。"""
        self._conn.execute(
            "DELETE FROM raw_log WHERE session_id = ? AND user_id = ?",
            (session_id, self._user_id),
        )
        self._conn.commit()

    # ── v1.4 新增：离线分析数据源 ──

    def load_all(self, session_id: str) -> list["MessageDict"]:
        """加载指定 session 的最近 N 条历史消息（离线分析用）。

        返回 entry_type='message' 的条目，按 turn_index 升序排列（旧→新）。
        取「最新」N 条而非最早 N 条：历史回补消费方需要最新上下文，
        旧实现取最早 500 条会导致超长会话「最新对话丢失」。
        """
        cursor = self._conn.execute(
            "SELECT content_json FROM raw_log "
            "WHERE session_id = ? AND user_id = ? AND entry_type = 'message' "
            "ORDER BY turn_index DESC LIMIT ?",
            (session_id, self._user_id, self._max_load_rows),
        )

        messages: list["MessageDict"] = []
        for row in cursor:
            msg: "MessageDict" = json.loads(row[0])
            messages.append(msg)
        # DESC 取到的是新→旧，反转为旧→新后返回
        messages.reverse()

        return messages

    def search_messages(
        self, query: str, limit: int = 60
    ) -> list[tuple[str, str, str]]:
        """按关键词全文搜索消息（命令面板 ⌘K）。

        LIKE 匹配 content_json（含 role/tool 噪声），转义通配符防注入。
        返回原始行 (session_id, content_json, created_at)，按时间倒序；
        调用方需二次解析 content_json 提取正文并复核命中，去除误配。
        """
        q = query.strip()
        if not q:
            return []
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        cursor = self._conn.execute(
            "SELECT session_id, content_json, created_at FROM raw_log "
            "WHERE user_id = ? AND entry_type = 'message' "
            "AND content_json LIKE ? ESCAPE '\\' "
            "ORDER BY created_at DESC LIMIT ?",
            (self._user_id, pattern, limit),
        )
        return [(row[0], row[1], row[2]) for row in cursor]

    def list_sessions(self) -> list[str]:
        """枚举所有已存在的 session_id。"""
        cursor = self._conn.execute(
            "SELECT DISTINCT session_id FROM raw_log WHERE user_id = ? "
            "ORDER BY session_id",
            (self._user_id,),
        )
        return [row[0] for row in cursor]
