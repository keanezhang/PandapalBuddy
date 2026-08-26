"""pandaren/memory/tests/test_risk_fixes.py — 风险修复回归测试（设计文档 memory_risk_fixes.design.md）

覆盖 24 个用例：SQL-1..5（唯一索引防线）、LDB-1..7（load_within_budget 至少保留一条）、
RD-1..5 / RFS-1..3 / PSS-1..4（文件字节上限防护）。

层级：SQL/LDB = integration（真实 SQLite 文件，tmp_path 落盘）；
RD/RFS/PSS = integration（真实文件系统）；RFS-3 = unit（纯构造断言）。
"""

from __future__ import annotations

import builtins
import logging
import sqlite3

import pytest

from pandaren.memory.backends.sqlite_raw_log import SQLiteRawLogBackend
from pandaren.memory.constants import (
    DEFAULT_POST_COMPACT_MAX_BYTES_PER_FILE,
    RECENT_FILE_READS_WM_KEY,
)
from pandaren.memory.models import PostCompactContext
from pandaren.memory.reinject.sources import (
    PlanStateSource,
    RecentFilesSource,
    _read_file_with_size_limit,
)

SOURCES_LOGGER = "pandaren.memory.reinject.sources"


class FakeWM:
    """内存 dict 实现的 WorkingMemoryAccessor（满足 get/set 协议）。"""

    def __init__(self) -> None:
        self._data: dict[str, object] = {}

    def get(self, key: str) -> object | None:
        return self._data.get(key)

    def set(self, key: str, value: object) -> None:
        self._data[key] = value


def _source_records(caplog: pytest.LogCaptureFixture, *, min_level: int):
    """按 logger name 前缀过滤 caplog 记录（设计文档日志断言基线）。"""
    return [
        r for r in caplog.records
        if r.name.startswith("pandaren.memory.reinject.sources")
        and r.levelno >= min_level
    ]


