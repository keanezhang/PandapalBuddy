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
from pandaren.memory.constants import RECENT_FILE_READS_WM_KEY
from pandaren.memory.models import PostCompactContext
from pandaren.memory.reinject.sources import (
    PlanStateSource,
    RecentFilesSource,
    _truncate_to_tokens,
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
# TR 组：_truncate_to_tokens = 统一尺子 + 标记计入上限（SPEC §2.5 P0）
# ─────────────────────────────────────────────


class _PerCharEstimator:
    """假尺子：1 字符 = ``ratio`` token。用来证明「用的是**注入的**尺子」。"""

    def __init__(self, ratio: float) -> None:
        self._ratio = ratio

    def estimate(self, messages) -> int:
        return int(
            sum(len(str(m.get("content") or "")) for m in messages) * self._ratio
        )


# TR-1：截断后（**含标记**）的真实估算不超上限 —— 旧实现把标记加在限额外
def test_tr1_truncated_result_stays_within_limit():
    truncated, est = _truncate_to_tokens("x" * 500, 100, _PerCharEstimator(1.0))

    assert truncated.endswith("[...truncated by PostCompactSource]")
    assert est <= 100
    assert est == _PerCharEstimator(1.0).estimate(
        [{"role": "tool", "content": truncated}]
    )


# TR-2：未超限 → 原样返回，不加标记
def test_tr2_within_limit_returned_verbatim():
    truncated, est = _truncate_to_tokens("hello", 100, _PerCharEstimator(1.0))

    assert truncated == "hello"
    assert est == 5


# TR-3：用**注入的**尺子，而不是 CHARS_PER_TOKEN 粗估
def test_tr3_uses_injected_estimator_not_chars_per_token():
    """1 字符 = 10 token → 上限 1,000 只允许 ~100 字符。

    旧实现按 chars/4 会给出 4,000 字符 → 超支约 40 倍（中文实际超支 ~2 倍）。
    """
    truncated, est = _truncate_to_tokens("y" * 5_000, 1_000, _PerCharEstimator(10.0))

    assert est <= 1_000
    assert len(truncated) < 200
    assert truncated.endswith("[...truncated by PostCompactSource]")


# TR-4：二分收敛 → 恰好用满上限（不是一刀切到保守值）
def test_tr4_converges_to_full_budget():
    truncated, est = _truncate_to_tokens("z" * 1_000, 100, _PerCharEstimator(1.0))

    assert est == 100
    assert len(truncated) == 100


# TR-5：空文本
def test_tr5_empty_text():
    assert _truncate_to_tokens("", 100, _PerCharEstimator(1.0)) == ("", 0)


# TR-6：极端 —— 上限比标记本身还小，仍不得超限（此时不带标记）
def test_tr6_limit_smaller_than_marker():
    truncated, est = _truncate_to_tokens("w" * 500, 10, _PerCharEstimator(1.0))

    assert est <= 10
    assert "[...truncated" not in truncated


# ─────────────────────────────────────────────
# RFS 组：RecentFilesSource = 文件清单**指针**（不注入正文）
# ─────────────────────────────────────────────


def _ctx(wm=None, meta=None, estimator=None):
    """构造 PostCompactContext（默认无 estimator → 走 chars/4 回退）。"""
    return PostCompactContext(
        session_id="s",
        run_id="r",
        working_memory=wm if wm is not None else FakeWM(),
        session_meta=meta or {},
        token_estimator=estimator,
    )


def _record(path, ts=1.0, size=None):
    r = {"path": str(path), "timestamp": ts}
    if size is not None:
        r["size_hint"] = size
    return r


# RFS-1：清单 = 路径 + 大小，**正文绝不进上下文**
def test_rfs1_lists_paths_not_content(tmp_path):
    f = tmp_path / "main.py"
    f.write_text("SECRET_BODY_MARKER" * 10, encoding="utf-8")

    wm = FakeWM()
    wm.set(RECENT_FILE_READS_WM_KEY, [_record(f, ts=1.0)])

    attachments = RecentFilesSource().collect(_ctx(wm))

    assert len(attachments) == 1
    content = attachments[0]["content"]
    assert "main.py" in content
    assert "SECRET_BODY_MARKER" not in content   # ← 指针模式的核心：正文不回注
    assert "read_file" in content                # 明确指引 AI 去重取
    assert "仅在确实需要" in content              # ← 指导语：给了 ≠ 要读（防逐个重读）


# RFS-2：同路径去重（留最近）+ 按时间倒序 + 只取前 max_files
def test_rfs2_dedup_and_recency_order(tmp_path):
    a, b, c = (tmp_path / n for n in ("a.py", "b.py", "c.py"))
    for f in (a, b, c):
        f.write_text("x", encoding="utf-8")

    wm = FakeWM()
    wm.set(RECENT_FILE_READS_WM_KEY, [
        _record(a, ts=1.0),
        _record(b, ts=3.0),
        _record(a, ts=5.0),      # a 重复 → 取更近的 5.0
        _record(c, ts=2.0),
    ])

    content = RecentFilesSource(max_files=2).collect(_ctx(wm))[0]["content"]

    assert content.index("a.py") < content.index("b.py")   # a(5.0) 排在 b(3.0) 前
    assert "c.py" not in content                            # max_files=2 → c 落选


# RFS-3：文件已删除 → 不列进清单（指针指向读不到的文件没有意义）
def test_rfs3_missing_file_skipped(tmp_path):
    here = tmp_path / "here.py"
    here.write_text("x", encoding="utf-8")

    wm = FakeWM()
    wm.set(RECENT_FILE_READS_WM_KEY, [
        _record(here, ts=2.0),
        _record(tmp_path / "gone.py", ts=1.0),
    ])

    content = RecentFilesSource().collect(_ctx(wm))[0]["content"]

    assert "here.py" in content
    assert "gone.py" not in content


# RFS-4：WM 无记录 / 格式非法 → 空列表（E4 降级，不抛异常）
def test_rfs4_empty_or_bad_records():
    assert RecentFilesSource().collect(_ctx()) == []

    wm = FakeWM()
    wm.set(RECENT_FILE_READS_WM_KEY, ["not-a-dict", {"no_path": 1}])
    assert RecentFilesSource().collect(_ctx(wm)) == []


# RFS-5：清单超上限 → 截断且**不超限**（用注入的同一把尺子）
def test_rfs5_listing_truncated_within_limit(tmp_path):
    wm = FakeWM()
    wm.set(
        RECENT_FILE_READS_WM_KEY,
        [
            _record(
                tmp_path / f"very_long_file_name_number_{i}.py", ts=float(i)
            )
            for i in range(5)
        ],
    )
    for i in range(5):
        (tmp_path / f"very_long_file_name_number_{i}.py").write_text(
            "x", encoding="utf-8"
        )

    att = RecentFilesSource(max_files=5, max_tokens=60).collect(
        _ctx(wm, estimator=_PerCharEstimator(1.0))
    )[0]

    assert att["estimated_tokens"] <= 60
    assert att["content"].endswith("[...truncated by PostCompactSource]")


# RFS-6：清单被截断时，**头部的指导语必须仍在**（截断切尾部 → 指导语放头部才安全）
def test_rfs6_guidance_survives_truncation(tmp_path):
    wm = FakeWM()
    records = []
    for i in range(10):
        f = tmp_path / f"long_file_name_{i}.py"
        f.write_text("x", encoding="utf-8")
        records.append(_record(f, ts=float(i)))
    wm.set(RECENT_FILE_READS_WM_KEY, records)

    att = RecentFilesSource(max_tokens=400).collect(
        _ctx(wm, estimator=_PerCharEstimator(1.0))
    )[0]
    content = att["content"]

    assert att["estimated_tokens"] <= 400
    assert "仅在确实需要" in content              # ← 指导语活下来了
    assert "long_file_name_9.py" in content      # 最近读的排第一行（倒序）→ 保留
    assert "long_file_name_0.py" not in content  # 最旧的排最后 → 被切（可接受）
    assert content.endswith("[...truncated by PostCompactSource]")


# RFS-7：配额不变式 —— 各 source cap 之和 ≤ 总预算
def test_rfs7_source_caps_fit_total_budget():
    """编排器超**总预算**时是整体丢弃（不是截断）——cap 之和一旦超过总预算，
    排后面的 source 会被前面的饿死（如 plan 指针被文件清单挤掉）。"""
    from pandaren.memory.constants import (
        DEFAULT_POST_COMPACT_FILES_LIST_MAX_TOKENS,
        DEFAULT_POST_COMPACT_PLAN_MAX_TOKENS,
        DEFAULT_POST_COMPACT_TOKEN_BUDGET,
    )

    total_caps = (
        DEFAULT_POST_COMPACT_FILES_LIST_MAX_TOKENS
        + DEFAULT_POST_COMPACT_PLAN_MAX_TOKENS
    )
    assert total_caps <= DEFAULT_POST_COMPACT_TOKEN_BUDGET, (
        f"cap 之和 {total_caps} 超过总预算 {DEFAULT_POST_COMPACT_TOKEN_BUDGET}"
    )


# ─────────────────────────────────────────────
# PSS 组：PlanStateSource = plan **路径指针**（不注入正文）
# ─────────────────────────────────────────────


# PSS-1：只给路径 + 指引，plan 正文绝不进上下文
def test_pss1_pointer_only(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("PLAN_BODY_MARKER", encoding="utf-8")

    attachments = PlanStateSource().collect(_ctx(meta={"plan_file_path": str(plan)}))

    assert len(attachments) == 1
    assert attachments[0]["source_name"] == "plan_state"
    assert "plan.md" in attachments[0]["title"]
    content = attachments[0]["content"]
    assert str(plan) in content
    assert "PLAN_BODY_MARKER" not in content   # ← 指针模式的核心
    assert "read_file" in content              # 明确指引 AI 去读正文
    assert "需要回顾计划细节时" in content      # ← 指导语：给了 ≠ 要读


# PSS-2：meta 缺失 / 文件不存在 → 空列表（E4 降级）
def test_pss2_missing_meta_or_file(tmp_path):
    assert PlanStateSource().collect(_ctx()) == []
    assert PlanStateSource().collect(
        _ctx(meta={"plan_file_path": str(tmp_path / "ghost.md")})
    ) == []


# PSS-3：路径类型非法（空串 / None / 非字符串）→ 空列表（防御）
def test_pss3_bad_path_types():
    for bad in ("", None, 123, ["x"]):
        assert PlanStateSource().collect(
            _ctx(meta={"plan_file_path": bad})
        ) == []


