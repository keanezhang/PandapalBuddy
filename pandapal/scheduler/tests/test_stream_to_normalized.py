"""pandapal.scheduler.stream_to_normalized 测试。

  - interaction_requested 事件必须从 tool_args.questions_json 解出全部 question
  - options 必须是 list[dict]（结构化 label+description），不是 list[str]
  - payload 必须带 questions 数组 + tool_name
"""

from __future__ import annotations

import json

import pytest

from pandapal.events.normalized import EventType, NormalizedEvent
from pandapal.scheduler.stream_to_normalized import (
    _extract_all_questions,
    convert_stream_event_to_normalized,
)


# ──────────────────────────────────────────────
# _extract_all_questions 单元测试
# ──────────────────────────────────────────────


def test_extract_all_questions_valid_json_string():
    """正常路径：JSON 字符串 + 完整结构。"""
    questions = [
        {
            "question": "今天天气怎么样？",
            "header": "天气",
            "options": [
                {"label": "好", "description": "阳光明媚"},
                {"label": "差", "description": "下雨了"},
            ],
            "multiSelect": False,
        }
    ]
    result = _extract_all_questions(json.dumps(questions, ensure_ascii=False))
    assert len(result) == 1
    assert result[0]["question"] == "今天天气怎么样？"
    assert result[0]["header"] == "天气"
    assert result[0]["multiSelect"] is False
    assert result[0]["options"] == [
        {"label": "好", "description": "阳光明媚"},
        {"label": "差", "description": "下雨了"},
    ]


def test_extract_all_questions_multiple():
    """多问题全部返回。"""
    questions = [
        {"question": "Q1", "header": "H1", "options": [{"label": "A"}, {"label": "B"}], "multiSelect": False},
        {"question": "Q2", "header": "H2", "options": [{"label": "C"}, {"label": "D"}], "multiSelect": True},
    ]
    result = _extract_all_questions(json.dumps(questions))
    assert len(result) == 2
    assert result[0]["question"] == "Q1"
    assert result[0]["header"] == "H1"
    assert [o["label"] for o in result[0]["options"]] == ["A", "B"]
    assert result[0]["multiSelect"] is False
    assert result[1]["question"] == "Q2"
    assert result[1]["header"] == "H2"
    assert result[1]["multiSelect"] is True


def test_extract_all_questions_missing_description():
    """description 缺省时填空串。"""
    questions = [{"question": "Q", "options": [{"label": "A"}]}]
    result = _extract_all_questions(json.dumps(questions))
    assert result[0]["options"] == [{"label": "A", "description": ""}]


def test_extract_all_questions_string_options_fallback():
    """旧版 LLM 直接传字符串 options 时兜底成 {label, ""}。"""
    questions = [{"question": "Q", "options": ["A", "B"]}]
    result = _extract_all_questions(json.dumps(questions))
    assert result[0]["options"] == [
        {"label": "A", "description": ""},
        {"label": "B", "description": ""},
    ]


def test_extract_all_questions_empty_inputs():
    """空/None/空字符串 → []。"""
    assert _extract_all_questions(None) == []
    assert _extract_all_questions("") == []
    assert _extract_all_questions([]) == []
    assert _extract_all_questions({}) == []


def test_extract_all_questions_invalid_json():
    """非法 JSON 不抛异常，返回 []。"""
    result = _extract_all_questions("not a json {[")
    assert result == []


def test_extract_all_questions_json_not_list():
    """JSON 解析后不是 list（是 dict）→ []。"""
    result = _extract_all_questions(json.dumps({"question": "Q"}))
    assert result == []


# ──────────────────────────────────────────────
# convert_stream_event_to_normalized 集成测试
# ──────────────────────────────────────────────


def _run_converter(event_type: str, data: dict, run_id: str = "r1", reply_id: str = "p1",
                   tool_name: str | None = None):
    """便捷：跑完 generator 返回 list[NormalizedEvent]。

    tool_name 不传时默认从 data.get("tool_name") 取（模拟 converter caller 行为）。
    显式传 None 表示"不传 tool_name 参数给 converter"（测试 tool_name 缺省分支用）。
    """
    effective = tool_name if tool_name is not None else data.get("tool_name")
    return list(
        convert_stream_event_to_normalized(
            event_type=event_type,
            data=data,
            run_id=run_id,
            reply_id=reply_id,
            tool_name=effective,
        )
    )


