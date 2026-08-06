"""Schema 迁移管理器。

负责：
- 发现 migrations/ 目录下的 SQL 迁移脚本
- 追踪当前 schema_version
- 按版本号顺序执行未应用的迁移
- 迁移失败时回滚事务并抛出 SchemaMigrationError
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from pandapal.storage.exceptions import SchemaMigrationError

logger = logging.getLogger(__name__)

# 迁移文件命名规则：v001_description.sql
_MIGRATION_PATTERN = re.compile(r"^v(\d{3})_.+\.sql$")

# migrations/ 目录位置（相对于本文件）
_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class SchemaManager:
    """管理 SQLite Schema 版本迁移。

    设计约束：
    - 迁移脚本按 v001, v002, ... 顺序执行
    - 每个迁移在显式事务中执行（Fix #1: 不再使用 executescript）
    - 失败时回滚当前迁移，不继续后续迁移
    - schema_version 表追踪已执行的最高版本
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def ensure_schema_version_table(self) -> None:
        """确保 schema_version 表存在（幂等）。"""
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL DEFAULT 0,
                applied_at TEXT NOT NULL
            )
            """
        )
        await self._conn.commit()

    async def get_current_version(self) -> int:
        """获取当前 schema 版本号。返回 0 表示尚未执行任何迁移。"""
        cursor = await self._conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def run_migrations(self) -> int:
        """执行所有未应用的迁移脚本。

        Returns:
            执行的迁移数量。

        Raises:
            SchemaMigrationError: 迁移执行失败。
        """
        await self.ensure_schema_version_table()
        current_version = await self.get_current_version()

        # 发现并排序迁移文件
        migrations = self._discover_migrations()

        # 护栏：全新库（version 0）却发现不到任何迁移脚本 = 建表脚本没被部署
        # （典型：PyInstaller 打包漏带 migrations/*.sql）。若放行，存储会"初始化成功"
        # 但一张业务表都没有，直到运行时才爆 no such table——最隐蔽的失效模式。
        # 此处显式 Fail-Fast，把打包/部署缺陷暴露在启动期而非运行期。
        if current_version == 0 and not migrations:
            raise SchemaMigrationError(
                0,
                f"未发现任何迁移脚本（{_MIGRATIONS_DIR}）。SQLite 存储无法建表。"
                "通常是打包/部署漏带 pandapal/storage/migrations/*.sql —— "
                "请确认它们随 sidecar 一并分发。",
            )

        pending = [
            (ver, path) for ver, path in migrations if ver > current_version
        ]

        if not pending:
            logger.debug(
                "Schema is up to date (version=%d)", current_version
            )
            return 0

        executed_count = 0
        for version, path in pending:
            await self._apply_migration(version, path)
            executed_count += 1

        logger.info(
            "Schema migrated from v%03d to v%03d (%d migrations applied)",
            current_version,
            current_version + executed_count,
            executed_count,
        )
        return executed_count

    def _discover_migrations(self) -> list[tuple[int, Path]]:
        """发现 migrations/ 目录下的所有迁移文件，按版本号排序。"""
        if not _MIGRATIONS_DIR.exists():
            return []

        migrations: list[tuple[int, Path]] = []
        for file in _MIGRATIONS_DIR.iterdir():
            match = _MIGRATION_PATTERN.match(file.name)
            if match:
                version = int(match.group(1))
                migrations.append((version, file))

        migrations.sort(key=lambda x: x[0])
        return migrations

    async def _apply_migration(self, version: int, path: Path) -> None:
        """执行单个迁移脚本（显式事务保护）。

        Fix #1: 替换 executescript 为逐条 execute，确保事务回滚有效。
        executescript 会隐式 COMMIT 当前事务，导致回滚无效。
        改为显式 BEGIN + 逐条执行 + COMMIT，失败时可真正回滚。
        """
        logger.debug("Applying migration v%03d: %s", version, path.name)

        try:
            sql = path.read_text(encoding="utf-8")
        except OSError as e:
            raise SchemaMigrationError(
                version, f"Cannot read migration file: {e}"
            ) from e

        # 按分号拆分 SQL 语句（过滤空语句和纯注释）
        statements = self._split_sql_statements(sql)

        try:
            # 显式开启事务
            await self._conn.execute("BEGIN")

            # 逐条执行 SQL 语句
            for stmt in statements:
                await self._conn.execute(stmt)

            # 更新 schema_version
            now = datetime.now(timezone.utc).isoformat()
            await self._conn.execute(
                """
                INSERT INTO schema_version (id, version, applied_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET version = ?, applied_at = ?
                """,
                (version, now, version, now),
            )

            await self._conn.commit()
            logger.debug("Migration v%03d applied successfully", version)

        except Exception as e:
            # 回滚失败的迁移（现在回滚是有效的，因为没有隐式 COMMIT）
            try:
                await self._conn.rollback()
                rollback_success = True
            except Exception:
                rollback_success = False

            raise SchemaMigrationError(
                version,
                f"SQL execution failed: {e}",
                rollback_success=rollback_success,
            ) from e

    @staticmethod
    def _split_sql_statements(sql: str) -> list[str]:
        """将多语句 SQL 文本按分号拆分为独立语句列表。

        过滤空语句和纯注释行。
        """
        statements: list[str] = []
        for raw_stmt in sql.split(";"):
            # 去除首尾空白和纯注释行
            lines = []
            for line in raw_stmt.strip().splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("--"):
                    lines.append(line)
            stmt = "\n".join(lines).strip()
            if stmt:
                statements.append(stmt)
        return statements
