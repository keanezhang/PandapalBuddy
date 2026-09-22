"""ToolBudget token 口径用例：覆盖 TB-1 / TB-2 / TB-3。

对应设计文档 compact-budget.design.md §6.8：
  - TB-1 注入 estimator 后走注入口径（非 bytes/4）
  - TB-2 未注入回落 bytes/4
  - TB-3 estimator 抛异常 → 回落 100 + WARNING（计费类留痕）
"""

from __future__ import annotations

import json
import logging

from pandaren.tool.definition.tool_schema import ToolSchema
from pandaren.tool.exposure.budget import ToolBudget


class _FixedEstimator:
    def __init__(self, value: int):
        self._value = value

    def estimate(self, messages) -> int:
        return self._value


class _RaisingEstimator:
    def estimate(self, messages) -> int:
        raise RuntimeError("estimator boom")


def _schema_text(schema: ToolSchema) -> str:
    return json.dumps(
        {
            "name": schema.name,
            "description": schema.description,
            "parameters": schema.parameters,
        },
        ensure_ascii=False,
    )


def test_tb1_injected_estimator_uses_estimator_path():
    schema = ToolSchema(name="t", description="", parameters={})
    budget = ToolBudget(token_estimator=_FixedEstimator(777))

    result = budget._estimate_tokens(schema)

    assert result == 777
    bytes_path = max(1, len(_schema_text(schema).encode("utf-8")) // 4)
    assert result != bytes_path  # 证明走的是注入口径，而非 bytes/4


def test_tb2_without_estimator_falls_back_to_bytes_div_4():
    schema = ToolSchema(
        name="工具",
        description="描述",
        parameters={"type": "object", "properties": {}},
    )
    expected = max(1, len(_schema_text(schema).encode("utf-8")) // 4)

    assert ToolBudget()._estimate_tokens(schema) == expected


def test_tb3_estimator_raises_falls_back_to_100_with_warning(caplog):
    schema = ToolSchema(name="t", description="", parameters={})
    budget = ToolBudget(token_estimator=_RaisingEstimator())

    with caplog.at_level(logging.WARNING, logger="pandaren.tool.exposure.budget"):
        result = budget._estimate_tokens(schema)

    assert result == 100
    assert "token 估算失败" in caplog.text
