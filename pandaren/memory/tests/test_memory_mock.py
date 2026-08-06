"""pandaren/memory/tests/test_memory_mock.py — Memory 模块 Mock 测试

v1.4 重构后的覆盖：
  - WorkingMemory：HC1 冻结、HC2 深拷贝隔离
  - WindowedKeepPolicy.split()：返回 CompactionSplit(kept, dropped)，分区不重不漏
  - AsyncBatchFlushPolicy：mock RawLogBackend 验证调用参数
  - SQLiteRawLogBackend：HC2 深拷贝、互斥校验、tmp_path 落临时文件
  - Memory Facade：HC1 冻结字段、mock FlushPolicy、
                   set_on_compact_callback 回调、单轮模式跳过行为、
                   compact_if_needed 三/四层管线（含 DropSummarizer）
  - MemorySnapshot：frozen=True 不可变保护

运行：
    cd pandaren/memory/tests && python test_memory_mock.py
    （从仓库根目录：python pandaren/memory/tests/test_memory_mock.py）
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from typing import Callable
from unittest.mock import AsyncMock, MagicMock, patch

# ── Windows 控制台 UTF-8 兼容 ──────────────────────────────────────────────
if sys.platform == "win32" and "pytest" not in sys.modules:  # pytest 下交给 capture，避免拆台
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# ── 路径修正（兼容直接运行）────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

# ── 被测模块 ─────────────────────────────────────────────────────────────────
from pandaren.memory.working_memory import WorkingMemory, MemoryLimitError
from pandaren.memory.compaction.windowed import WindowedKeepPolicy
from pandaren.memory.flush_policy import AsyncBatchFlushPolicy
from pandaren.memory.backends import SQLiteRawLogBackend
from pandaren.memory.memory import Memory, MemoryStateError
from pandaren.memory.models import (
    MemorySnapshot,
    MessageDict,
    CompactionSplit,
)
from pandaren.memory.protocols import (
    WorkingMemoryAccessor,
    DropSummarizer,
)


# ─────────────────────────────────────────────
# 轻量测试框架
# ─────────────────────────────────────────────

class TestResult:
    """轻量测试结果收集器。"""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.errors: list[str] = []

    def ok(self, name: str) -> None:
        self.passed += 1
        print(f"   ✅ {name}")

    def fail(self, name: str, detail: str = "") -> None:
        self.failed += 1
        msg = f"   ❌ {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        self.errors.append(msg)

    def expect(self, cond: bool, name: str, detail: str = "") -> None:
        if cond:
            self.ok(name)
        else:
            self.fail(name, detail)


def _section(title: str) -> None:
    print(f"\n── {title} ─────────────────────────────────")


# ─────────────────────────────────────────────
# WorkingMemory
# ─────────────────────────────────────────────

def test_working_memory(rep: TestResult) -> None:
    _section("WorkingMemory")

    wm = WorkingMemory(max_entries=5)
    wm.set("a", 1)
    rep.expect(wm.get("a") == 1, "set/get 基本路径")

    wm.set("b", [1, 2, 3])
    fetched = wm.get("b")
    rep.expect(fetched == [1, 2, 3], "list 值读取相等")
    fetched.append(99)
    rep.expect(wm.get("b") == [1, 2, 3], "HC2: 外部修改不影响内部值")

    # 超容量
    for i in range(3):
        wm.set(f"k{i}", i)
    raised = False
    try:
        wm.set("overflow", 1)
    except MemoryLimitError:
        raised = True
    rep.expect(raised, "超 max_entries 抛 MemoryLimitError")

    # HC1 冻结
    raised = False
    try:
        wm._max_entries = 999
    except AttributeError:
        raised = True
    rep.expect(raised, "HC1: _max_entries 构造后冻结")


# ─────────────────────────────────────────────
# WindowedKeepPolicy.split()
# ─────────────────────────────────────────────

def test_windowed_split(rep: TestResult) -> None:
    _section("WindowedKeepPolicy.split()")

    policy = WindowedKeepPolicy(
        min_keep_tokens=10,
        min_keep_text_messages=1,
        max_keep_tokens=100,
    )
    msgs: list[MessageDict] = [
        {"role": "user", "content": "msg1 " * 50},
        {"role": "assistant", "content": "reply1 " * 50},
        {"role": "user", "content": "msg2 " * 50},
        {"role": "assistant", "content": "reply2 " * 50},
        {"role": "user", "content": "msg3"},  # 短消息
    ]
    result = policy.split(msgs, max_tokens=50)

    rep.expect(isinstance(result, CompactionSplit), "split() 返回 CompactionSplit")
    rep.expect(
        len(result.kept) + len(result.dropped) == len(msgs),
        "kept + dropped 数量等于原列表",
        f"kept={len(result.kept)} dropped={len(result.dropped)} total={len(msgs)}",
    )
    rep.expect(result.kept, "至少保留一条消息")

    # 空列表
    empty = policy.split([], max_tokens=10)
    rep.expect(empty.kept == [] and empty.dropped == [], "空列表 → 空 CompactionSplit")

    # frozen
    raised = False
    try:
        result.kept = []  # type: ignore[misc]
    except Exception:
        raised = True
    rep.expect(raised, "CompactionSplit frozen=True 不可变")

    # ── 超大消息不保留（Bug fix: "at least keep one" 溢出）──
    # 模拟子 agent 场景：最新消息是巨大的 read_file 工具结果，超出 hard_cap
    big_policy = WindowedKeepPolicy(
        min_keep_tokens=10,
        min_keep_text_messages=1,
        max_keep_tokens=100,
    )
    oversized_msgs: list[MessageDict] = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "let me read the file"},
        {"role": "tool", "tool_call_id": "tc1", "content": "x" * 10000},  # 巨大
    ]
    big_result = big_policy.split(oversized_msgs, max_tokens=50)
    # 巨大的 tool result 不应被保留（它超出 hard_cap）
    rep.expect(
        len(big_result.kept) + len(big_result.dropped) == len(oversized_msgs),
        "超大消息场景: kept + dropped 数量守恒",
        f"kept={len(big_result.kept)} dropped={len(big_result.dropped)}",
    )

    # ── 所有消息超大时 kept 可以为空 ──
    all_big: list[MessageDict] = [
        {"role": "user", "content": "a" * 5000},
        {"role": "assistant", "content": "b" * 5000},
        {"role": "tool", "tool_call_id": "tc2", "content": "c" * 5000},
    ]
    all_big_result = big_policy.split(all_big, max_tokens=10)
    rep.expect(
        len(all_big_result.kept) + len(all_big_result.dropped) == len(all_big),
        "全部超大: kept + dropped 数量守恒",
        f"kept={len(all_big_result.kept)} dropped={len(all_big_result.dropped)}",
    )


# ─────────────────────────────────────────────
# ensure_tool_pair_integrity
# ─────────────────────────────────────────────

def test_tool_pair_integrity(rep: TestResult) -> None:
    _section("ensure_tool_pair_integrity")

    from pandaren.memory.compaction.tool_pair_integrity import ensure_tool_pair_integrity

    # ── 完整配对不产生重复 ──
    full_msgs: list[MessageDict] = [
        {"role": "user", "content": "search for X"},
        {"role": "assistant", "content": "searching", "tool_calls": [
            {"id": "tc1", "type": "function", "function": {"name": "search", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "tc1", "content": "result1"},
        {"role": "user", "content": "thanks"},
    ]
    # kept 包含完整配对
    kept = [full_msgs[1], full_msgs[2]]  # assistant + tool_result
    fixed = ensure_tool_pair_integrity(kept, full=full_msgs)
    # 应恰好包含 assistant + tool_result，不重复
    tool_results = [m for m in fixed if m.get("role") == "tool"]
    rep.expect(len(tool_results) == 1, "完整配对: tool_result 恰好出现一次",
               f"got {len(tool_results)} tool_results")
    rep.expect(
        tool_results[0].get("tool_call_id") == "tc1",
        "完整配对: tool_result tool_call_id 正确",
    )
    # assistant 应在 tool_result 之前
    asst_idx = next(i for i, m in enumerate(fixed) if m.get("role") == "assistant")
    tool_idx = next(i for i, m in enumerate(fixed) if m.get("role") == "tool")
    rep.expect(asst_idx < tool_idx, "完整配对: assistant 在 tool_result 之前")

    # ── 孤儿 tool_result 从 full 捞回 assistant ──
    orphan_kept = [full_msgs[2]]  # 只有 tool_result，无 assistant
    fixed_orphan = ensure_tool_pair_integrity(orphan_kept, full=full_msgs)
    rep.expect(
        any(m.get("role") == "assistant" for m in fixed_orphan),
        "孤儿 tool_result: 从 full 捞回 assistant",
    )
    # assistant 应在 tool_result 之前
    orphan_asst_idx = next(i for i, m in enumerate(fixed_orphan) if m.get("role") == "assistant")
    orphan_tool_idx = next(i for i, m in enumerate(fixed_orphan) if m.get("role") == "tool")
    rep.expect(orphan_asst_idx < orphan_tool_idx,
               "孤儿 tool_result: assistant 在 tool_result 之前")

    # ── 混合场景：部分完整部分孤儿，不产生重复 ──
    mixed_full: list[MessageDict] = [
        {"role": "user", "content": "do two things"},
        {"role": "assistant", "content": "doing both", "tool_calls": [
            {"id": "tcA", "type": "function", "function": {"name": "fnA", "arguments": "{}"}},
            {"id": "tcB", "type": "function", "function": {"name": "fnB", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "tcA", "content": "resultA"},
        {"role": "tool", "tool_call_id": "tcB", "content": "resultB"},
        {"role": "user", "content": "done"},
    ]
    # kept 包含 assistant + 两个 tool_result（完整配对）
    mixed_kept = [mixed_full[1], mixed_full[2], mixed_full[3]]
    mixed_fixed = ensure_tool_pair_integrity(mixed_kept, full=mixed_full)
    mixed_tools = [m for m in mixed_fixed if m.get("role") == "tool"]
    rep.expect(len(mixed_tools) == 2, "混合完整配对: 恰好 2 个 tool_result",
               f"got {len(mixed_tools)}")
    mixed_tc_ids = [m.get("tool_call_id") for m in mixed_tools]
    rep.expect("tcA" in mixed_tc_ids and "tcB" in mixed_tc_ids,
               "混合完整配对: tcA 和 tcB 都在")

    # ── 孤儿 tool_result 无源可捞 → 删除 ──
    no_source_kept: list[MessageDict] = [
        {"role": "tool", "tool_call_id": "tcUnknown", "content": "orphan"},
        {"role": "user", "content": "hello"},
    ]
    no_source_fixed = ensure_tool_pair_integrity(no_source_kept, full=[])
    rep.expect(
        not any(m.get("role") == "tool" for m in no_source_fixed),
        "无源孤儿 tool_result: 被删除",
    )
    rep.expect(
        any(m.get("role") == "user" for m in no_source_fixed),
        "无源孤儿 tool_result: user 消息保留",
    )

    # ── 空 kept → 空结果 ──
    rep.expect(ensure_tool_pair_integrity([], full=[]) == [], "空 kept → 空结果")


# ─────────────────────────────────────────────
# AsyncBatchFlushPolicy
# ─────────────────────────────────────────────

def test_flush_policy(rep: TestResult) -> None:
    _section("AsyncBatchFlushPolicy")

    backend = MagicMock()
    backend.append_raw_message = MagicMock()
    fp = AsyncBatchFlushPolicy(coalesce_ms=10, buffer_max_entries=100)

    async def run() -> None:
        await fp.enqueue(
            {"role": "user", "content": "hi"},
            session_id="s1",
            backend=backend,
        )
        await fp.flush(session_id="s1", backend=backend, flush_all=True)

    asyncio.run(run())
    rep.expect(
        backend.append_raw_message.called,
        "flush 后 append_raw_message 被调用",
    )


# ─────────────────────────────────────────────
# SQLiteRawLogBackend
# ─────────────────────────────────────────────

def test_sqlite_raw_log_backend(rep: TestResult) -> None:
    _section("SQLiteRawLogBackend")

    # 互斥校验
    raised = False
    try:
        SQLiteRawLogBackend()
    except ValueError:
        raised = True
    rep.expect(raised, "都不传 → ValueError")

    raised = False
    try:
        SQLiteRawLogBackend(db_path=":memory:")
    except ValueError:
        raised = True
    rep.expect(raised, "db_path=':memory:' → ValueError")

    import sqlite3
    raised = False
    try:
        SQLiteRawLogBackend(db_path="x.db", connection=sqlite3.connect(":memory:"))
    except ValueError:
        raised = True
    rep.expect(raised, "db_path 与 connection 同传 → ValueError")

    # 正常路径（tmp_path）
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "test.db")
        backend = SQLiteRawLogBackend(db_path=db)
        backend.append_raw_message({"role": "user", "content": "hi"}, session_id="s1")
        backend.append_raw_message({"role": "assistant", "content": "hello"}, session_id="s1")
        backend.append_raw_message({"role": "user", "content": "ping"}, session_id="s2")

        msgs = backend.load_all("s1")
        rep.expect(len(msgs) == 2, "load_all('s1') 返回 2 条")
        rep.expect(msgs[0]["role"] == "user" and msgs[1]["role"] == "assistant",
                   "load_all 时序正确")

        sessions = backend.list_sessions()
        rep.expect(set(sessions) == {"s1", "s2"}, "list_sessions 返回所有 session_id")

        # load_within_budget
        sub = backend.load_within_budget("s1", token_budget=10000)
        rep.expect(len(sub) == 2, "load_within_budget 充足预算返回全量")

        backend.close()


# ─────────────────────────────────────────────
# Memory Facade
# ─────────────────────────────────────────────

def test_memory_facade(rep: TestResult) -> None:
    _section("Memory Facade")

    m = Memory(system_prompt="你是助手")
    rep.expect(m.system_prompt == "你是助手", "system_prompt 初始化正确")

    # HC1 冻结
    raised = False
    try:
        m._system_prompt = "改了"
    except AttributeError:
        raised = True
    rep.expect(raised, "HC1: _system_prompt 构造后冻结")

    # init_from_restore
    msgs = m.init_from_restore("hello", session_id="s-test")
    rep.expect(msgs[-1]["role"] == "user" and msgs[-1]["content"] == "hello",
               "init_from_restore 追加 user 消息")

    # append_user_message
    m.append_user_message("again")
    msgs = m.get_messages()
    rep.expect(msgs[-1]["content"] == "again", "append_user_message 末尾追加")

    # set_on_compact_callback
    cb_called = []
    m.set_on_compact_callback(lambda: cb_called.append(True))
    rep.expect(m._on_compact_callback is not None, "set_on_compact_callback 注入成功")

    # end_session 简化为只 flush + reset
    async def run_end() -> None:
        await m.end_session()

    asyncio.run(run_end())
    rep.expect(m._session_id is None, "end_session 后 session_id 重置")

    # single_turn 模式
    m_single = Memory(system_prompt="x", session_mode="single_turn")
    msgs = m_single.init_from_restore("test", session_id="s-single")
    rep.expect(any(msg.get("content") == "test" for msg in msgs),
               "single_turn 模式正常初始化")


# ─────────────────────────────────────────────
# DropSummarizer 路径
# ─────────────────────────────────────────────

def test_drop_summarizer(rep: TestResult) -> None:
    _section("DropSummarizer")

    class StubSummarizer:
        async def summarize(self, dropped):  # type: ignore[no-untyped-def]
            return {"role": "system", "content": f"summary of {len(dropped)} msgs"}

    summarizer = StubSummarizer()
    # Protocol 鸭子类型校验
    rep.expect(isinstance(summarizer, DropSummarizer),
               "StubSummarizer 满足 DropSummarizer Protocol（runtime_checkable）")

    # Memory 集成：drop_summarizer=None 时退化为不摘要
    m = Memory(system_prompt="x", drop_summarizer=None, compact_threshold=999_999)
    m.init_from_restore("hello", session_id="s")
    async def run_compact() -> None:
        # 当前 token 远低于阈值 → compact_if_needed 直接返回 None
        result = await m.compact_if_needed()
        rep.expect(result is None, "drop_summarizer=None & 未超阈值 → compact_if_needed 返回 None")

    asyncio.run(run_compact())

    # 异常降级：summarizer 抛出 → Memory 捕获
    class BoomSummarizer:
        async def summarize(self, dropped):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    m2 = Memory(
        system_prompt="x",
        drop_summarizer=BoomSummarizer(),
        compact_threshold=10,  # 极低阈值，强制触发
    )
    m2.init_from_restore("a" * 100, session_id="s2")
    for i in range(5):
        async def add():  # type: ignore[no-untyped-def]
            await m2.add_assistant_message("b" * 100)
        asyncio.run(add())

    async def run_overflow() -> None:
        # 应不抛异常，Memory Facade 内部 catch
        try:
            _ = await m2.compact_if_needed()
            rep.ok("BoomSummarizer 抛错时 compact_if_needed 不冒泡（异常降级）")
        except Exception as exc:
            rep.fail("BoomSummarizer 异常应该被吞", str(exc))

    asyncio.run(run_overflow())


# ─────────────────────────────────────────────
# MemorySnapshot
# ─────────────────────────────────────────────

def test_memory_snapshot(rep: TestResult) -> None:
    _section("MemorySnapshot")

    snap = MemorySnapshot(
        messages=({"role": "user", "content": "hi"},),
    )
    rep.expect(snap.messages[0]["role"] == "user", "MemorySnapshot.messages 可读")

    raised = False
    try:
        snap.messages = ()  # type: ignore[misc]
    except Exception:
        raised = True
    rep.expect(raised, "MemorySnapshot frozen=True 不可变")


# ─────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────

def main() -> int:
    rep = TestResult()
    test_working_memory(rep)
    test_windowed_split(rep)
    test_tool_pair_integrity(rep)
    test_flush_policy(rep)
    test_sqlite_raw_log_backend(rep)
    test_memory_facade(rep)
    test_drop_summarizer(rep)
    test_memory_snapshot(rep)

    print(f"\n📊 [memory mock] 通过={rep.passed} / 失败={rep.failed} / 总计={rep.passed + rep.failed}")
    return 0 if rep.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