def _index_names(db_path: str) -> set[str]:
    """以裸连接读取库内全部索引名（用于脏库构造失败后的残留检查）。"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        return {r[0] for r in rows.fetchall()}
    finally:
        conn.close()


# 与源码 _SCHEMA_RAW_MESSAGES / _SCHEMA_COMPACT_BOUNDARIES 同构，但**去掉** UNIQUE INDEX 行，
# 以便手工插入重复 (session_id, seq) 构造历史脏库。
_DIRTY_RAW_MESSAGES_DDL = """
CREATE TABLE IF NOT EXISTS raw_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT    NOT NULL,
    seq          INTEGER NOT NULL,
    role         TEXT    NOT NULL,
    content      TEXT,
    tool_calls   TEXT,
    tool_call_id TEXT,
    ts           TEXT    NOT NULL,
    reasoning_content TEXT,
    run_id       TEXT,
    step         INTEGER
);
"""

_DIRTY_COMPACT_BOUNDARIES_DDL = """
CREATE TABLE IF NOT EXISTS compact_boundaries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    seq         INTEGER NOT NULL,
    ts          TEXT    NOT NULL,
    metadata    TEXT
);
"""


# ─────────────────────────────────────────────
# SQL 组：唯一索引防线（sqlite_raw_log.py）
# ─────────────────────────────────────────────

# SQL-1 inv-S1 + Risk-S3
def test_sql1_new_db_creates_both_unique_indexes_and_reinit_idempotent(tmp_path):
    db = tmp_path / "raw.db"

    with SQLiteRawLogBackend(db_path=db) as backend:
        names = {
            r["name"]
            for r in backend._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert "uq_raw_session_seq" in names
        assert "uq_boundary_session_seq" in names

    # 幂等：重复构造成功（IF NOT EXISTS 不冲突）——建索引失败未被静默吞掉（Risk-S3）
    with SQLiteRawLogBackend(db_path=db):
        pass


# SQL-2 inv-S4（索引不误伤正常顺序 append）
def test_sql2_consecutive_appends_get_incrementing_seq(tmp_path):
    with SQLiteRawLogBackend(db_path=tmp_path / "raw.db") as backend:
        for content in ("m1", "m2", "m3"):
            backend.append_raw_message(
                {"role": "user", "content": content}, session_id="s"
            )

        loaded = backend.load_all("s")
        assert [m["content"] for m in loaded] == ["m1", "m2", "m3"]

        seqs = [
            r["seq"]
            for r in backend._conn.execute(
                "SELECT seq FROM raw_messages WHERE session_id='s' ORDER BY seq"
            )
        ]
        assert seqs == [1, 2, 3]


# SQL-3 inv-S3 + Risk-S1（唯一例外：monkeypatch _next_seq 等价模拟并发竞态结果）
def test_sql3_duplicate_seq_append_raises_integrity_error(tmp_path, monkeypatch):
    with SQLiteRawLogBackend(db_path=tmp_path / "raw.db") as backend:
        monkeypatch.setattr(backend, "_next_seq", lambda session_id: 1)

        backend.append_raw_message({"role": "user", "content": "a"}, session_id="s")

        with pytest.raises(sqlite3.IntegrityError):
            backend.append_raw_message(
                {"role": "user", "content": "b"}, session_id="s"
            )

        # 重复行未被写入，无脏数据残留
        row = backend._conn.execute(
            "SELECT COUNT(*) AS n FROM raw_messages WHERE session_id='s'"
        ).fetchone()
        assert row["n"] == 1


# SQL-4 inv-S4 + Risk-S2 + Risk-S3（脏库 raw_messages）
def test_sql4_dirty_raw_messages_raises_runtime_error_with_guidance(tmp_path):
    db = tmp_path / "dirty.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_DIRTY_RAW_MESSAGES_DDL)
        for _ in range(2):
            conn.execute(
                "INSERT INTO raw_messages (session_id, seq, role, content, ts) "
                "VALUES ('s', 1, 'user', 'a', 't')"
            )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="exactly one writer") as excinfo:
        SQLiteRawLogBackend(db_path=db)

    # 设计文档允许同义引导词："concurrent" 与 "exactly one writer"（源码文案）
    assert "concurrent" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, sqlite3.IntegrityError)
    # 无部分索引残留
    assert "uq_raw_session_seq" not in _index_names(str(db))


# SQL-5 inv-S2 + Risk-S1 + inv-S4（脏库 compact_boundaries，与 SQL-4 对称）
def test_sql5_dirty_compact_boundaries_raises_runtime_error_with_guidance(tmp_path):
    db = tmp_path / "dirty2.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_DIRTY_COMPACT_BOUNDARIES_DDL)
        for _ in range(2):
            conn.execute(
                "INSERT INTO compact_boundaries (session_id, seq, ts, metadata) "
                "VALUES ('s', 1, 't', '{}')"
            )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="exactly one writer") as excinfo:
        SQLiteRawLogBackend(db_path=db)

    assert isinstance(excinfo.value.__cause__, sqlite3.IntegrityError)
    # uq_boundary_session_seq 与 uq_raw_session_seq 两条防线都在（inv-S2）
    assert "uq_boundary_session_seq" not in _index_names(str(db))


# ─────────────────────────────────────────────
# LDB 组：load_within_budget（sqlite_raw_log.py）
# Oracle 基线：CharBasedTokenEstimator 公式 max(1, int(chars/4))，
# 40 字符 = 10 tokens、200 字符 = 50 tokens（人工可推导）。
# ─────────────────────────────────────────────

# LDB-1 inv-L3 + Risk-L1（判定 (T, F)：首条超预算但 keep_count==0 不 break）
def test_ldb1_first_message_over_budget_still_returned(tmp_path):
    with SQLiteRawLogBackend(db_path=tmp_path / "raw.db") as backend:
        backend.append_raw_message(
            {"role": "user", "content": "x" * 200}, session_id="s"  # 50 tokens
        )

        result = backend.load_within_budget("s", token_budget=10)

        assert len(result) == 1
        assert result[0]["content"] == "x" * 200


# LDB-2 inv-L3 + Risk-L1 + inv-L1（预算装不下任何一条 → 保留最新一条而非空）
def test_ldb2_tiny_budget_keeps_only_latest(tmp_path):
    with SQLiteRawLogBackend(db_path=tmp_path / "raw.db") as backend:
        for c in ("a", "b", "c"):
            backend.append_raw_message(
                {"role": "user", "content": c * 40}, session_id="s"  # 各 10 tokens
            )

        result = backend.load_within_budget("s", token_budget=5)

        assert len(result) == 1
        assert result[0]["content"] == "c" * 40


# LDB-3 inv-L4 + inv-L1 + Risk-L2 + Risk-L3（从尾向前截断后翻回正序）
def test_ldb3_truncates_from_tail_and_returns_chronological(tmp_path):
    with SQLiteRawLogBackend(db_path=tmp_path / "raw.db") as backend:
        for c in ("a", "b", "c", "d"):
            backend.append_raw_message(
                {"role": "user", "content": c * 40}, session_id="s"
            )

        result = backend.load_within_budget("s", token_budget=25)

        # 10+10=20 ≤ 25 继续，+10=30 > 25 break → 保留 seq=4、3
        assert len(result) == 2
        assert [m["content"] for m in result] == ["c" * 40, "d" * 40]


# LDB-4 inv-L4 + Risk-L4（累计恰好等于预算不 break，`>` 而非 `>=`）
def test_ldb4_exact_budget_does_not_break(tmp_path):
    with SQLiteRawLogBackend(db_path=tmp_path / "raw.db") as backend:
        backend.append_raw_message(
            {"role": "user", "content": "a" * 40}, session_id="s"
        )
        backend.append_raw_message(
            {"role": "user", "content": "b" * 40}, session_id="s"
        )

        result = backend.load_within_budget("s", token_budget=20)

        assert [m["content"] for m in result] == ["a" * 40, "b" * 40]


# LDB-5 inv-L5（token_budget <= 0 → 空列表）
def test_ldb5_non_positive_budget_returns_empty(tmp_path):
    with SQLiteRawLogBackend(db_path=tmp_path / "raw.db") as backend:
        backend.append_raw_message(
            {"role": "user", "content": "a" * 40}, session_id="s"
        )

        assert backend.load_within_budget("s", token_budget=0) == []
        assert backend.load_within_budget("s", token_budget=-1) == []


# LDB-6 inv-L2 + Risk-L5（早于最新 boundary 的消息不回读）
def test_ldb6_messages_before_latest_boundary_not_reread(tmp_path):
    with SQLiteRawLogBackend(db_path=tmp_path / "raw.db") as backend:
        for c in ("a", "b", "c"):
            backend.append_raw_message(
                {"role": "user", "content": c * 40}, session_id="s"
            )
        backend.append_compact_boundary(
            {
                "type": "compact_boundary",
                "timestamp": "t",
                "tokens_before": 30,
                "tokens_after": 10,
                "kept_message_count": 3,
            },
            session_id="s",
        )
        backend.append_raw_message(
            {"role": "user", "content": "d" * 40}, session_id="s"
        )

        result = backend.load_within_budget("s", token_budget=1000)

        # boundary seq=4，只读 seq > 4 的消息
        assert len(result) == 1
        assert result[0]["content"] == "d" * 40


# LDB-7 inv-L5 + inv-L6（空 session → 空；同库同 budget 确定性）
def test_ldb7_empty_session_returns_empty_and_deterministic(tmp_path):
    with SQLiteRawLogBackend(db_path=tmp_path / "raw.db") as backend:
        backend.append_raw_message(
            {"role": "user", "content": "a" * 40}, session_id="s"
        )

        assert backend.load_within_budget("", token_budget=100) == []

        r1 = backend.load_within_budget("s", token_budget=30)
        r2 = backend.load_within_budget("s", token_budget=30)
        assert r1 == r2


# ─────────────────────────────────────────────
# RD 组：_read_file_with_size_limit helper（reinject/sources.py）
# ─────────────────────────────────────────────

# RD-1 inv-R3（正常文件返回全文）
def test_rd1_helper_reads_full_file(tmp_path):
    f = tmp_path / "small.txt"
    f.write_text("hello world, read me", encoding="utf-8")  # 20 字节

    text = _read_file_with_size_limit(str(f), max_bytes=1024, source_name="test_src")

    assert text == "hello world, read me"


# RD-2 inv-R3 + Risk-R3（恰好等于上限不误杀，无 warning）
def test_rd2_helper_exact_limit_still_reads(tmp_path, caplog):
    f = tmp_path / "exact.txt"
    f.write_text("a" * 64, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=SOURCES_LOGGER):
        text = _read_file_with_size_limit(str(f), max_bytes=64, source_name="test_src")

    assert text == "a" * 64
    assert _source_records(caplog, min_level=logging.WARNING) == []


# RD-3 inv-R1 + Risk-R1（超限 → None + warning，stat 后跳过不整读）
def test_rd3_helper_over_limit_returns_none_with_warning(tmp_path, caplog):
    f = tmp_path / "big.txt"
    f.write_text("y" * 100, encoding="utf-8")  # 100 > 64

    with caplog.at_level(logging.WARNING, logger=SOURCES_LOGGER):
        text = _read_file_with_size_limit(str(f), max_bytes=64, source_name="test_src")

    assert text is None
    warnings = _source_records(caplog, min_level=logging.WARNING)
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "exceeds limit" in msg and "100" in msg and "64" in msg and "big.txt" in msg


# RD-4 inv-R2 + Risk-R2（文件不存在 → None + info，E4 降级）
def test_rd4_helper_missing_file_returns_none_with_info(tmp_path, caplog):
    missing = tmp_path / "nope.txt"

    with caplog.at_level(logging.INFO, logger=SOURCES_LOGGER):
        text = _read_file_with_size_limit(
            str(missing), max_bytes=1024, source_name="test_src"
        )

    assert text is None
    infos = _source_records(caplog, min_level=logging.INFO)
    assert len(infos) == 1
    msg = infos[0].getMessage()
    assert "cannot stat" in msg and "nope.txt" in msg


# RD-5 inv-R2 + Risk-R2（stat 成功、read 失败 → None + info，不抛异常）
def test_rd5_helper_read_failure_returns_none_with_info(tmp_path, monkeypatch, caplog):
    f = tmp_path / "readable_stat.txt"
    f.write_text("x" * 16, encoding="utf-8")

    real_open = builtins.open

    def fake_open(*args, **kwargs):
        if args and str(args[0]) == str(f):
            raise OSError("simulated read failure")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)

    with caplog.at_level(logging.INFO, logger=SOURCES_LOGGER):
        text = _read_file_with_size_limit(str(f), max_bytes=1024, source_name="test_src")

    assert text is None
    infos = _source_records(caplog, min_level=logging.INFO)
    assert len(infos) == 1
    msg = infos[0].getMessage()
    assert "failed to read" in msg and "readable_stat.txt" in msg


# ─────────────────────────────────────────────
# RFS 组：RecentFilesSource 字节上限（reinject/sources.py）
# ─────────────────────────────────────────────

# RFS-1 inv-R4 + Risk-R4（混用：超限跳过，正常文件照常回注）
def test_rfs1_mixed_files_skip_oversized_but_reinject_normal(tmp_path, caplog):
    small = tmp_path / "small.txt"
    small.write_text("hello world", encoding="utf-8")  # 11 字节
    big = tmp_path / "big.txt"
    big.write_text("y" * 100, encoding="utf-8")  # 100 > 64

    fake_wm = FakeWM()
    fake_wm.set(
        RECENT_FILE_READS_WM_KEY,
        [
            {"path": str(small), "timestamp": 2.0},
            {"path": str(big), "timestamp": 1.0},
        ],
    )
    source = RecentFilesSource(
        max_files=5,
        max_tokens_per_file=1000,
        total_token_budget=10000,
        max_bytes_per_file=64,
    )
    ctx = PostCompactContext(
        session_id="s",
        run_id="r",
        working_memory=fake_wm,
        skill_registry=None,
        session_meta={},
    )

    with caplog.at_level(logging.INFO, logger=SOURCES_LOGGER):
        attachments = source.collect(ctx)

    assert len(attachments) == 1
    assert attachments[0]["source_name"] == "recent_files"
    assert "small.txt" in attachments[0]["title"]
    assert attachments[0]["content"] == "hello world"

    warnings = _source_records(caplog, min_level=logging.WARNING)
    assert any(
        "big.txt" in r.getMessage() and "exceeds limit" in r.getMessage()
        for r in warnings
    )
    infos = _source_records(caplog, min_level=logging.INFO)
    assert any("reinjected 1 file(s)" in r.getMessage() for r in infos)


# RFS-2 inv-R4 + Risk-R1（全超限 → 空列表，E4 不崩溃）
def test_rfs2_all_files_oversized_returns_empty(tmp_path, caplog):
    f1 = tmp_path / "f1.txt"
    f1.write_text("x" * 100, encoding="utf-8")
    f2 = tmp_path / "f2.txt"
    f2.write_text("z" * 100, encoding="utf-8")

    fake_wm = FakeWM()
    fake_wm.set(
        RECENT_FILE_READS_WM_KEY,
        [
            {"path": str(f1), "timestamp": 2.0},
            {"path": str(f2), "timestamp": 1.0},
        ],
    )
    source = RecentFilesSource(
        max_files=5,
        max_tokens_per_file=1000,
        total_token_budget=10000,
        max_bytes_per_file=64,
    )
    ctx = PostCompactContext(
        session_id="s",
        run_id="r",
        working_memory=fake_wm,
        skill_registry=None,
        session_meta={},
    )

    with caplog.at_level(logging.INFO, logger=SOURCES_LOGGER):
        attachments = source.collect(ctx)

    assert attachments == []
    infos = _source_records(caplog, min_level=logging.INFO)
    assert not any("reinjected" in r.getMessage() for r in infos)


# RFS-3 inv-R6 + Risk-R5（构造默认 max_bytes = 1 MiB 回归）
def test_rfs3_default_max_bytes_is_1mib():
    rfs = RecentFilesSource()
    pss = PlanStateSource()

    assert rfs._max_bytes_per_file == 1_048_576 == DEFAULT_POST_COMPACT_MAX_BYTES_PER_FILE
    assert pss._max_bytes == 1_048_576 == DEFAULT_POST_COMPACT_MAX_BYTES_PER_FILE


# ─────────────────────────────────────────────
# PSS 组：PlanStateSource 字节上限（reinject/sources.py）
# ─────────────────────────────────────────────

# PSS-1 inv-R5（正常 plan 文件 → 单附件，content 未截断）
def test_pss1_normal_plan_file_reinjects_single_attachment(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("plan body text", encoding="utf-8")  # 14 字符

    ctx = PostCompactContext(
        session_id="s",
        run_id="r",
        working_memory=FakeWM(),
        skill_registry=None,
        session_meta={"plan_file_path": str(plan)},
    )
    source = PlanStateSource(max_bytes=1024)

    attachments = source.collect(ctx)

    assert len(attachments) == 1
    assert attachments[0]["source_name"] == "plan_state"
    assert "plan.md" in attachments[0]["title"]
    assert attachments[0]["content"] == "plan body text"


# PSS-2 inv-R5 + Risk-R1（plan 超限 → 空列表 + warning）
def test_pss2_oversized_plan_returns_empty(tmp_path, caplog):
    plan = tmp_path / "plan.md"
    plan.write_text("y" * 100, encoding="utf-8")

    ctx = PostCompactContext(
        session_id="s",
        run_id="r",
        working_memory=FakeWM(),
        skill_registry=None,
        session_meta={"plan_file_path": str(plan)},
    )
    source = PlanStateSource(max_bytes=64)

    with caplog.at_level(logging.WARNING, logger=SOURCES_LOGGER):
        attachments = source.collect(ctx)

    assert attachments == []
    warnings = _source_records(caplog, min_level=logging.WARNING)
    assert any("exceeds limit" in r.getMessage() for r in warnings)


# PSS-3 inv-R5 + Risk-R2（meta 缺失 / 文件不存在 → 空列表，降级不崩溃）
def test_pss3_missing_meta_or_file_returns_empty(tmp_path, caplog):
    source = PlanStateSource(max_bytes=1024)
    ctx_a = PostCompactContext(
        session_id="s",
        run_id="r",
        working_memory=FakeWM(),
        skill_registry=None,
        session_meta={},
    )
    ghost = tmp_path / "ghost.md"
    ctx_b = PostCompactContext(
        session_id="s",
        run_id="r",
        working_memory=FakeWM(),
        skill_registry=None,
        session_meta={"plan_file_path": str(ghost)},
    )

    with caplog.at_level(logging.INFO, logger=SOURCES_LOGGER):
        a = source.collect(ctx_a)
        b = source.collect(ctx_b)

    assert a == []
    assert b == []
    infos = _source_records(caplog, min_level=logging.INFO)
    assert any(
        ("cannot stat" in r.getMessage() or "failed to read" in r.getMessage())
        and "ghost.md" in r.getMessage()
        for r in infos
    )


# PSS-4 inv-R5（空白 plan 文件 → 空列表，不回注）
def test_pss4_blank_plan_returns_empty(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("   \n\t", encoding="utf-8")

    ctx = PostCompactContext(
        session_id="s",
        run_id="r",
        working_memory=FakeWM(),
        skill_registry=None,
        session_meta={"plan_file_path": str(plan)},
    )
    source = PlanStateSource(max_bytes=1024)

    assert source.collect(ctx) == []
