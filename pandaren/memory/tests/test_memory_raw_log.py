"""Memory raw_log 双写链路测试（组 A：timestamp 注入 + append_user_message 同步直写）。

覆盖设计用例：
  - A1  _with_timestamp：无 timestamp 注入 UTC ISO；已有 timestamp 幂等保留；输入不被原地修改（HC2）
  - A2  append_user_message → raw_log 同步直写：消息含 timestamp，run_id/step 透传

组件层用可审计的 Recording Fake（有真实状态：记录 append 调用），
断言 Memory 写给 RawLogBackend 的「调用契约」；真实 SQLite 持久化由组 E 覆盖。
"""

from __future__ import annotations

from datetime import datetime, timezone

from pandaren.memory.memory import Memory


class _RecordingRawLogBackend:
    """记录 append_raw_message 调用的可审计 Fake（含 load 供 init_from_restore 恢复路径）。"""

    def __init__(self) -> None:
        self.appends: list[tuple[dict, str, str, int | None]] = []

    def append_raw_message(
        self, message: dict, session_id: str, run_id: str = "", step: int | None = None,
    ) -> None:
        self.appends.append((message, session_id, run_id, step))

    def load_within_budget(self, session_id: str, token_budget: int) -> list[dict]:
        return []


# ─────────────────────────────────────────────
# A1: _with_timestamp（inv-1 确定性 + Risk-5 幂等）
# ─────────────────────────────────────────────


def test_with_timestamp_injects_utc_iso_timestamp():
    # Risk-1 无 timestamp → 注入可解析的 UTC ISO；原字段保留
    out = Memory._with_timestamp({"role": "user", "content": "hi"})

    assert out["role"] == "user"
    assert out["content"] == "hi"
    ts = datetime.fromisoformat(out["timestamp"])
    assert ts.tzinfo == timezone.utc


def test_with_timestamp_keeps_existing_timestamp():
    # Risk-5 幂等：已有 timestamp 原样返回，不覆盖
    msg = {"role": "user", "content": "x", "timestamp": "2024-01-01T00:00:00+00:00"}

    out = Memory._with_timestamp(msg)

    assert out is msg
    assert out["timestamp"] == "2024-01-01T00:00:00+00:00"


def test_with_timestamp_does_not_mutate_input():
    # HC2 深拷贝：注入 timestamp 不改动原消息（不污染 STM 内部）
    msg = {"role": "user", "content": "hi"}

    Memory._with_timestamp(msg)

    assert "timestamp" not in msg


# ─────────────────────────────────────────────
# A2: append_user_message 同步直写 raw_log（Risk-4 双写完整性）
# ─────────────────────────────────────────────


def test_append_user_message_writes_timestamped_msg_with_run_context():
    backend = _RecordingRawLogBackend()
    mem = Memory(system_prompt="你是助手", raw_log_backend=backend)

    mem.init_from_restore("开场白", "s1")
    mem.set_run_context("run-9", 3)
    mem.append_user_message("你好")

    # init_from_restore 直写 1 条；append_user_message 再直写 1 条 → 断言最后一条
    msg, session_id, run_id, step = backend.appends[-1]
    assert session_id == "s1"
    assert run_id == "run-9"
    assert step == 3
    assert msg["role"] == "user"
    assert msg["content"] == "你好"
    # timestamp 已注入（格式校验，不断具体值）
    ts = datetime.fromisoformat(msg["timestamp"])
    assert ts.tzinfo == timezone.utc
