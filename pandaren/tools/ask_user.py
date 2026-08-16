"""pandaren/tools/ask_user.py — 结构化用户提问题内置工具

对标 Claude Code AskUserQuestionTool，在 Agent 对话执行过程中向用户发起
结构化的单选/多选提问，用于澄清歧义、收集偏好或提供选项。

交互机制：
    本工具的 ToolPolicy.requires_user_interaction = True。
    当引擎检测到此标志时，不执行工具函数，而是：
      1. 暂停 Agent Loop
      2. 持久化 RunState
      3. yield INTERACTION_REQUESTED 事件
      4. Scheduler 展示问题给用户，等待回复
      5. 用户回复后 Scheduler 调用 agent.run(interaction_response=...)
      6. 引擎恢复，执行本工具函数，ctx.metadata["interaction_response"] 含用户答案
      7. 工具返回格式化答案文本 → LLM 继续执行

因此 LLM 只会看到一次 tool call → 一次 tool result（不会死循环）。
"""

import json
import logging

from pandaren.tool.definition.tool import Tool
from pandaren.tool.definition.tool_policy import ToolPolicy
from pandaren.tool.definition.tool_lifecycle import ToolLifecycle
from pandaren.tool.definition.tool_result import ValidationResult
from pandaren.tool.definition.context import ToolContext
from pandaren.tool.types import ToolTier, SensitivityLevel

logger = logging.getLogger(__name__)

# ── Input Schema ──────────────────────────────────────────────────────────

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "questions_json": {
            "type": "string",
            "description": (
                "JSON 字符串格式的问题数组。"
                "每个问题包含: question (完整疑问句), "
                "header (短标签, 最多12字符), "
                "options (选项数组，每个有 label + description), "
                "multiSelect (可选布尔值，允许多选)。"
                "options 中必须包含一个 label 固定为「自由输入」的选项，"
                "允许用户自行填写想法。"
            ),
        }
    },
    "required": ["questions_json"],
}

# ── LLM 使用指南 ────────────────────────────────────────────────────────────

ASK_USER_LLM_GUIDE = """向用户发起结构化提问。

使用场景：
- 需要向用户确认意图、澄清歧义或收集偏好时调用
- 一次性把所有需要澄清的问题全部问完，不要多次调用，特别时不要老是问重复的问题

提问规则：
- 每个 question 写完整疑问句，以问号结尾
- header 用简短标签概括主题，最多 12 字符
- 推荐选项放在第一个
- 使用 multiSelect: true 允许多选
- 每题 1-5 个选项（含自由输入），每轮 1-6 个问题，根据需要调整数量
- 每个问题的 options 里必须包含一个 label 固定为「自由输入」的选项（用户可自行填写想法）
- description 说明该选项的含义或影响
- 字段名必须用 "options"（复数），不要写成 "option"（单数）
"""


# ── Validator ────────────────────────────────────────────────────────────────

def _validate_questions(
    args: dict, ctx: ToolContext,
) -> ValidationResult | None:
    """校验 questions_json 的结构合法性。"""
    try:
        questions = json.loads(args["questions_json"])
    except json.JSONDecodeError as e:
        return ValidationResult(
            False, f"questions_json 格式无效，无法解析：{e}", error_code=1,
        )

    if not isinstance(questions, list):
        return ValidationResult(
            False, "questions_json 必须是 JSON 数组", error_code=2,
        )

    n = len(questions)
    if n < 1 or n > 6:
        return ValidationResult(
            False, f"问题数量必须在 1-6，当前为 {n}", error_code=3,
        )

    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            return ValidationResult(
                False,
                f"第 {i + 1} 个 question 必须是 JSON 对象",
                error_code=4,
            )

        question_text = q.get("question", f"问题 {i + 1}")

        # options 校验：1-5 个，且必须包含一个 label 为「自由输入」的选项
        opts = q.get("options", [])
        if not isinstance(opts, list) or len(opts) < 1 or len(opts) > 5:
            return ValidationResult(
                False,
                f"'{question_text}' 的 options 数量必须在 1-5，当前为 {len(opts)}",
                error_code=5,
            )

        # 自由输入选项必须存在（label 固定为「自由输入」）
        if not any(
            isinstance(o, dict) and o.get("label") == "自由输入"
            for o in opts
        ):
            return ValidationResult(
                False,
                f"'{question_text}' 的 options 必须包含一个 label 为「自由输入」的选项",
                error_code=8,
            )

        # label 唯一性校验
        labels = [o.get("label", "") for o in opts]
        if len(labels) != len(set(labels)):
            return ValidationResult(
                False,
                f"'{question_text}' 的 option labels 必须唯一",
                error_code=6,
            )

        # header 长度校验
        header = q.get("header", "")
        if header and len(header) > 12:
            return ValidationResult(
                False,
                f"'{question_text}' 的 header 最多 12 字符，当前为 {len(header)}",
                error_code=7,
            )

    return None  # 校验通过