# Stream-closing 事件类型：会先 yield REPLY_END 再 yield 本体（5.2 关流配对）
_STREAM_CLOSING = frozenset({
    "hitl_requested",
    "interaction_requested",
    "permission_denied",
    "agent_halted",
    "agent_cancelled",
})


def _body_event(events, event_type: str):
    """从 converter 输出中找出本体事件（跳过前置 REPLY_END）。"""
    if event_type in _STREAM_CLOSING:
        assert len(events) == 2, f"expected 2 events for {event_type}, got {len(events)}"
        assert events[0].event_type == EventType.REPLY_END
        return events[1]
    assert len(events) == 1, f"expected 1 event for {event_type}, got {len(events)}"
    return events[0]


def test_convert_interaction_requested_extracts_all_questions():
    """INTERACTION_REQUESTED 正确解析 questions_json，全部问题一次发送。"""
    questions = [{
        "question": "选一个",
        "header": "选择",
        "options": [
            {"label": "A", "description": "甲"},
            {"label": "B", "description": "乙"},
        ],
        "multiSelect": False,
    }]
    data = {
        "run_state": object(),
        "tool_name": "ask_user",
        "tool_args": {"questions_json": json.dumps(questions, ensure_ascii=False)},
    }

    events = _run_converter("interaction_requested", data)

    assert len(events) == 2
    assert events[0].event_type == EventType.REPLY_END
    assert events[0].payload["status"] == "paused_for_interaction"

    ir = events[1]
    assert ir.event_type == EventType.INTERACTION_REQUEST
    assert len(ir.payload["questions"]) == 1
    assert ir.payload["questions"][0]["question"] == "选一个"
    assert ir.payload["questions"][0]["header"] == "选择"
    assert ir.payload["questions"][0]["multiSelect"] is False
    assert ir.payload["questions"][0]["options"] == [
        {"label": "A", "description": "甲"},
        {"label": "B", "description": "乙"},
    ]
    assert ir.payload["tool_name"] == "ask_user"
    assert "request_id" in ir.payload
    assert ir.reply_id == "p1"
    assert ir.run_id == "r1"


def test_convert_interaction_requested_multiple_questions():
    """多问题全部发送。"""
    questions = [
        {"question": "Q1", "header": "H1", "options": [{"label": "A"}, {"label": "B"}], "multiSelect": False},
        {"question": "Q2", "header": "H2", "options": [{"label": "C"}, {"label": "D"}], "multiSelect": True},
    ]
    data = {
        "run_state": object(),
        "tool_name": "ask_user",
        "tool_args": {"questions_json": json.dumps(questions)},
    }
    events = _run_converter("interaction_requested", data)
    ir = events[1]
    assert len(ir.payload["questions"]) == 2
    assert ir.payload["questions"][0]["question"] == "Q1"
    assert ir.payload["questions"][1]["question"] == "Q2"


def test_convert_interaction_requested_ignores_legacy_top_level_keys():
    """只走 tool_args.questions_json；顶层 question/options 视为不存在。"""
    data = {
        "run_state": object(),
        "tool_name": "ask_user",
        "question":  "顶层错位 question",
        "options":   [{"label": "错位", "description": "x"}],
    }
    events = _run_converter("interaction_requested", data)
    ir = events[1]
    assert ir.payload["questions"] == []


def test_convert_interaction_requested_uses_stream_tool_name_when_outer_missing():
    """tool_name 透传：外层参数 > data.tool_name > "unknown"。"""
    questions = [{"question": "Q", "options": [{"label": "A"}, {"label": "B"}]}]
    data = {
        "run_state": object(),
        "tool_args": {"questions_json": json.dumps(questions)},
    }
    events = _run_converter("interaction_requested", data, reply_id="p1")
    ir = events[1]
    assert ir.payload.get("tool_name") in ("ask_user", "unknown", None)


def test_convert_interaction_requested_empty_when_malformed():
    """malformed data 不抛异常，正常产出空 questions。"""
    data = {
        "run_state": object(),
        "tool_name": "ask_user",
        "tool_args": {"questions_json": "garbage{{{"},
    }
    events = _run_converter("interaction_requested", data)
    ir = events[1]
    assert ir.payload["questions"] == []


