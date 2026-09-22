"""上下文组成拆解：四段之和必须等于真实 prompt_tokens（history 取残差）。"""

from __future__ import annotations

from pandaren.engine.run_core import estimate_context_breakdown


class _FakeMemory:
    """最小 memory 替身：estimate_text 用 chars/4，attachments 空。"""

    def __init__(self, attachments: tuple[dict, ...] = ()) -> None:
        self._atts = attachments

    def estimate_text(self, text: str) -> int:
        return len(text) // 4

    @property
    def post_compact_attachments(self) -> tuple[dict, ...]:
        return self._atts


def test_breakdown_sums_to_real_total():
    msgs = [{"role": "system", "content": "x" * 400}, {"role": "user", "content": "hi"}]
    tools = [{"type": "function", "function": {"name": "f"}}]
    b = estimate_context_breakdown(_FakeMemory(), msgs, tools, 10_000)

    assert b is not None
    assert b["system"] == 100                      # 400 chars / 4
    assert sum(b.values()) == 10_000               # 真实总量由 history 残差吸收
    assert b["history"] == 10_000 - b["system"] - b["tools"]


def test_breakdown_counts_attachments_separately():
    msgs = [{"role": "system", "content": "y" * 40}]
    atts = ({"content": "z" * 200},)
    b = estimate_context_breakdown(_FakeMemory(atts), msgs, None, 5_000)

    assert b is not None
    assert b["attachments"] == 50
    assert b["tools"] == 0
    assert sum(b.values()) == 5_000


def test_breakdown_degrades_without_real_total():
    """无真实 usage（0）→ None；绝不产出会误导 UI 的数据。"""
    assert estimate_context_breakdown(_FakeMemory(), [{"role": "system", "content": "a"}], None, 0) is None
    assert estimate_context_breakdown(_FakeMemory(), [], None, -1) is None


def test_breakdown_handles_missing_system_message():
    b = estimate_context_breakdown(
        _FakeMemory(), [{"role": "user", "content": "hello"}], None, 1_000,
    )
    assert b is not None
    assert b["system"] == 0
    assert b["history"] == 1_000


def test_breakdown_history_never_negative():
    """小头估算之和超过真实总量时，history 归零（不出现负数段）。"""
    msgs = [{"role": "system", "content": "x" * 4_000}]   # 估算 1000
    b = estimate_context_breakdown(_FakeMemory(), msgs, None, 10)
    assert b is not None
    assert b["history"] == 0