# ── Formatter: 恢复路径 ────────────────────────────────────────────────────

def _format_user_answers(response: str) -> str:
    """将用户回复文本格式化为 LLM 可读的结果。"""
    return f"用户选择了：{response}"


# ── Formatter: 降级路径 ────────────────────────────────────────────────────

def _format_questions(questions: list[dict]) -> str:
    """降级路径：直接格式化问题文本。"""
    lines = ["📋 请回答以下问题：", ""]
    for i, q in enumerate(questions, 1):
        header = q.get("header", f"问题 {i}")
        question = q.get("question", "")
        opts = q.get("options", [])
        lines.append(f"{i}. [{header}] {question}")
        for j, opt in enumerate(opts):
            label = opt.get("label", "")
            desc = opt.get("description", "")
            lines.append(
                f"   {'ABCDEFGH'[j]}. {label}"
                + (f" — {desc}" if desc else "")
            )
        lines.append("")
    return "\n".join(lines)


# ── Executor ─────────────────────────────────────────────────────────────────

def _ask_user_executor(ctx: ToolContext, questions_json: str) -> str:
    """向用户发起结构化提问。

    正常路径：
      - 第一次调用：引擎检测到 requires_user_interaction=True，暂停 Loop。
        本函数体不会被调用（暂停发生在 Phase 4-5）。

      - 恢复执行：引擎通过直接执行路径调用本函数。
        ctx.metadata["interaction_response"] 含有用户回复文本。
        格式化后返回给 LLM。

    降级路径：
      - 仅在引擎未启用交互暂停时作为兜底。
    """
    # 恢复路径：有 interaction_response → 格式化用户答案
    interaction_response = (ctx.metadata or {}).get("interaction_response")
    if interaction_response:
        logger.info(
            "ask_user: resume path with response=%s",
            interaction_response[:100],
        )
        return _format_user_answers(interaction_response)

    # 降级路径：无 interaction_response → 格式化问题
    logger.warning(
        "ask_user: no interaction_response in ctx — fallback to questions text"
    )
    try:
        questions = json.loads(questions_json)
    except json.JSONDecodeError as e:
        return f"❌ questions_json 格式无效，无法解析：{e}"
    return _format_questions(questions)


# ── 工具定义 ─────────────────────────────────────────────────────────────────

ask_user_tool = Tool(
    name="ask_user",
    description=(
        "向用户发起结构化提问，用于收集偏好、澄清歧义、提供选项。奥卡姆剃刀原则，如无必要不要调用"
        "支持单选和多选，每轮 1-6 个问题，每题 1-5 个选项，根据需要调整数量，但每个问题的 options 里必须包含一个 label 固定为「自由输入」的选项（用户可自行填写想法）"
    ),
    executor=_ask_user_executor,
    input_schema=INPUT_SCHEMA,
    tier=ToolTier.ALWAYS,
    when_to_use=(
        "当需要向用户提问以澄清意图、收集偏好、确认选择或消除歧义时调用。奥卡姆剃刀原则，如无必要，不要调用"
    ),
    policy=ToolPolicy(
        sensitivity=SensitivityLevel.LOW,
        is_reversible=True,
        audit_required=True,
        is_idempotent=True,
        read_only=True,
        requires_user_interaction=True,
        max_output_bytes=5_000,
    ),
    lifecycle=ToolLifecycle(validate_input=_validate_questions),
    llm_guide=ASK_USER_LLM_GUIDE,
)