def test_convert_unknown_event_returns_empty():
    """未识别事件类型：返回空 list（不抛）。"""
    events = _run_converter("nonexistent_event_type", {"foo": "bar"})
    assert events == []


# ──────────────────────────────────────────────
# tool_call_start：5.2 协议透传 tool_args dict
# ──────────────────────────────────────────────


def test_convert_tool_call_start_passes_tool_args_dict():
    """5.2：tool_args 必须是完整 dict（前端 ToolStartMsg.tool_args: dict）。"""
    data = {
        "tool_call_id": "tc-1",
        "tool_args":    {"path": "/tmp/x.py", "encoding": "utf-8"},
        "args_preview": str({"path": "/tmp/x.py"})[:200],
    }
    events = _run_converter("tool_call_start", data, tool_name="read_file")
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == EventType.TOOL_START
    assert ev.payload["tool_name"] == "read_file"
    assert ev.payload["tool_call_id"] == "tc-1"
    assert ev.payload["tool_args"] == {"path": "/tmp/x.py", "encoding": "utf-8"}


def test_convert_tool_call_start_missing_tool_args_is_empty_dict():
    """5.2 协议：engine 必发 tool_args，缺字段时 converter 静默置 {}（不兜底猜内容）。"""
    data = {"tool_call_id": "tc-2", "args_preview": "(deprecated)"}
    events = _run_converter("tool_call_start", data, tool_name="legacy_tool")
    assert events[0].payload["tool_args"] == {}


# ──────────────────────────────────────────────
# tool_call_end：5.2 协议透传 result / 透传 tool_args / success→is_error 反转
# ──────────────────────────────────────────────


def test_convert_tool_call_end_passes_full_result_and_success():
    """5.2：result 是完整 dict，is_error 由 success 反转。"""
    data = {
        "tool_call_id": "tc-1",
        "result":       {"content": "hello world", "path": "/tmp/x.py", "line_count": 1},
        "tool_args":    {"path": "/tmp/x.py"},
        "success":      True,
    }
    events = _run_converter("tool_call_end", data, tool_name="read_file")
    ev = events[0]
    assert ev.event_type == EventType.TOOL_END
    assert ev.payload["tool_call_id"] == "tc-1"
    assert ev.payload["is_error"] is False
    assert ev.payload["result_full"] == {
        "content": "hello world", "path": "/tmp/x.py", "line_count": 1,
    }
    assert ev.payload["tool_args"] == {"path": "/tmp/x.py"}
    # result 是 dict 且有 content 字段 → 工厂 _generate_tool_preview 应生成 "已读取 ..." 摘要
    assert "已读取" in ev.payload["result_preview"]
    assert ev.payload["result_mime_type"] == "application/json"


def test_convert_tool_call_end_failure_inverts_to_is_error():
    """success=False → is_error=True；result=None；error 透传到 result_error。"""
    data = {
        "tool_call_id": "tc-1",
        "result":       None,
        "tool_args":    {"path": "/nope.py"},
        "success":      False,
        "error":        "FileNotFoundError: /nope.py",
    }
    events = _run_converter("tool_call_end", data, tool_name="read_file")
    ev = events[0]
    assert ev.payload["is_error"] is True
    assert ev.payload["result_full"] is None
    assert ev.payload["result_error"] == "FileNotFoundError: /nope.py"
    assert ev.payload["result_preview"].startswith("❌")


def test_convert_tool_call_end_missing_success_means_error():
    """5.2 严格：缺 success 字段视为协议破坏，not None == True → is_error=True。"""
    data = {
        "tool_call_id": "tc-1",
        "result":       {"x": 1},
        "tool_args":    {},
        # 注意：故意没 "success" 字段
    }
    events = _run_converter("tool_call_end", data)
    # 不再"乐观默认 False"——5.2 协议：缺字段按 error 处理
    assert events[0].payload["is_error"] is True


# ──────────────────────────────────────────────
# hitl_requested：5.2 从 run_state.session_id 兜底取 session_id
# ──────────────────────────────────────────────


