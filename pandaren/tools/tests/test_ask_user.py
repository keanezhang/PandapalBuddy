"""ask_user 问卷「自由输入」显式化修复测试。

覆盖对象：
  - _validate_questions(args, ctx)  — 分支覆盖（数量 1..5 / 自由输入存在 / 精确 label）
  - _format_questions(questions)     — 语句覆盖（删除硬编码「💬 自由输入」追加行）

运行：
  python -m pytest pandaren/tools/tests/test_ask_user.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from pandaren.tool import ToolContext
from pandaren.tools.ask_user import _format_questions, _validate_questions

# ── 确保项目根在 sys.path（pytest 从任意 cwd 运行时都能 import pandaren）──
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(
        run_id="t",
        step_n=0,
        agent_id="a",
        session_id="s",
    )


def _args(questions: list) -> dict:
    return {"questions_json": json.dumps(questions)}


# ── _validate_questions ────────────────────────────────────────────────────


# inv-P1 + V1 options 数量下界越界 [P1]
def test_options_count_zero_returns_error_code_5(ctx):
    res = _validate_questions(
        _args([{"question": "Q?", "options": []}]),
        ctx,
    )

    assert res.valid is False
    assert res.error_code == 5
    assert "options 数量必须在 1-5，当前为 0" in res.message


# inv-P1 + V2 options 数量上界越界 [P1]
def test_options_count_six_returns_error_code_5(ctx):
    res = _validate_questions(
        _args(
            [
                {
                    "question": "Q?",
                    "options": [
                        {"label": "A"},
                        {"label": "B"},
                        {"label": "C"},
                        {"label": "D"},
                        {"label": "E"},
                        {"label": "自由输入"},
                    ],
                }
            ]
        ),
        ctx,
    )

    assert res.valid is False
    assert res.error_code == 5
    assert "当前为 6" in res.message


# inv-P1 + V1 下界边界 [P1]：仅「自由输入」即通过
def test_options_count_one_free_input_only_passes(ctx):
    res = _validate_questions(
        _args([{"question": "Q?", "options": [{"label": "自由输入"}]}]),
        ctx,
    )

    assert res is None


# inv-P1 + V2 上界边界 [P1]：4 普通 + 1 自由输入 = 5，通过（核心 bug 修复）
def test_options_count_five_with_free_input_passes(ctx):
    res = _validate_questions(
        _args(
            [
                {
                    "question": "Q?",
                    "options": [
                        {"label": "A"},
                        {"label": "B"},
                        {"label": "C"},
                        {"label": "D"},
                        {"label": "自由输入"},
                    ],
                }
            ]
        ),
        ctx,
    )

    assert res is None


# inv-P2 + V3 自由输入缺失 [P0]
def test_missing_free_input_returns_error_code_8(ctx):
    res = _validate_questions(
        _args([{"question": "Q?", "options": [{"label": "A"}, {"label": "B"}]}]),
        ctx,
    )

    assert res.valid is False
    assert res.error_code == 8
    assert "必须包含一个 label 为「自由输入」的选项" in res.message


# inv-P2 + V4 精确匹配误伤 [P1]：子串相似 label 不算自由输入
def test_only_free_input_prefix_similar_returns_error_code_8(ctx):
    res = _validate_questions(
        _args([{"question": "Q?", "options": [{"label": "自由输入xxx"}]}]),
        ctx,
    )

    assert res.valid is False
    assert res.error_code == 8


# inv-P1 + V4 精确匹配 [P1]：相似普通选项与精确自由输入并存，通过
def test_free_input_similar_and_exact_coexist_passes(ctx):
    res = _validate_questions(
        _args(
            [
                {
                    "question": "Q?",
                    "options": [
                        {"label": "自由输入xxx"},
                        {"label": "自由输入"},
                    ],
                }
            ]
        ),
        ctx,
    )

    assert res is None


# inv-P2 + V5 自由输入重复 [P1]
def test_duplicate_free_input_returns_error_code_6(ctx):
    res = _validate_questions(
        _args(
            [
                {
                    "question": "Q?",
                    "options": [
                        {"label": "自由输入"},
                        {"label": "自由输入"},
                    ],
                }
            ]
        ),
        ctx,
    )

    assert res.valid is False
    assert res.error_code == 6
    assert "labels 必须唯一" in res.message


# inv-P2 + V6 分支顺序 [P2]：缺自由输入优先于 label 重复（8 而非 6）
def test_missing_free_input_and_duplicate_label_returns_error_code_8(ctx):
    res = _validate_questions(
        _args([{"question": "Q?", "options": [{"label": "A"}, {"label": "A"}]}]),
        ctx,
    )

    assert res.valid is False
    assert res.error_code == 8


# ── _format_questions ──────────────────────────────────────────────────────


# inv-P3 删除硬编码「💬 自由输入」追加行 [P1]
def test_format_questions_has_no_hardcoded_free_input_line():
    questions = [
        {
            "header": "主题",
            "question": "你的想法?",
            "options": [
                {"label": "自由输入", "description": "自行填写"},
                {"label": "选项A"},
            ],
        }
    ]

    out = _format_questions(questions)

    # 设计文档 PY-10 golden（规格推导权威值）：单尾部换行，无硬编码「💬 自由输入」追加行。
    assert out == (
        "📋 请回答以下问题：\n"
        "\n"
        "1. [主题] 你的想法?\n"
        "   A. 自由输入 — 自行填写\n"
        "   B. 选项A\n"
    )
    assert "💬 自由输入" not in out
    assert out.count("自由输入") == 1


# ── Known-Gap ──────────────────────────────────────────────────────────────
#
# 期望：options 键存在且值为 null 时应返回 error_code=5。
# 现状：_validate_questions 在构造 message 时对 None 调 len() 抛 TypeError。
# 该缺陷不在本次改动范围（见设计文档 §7），用 xfail 显式标记，修复后转 XPASS 报警。


@pytest.mark.xfail(
    raises=TypeError,
    strict=True,
    reason=(
        "已知差距：options: null 期望返回 error_code=5，"
        "现状在构造 message 时 len(None) 抛 TypeError"
    ),
)
def test_options_null_should_return_error_code_5_known_gap(ctx):
    res = _validate_questions(
        _args([{"question": "Q?", "options": None}]),
        ctx,
    )

    assert res.valid is False
    assert res.error_code == 5
