"""渲染零漂移回归测试（设计 §Step 6「硬不变量」/ 用例种子 #19）。

`render_tool_result_for_llm` 抽自 run_core 中三处**完全同构**的内联表达式：

    result_text = (
        _tool_data_to_text(tool_result.data) if tool_result.success
        else (tool_result.error or "Error")
    )

它是本设计唯一触及「所有工具输出」的改动——一旦新函数与旧内联表达式有任何
格式差异（多一个空格、换行不同、错误分支措辞变化），改变的不是门控的反馈，
而是**每一个工具**给 LLM 看的文本，包括所有未注入门控的 Agent。

故本文件锁死一条硬不变量：

    feedback is None  ⟹  render_tool_result_for_llm(r) 与旧表达式**逐字节相等**

`_OLD_INLINE` 是重构前那段表达式的**逐字复制**，作为参照实现存在。它不得被
"顺手简化"——它的价值正在于是历史行为的独立副本。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from pandaren.engine.run_core import _tool_data_to_text, render_tool_result_for_llm
from pandaren.tool.definition.tool_result import (
    COMPOSITE_SOURCE,
    FeedbackSeverity,
    ToolFeedback,
    ToolResult,
)


def _OLD_INLINE(tool_result: ToolResult) -> str:
    """重构前 run_core 内联表达式的逐字副本（参照实现，勿简化）。"""
    return (
        _tool_data_to_text(tool_result.data) if tool_result.success
        else (tool_result.error or "Error")
    )


# ─── 数据类型矩阵：覆盖 success/failure × data 各类型 ──────────────────

_SUCCESS_DATA: list[Any] = [
    None,                                        # → "(空)"（LLM API 兼容性保底）
    "",                                          # 空字符串
    "plain text",                                # str
    "多行\n文本\t含制表符",                        # str with escapes
    {"a": 1, "b": "中文"},                        # dict → json.dumps(ensure_ascii=False)
    {},                                          # 空 dict
    {"nested": {"deep": [1, 2, {"x": None}]}},   # 嵌套 dict
    {"escaped": "line1\nline2"},                 # dict 含换行（json.dumps 会转义）
    [1, 2, 3],                                   # list → str()
    [],                                          # 空 list
    [{"type": "text", "text": "hi"},
     {"type": "image_url", "image_url": {"url": "data:..."}}],  # multimodal 形状
    0,                                           # falsy int
    False,                                       # falsy bool
    3.14,                                        # float
    ("tuple", "of", "things"),                   # tuple
]

_FAILURE_ERRORS: list[str | None] = [
    None,          # → "Error" 兜底
    "",           # falsy → "Error" 兜底
    "boom",
    "Error: Permission denied for tool 'write_file'",
    "多行\n错误",
]


@pytest.mark.parametrize("data", _SUCCESS_DATA, ids=lambda d: repr(d)[:40])
def test_render_success_matches_old_inline_bytewise(data: Any) -> None:
    """success 分支：新函数与旧内联表达式逐字节相等。"""
    r = ToolResult(success=True, data=data, tool_name="write_file")

    assert r.feedback is None, "未挂载反馈时 feedback 必须默认为 None"
    assert render_tool_result_for_llm(r) == _OLD_INLINE(r)


@pytest.mark.parametrize("error", _FAILURE_ERRORS, ids=lambda e: repr(e)[:40])
def test_render_failure_matches_old_inline_bytewise(error: str | None) -> None:
    """failure 分支：新函数与旧内联表达式逐字节相等（含 error 为空时的 "Error" 兜底）。"""
    r = ToolResult(success=False, data=None, error=error, tool_name="write_file")

    assert render_tool_result_for_llm(r) == _OLD_INLINE(r)


def test_render_failure_ignores_data() -> None:
    """failure 时走 error 分支，data 不参与渲染——锁死旧行为的这个细节。"""
    r = ToolResult(success=False, data={"should": "be ignored"}, error="boom")

    assert render_tool_result_for_llm(r) == "boom" == _OLD_INLINE(r)


def test_render_success_dict_uses_json_not_str() -> None:
    """dict 走 json.dumps(ensure_ascii=False) 而非 str()——中文不转义、用双引号。

    这是 _tool_data_to_text 的既有契约（见其 docstring：避免 \\n 转义问题），
    抽函数不得改变它。
    """
    r = ToolResult(success=True, data={"msg": "你好"})

    rendered = render_tool_result_for_llm(r)
    assert rendered == '{"msg": "你好"}'
    assert rendered == json.dumps({"msg": "你好"}, ensure_ascii=False)
    assert rendered != str({"msg": "你好"})


def test_render_none_data_returns_empty_placeholder() -> None:
    """data=None → "(空)"（LLM API 兼容性保底，非空占位符）。"""
    assert render_tool_result_for_llm(ToolResult(success=True, data=None)) == "(空)"

    # 默认值 "" 也一样走 "(空)" 占位符
    assert render_tool_result_for_llm(ToolResult(success=True)) == "(空)"


# ─── 反馈挂载后的渲染（设计 §5A-b「渲染顺序：反馈必须前置」）────────────


def _fb(text: str = "该文件有 1 个 error", source: str = "code_quality_gate") -> ToolFeedback:
    return ToolFeedback(text=text, severity=FeedbackSeverity.ERROR, source=source)


def test_render_with_feedback_puts_feedback_first() -> None:
    """反馈段必须**前置**于原始 result_text。

    这不是排版偏好，是正确性要求：Memory.add_tool_result 入口的 MicroCompact
    单条截断切的是**尾部**，反馈若在尾部会被静默切掉（见设计 §5A-b）。
    """
    r = ToolResult(success=True, data="written to a.py", feedback=_fb())

    rendered = render_tool_result_for_llm(r)

    assert rendered.index("该文件有 1 个 error") < rendered.index("written to a.py"), (
        "反馈必须在原始 result_text 之前，否则会被 MicroCompact 从尾部截掉"
    )


def test_render_with_feedback_preserves_original_text_verbatim() -> None:
    """挂载反馈后，原始 result_text 仍原样出现在输出里（只前置、不改写）。"""
    r = ToolResult(success=True, data={"path": "a.py"}, feedback=_fb())

    rendered = render_tool_result_for_llm(r)

    assert _OLD_INLINE(r) in rendered


def test_render_with_feedback_carries_source_and_text() -> None:
    """反馈段含 source 标识与正文——供 LLM 辨认反馈来源（HC4 可溯源）。"""
    r = ToolResult(success=True, data="ok", feedback=_fb(text="F401 unused import"))

    rendered = render_tool_result_for_llm(r)

    assert "code_quality_gate" in rendered
    assert "F401 unused import" in rendered


def test_render_composite_does_not_double_prefix() -> None:
    """composite 的 text 里各段已自带 [source]，渲染不得再加外层标签。

    否则输出成 `[composite] [code_quality_gate] ...` —— 双重前缀，且 "composite"
    这个词对 LLM 不含任何信息。
    """
    merged = ToolFeedback(
        text="[code_quality_gate] F401 unused import\n\n[secret_scan] 疑似 AWS key",
        severity=FeedbackSeverity.ERROR,
        source=COMPOSITE_SOURCE,
    )
    rendered = render_tool_result_for_llm(ToolResult(success=True, data="ok", feedback=merged))

    assert "[composite]" not in rendered
    assert rendered.startswith("[code_quality_gate] ")
    assert "[secret_scan] 疑似 AWS key" in rendered


def test_render_feedback_on_failed_result() -> None:
    """失败结果上若挂了反馈，仍是「反馈段 + 错误文本」，不吞掉 error。"""
    r = ToolResult(success=False, error="disk full", feedback=_fb())

    rendered = render_tool_result_for_llm(r)

    assert "该文件有 1 个 error" in rendered
    assert "disk full" in rendered
    assert rendered.index("该文件有 1 个 error") < rendered.index("disk full")