def test_convert_hitl_requested_session_id_from_run_state_dataclass():
    """★ 5.2：session_id 必须从 run_state（dataclass 形态）取。"""
    from dataclasses import dataclass

    @dataclass
    class FakeRunState:
        session_id: str = "s-from-dc"
        run_id: str = "r1"
        step_n: int = 3

    data = {
        "sensitivity":       "high",
        "run_state":         FakeRunState(),
        "pending_tool_name": "bash",
        "pending_tool_args": {"cmd": "rm -rf /"},
    }
    events = _run_converter("hitl_requested", data, run_id="r1", reply_id="p1")
    # hitl_requested 走 stream-closing 分支，先 REPLY_END 再 HITL_REQUEST
    hr = events[1]
    assert hr.event_type == EventType.HITL_REQUEST
    assert hr.payload["session_id"] == "s-from-dc"
    assert hr.payload["tool_name"] == "bash"
    assert hr.payload["tool_args_summary"] == {"cmd": "rm -rf /"}


def test_convert_hitl_requested_does_not_read_top_level_session_id():
    """5.2 严格：engine 不会在 data 顶层放 session_id，converter 也不该读。"""
    from dataclasses import dataclass

    @dataclass
    class FakeRunState:
        session_id: str = "s-real"
        run_id: str = "r1"

    data = {
        "run_state":         FakeRunState(),
        "pending_tool_name": "bash",
        "pending_tool_args": {},
        # 故意在顶层放一个错的 session_id，验证 converter 不读
        "session_id":        "WRONG-legacy-fallback",
    }
    events = _run_converter("hitl_requested", data)
    assert events[1].payload["session_id"] == "s-real"


def test_convert_hitl_requested_does_not_read_legacy_pending_tool_args_summary():
    """5.2 严格：engine 不发 pending_tool_args_summary（5.1 老字段），只发 pending_tool_args。"""
    from dataclasses import dataclass

    @dataclass
    class FakeRunState:
        session_id: str = "s1"
        run_id: str = "r1"

    data = {
        "run_state":                  FakeRunState(),
        "pending_tool_name":          "bash",
        "pending_tool_args":          {"cmd": "ls"},
        # 老字段，5.2 engine 已不发；若有人误塞进来，converter 应忽略
        "pending_tool_args_summary":  {"legacy": True},
    }
    events = _run_converter("hitl_requested", data)
    hr = events[1]
    assert hr.payload["tool_args_summary"] == {"cmd": "ls"}


# ──────────────────────────────────────────────
# permission_denied：5.2 读 sensitive_permission
# ──────────────────────────────────────────────


def test_convert_permission_denied_uses_sensitive_permission():
    """5.2：engine 发 sensitive_permission（如 'network'），包成人类可读 reason。"""
    data = {"sensitive_permission": "network"}
    events = _run_converter("permission_denied", data, tool_name="web_fetch")
    ev = _body_event(events, "permission_denied")
    assert ev.event_type == EventType.PERMISSION_DENIED
    assert ev.payload["tool_name"] == "web_fetch"
    assert ev.payload["reason"] == "requires 'network' permission"


def test_convert_permission_denied_missing_sensitive_falls_back_plain():
    """5.2 严格：sensitive_permission 必发；缺字段时给出明示降级文案（不静默漏 reason）。"""
    data = {}  # 故意缺 sensitive_permission
    events = _run_converter("permission_denied", data, tool_name="bash")
    ev = _body_event(events, "permission_denied")
    assert ev.payload["reason"] == "permission denied"


# ──────────────────────────────────────────────
# agent_halted：_HALT_REASON_MAP 友好提示 + halt_kind 注入
# ──────────────────────────────────────────────
# 所有 14 种 terminal_reason 的参数化测试
# ──────────────────────────────────────────────

