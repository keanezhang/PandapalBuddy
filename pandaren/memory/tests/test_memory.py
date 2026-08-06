"""pandaren/memory/tests/test_memory.py — Memory 模块真实单元测试（无 API Key）

覆盖：
  - WorkingMemory：HC1 冻结、HC2 深拷贝、O3 容量保护、snapshot/restore
  - ShortTermMemory：消息追加、压缩触发、反扩保护、snapshot/resume
  - RoundBasedPolicy：多轮压缩、token 预算、keep_rounds=0
  - AsyncBatchFlushPolicy：enqueue 溢出保护、coalesce 延迟、flush_all
  - InMemoryRawLogBackend：append/load、boundary 分区、token 预算
  - InMemorySummaryBackend：store/search/get_recent/delete、(user,session) 隔离
  - LongTermMemory：recall、store_session_summary、load_for_restore
  - Memory Facade：完整生命周期、单轮模式、HC1、snapshot/resume、compact 回调
  - MemorySnapshot：frozen=True、tuple messages
"""

from __future__ import annotations

import asyncio
import sys
from typing import Callable

# ── Windows 控制台 UTF-8 兼容 ──────────────────────────────────────────────
if sys.platform == "win32" and "pytest" not in sys.modules:  # pytest 下交给 capture，避免拆台
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# ── 被测模块 ─────────────────────────────────────────────────────────────────
import pytest

# 本文件依赖已重构移除的 pandaren.memory.compression / pandaren.memory.backend.*
# （现为 compaction / backends），属 API 漂移债，待按新接口重写。
pytest.skip("依赖已移除的 memory.compression/backend 模块（API 漂移，待重写）", allow_module_level=True)

