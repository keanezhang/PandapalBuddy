"""反馈的 UI 通道：ToolResult.feedback → StreamEvent → NormalizedEvent → IPC 消息。

本文件锁的是**「看得见」那一半**。

背景（这不是假想缺陷，是实际发生过的）：门控上线后，反馈只走了
`render_tool_result_for_llm` 进 LLM 的 tool 消息，而 TOOL_CALL_END 的 payload
只带 `result/success/error/tool_args` —— feedback 在 stream 边界被**整个丢掉**。
净效果：门控报了 15 个 error，LLM 全看到了，用户一个字都没看到。
层层都绿（130 个测试全过），链子断在没人测的接缝上。

故本文件按**真实数据流的顺序**逐段驱动真函数（不 mock 中间层）：

    feedback_to_event_data          ← pandaren/engine/run_core.py（SDK 发射端）
      → convert_stream_event_to_normalized  ← 本包（converter）
        → NormalizedEvent.tool_end          ← pandapal/events/normalized.py（工厂）
          → IpcStdoutTransport._to_ipc_schema ← desktop_ipc（协议真相源）

对侧 types/api.ts 的 ToolEndMsg.feedback 无法在 Python 侧断言，靠 CLAUDE.md
的同步纪律 + tsc 保证。
"""

from __future__ import annotations

import pytest

from pandaren.engine.run_core import feedback_to_event_data, tool_call_end_data
from pandaren.tool.definition.tool_result import (
    COMPOSITE_SOURCE,
    FeedbackSeverity,
    ToolFeedback,
    ToolResult,
)

from pandapal.desktop_ipc.ipc_transport import IpcStdoutTransport
from pandapal.events.normalized import EventType, NormalizedEvent
from pandapal.scheduler.stream_to_normalized import convert_stream_event_to_normalized

_GATE_TEXT = "该文件有 2 个 error：\noutput/x.py:1:8 F401 `os` imported but unused\n请修复后重新写入。"


def _feedback(severity=FeedbackSeverity.ERROR, source="code_quality_gate") -> ToolFeedback:
    return ToolFeedback(text=_GATE_TEXT, severity=severity, source=source)


def _emit_data(feedback: ToolFeedback | None, *, success: bool = True) -> dict:
    """驱动 run_core 发射点**真正调用的那个函数**。

    ★ 勿改回手搓字典：本文件早期版本复刻了一份 data 字面量，结果是「谁把 feedback
      从 run_core 删掉，测试照样全绿」—— 测试在测自己，不在测被测物。
      run_core 的两处发射点现已统一走 tool_call_end_data()，此处必须调它。
    """
    return tool_call_end_data(
        "call_1",
        {"file_path": "output/x.py"},
        ToolResult(
            success=success,
            data="✅ 已创建新文件" if success else "",
            error="" if success else "boom",
            tool_name="write_file",
            feedback=feedback,
        ),
    )


def _to_ipc(data: dict) -> dict:
    """驱动 converter → 工厂 → IPC schema，返回前端真正收到的那个 dict。"""
    events = list(convert_stream_event_to_normalized(
        "tool_call_end", data, run_id="r-1", reply_id="r-1", tool_name="write_file",
    ))
    assert len(events) == 1, f"tool_call_end 应恰好产出 1 个事件，实得 {len(events)}"
    assert events[0].event_type is EventType.TOOL_END
    # session_id 缺失只触发 warning，不影响本文件关注的 feedback 字段
    return IpcStdoutTransport()._to_ipc_schema(events[0])


# ══════════════════════════════════════════════
#  第一段：SDK 发射端的序列化
# ══════════════════════════════════════════════

def test_serialize_shape() -> None:
    out = feedback_to_event_data(_feedback())
    assert out == {
        "text": _GATE_TEXT,
        "severity": "error",       # 小写名而非 IntEnum 的 3
        "source": "code_quality_gate",
    }


def test_serialize_none_passthrough() -> None:
    assert feedback_to_event_data(None) is None


@pytest.mark.parametrize(("sev", "wire"), [
    (FeedbackSeverity.INFO, "info"),
    (FeedbackSeverity.WARNING, "warning"),
    (FeedbackSeverity.ERROR, "error"),
])
def test_serialize_every_severity(sev: FeedbackSeverity, wire: str) -> None:
    """三档 severity 全覆盖：api.ts 的联合类型 "info"|"warning"|"error" 必须能收下每一个。

    漏一档 = 前端 TONE 查表 miss → 静默落到 info 灰色，ERROR 被显示成普通提示。
    """
    assert feedback_to_event_data(_feedback(severity=sev))["severity"] == wire


# ══════════════════════════════════════════════
#  全链路：门控的诊断真的到得了前端
# ══════════════════════════════════════════════

