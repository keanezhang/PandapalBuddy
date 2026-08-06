"""pandapal/scheduler/tests/test_executor_resume_model.py — resume 保持同一模型。

事故：逐条消息选了非默认模型（如 deepseek）的会话，答完 ask_user 续跑时切回默认模型
（dashscope），进而（在 dashscope 额度耗尽时）被预算停机。根因不是存储模式——RunState
经 pickle 完整 round-trip、sqlite BLOB 也原样存取——而是 **executor 暂停时漏把 model_id
写进 RunState.metadata**（只写了 active_app_id）。resume 因此读不到 model_id → 回落默认。

这些用例锁定 AgentExecutor._persist_resume_model：暂停前把 model_id 补进 RunState.metadata，
并验证它能经「pickle → sqlite BLOB → 反序列化」完整恢复（证伪「sqlite 丢数据」假设）。
"""

from __future__ import annotations

import pickle
import sqlite3

from pandapal.scheduler.executor import AgentExecutor
from pandaren.engine.models import RunState


class _FakeSE:
    """最小 stream event：data 为 dict，可含 run_state。"""
    def __init__(self, data) -> None:
        self.data = data


def _run_state() -> RunState:
    rs = RunState(run_id="r-abcd1234", agent_id="pandapal", step_n=1, session_id="sess-x")
    # 模拟 SDK 暂停时 metadata 已含内部键（pending_interaction 等）
    rs.metadata = {"pending_interaction": {"tool_name": "ask_user"}}
    return rs


def test_persist_writes_model_id_into_run_state_metadata():
    rs = _run_state()
    se = _FakeSE({"run_state": rs})
    AgentExecutor._persist_resume_model(se, {"model_id": "deepseek-chat", "user_id": "u1"})
    assert rs.metadata["model_id"] == "deepseek-chat"
    # 不破坏既有 metadata
    assert rs.metadata["pending_interaction"]["tool_name"] == "ask_user"


def test_persist_noop_when_no_model_id():
    rs = _run_state()
    se = _FakeSE({"run_state": rs})
    AgentExecutor._persist_resume_model(se, {"user_id": "u1"})  # 无 model_id → 默认模型，不写
    assert "model_id" not in rs.metadata


def test_persist_noop_when_no_run_state():
    # 非暂停事件：se.data 无 run_state → 不抛
    AgentExecutor._persist_resume_model(_FakeSE({"token": "hi"}), {"model_id": "deepseek-chat"})
    AgentExecutor._persist_resume_model(_FakeSE(None), {"model_id": "deepseek-chat"})


def test_model_id_survives_pause_sqlite_resume_roundtrip():
    """端到端：stamp → pickle（pause 序列化）→ sqlite BLOB 存取 → 反序列化 → model_id 仍在。

    直接证伪「切 sqlite 后 model_id 丢失是 sqlite 记录问题」：只要 model_id 进了 metadata，
    sqlite 一定能原样带回来。
    """
    rs = _run_state()
    se = _FakeSE({"run_state": rs})
    AgentExecutor._persist_resume_model(se, {"model_id": "deepseek-chat"})

    # pause 的序列化路径（interaction/hitl manager._serialize 回落 pickle）
    blob = pickle.dumps({"session_id": "sess-x", "state": rs})

    # sqlite run_states 表（与 v001 schema 一致）BLOB 存取
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE run_states(session_id TEXT, run_id TEXT, serialized_state BLOB, "
        "created_at TEXT, PRIMARY KEY(session_id, run_id))"
    )
    conn.execute(
        "INSERT OR REPLACE INTO run_states VALUES(?,?,?,?)",
        ("sess-x", "r-abcd1234", blob, "now"),
    )
    conn.commit()
    got = conn.execute(
        "SELECT serialized_state FROM run_states WHERE session_id=? AND run_id=?",
        ("sess-x", "r-abcd1234"),
    ).fetchone()[0]
    conn.close()

    back = pickle.loads(got)["state"]
    assert back.metadata.get("model_id") == "deepseek-chat"  # resume 能取回 → 按 deepseek 续跑
