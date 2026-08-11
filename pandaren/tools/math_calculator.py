"""pandaren/tools/math_calculator.py — 数学计算器内置工具

安全的数学表达式求值器，支持基础运算和常用数学函数。
纯 stdlib 实现，无外部依赖。
"""

import math

from pandaren.tool.types import ToolTier, SensitivityLevel
from pandaren.tool import ToolContext
from pandaren.tool.definition.tool_policy import ToolPolicy
from pandaren.tool.decorator import tool


MathLLMGuide = """安全的数学表达式求值器。

重要规则：
- 支持基础运算：+ - * / **（幂）//（整除）%（取余）()（括号）
- 支持常用函数：sqrt, sin, cos, tan, log, log10, abs, round, min, max, pow
- 支持常量：pi, e
- 表达式仅允许数学运算，不能执行任意 Python 代码
"""


@tool.function(
    tier=ToolTier.DEFERRED,
    name="math_calculator",
    description=(
        "数学表达式计算器，支持加减乘除和常用数学函数"
        "（sqrt, sin, cos, tan, log, log10, abs, round, min, max, pow, pi, e 等）"
    ),
    when_to_use=(
        "当用户需要进行数学计算、四则运算或使用数学函数时调用。"
        "表达式中的运算符号：+ - * / **（幂） //（整除）%（取余）()（括号）"
    ),
    policy=ToolPolicy(
        sensitivity=SensitivityLevel.LOW,
        is_reversible=True,
        audit_required=False,
        is_idempotent=True,
    ),
    llm_guide=MathLLMGuide,
)
def math_calculator(ctx: ToolContext, expression: str) -> str:
    """安全的数学表达式计算器。

    支持的函数和常量：
        sqrt, sin, cos, tan, log, log10, abs, round, min, max, pow, pi, e

    计算使用受限的 eval 环境，仅允许安全的数学运算。

    Args:
        ctx: 工具上下文。
        expression: 数学表达式字符串。

    Returns:
        计算结果文本；表达式非法或含不安全操作时返回错误信息。
    """
    allowed_names = {
        "abs": abs, "round": round, "min": min, "max": max,
        "pow": pow, "sqrt": math.sqrt, "pi": math.pi, "e": math.e,
        "sin": math.sin, "cos": math.cos, "tan": math.tan,
        "log": math.log, "log10": math.log10,
    }
    try:
        result = eval(expression, {"__builtins__": {}}, allowed_names)
        return f"{expression} = {result}"
    except SyntaxError as e:
        return f"表达式语法错误：{e}"
    except (ValueError, ZeroDivisionError, ArithmeticError) as e:
        return f"计算错误：{e}"
    except Exception as e:
        return f"无法计算表达式：{e}"