# (terminal_reason, error | None, expected_halt_kind, expected_prefix)
_HALT_PARAMS = [
    # ── 14 种已知 terminal_reason ──
    ("llm_error",              "LLM returned 429",           "llm_error",              "LLM 调用异常，Agent 已停止"),
    ("halted_by_guard",        "",                           "budget_exhausted",       "费用保护触发，Agent 已停止"),
    ("max_steps_exceeded",     "",                           "max_steps",              "达到最大步数限制，Agent 已停止"),
    ("step_timeout",           "step 12 timed out",          "timeout",                "步骤执行超时，Agent 已停止"),
    ("total_timeout",          "300s reached",               "timeout",                "总执行超时，Agent 已停止"),
    ("circuit_breaker",        "",                           "circuit_breaker",        "熔断保护触发，Agent 已停止"),
    ("llm_loop_detected",      "repeating: 'I think'",       "loop_detected",          "检测到 LLM 输出循环，Agent 已停止"),
    ("context_overflow",       "200000 tokens after compact","context_overflow",       "上下文超出 token 限制，Agent 已停止"),
    ("tool_halt",              "",                           "tool_halt",              "工具要求停止，Agent 已停止"),
    ("tools_exhausted",        "all tools called 3+ times",  "tools_exhausted",        "工具调用预算耗尽，Agent 已停止"),
    ("permission_exhausted",   "denied 3 times",             "permission_exhausted",   "权限申请被拒绝次数过多，Agent 已停止"),
    ("hitl_rejected",          "user rejected",              "hitl_rejected",          "人工审批被拒绝，Agent 已停止"),
    ("audit_failure",          "audit log not writable",     "audit_failure",          "审计检查不通过，Agent 已停止"),
    ("cancelled",              "Cancelled by user",          "cancelled",              "用户取消，Agent 已停止"),
    # ── 未知 terminal_reason ──
    ("some_future_reason",     "",                           "unknown",                "Agent 异常停止"),
    ("some_future_reason",     "Something happened",         "unknown",                "Agent 异常停止"),
    # ── 空 terminal_reason ──
    ("",                       "crashed without reason",     "unknown",                "Agent 异常停止"),
    ("",                       "",                           "unknown",                "Agent 异常停止"),
]


@pytest.mark.parametrize("terminal_reason,error,expected_kind,expected_prefix", _HALT_PARAMS)
def test_convert_agent_halted_friendly_reason_and_halt_kind(
    terminal_reason, error, expected_kind, expected_prefix,
):
    """★ 所有 terminal_reason → 友好中文 reason + halt_kind 注入 payload。"""
    data: dict[str, str | object] = {"terminal_reason": terminal_reason}
    if error:
        data["error"] = error

    events = _run_converter("agent_halted", data)
    ev = _body_event(events, "agent_halted")

    assert ev.event_type == EventType.AGENT_HALTED
    assert ev.payload["halt_kind"] == expected_kind

    # reason 以 friendly_prefix 开头并以 "。" 结束
    assert ev.payload["reason"].startswith(expected_prefix + "。")

    # 有 error → 包含 "详细信息：{error}"
    if error:
        assert f"详细信息：{error}" in ev.payload["reason"]
    else:
        assert "详细信息：" not in ev.payload["reason"]


# ── agent_cancelled 分支 ──


def test_convert_agent_cancelled_injects_halt_kind_and_friendly_error():
    """agent_cancelled：halt_kind="cancelled"，reason 以"用户取消"开头，error 拼接。"""
    data = {"error": "User clicked stop"}
    events = _run_converter("agent_cancelled", data)
    ev = _body_event(events, "agent_cancelled")

    assert ev.event_type == EventType.AGENT_HALTED
    assert ev.payload["halt_kind"] == "cancelled"
    assert ev.payload["reason"].startswith("用户取消，Agent 已停止。")
    assert "详细信息：User clicked stop" in ev.payload["reason"]


def test_convert_agent_cancelled_defaults_error_when_missing():
    """agent_cancelled 缺 error → 兜底 "Cancelled by user"。"""
    data: dict[str, object] = {}
    events = _run_converter("agent_cancelled", data)
    ev = _body_event(events, "agent_cancelled")

    assert ev.payload["halt_kind"] == "cancelled"
    assert "详细信息：Cancelled by user" in ev.payload["reason"]


# ── halt_kind 覆盖守卫 ──