def test_diagnostics_reach_ipc_message() -> None:
    """本文件的主张本身：诊断原文出现在前端收到的 IPC 消息里。"""
    msg = _to_ipc(_emit_data(_feedback()))

    assert msg["type"] == "TOOL_END"
    assert msg["feedback"] == {
        "text": _GATE_TEXT,
        "severity": "error",
        "source": "code_quality_gate",
    }
    assert "F401" in msg["feedback"]["text"]


def test_feedback_survives_failed_tool() -> None:
    """工具**失败**时 feedback 依然到达 —— 锁 normalized.tool_end 的 is_error 分支。

    feedback 与工具成败正交（provider 对失败的工具也可能有话说，如密钥扫描）。
    payload["feedback"] 若被分别塞进 if/else 两个 dict，漏掉的那个只在
    「失败 + 有反馈」时现形 —— 正是这条用例存在的理由。
    """
    msg = _to_ipc(_emit_data(_feedback(), success=False))
    assert msg["is_error"] is True
    assert msg["feedback"]["text"] == _GATE_TEXT


def test_composite_source_passes_through_unexplained() -> None:
    """多源合并的 composite 原样上线，converter/transport 不替前端解释。"""
    msg = _to_ipc(_emit_data(_feedback(source=COMPOSITE_SOURCE)))
    assert msg["feedback"]["source"] == COMPOSITE_SOURCE


# ══════════════════════════════════════════════
#  零影响：未注入 provider 的工具调用一切照旧
# ══════════════════════════════════════════════

def test_pass_note_reaches_ui_even_though_llm_never_sees_it() -> None:
    """★ 绿灯的命脉：llm_visible=False 的反馈**照样上线**。

    UI 通道与 LLM 通道在此分岔 —— render_tool_result_for_llm 跳过它（LLM 零 token），
    feedback_to_event_data 照发（用户看到绿灯）。若哪天有人"顺手"让序列化也尊重
    llm_visible，绿灯会**整个消失**且没有任何报错，本用例是那道防线。
    """
    passed = ToolFeedback(
        text="Lint 检查通过（ruff），未发现问题。",
        severity=FeedbackSeverity.INFO,
        source="code_quality_gate",
        llm_visible=False,
    )
    msg = _to_ipc(_emit_data(passed))

    assert msg["feedback"] is not None, "llm_visible=False 不得影响上线"
    assert msg["feedback"]["severity"] == "info"   # 前端据此渲染绿色
    assert "通过" in msg["feedback"]["text"]


def test_llm_and_ui_channels_diverge_on_pass_note() -> None:
    """同一条 ToolFeedback，两个受众拿到不同结果 —— 这正是设计意图。"""
    from pandaren.engine.run_core import render_tool_result_for_llm

    passed = ToolFeedback(
        text="Lint 检查通过（ruff），未发现问题。",
        severity=FeedbackSeverity.INFO,
        source="code_quality_gate",
        llm_visible=False,
    )
    result = ToolResult(success=True, data="✅ 已创建新文件",
                        tool_name="write_file", feedback=passed)

    # LLM：逐字节等于没有反馈时（零打扰硬不变量）
    assert render_tool_result_for_llm(result) == "✅ 已创建新文件"
    # UI：拿到完整反馈
    assert _to_ipc(_emit_data(passed))["feedback"]["text"] == passed.text


def test_no_feedback_yields_null_not_missing() -> None:
    """无反馈 → 字段为 None 而**非缺失**。

    前端 `msg.feedback ?? null` 对两者都成立，但契约上"总是有这个键、值可能为 null"
    比"有时没这个键"更好定型（api.ts 声明 feedback?: ToolFeedback | null）。
    """
    msg = _to_ipc(_emit_data(None))
    assert "feedback" in msg
    assert msg["feedback"] is None


def test_untouched_fields_unchanged() -> None:
    """既有字段逐个不变 —— 本次改动是**加法**，不得动到任何已有工具的显示。

    TOOL_END 是每个工具调用都发的事件；这里漂移一下，影响面是整个时间线。
    """
    msg = _to_ipc(_emit_data(_feedback()))
    assert msg["tool_name"] == "write_file"
    assert msg["tool_call_id"] == "call_1"
    assert msg["is_error"] is False
    assert msg["result_full"] == "✅ 已创建新文件"
    assert msg["tool_args"] == {"file_path": "output/x.py"}


def test_feedback_not_folded_into_result() -> None:
    """feedback 与 result_full 是两个字段，不许把反馈拼进结果里。

    工具自己说的（"已创建新文件"）与第三方对它的评价（"但有 15 个 error"）
    混成一坨，前端就没法分别渲染，且会让人误以为是工具自己报的错。
    """
    msg = _to_ipc(_emit_data(_feedback()))
    assert msg["result_full"] == "✅ 已创建新文件"
    assert "F401" not in str(msg["result_full"])
    assert "F401" in msg["feedback"]["text"]