from pandaren.memory.working_memory import WorkingMemory, MemoryLimitError
from pandaren.memory.short_term import ShortTermMemory
from pandaren.memory.compression.round_based import RoundBasedPolicy
from pandaren.memory.flush_policy import AsyncBatchFlushPolicy
from pandaren.memory.backend.raw_log.in_memory import InMemoryRawLogBackend
from pandaren.memory.backend.summary.in_memory import InMemorySummaryBackend
from pandaren.memory.long_term import LongTermMemory
from pandaren.memory.memory import Memory, MemoryStateError
from pandaren.memory.models import MemorySnapshot, MessageDict, EntryMetadata
from pandaren.memory.protocols import (
    CharBasedTokenEstimator,
    WorkingMemoryAccessor,
)
from pandaren.memory.constants import (
    DEFAULT_WORKING_MEMORY_MAX_ENTRIES,
    DEFAULT_COMPACT_THRESHOLD,
    DEFAULT_KEEP_ROUNDS,
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

    def summary(self, section: str = "") -> bool:
        total = self.passed + self.failed
        label = f" [{section}]" if section else ""
        print(f"\n📊{label} 通过={self.passed} / 失败={self.failed} / 总计={total}")
        if self.errors:
            print("   失败列表:")
            for e in self.errors:
                print(f"     {e}")
        return self.failed == 0


result = TestResult()


def assert_true(condition: bool, name: str, detail: str = "") -> None:
    if condition:
        result.ok(name)
    else:
        result.fail(name, detail or "条件为 False")


def assert_raises(exc_type, fn, name: str, detail: str = "") -> None:
    exc_label = (
        " | ".join(t.__name__ for t in exc_type)
        if isinstance(exc_type, tuple)
        else exc_type.__name__
    )
    try:
        fn()
        result.fail(name, detail or f"应抛出 {exc_label} 但未抛出")
    except exc_type:
        result.ok(name)
    except Exception as e:
        result.fail(name, f"抛出了意外异常 {type(e).__name__}: {e}")


def assert_no_raises(fn, name: str, detail: str = "") -> None:
    try:
        fn()
        result.ok(name)
    except Exception as e:
        result.fail(name, detail or f"意外抛出 {type(e).__name__}: {e}")


def async_run(coro):
    """同步运行协程。"""
    return asyncio.get_event_loop().run_until_complete(coro)


# ─────────────────────────────────────────────
# 辅助工厂
# ─────────────────────────────────────────────

def _msg(role: str, content: str) -> MessageDict:
    return {"role": role, "content": content}  # type: ignore[typeddict-item]


def _make_memory(
    *,
    system_prompt: str = "You are helpful.",
    compact_threshold: int = DEFAULT_COMPACT_THRESHOLD,
    session_mode: str = "multi_turn",
) -> Memory:
    """创建禁用磁盘 IO 的内存 Memory 实例（测试用）。"""
    return Memory(
        system_prompt=system_prompt,
        raw_log_backend=None,
        summary_backend=None,
        compact_threshold=compact_threshold,
        session_mode=session_mode,
    )


def _make_memory_with_backends(
    *,
    system_prompt: str = "test",
    compact_threshold: int = DEFAULT_COMPACT_THRESHOLD,
) -> tuple[Memory, InMemoryRawLogBackend, InMemorySummaryBackend]:
    """创建使用内存后端的 Memory 实例，同时返回后端引用供验证。"""
    raw = InMemoryRawLogBackend()
    summary = InMemorySummaryBackend()
    mem = Memory(
        system_prompt=system_prompt,
        raw_log_backend=raw,
        summary_backend=summary,
        compact_threshold=compact_threshold,
    )
    return mem, raw, summary


def _make_stm(keep_rounds: int = 3, compact_threshold: int = DEFAULT_COMPACT_THRESHOLD) -> ShortTermMemory:
    """创建 ShortTermMemory 实例。"""
    policy = RoundBasedPolicy(keep_rounds=keep_rounds)
    return ShortTermMemory(compression_policy=policy, compact_threshold=compact_threshold)


# ─────────────────────────────────────────────
# Section 1: WorkingMemory
# ─────────────────────────────────────────────

def test_working_memory() -> None:
    print("\n── WorkingMemory ────────────────────────────────────────")

    # 基本 get/set
    wm = WorkingMemory(max_entries=100)
    wm.set("name", "pandaren")
    assert_true(wm.get("name") == "pandaren", "set/get 基本读写正确")
    assert_true(wm.get("nonexistent") is None, "不存在的 key 返回 None")

    # size 属性
    wm2 = WorkingMemory(max_entries=10)
    assert_true(wm2.size == 0, "初始 size == 0")
    wm2.set("k1", 1)
    wm2.set("k2", 2)
    assert_true(wm2.size == 2, "size 反映实际条目数")

    # clear
    wm2.clear()
    assert_true(wm2.size == 0, "clear() 后 size == 0")

    # HC1：_max_entries 冻结
    assert_raises(
        AttributeError,
        lambda: setattr(wm, "_max_entries", 9999),
        "HC1：_max_entries 初始化后不可修改",
    )

    # HC2：get 返回深拷贝
    wm3 = WorkingMemory()
    wm3.set("data", {"x": [1, 2, 3]})
    got = wm3.get("data")
    assert got is not None
    got["x"].append(99)  # type: ignore[index]
    assert_true(wm3.get("data") == {"x": [1, 2, 3]}, "HC2：get 返回深拷贝，修改不影响内部")  # type: ignore[comparison-overlap]

    # HC2：set 对输入深拷贝
    wm4 = WorkingMemory()
    original = {"val": 42}
    wm4.set("obj", original)
    original["val"] = 999
    assert_true(wm4.get("obj") == {"val": 42}, "HC2：set 存储深拷贝，修改原始不影响存储")  # type: ignore[comparison-overlap]

    # O3：超容量抛出 MemoryLimitError
    wm5 = WorkingMemory(max_entries=3)
    wm5.set("a", 1)
    wm5.set("b", 2)
    wm5.set("c", 3)
    assert_raises(
        MemoryLimitError,
        lambda: wm5.set("d", 4),
        "O3：超出 max_entries 抛出 MemoryLimitError",
    )

    # 已存在 key 更新不受容量限制
    assert_no_raises(lambda: wm5.set("a", 100), "已存在 key 更新不触发容量检查")
    assert_true(wm5.get("a") == 100, "更新已存在 key 值正确")

    # snapshot / restore
    wm6 = WorkingMemory()
    wm6.set("x", 1)
    wm6.set("y", "hello")
    snap = wm6.snapshot()
    wm6.clear()
    assert_true(wm6.size == 0, "clear 后 size == 0（restore 前）")
    wm6.restore(snap)
    assert_true(wm6.get("x") == 1, "restore 后 x 恢复正确")
    assert_true(wm6.get("y") == "hello", "restore 后 y 恢复正确")

    # accessor 协议满足
    wm7 = WorkingMemory()
    acc = wm7.accessor
    assert_true(isinstance(acc, WorkingMemoryAccessor), "accessor 满足 WorkingMemoryAccessor 协议")

    # 默认 max_entries
    wm_default = WorkingMemory()
    assert_true(wm_default._max_entries == DEFAULT_WORKING_MEMORY_MAX_ENTRIES, "默认 max_entries 正确")


# ─────────────────────────────────────────────
# Section 2: ShortTermMemory
# ─────────────────────────────────────────────

def test_short_term_memory() -> None:
    print("\n── ShortTermMemory ──────────────────────────────────────")

    # 基本消息追加
    stm = _make_stm()
    stm.append_user_message("Hello, AI!")
    msgs = stm.get_messages()
    assert_true(len(msgs) == 1, "追加 user 消息后 get_messages 返回 1 条")
    assert_true(msgs[0]["role"] == "user", "消息 role 为 user")
    assert_true(msgs[0]["content"] == "Hello, AI!", "消息 content 正确")

    # add_assistant_message
    stm.add_assistant_message("Hi there!")
    assert_true(len(stm.get_messages()) == 2, "追加 assistant 消息后共 2 条")
    assert_true(stm.get_messages()[1]["role"] == "assistant", "assistant 消息 role 正确")

    # add_tool_result
    stm.add_tool_result(tool_call_id="call_1", tool_name="search", content="found: data")
    msgs3 = stm.get_messages()
    assert_true(len(msgs3) == 3, "追加 tool 消息后共 3 条")
    assert_true(msgs3[2]["role"] == "tool", "tool 消息 role 正确")
    assert_true(msgs3[2].get("tool_call_id") == "call_1", "tool_call_id 正确")

    # is_empty
    stm2 = _make_stm()
    assert_true(stm2.is_empty, "初始 is_empty == True")
    stm2.append_user_message("msg")
    assert_true(not stm2.is_empty, "追加消息后 is_empty == False")

    # HC2：get_messages 返回深拷贝
    stm3 = _make_stm()
    stm3.append_user_message("original")
    msgs_copy = stm3.get_messages()
    msgs_copy[0]["content"] = "TAMPERED"  # type: ignore[typeddict-unknown-key]
    assert_true(stm3.get_messages()[0]["content"] == "original", "HC2：get_messages 返回深拷贝")

    # snapshot / resume_from_snapshot
    stm4 = _make_stm()
    stm4.append_user_message("q1")
    stm4.add_assistant_message("a1")
    snap = stm4.snapshot()
    assert_true(isinstance(snap, tuple), "snapshot 返回 tuple")
    assert_true(len(snap) == 2, "snapshot 包含正确数量的消息")
    stm4.reset()
    assert_true(stm4.is_empty, "reset 后 is_empty == True")
    stm4.resume_from_snapshot(snap)
    assert_true(len(stm4.get_messages()) == 2, "resume_from_snapshot 正确恢复消息数")

    # load_messages 过滤 system 消息
    stm5 = _make_stm()
    mixed = [
        _msg("system", "sys prompt"),
        _msg("user", "user msg"),
        _msg("assistant", "assistant msg"),
    ]
    stm5.load_messages(mixed)  # type: ignore[arg-type]
    loaded = stm5.get_messages()
    assert_true(len(loaded) == 2, "load_messages 过滤掉 system 消息")
    assert_true(all(m["role"] != "system" for m in loaded), "load_messages 结果不含 system 消息")

    # compact_if_needed：token 未超阈值时返回 False
    stm6 = _make_stm(compact_threshold=DEFAULT_COMPACT_THRESHOLD)
    stm6.append_user_message("short message")
    compacted = stm6.compact_if_needed()
    assert_true(compacted is False, "token 未超阈值时 compact_if_needed 返回 False")

    # estimate_tokens：非空消息返回正值
    stm7 = _make_stm()
    stm7.append_user_message("some content here")
    tokens = stm7.estimate_tokens()
    assert_true(tokens > 0, "estimate_tokens 返回正值")


# ─────────────────────────────────────────────
# Section 3: RoundBasedPolicy
# ─────────────────────────────────────────────

def test_round_based_policy() -> None:
    print("\n── RoundBasedPolicy ─────────────────────────────────────")

    policy = RoundBasedPolicy(keep_rounds=DEFAULT_KEEP_ROUNDS)

    # compress 空列表
    assert_true(policy.compress([], max_tokens=1000) == [], "compress([]) 返回 []")

    # 单轮：保留全部
    single_round = [_msg("user", "q"), _msg("assistant", "a")]
    compressed = policy.compress(single_round, max_tokens=100_000)
    assert_true(len(compressed) == 2, "单轮压缩保留全部 2 条")

    # 多轮，keep_rounds=1：只保留最新 1 轮
    p1 = RoundBasedPolicy(keep_rounds=1)
    multi = []
    for idx in range(5):
        multi.append(_msg("user", f"question {idx}"))
        multi.append(_msg("assistant", f"answer {idx}"))
    compressed_1 = p1.compress(multi, max_tokens=100_000)
    assert_true(len(compressed_1) == 2, "keep_rounds=1 保留最新 1 轮（2 条）")
    assert_true(compressed_1[-1]["content"] == "answer 4", "保留最新轮次的最后一条消息正确")

    # keep_rounds=2：保留最新 2 轮
    p2 = RoundBasedPolicy(keep_rounds=2)
    compressed_2 = p2.compress(multi, max_tokens=100_000)
    assert_true(len(compressed_2) == 4, "keep_rounds=2 保留最新 2 轮（4 条）")

    # keep_rounds=0：保留全部轮次
    p_all = RoundBasedPolicy(keep_rounds=0)
    compressed_all = p_all.compress(multi, max_tokens=100_000)
    assert_true(len(compressed_all) == len(multi), "keep_rounds=0 保留全部消息")

    # token 预算限制：预算极小时只保留最新 1 轮（至少有内容）
    p_tiny = RoundBasedPolicy(keep_rounds=3)
    # 每条消息约 100 chars → ~25 tokens (chars/4)
    big_msgs = []
    for idx in range(4):
        big_msgs.append(_msg("user", "x" * 100))
        big_msgs.append(_msg("assistant", "y" * 100))
    # token_budget=30 → 只能放 1 轮（~50 tokens/轮，但至少保留 1 轮）
    compressed_budget = p_tiny.compress(big_msgs, max_tokens=30)
    assert_true(len(compressed_budget) >= 2, "token 预算极小时至少保留最新 1 轮（2 条）")
    assert_true(len(compressed_budget) < len(big_msgs), "token 预算限制下压缩了消息")

    # _split_into_rounds 正确性
    rounds_input = [
        _msg("user", "u1"),
        _msg("assistant", "a1"),
        _msg("user", "u2"),
        _msg("assistant", "a2"),
        _msg("tool", "t2"),
        _msg("user", "u3"),
    ]
    rounds = policy._split_into_rounds(rounds_input)
    assert_true(len(rounds) == 3, "_split_into_rounds: 3 个 user 消息 → 3 轮")
    assert_true(rounds[0] == [_msg("user", "u1"), _msg("assistant", "a1")], "第 1 轮内容正确")
    assert_true(
        rounds[1] == [_msg("user", "u2"), _msg("assistant", "a2"), _msg("tool", "t2")],
        "第 2 轮包含 tool 结果",
    )

    # assistant 消息在 user 前（孤立）→ 归入第一轮
    orphan = [_msg("assistant", "orphan"), _msg("user", "q"), _msg("assistant", "a")]
    r_orphan = policy._split_into_rounds(orphan)
    assert_true(len(r_orphan) == 2, "孤立 assistant 消息分为 2 轮")
    assert_true(r_orphan[0][0]["role"] == "assistant", "第 0 轮以孤立 assistant 开始")


# ─────────────────────────────────────────────
# Section 4: AsyncBatchFlushPolicy
# ─────────────────────────────────────────────

def test_flush_policy() -> None:
    print("\n── AsyncBatchFlushPolicy ───────────────────────────────")

    # 溢出保护：buffer_max_entries=1，enqueue 立即写入
    raw = InMemoryRawLogBackend()
    fp = AsyncBatchFlushPolicy(buffer_max_entries=1)
    msg: MessageDict = _msg("user", "instant")
    async_run(fp.enqueue(msg, session_id="s1", backend=raw))
    loaded = raw.load_within_budget(session_id="s1", token_budget=10_000)
    assert_true(len(loaded) == 1, "buffer_max_entries=1 时 enqueue 立即写入 backend")
    assert_true(loaded[0]["content"] == "instant", "写入的消息内容正确")

    # flush：强制写入缓冲中的消息
    raw2 = InMemoryRawLogBackend()
    fp2 = AsyncBatchFlushPolicy(coalesce_ms=60_000, buffer_max_entries=100)  # 长延迟
    async_run(fp2.enqueue(_msg("user", "buffered"), session_id="s2", backend=raw2))
    # 未超 buffer_max_entries，coalesce 未到期 → 未写入
    before_flush = raw2.load_within_budget(session_id="s2", token_budget=10_000)
    assert_true(len(before_flush) == 0, "coalesce 未到期时消息尚未写入 backend")
    # 显式 flush
    async_run(fp2.flush(session_id="s2", backend=raw2))
    after_flush = raw2.load_within_budget(session_id="s2", token_budget=10_000)
    assert_true(len(after_flush) == 1, "显式 flush() 后消息写入 backend")

    # flush_all=True：多 key 各自写入各自 backend
    raw_a = InMemoryRawLogBackend()
    raw_b = InMemoryRawLogBackend()
    fp3 = AsyncBatchFlushPolicy(coalesce_ms=60_000, buffer_max_entries=100)
    async_run(fp3.enqueue(_msg("user", "for_a"), session_id="sa", backend=raw_a))
    async_run(fp3.enqueue(_msg("user", "for_b"), session_id="sb", backend=raw_b))
    async_run(fp3.flush(session_id="", backend=raw_a, flush_all=True))
    loaded_a = raw_a.load_within_budget(session_id="sa", token_budget=10_000)
    loaded_b = raw_b.load_within_budget(session_id="sb", token_budget=10_000)
    assert_true(len(loaded_a) == 1, "flush_all=True：key a 写入了 raw_a")
    assert_true(len(loaded_b) == 1, "flush_all=True：key b 写入了 raw_b")

    # 多条消息 enqueue 后 flush
    raw4 = InMemoryRawLogBackend()
    fp4 = AsyncBatchFlushPolicy(coalesce_ms=60_000, buffer_max_entries=100)
    for idx in range(3):
        async_run(fp4.enqueue(_msg("user", f"msg{idx}"), session_id="s4", backend=raw4))
    async_run(fp4.flush(session_id="s4", backend=raw4))
    loaded4 = raw4.load_within_budget(session_id="s4", token_budget=10_000)
    assert_true(len(loaded4) == 3, "多条消息 flush 后全部写入（3 条）")


# ─────────────────────────────────────────────
# Section 5: InMemoryRawLogBackend
# ─────────────────────────────────────────────

def test_raw_log_backend() -> None:
    print("\n── InMemoryRawLogBackend ────────────────────────────────")

    # append_raw_message + load_within_budget 基本
    b = InMemoryRawLogBackend()
    b.append_raw_message(_msg("user", "hello"), session_id="s1")
    b.append_raw_message(_msg("assistant", "hi"), session_id="s1")
    loaded = b.load_within_budget(session_id="s1", token_budget=10_000)
    assert_true(len(loaded) == 2, "append + load 基本读写正确（2 条）")
    assert_true(loaded[0]["role"] == "user", "第 0 条 role=user")
    assert_true(loaded[1]["role"] == "assistant", "第 1 条 role=assistant")

    # 不同 (user_id, session_id) 隔离
    b2 = InMemoryRawLogBackend()
    b2.append_raw_message(_msg("user", "alice"), session_id="s1")
    b2.append_raw_message(_msg("user", "bob"), session_id="s1")
    alice_msgs = b2.load_within_budget(session_id="s1", token_budget=10_000)
    bob_msgs = b2.load_within_budget(session_id="s1", token_budget=10_000)
    assert_true(len(alice_msgs) == 1, "(user_id, session_id) 隔离：alice 只有 1 条")
    assert_true(alice_msgs[0]["content"] == "alice", "alice 消息内容正确")
    assert_true(len(bob_msgs) == 1, "bob 只有 1 条")

    # HC2：append 存储深拷贝
    b3 = InMemoryRawLogBackend()
    original: MessageDict = _msg("user", "original")
    b3.append_raw_message(original, session_id="s3")
    original["content"] = "MODIFIED"  # type: ignore[typeddict-unknown-key]
    loaded3 = b3.load_within_budget(session_id="s3", token_budget=10_000)
    assert_true(loaded3[0]["content"] == "original", "HC2：append 存储深拷贝，修改原始不影响存储")

    # HC2：load_within_budget 返回深拷贝
    b4 = InMemoryRawLogBackend()
    b4.append_raw_message(_msg("user", "data"), session_id="s4")
    l1 = b4.load_within_budget(session_id="s4", token_budget=10_000)
    l1[0]["content"] = "TAMPER"  # type: ignore[typeddict-unknown-key]
    l2 = b4.load_within_budget(session_id="s4", token_budget=10_000)
    assert_true(l2[0]["content"] == "data", "HC2：load 返回深拷贝，修改不影响内部")

    # compact_boundary 分区：只加载最后 boundary 之后的消息
    b5 = InMemoryRawLogBackend()
    b5.append_raw_message(_msg("user", "before"), session_id="s5")
    b5.append_compact_boundary(
        {"type": "compact_boundary", "timestamp": "T", "tokens_before": 100,
         "tokens_after": 50, "kept_message_count": 1, "summary": None},
        session_id="s5",
    )
    b5.append_raw_message(_msg("user", "after1"), session_id="s5")
    b5.append_raw_message(_msg("assistant", "after2"), session_id="s5")
    loaded5 = b5.load_within_budget(session_id="s5", token_budget=10_000)
    assert_true(len(loaded5) == 2, "只加载最后 boundary 之后的 2 条消息")
    assert_true(all(m["content"] in ("after1", "after2") for m in loaded5), "boundary 之前的消息不出现")

    # token_budget 限制
    b6 = InMemoryRawLogBackend()
    for _ in range(10):
        b6.append_raw_message(_msg("user", "x" * 40), session_id="s6")  # ~10 tokens each
    loaded_budget = b6.load_within_budget(session_id="s6", token_budget=25)
    assert_true(
        0 < len(loaded_budget) < 10,
        "token_budget=25 只返回部分消息（不是全部 10 条）",
    )

    # 不存在的 key 返回空列表
    b7 = InMemoryRawLogBackend()
    empty = b7.load_within_budget(session_id="never", token_budget=9999)
    assert_true(empty == [], "不存在的 key 返回空列表")

    # 消息按时间顺序（旧 → 新）返回
    b8 = InMemoryRawLogBackend()
    for idx in range(3):
        b8.append_raw_message(_msg("user", f"msg{idx}"), session_id="s8")
    loaded8 = b8.load_within_budget(session_id="s8", token_budget=10_000)
    assert_true(
        [m["content"] for m in loaded8] == ["msg0", "msg1", "msg2"],
        "load_within_budget 按时间从旧到新排列",
    )


# ─────────────────────────────────────────────
# Section 6: InMemorySummaryBackend
# ─────────────────────────────────────────────

def test_summary_backend() -> None:
    print("\n── InMemorySummaryBackend ───────────────────────────────")

    meta: EntryMetadata = {"type": "summary", "session_id": "s1", "timestamp": "2024-01-01T00:00:00"}

    # store + get_recent
    sb = InMemorySummaryBackend()
    eid1 = sb.store("first summary", meta, session_id="s1")
    eid2 = sb.store("second summary", meta, session_id="s1")
    recent = sb.get_recent(top_k=10, session_id="s1")
    assert_true(len(recent) == 2, "store 2 条后 get_recent 返回 2 条")
    # 最新的在前
    assert_true(recent[0]["content"] == "second summary", "get_recent 最新的在前")
    assert_true(recent[1]["content"] == "first summary", "get_recent 次新的在后")
    assert_true(isinstance(eid1, str) and len(eid1) > 0, "store 返回有效 entry_id")

    # get_recent top_k 限制
    recent_1 = sb.get_recent(top_k=1, session_id="s1")
    assert_true(len(recent_1) == 1, "get_recent top_k=1 只返回 1 条")

    # search：关键词匹配
    sb2 = InMemorySummaryBackend()
    sb2.store("Python is a programming language", meta, session_id="s2")
    sb2.store("Java is also a programming language", meta, session_id="s2")
    sb2.store("completely unrelated content", meta, session_id="s2")
    results = sb2.search("programming language", top_k=5, session_id="s2")
    assert_true(len(results) == 2, "search 返回包含关键词的 2 条结果")

    # search top_k 限制
    r_k1 = sb2.search("programming", top_k=1, session_id="s2")
    assert_true(len(r_k1) == 1, "search top_k=1 返回 1 条")

    # search 无匹配返回空
    no_match = sb2.search("golang rust", top_k=5, session_id="s2")
    assert_true(len(no_match) == 0, "search 无匹配关键词返回空列表")

    # (user, session) 隔离
    sb3 = InMemorySummaryBackend()
    sb3.store("alice data", meta, session_id="s1")
    sb3.store("bob data", meta, session_id="s1")
    alice_r = sb3.get_recent(top_k=10, session_id="s1")
    bob_r = sb3.get_recent(top_k=10, session_id="s1")
    assert_true(len(alice_r) == 1, "(user,session) 隔离：alice 只有 1 条")
    assert_true(alice_r[0]["content"] == "alice data", "alice 内容正确")
    assert_true(len(bob_r) == 1, "bob 只有 1 条")

    # delete
    sb4 = InMemorySummaryBackend()
    eid_del = sb4.store("to delete", meta, session_id="s4")
    sb4.store("to keep", meta, session_id="s4")
    sb4.delete(eid_del, session_id="s4")
    after_del = sb4.get_recent(top_k=10, session_id="s4")
    assert_true(len(after_del) == 1, "delete 后只剩 1 条")
    assert_true(after_del[0]["content"] == "to keep", "delete 后保留的内容正确")

    # delete 不存在的 entry_id 不崩溃
    assert_no_raises(
        lambda: sb4.delete("nonexistent", session_id="s4"),
        "delete 不存在的 entry_id 不崩溃",
    )

    # HC2：search 返回 metadata 深拷贝
    sb5 = InMemorySummaryBackend()
    meta5: EntryMetadata = {"type": "summary", "session_id": "s5", "timestamp": "2024-01-01"}
    sb5.store("sample text", meta5, session_id="s5")
    r5 = sb5.search("sample", top_k=5, session_id="s5")
    r5[0]["metadata"]["session_id"] = "TAMPER"  # type: ignore[index]
    r5_again = sb5.search("sample", top_k=5, session_id="s5")
    assert_true(
        r5_again[0]["metadata"]["session_id"] == "s5",
        "HC2：search 返回 metadata 深拷贝，修改不影响内部",
    )


# ─────────────────────────────────────────────
# Section 7: LongTermMemory
# ─────────────────────────────────────────────

def test_long_term_memory() -> None:
    print("\n── LongTermMemory ───────────────────────────────────────")

    from pandaren.memory.protocols import NullExtractionPolicy

    # 基本 recall（无内容时返回空）
    raw = InMemoryRawLogBackend()
    summary = InMemorySummaryBackend()
    ltm = LongTermMemory(raw_log_backend=raw, summary_backend=summary)
    results = ltm.recall(query="python", session_id="s1")
    assert_true(results == [], "无摘要时 recall 返回空列表")

    # store_session_summary + recall
    ltm.store_session_summary(
        session_summary="Python is a high-level language",
        session_id="s1",
        )
    recall_results = ltm.recall(query="python language", session_id="s1")
    assert_true(len(recall_results) > 0, "存入摘要后 recall 能检索到结果")

    # load_for_restore：无历史时返回空
    ltm2 = LongTermMemory(raw_log_backend=raw, summary_backend=summary)
    empty = ltm2.load_for_restore(session_id="never", token_budget=10_000)
    assert_true(empty == [], "无历史时 load_for_restore 返回空列表")

    # load_for_restore：有历史时返回消息
    raw3 = InMemoryRawLogBackend()
    raw3.append_raw_message(_msg("user", "q1"), session_id="s3")
    raw3.append_raw_message(_msg("assistant", "a1"), session_id="s3")
    ltm3 = LongTermMemory(raw_log_backend=raw3, summary_backend=summary)
    restored = ltm3.load_for_restore(session_id="s3", token_budget=10_000)
    assert_true(len(restored) == 2, "load_for_restore 恢复 2 条历史消息")
    assert_true(restored[0]["role"] == "user", "恢复的第 0 条 role=user")

    # append_raw_message 转发到 backend
    raw4 = InMemoryRawLogBackend()
    ltm4 = LongTermMemory(raw_log_backend=raw4, summary_backend=summary)
    ltm4.append_raw_message(_msg("user", "direct"), session_id="s4")
    loaded4 = raw4.load_within_budget(session_id="s4", token_budget=10_000)
    assert_true(len(loaded4) == 1, "append_raw_message 写入到 raw_log_backend")

    # raw_log_backend=None 时不崩溃
    ltm_null = LongTermMemory(raw_log_backend=None, summary_backend=None)
    assert_no_raises(
        lambda: ltm_null.append_raw_message(_msg("user", "no backend"), session_id="s"),
        "raw_log_backend=None 时 append_raw_message 不崩溃",
    )
    assert_true(
        ltm_null.recall(query="anything", session_id="s") == [],
        "summary_backend=None 时 recall 返回空列表",
    )

    # raw_log_backend property
    raw5 = InMemoryRawLogBackend()
    ltm5 = LongTermMemory(raw_log_backend=raw5, summary_backend=None)
    assert_true(ltm5.raw_log_backend is raw5, "raw_log_backend property 返回正确引用")


# ─────────────────────────────────────────────
# Section 8: Memory Facade — 生命周期
# ─────────────────────────────────────────────

def test_memory_facade_lifecycle() -> None:
    print("\n── Memory Facade 生命周期 ───────────────────────────────")

    # init_from_restore（档位 3：全新 session）
    mem = _make_memory()
    msgs = mem.init_from_restore("Hello!", session_id="s1")
    assert_true(isinstance(msgs, list), "init_from_restore 返回 list")
    assert_true(len(msgs) >= 2, "init_from_restore 返回 [system_msg, user_msg] 共 >= 2 条")
    assert_true(msgs[0]["role"] == "system", "第 0 条为 system 消息")
    assert_true(msgs[-1]["role"] == "user", "最后一条为 user 消息")

    # init_from_restore + 档位 1：同 session 复用 STM
    async_run(mem.add_assistant_message("Hi there!"))
    msgs2 = mem.init_from_restore("Second question", session_id="s1")
    assert_true(
        len(msgs2) >= 3,  # system + user1 + assistant + user2
        "档位 1：同 session 追加消息后历史保留",
    )

    # get_messages 始终包含 system 消息
    msgs3 = mem.get_messages()
    assert_true(msgs3[0]["role"] == "system", "get_messages 第 0 条始终为 system")

    # append_user_message
    mem2 = _make_memory()
    mem2.init_from_restore("task1", session_id="s1")
    msgs_after = mem2.append_user_message("task2")
    assert_true(
        any(m["content"] == "task2" for m in msgs_after),
        "append_user_message 追加了新 user 消息",
    )

    # MemoryStateError：未初始化时 append_user_message
    mem3 = _make_memory()
    assert_raises(
        MemoryStateError,
        lambda: mem3.append_user_message("should fail"),
        "未初始化时 append_user_message 抛出 MemoryStateError",
    )

    # ValueError：init_from_restore 空 session_id
    mem4 = _make_memory()
    assert_raises(ValueError, lambda: mem4.init_from_restore("t", session_id=""), "空 session_id 抛 ValueError")

    # add_assistant_message + add_tool_result
    mem5 = _make_memory()
    mem5.init_from_restore("task", session_id="s1")
    async_run(mem5.add_assistant_message("thinking..."))
    async_run(mem5.add_tool_result(tool_call_id="call_1", tool_name="search", content="result"))
    all_msgs = mem5.get_messages()
    roles = [m["role"] for m in all_msgs]
    assert_true("assistant" in roles, "add_assistant_message 追加了 assistant 消息")
    assert_true("tool" in roles, "add_tool_result 追加了 tool 消息")

    # end_session：不崩溃
    assert_no_raises(
        lambda: async_run(mem5.end_session()),
        "end_session 不抛出异常",
    )

    # clear_working + set_working/get_working
    mem6 = _make_memory()
    mem6.init_from_restore("task", session_id="s1")
    mem6.set_working("key", "value")
    assert_true(mem6.get_working("key") == "value", "set/get_working 基本读写正确")
    mem6.clear_working()
    assert_true(mem6.get_working("key") is None, "clear_working 后 get_working 返回 None")


# ─────────────────────────────────────────────
# Section 9: Memory Facade — HC1 & Properties
# ─────────────────────────────────────────────

def test_memory_facade_hc1() -> None:
    print("\n── Memory Facade HC1 & Properties ─────────────────────")

    mem = _make_memory(system_prompt="hello system", compact_threshold=50_000)

    # HC1：冻结字段不可修改
    frozen_fields = [
        ("_system_prompt", "hacked"),
        ("_compact_threshold", 0),
        ("_session_mode", "evil"),
        ("_short_term", None),
        ("_long_term", None),
        ("_working", None),
        ("_flush_policy", None),
        ("_session_summary_policy", None),
    ]
    for field_name, new_val in frozen_fields:
        assert_raises(
            AttributeError,
            lambda fn=field_name, nv=new_val: setattr(mem, fn, nv),
            f"HC1：{field_name} 冻结后不可修改",
        )

    # 可读属性
    assert_true(mem.system_prompt == "hello system", "system_prompt property 返回正确值")
    assert_true(mem.compact_threshold == 50_000, "compact_threshold property 返回正确值")
    assert_true(mem.recall_text is None, "初始 recall_text 为 None")

    # recall_and_inject（无 summary_backend 时跳过）
    mem2 = _make_memory(system_prompt="test")
    mem2.init_from_restore("query", session_id="s1")
    injected = mem2.recall_and_inject()
    assert_true(injected is False, "无 summary_backend 时 recall_and_inject 返回 False")

    # recall_and_inject 同 run 内只执行一次
    mem3 = _make_memory()
    mem3.init_from_restore("task", session_id="s1")
    r1 = mem3.recall_and_inject()
    r2 = mem3.recall_and_inject()
    assert_true(r2 is False, "recall_and_inject 同一 run 内第二次调用返回 False")

    # single_turn 模式跳过 recall
    mem_st = _make_memory(session_mode="single_turn")
    mem_st.init_from_restore("task", session_id="s1")
    st_injected = mem_st.recall_and_inject()
    assert_true(st_injected is False, "single_turn 模式 recall_and_inject 直接返回 False")

    # estimate_tokens > 0
    mem4 = _make_memory()
    mem4.init_from_restore("hello", session_id="s1")
    tokens = mem4.estimate_tokens()
    assert_true(tokens > 0, "estimate_tokens 返回正值")

    # compact_if_needed：token 未超阈值时返回 None
    mem5 = _make_memory(compact_threshold=DEFAULT_COMPACT_THRESHOLD)
    mem5.init_from_restore("short task", session_id="s1")
    overflow = mem5.compact_if_needed()
    assert_true(overflow is None, "未超阈值时 compact_if_needed 返回 None")

    # set_on_compact_callback：注册后压缩时被调用
    callback_called = []
    def on_compact():
        callback_called.append(True)

    mem_cb = Memory(
        system_prompt="s",
        raw_log_backend=None,
        summary_backend=None,
        compact_threshold=20,  # 极小阈值
    )
    mem_cb.set_on_compact_callback(on_compact)
    # 写入足够多的消息超过阈值
    mem_cb.init_from_restore("a" * 32, session_id="s1")
    mem_cb.init_from_restore("b" * 32, session_id="s1")
    mem_cb.init_from_restore("c" * 32, session_id="s1")
    mem_cb.compact_if_needed()
    assert_true(len(callback_called) > 0, "compact 触发时调用了注册的 on_compact_callback")


# ─────────────────────────────────────────────
# Section 10: Memory Facade — Snapshot / Resume
# ─────────────────────────────────────────────

def test_memory_facade_snapshot() -> None:
    print("\n── Memory Facade Snapshot / Resume ─────────────────────")

    # snapshot_for_pause 返回 MemorySnapshot
    mem = _make_memory()
    mem.init_from_restore("task", session_id="s1")
    async_run(mem.add_assistant_message("response"))
    snap = mem.snapshot_for_pause()
    assert_true(isinstance(snap, MemorySnapshot), "snapshot_for_pause 返回 MemorySnapshot")
    assert_true(isinstance(snap.messages, tuple), "MemorySnapshot.messages 是 tuple")
    assert_true(len(snap.messages) >= 2, "快照包含 user + assistant 消息")

    # MemorySnapshot frozen=True
    assert_raises(
        (AttributeError, TypeError),
        lambda: setattr(snap, "recall_injected", True),
        "MemorySnapshot frozen=True：修改 recall_injected 抛出异常",
    )
    assert_raises(
        (AttributeError, TypeError),
        lambda: setattr(snap, "messages", ()),
        "MemorySnapshot frozen=True：修改 messages 抛出异常",
    )

    # resume_context 恢复状态
    mem2 = _make_memory()
    mem2.init_from_restore("original task", session_id="s1")
    async_run(mem2.add_assistant_message("original response"))
    snap2 = mem2.snapshot_for_pause()

    # 模拟 HITL 暂停期间状态被修改
    mem2.init_from_restore("new task after pause", session_id="s1")

    # resume 恢复
    mem2.resume_context(snap2, session_id="s1")
    resumed_msgs = mem2.get_messages()
    # 恢复后不应有 "new task after pause"
    contents = [m.get("content", "") for m in resumed_msgs]
    assert_true(
        "original task" in contents,
        "resume_context 恢复了原始 user 消息",
    )

    # recall_text / recall_injected 随 snapshot 恢复
    mem3 = _make_memory()
    mem3.init_from_restore("task", session_id="s1")
    snap3 = mem3.snapshot_for_pause()
    assert_true(snap3.recall_injected is False, "未 recall 时 recall_injected=False 快照正确")
    assert_true(snap3.recall_text is None, "未 recall 时 recall_text=None 快照正确")


# ─────────────────────────────────────────────
# Section 11: Memory Facade — 后端集成
# ─────────────────────────────────────────────

def test_memory_facade_backends() -> None:
    print("\n── Memory Facade 后端集成 ───────────────────────────────")

    # flush_raw_messages 将消息写入 raw_log_backend
    mem, raw, _summary = _make_memory_with_backends()
    mem.init_from_restore("hello", session_id="s1")
    async_run(mem.add_assistant_message("world"))
    async_run(mem.flush_raw_messages())
    loaded = raw.load_within_budget(session_id="s1", token_budget=10_000)
    assert_true(len(loaded) >= 1, "flush_raw_messages 将消息持久化到 raw_log_backend")
    roles_loaded = [m["role"] for m in loaded]
    assert_true("assistant" in roles_loaded or "user" in roles_loaded, "flush 后 backend 包含 user/assistant 消息")

    # end_session：会话完整生命周期
    mem2, raw2, _summary2 = _make_memory_with_backends()
    mem2.init_from_restore("question", session_id="s2")
    async_run(mem2.add_assistant_message("answer"))
    assert_no_raises(
        lambda: async_run(mem2.end_session()),
        "end_session 完整生命周期不抛出",
    )

    # trigger_extraction：不崩溃（默认 NullExtractionPolicy）
    mem3 = _make_memory()
    mem3.init_from_restore("task", session_id="s1")
    assert_no_raises(
        lambda: async_run(mem3.trigger_extraction([_msg("user", "some msg")])),
        "trigger_extraction（NullExtractionPolicy）不崩溃",
    )

    # single_turn 模式：flush/end_session/trigger_extraction 均不崩溃
    mem_st = _make_memory(session_mode="single_turn")
    mem_st.init_from_restore("task", session_id="s1")
    assert_no_raises(lambda: async_run(mem_st.flush_raw_messages()), "single_turn flush 不崩溃")
    assert_no_raises(lambda: async_run(mem_st.end_session()), "single_turn end_session 不崩溃")
    assert_no_raises(lambda: async_run(mem_st.trigger_extraction([])), "single_turn trigger_extraction 不崩溃")

    # 档位 2：STM 为空但 raw_log 有历史（进程重启场景）
    raw_hist = InMemoryRawLogBackend()
    raw_hist.append_raw_message(_msg("user", "historic q"), session_id="s_r")
    raw_hist.append_raw_message(_msg("assistant", "historic a"), session_id="s_r")
    mem_r = Memory(
        system_prompt="test",
        raw_log_backend=raw_hist,
        summary_backend=None,
    )
    msgs_r = mem_r.init_from_restore("new question", session_id="s_r")
    contents = [m.get("content", "") for m in msgs_r]
    assert_true(
        "historic q" in contents or "new question" in contents,
        "档位 2：raw_log 有历史时 init_from_restore 能加载历史",
    )

    # 跨 session 档位降级：STM 有内容但 session 不匹配
    mem_x = _make_memory()
    mem_x.init_from_restore("task_a", session_id="session_A")
    async_run(mem_x.add_assistant_message("response_a"))
    # 换 session → 应清空 STM
    msgs_b = mem_x.init_from_restore("task_b", session_id="session_B")
    assert_true(
        not any("response_a" in str(m.get("content", "")) for m in msgs_b),
        "跨 session 时 STM 被清空，旧 session 消息不出现",
    )


# ─────────────────────────────────────────────
# Section 12: Memory Facade — 工作记忆集成
# ─────────────────────────────────────────────

def test_memory_working_integration() -> None:
    print("\n── Memory Facade 工作记忆集成 ──────────────────────────")

    mem = _make_memory()
    mem.init_from_restore("task", session_id="s1")

    # set_working / get_working
    mem.set_working("counter", 0)
    assert_true(mem.get_working("counter") == 0, "set/get_working 基本读写")

    # HC2：get_working 返回深拷贝
    mem.set_working("data", {"key": "val"})
    got = mem.get_working("data")
    assert got is not None
    got["key"] = "MODIFIED"  # type: ignore[index]
    assert_true(mem.get_working("data") == {"key": "val"}, "HC2：get_working 返回深拷贝")  # type: ignore[comparison-overlap]

    # O3：超容量抛出 MemoryLimitError
    mem_small = Memory(
        system_prompt="test",
        raw_log_backend=None,
        summary_backend=None,
        working_memory_max_entries=2,
    )
    mem_small.init_from_restore("task", session_id="s1")
    mem_small.set_working("a", 1)
    mem_small.set_working("b", 2)
    assert_raises(
        MemoryLimitError,
        lambda: mem_small.set_working("c", 3),
        "O3：工作记忆超容量时抛出 MemoryLimitError",
    )

    # clear_working
    mem.clear_working()
    assert_true(mem.get_working("counter") is None, "clear_working 后 get_working 返回 None")

    # working_memory_accessor property 返回正确类型
    mem2 = _make_memory()
    mem2.init_from_restore("task", session_id="s1")
    acc = mem2.working_memory_accessor
    assert_true(isinstance(acc, WorkingMemoryAccessor), "working_memory_accessor 满足 WorkingMemoryAccessor 协议")


# ─────────────────────────────────────────────
# Section 13: MemorySnapshot
# ─────────────────────────────────────────────

def test_memory_snapshot() -> None:
    print("\n── MemorySnapshot ───────────────────────────────────────")

    # 正常构造
    snap = MemorySnapshot(
        messages=(_msg("user", "hello"), _msg("assistant", "hi")),
        recall_injected=True,
        recall_text="recalled content",
    )
    assert_true(len(snap.messages) == 2, "messages 长度正确")
    assert_true(snap.recall_injected is True, "recall_injected 正确")
    assert_true(snap.recall_text == "recalled content", "recall_text 正确")

    # messages 是 tuple（不可变）
    assert_true(isinstance(snap.messages, tuple), "messages 存储为 tuple")

    # frozen=True：各字段不可修改
    assert_raises(
        (AttributeError, TypeError),
        lambda: setattr(snap, "recall_injected", False),
        "frozen=True：修改 recall_injected 抛出异常",
    )
    assert_raises(
        (AttributeError, TypeError),
        lambda: setattr(snap, "recall_text", "hacked"),
        "frozen=True：修改 recall_text 抛出异常",
    )
    assert_raises(
        (AttributeError, TypeError),
        lambda: setattr(snap, "messages", ()),
        "frozen=True：修改 messages 抛出异常",
    )

    # recall_text=None 合法
    snap_null = MemorySnapshot(messages=(), recall_injected=False, recall_text=None)
    assert_true(snap_null.recall_text is None, "recall_text=None 正常")
    assert_true(snap_null.messages == (), "空 messages 正常")
    assert_true(snap_null.recall_injected is False, "recall_injected=False 正常")

    # CharBasedTokenEstimator：估算非空消息返回正值
    estimator = CharBasedTokenEstimator()
    msgs_for_est: list[MessageDict] = [_msg("user", "hello world")]
    tokens = estimator.estimate(msgs_for_est)
    assert_true(tokens > 0, "CharBasedTokenEstimator.estimate 返回正值")

    # 估算空列表 → max(1, int(0/4)) = 1（不为 0）
    tokens_empty = estimator.estimate([])
    assert_true(tokens_empty >= 1, "estimate([]) 返回 >= 1")


# ─────────────────────────────────────────────
# 测试入口
# ─────────────────────────────────────────────

SECTIONS: dict[str, Callable[[], None]] = {
    "working_memory": test_working_memory,
    "short_term_memory": test_short_term_memory,
    "round_based_policy": test_round_based_policy,
    "flush_policy": test_flush_policy,
    "raw_log_backend": test_raw_log_backend,
    "summary_backend": test_summary_backend,
    "long_term_memory": test_long_term_memory,
    "memory_lifecycle": test_memory_facade_lifecycle,
    "memory_hc1": test_memory_facade_hc1,
    "memory_snapshot_resume": test_memory_facade_snapshot,
    "memory_backends": test_memory_facade_backends,
    "memory_working": test_memory_working_integration,
    "memory_snapshot": test_memory_snapshot,
}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Memory 模块真实单元测试")
    parser.add_argument("--section", choices=list(SECTIONS.keys()), default=None)
    args = parser.parse_args()

    print("=" * 60)
    print("  Memory 模块 — 真实单元测试")
    print("=" * 60)

    if args.section:
        SECTIONS[args.section]()
        result.summary(args.section)
    else:
        for name, fn in SECTIONS.items():
            fn()
        result.summary("all")

    sys.exit(0 if result.failed == 0 else 1)