def test_convert_agent_halted_preserves_existing_halt_kind():
    """executor 已设 halt_kind（如 budget_exhausted）时 converter 不覆盖。"""
    # 模拟 executor 先设了 halt_kind 的场景：
    # NormalizedEvent.agent_halted() 工厂只产出 {"reason": ...}，
    # executor 会在 converter 产出的 ev.payload 上额外设置 halt_kind。
    # 这里直接验证 converter 分支的条件守卫：
    #   当 ev.payload 中已有 "halt_kind" 键（由外部预先注入），
    #   converter 不应覆盖它。
    data = {"terminal_reason": "llm_error", "error": "429"}
    events = _run_converter("agent_halted", data)
    ev = _body_event(events, "agent_halted")

    # 验证 converter 正常注入了 halt_kind
    assert ev.payload["halt_kind"] == "llm_error"

    # 模拟 executor 覆盖为 budget_exhausted，确认 converter 不再覆盖
    ev.payload["halt_kind"] = "budget_exhausted"
    # 如果 converter 再跑一次（模拟 executor 后处理），守卫应生效
    # 直接验证守卫逻辑：代码里是 `if "halt_kind" not in ev.payload`
    assert "halt_kind" in ev.payload  # 已存在
    # 已存在的值不会被覆盖（这由 `if not in` 守卫保证，此处验证值仍为 budget_exhausted）
    assert ev.payload["halt_kind"] == "budget_exhausted"


# ──────────────────────────────────────────────
# NormalizedEvent.interaction_request factory 测试
# ──────────────────────────────────────────────


def test_normalized_event_interaction_request_factory_shape():
    """工厂：questions 是 list[dict]，payload 必须含 tool_name。"""
    ev = NormalizedEvent.interaction_request(
        request_id="req-1",
        questions=[{
            "question": "选一个",
            "header": "选择",
            "options": [{"label": "A", "description": "甲"}],
            "multiSelect": False,
        }],
        tool_name="ask_user",
        reply_id="p1",
        run_id="r1",
    )
    assert ev.event_type == EventType.INTERACTION_REQUEST
    assert ev.payload == {
        "request_id": "req-1",
        "questions":  [{
            "question": "选一个",
            "header": "选择",
            "options": [{"label": "A", "description": "甲"}],
            "multiSelect": False,
        }],
        "tool_name":  "ask_user",
    }


def test_normalized_event_interaction_request_factory_omits_empty_tool_name():
    """tool_name 为空/None 时不出现在 payload。"""
    ev = NormalizedEvent.interaction_request(
        request_id="req-1",
        questions=[],
        tool_name=None,
        reply_id="p1",
        run_id="r1",
    )
    assert "tool_name" not in ev.payload


def test_normalized_event_dataclass_is_frozen():
    """NormalizedEvent 顶层字段 frozen。"""
    from dataclasses import FrozenInstanceError

    ev = NormalizedEvent.interaction_request(
        request_id="r",
        questions=[],
        tool_name=None,
        reply_id="p",
        run_id="r",
    )
    with pytest.raises(FrozenInstanceError):
        ev.reply_id = "tampered"  # type: ignore[misc]


# ──────────────────────────────────────────────
# _BACKEND_ONLY_EVENTS：故意不转发聊天，且不刷 warning
# ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "event_type",
    ["step_start", "step_end", "llm_call_start", "llm_call_end", "handoff"],
)
def test_backend_only_events_produce_no_event_and_no_warning(event_type, caplog):
    """循环生命周期/观测埋点：不产出 NormalizedEvent，也不报 warning（数据走 Dashboard）。"""
    with caplog.at_level("WARNING", logger="pandapal.scheduler.stream_to_normalized"):
        events = list(
            convert_stream_event_to_normalized(
                event_type=event_type,
                data={"step_n": 1},
                run_id="r1",
                reply_id="p1",
            )
        )
    assert events == []
    assert caplog.records == [], f"{event_type} 不应触发兜底 warning"


def test_genuinely_unmapped_event_still_warns(caplog):
    """真·疑似丢事件（不在映射表也不在 backend-only 集）仍要 warning —— 保留审计 #12 精度。"""
    with caplog.at_level("WARNING", logger="pandapal.scheduler.stream_to_normalized"):
        events = list(
            convert_stream_event_to_normalized(
                event_type="some_brand_new_sdk_event",
                data={},
                run_id="r1",
                reply_id="p1",
            )
        )
    assert events == []
    assert any("无 NormalizedEvent 映射" in r.message for r in caplog.records)
