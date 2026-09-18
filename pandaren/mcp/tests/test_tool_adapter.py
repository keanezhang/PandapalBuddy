"""pandaren/mcp/tests/test_tool_adapter.py — MCP 工具 → Tool 适配（unit，零 mock）。

设计依据：mcp-core.design.md 分组 B（MCP-14..30）。
风险映射：R-SEC1/2/3（安全判定，P0）、R-ADPT1/2/3/4、R-TOK1(P2)。
契约更新（KG-2 裁决）：名称启发式是**下界**，annotations 只升不降，配置覆盖最终裁定。
"""

from __future__ import annotations

import pytest

from pandaren.identity.models import TrustLevel
from pandaren.mcp.config import McpServerConfig
from pandaren.mcp.tool_adapter import (
    MCP_TOOL_MAX_OUTPUT_BYTES,
    McpToolAdapter,
    _CRITICAL_KEYWORDS,
    _LOW_KEYWORDS,
    _tokenize,
)
from pandaren.tool.definition.tool_result import ToolResult
from pandaren.tool.types import SensitivityLevel

from .conftest import RecordingHandle, make_descriptor


def _adapter(server_name: str = "fs", **cfg_overrides) -> McpToolAdapter:
    cfg = McpServerConfig(
        name=server_name, transport="stdio", command="py", **cfg_overrides
    )
    return McpToolAdapter(cfg, RecordingHandle(returns=None))


def _level(name: str, *, annotations=None, server_name: str = "fs", **cfg_overrides):
    return _adapter(server_name, **cfg_overrides).classify(
        make_descriptor(name, annotations=annotations)
    )


# R-SEC2 annotations destructiveHint → CRITICAL（camel/snake 兼容）[P0]
@pytest.mark.parametrize(
    "annotations",
    [{"destructiveHint": True}, {"destructive_hint": True}],
)
def test_destructive_hint_upgrades_to_critical(annotations):
    assert _level("poke", annotations=annotations) == SensitivityLevel.CRITICAL


# KG-2 裁决：readOnlyHint 不得把等级压到名称启发式之下 [P0]
@pytest.mark.parametrize(
    "name, expected",
    [
        ("x", SensitivityLevel.MEDIUM),  # 中性名 + readOnlyHint 仍是 MEDIUM
        ("write_file", SensitivityLevel.CRITICAL),  # 变更类名不被 readOnlyHint 降级
    ],
)
def test_readonly_hint_never_downgrades_below_name_heuristic(name, expected):
    assert _level(name, annotations={"readOnlyHint": True}) == expected


# R-SEC1 + inv-a1 名称启发——变更/破坏/外发类动词 → CRITICAL [P0, property]
@pytest.mark.parametrize("keyword", sorted(_CRITICAL_KEYWORDS))
@pytest.mark.parametrize("template", ["{kw}_thing", "do_{kw}", "{kw}Thing"])
def test_name_heuristic_critical_keywords(keyword, template):
    name = template.format(kw=keyword)
    level = McpToolAdapter._name_heuristic(name)

    assert level == SensitivityLevel.CRITICAL
    assert level != SensitivityLevel.HIGH


@pytest.mark.parametrize(
    "name",
    [
        "write_file",
        "create_user",
        "update_row",
        "delete_thing",
        "exec_cmd",
        "rm_path",
        "drop_db",
        "send_mail",
        "deploy_app",
        "install_pkg",
    ],
)
def test_name_heuristic_representative_change_tools(name):
    assert McpToolAdapter._name_heuristic(name) == SensitivityLevel.CRITICAL


# inv-a1 核心安全：启发式永不产 HIGH（HIGH=免审批）[P0, property]
_CORPUS = (
    [f"{kw}_thing" for kw in _CRITICAL_KEYWORDS]
    + [f"{kw}_items" for kw in _LOW_KEYWORDS]
    + ["target", "echo", "poke", "thing", "structured_tool"]
    + ["get_or_create", "list_and_write", "readWriteFile"]
)


@pytest.mark.parametrize("name", _CORPUS)
def test_heuristic_never_yields_high(name):
    levels = {
        McpToolAdapter._name_heuristic(name),
        _level(name),
    }
    assert SensitivityLevel.HIGH not in levels


# inv-a2 只读类名 → LOW [P1, property]
@pytest.mark.parametrize("keyword", sorted(_LOW_KEYWORDS))
def test_name_heuristic_low_keywords(keyword):
    assert McpToolAdapter._name_heuristic(f"{keyword}_items") == SensitivityLevel.LOW


@pytest.mark.parametrize("name", ["list_items", "get_user", "search_log"])
def test_name_heuristic_representative_readonly_tools(name):
    assert McpToolAdapter._name_heuristic(name) == SensitivityLevel.LOW


# R-TOK1 "target" 不被 "get" 误伤 [P2]
def test_tokenize_does_not_hit_target_with_get():
    assert "get" not in _tokenize("target")
    assert McpToolAdapter._name_heuristic("target") == SensitivityLevel.MEDIUM


# R-SEC3 safe_tools 覆盖强制 LOW（最高优先级）[P0]
def test_safe_tools_override_forces_low():
    assert (
        _level("delete_thing", safe_tools=("delete_thing",)) == SensitivityLevel.LOW
    )


# R-SEC3 high_risk_tools 覆盖强制 CRITICAL [P0]
def test_high_risk_tools_override_forces_critical():
    assert _level("echo", high_risk_tools=("echo",)) == SensitivityLevel.CRITICAL


# KG-2 救援路径：high_risk_tools 可纠正谎报只读的变更类工具
def test_high_risk_tools_rescues_misreported_readonly():
    level = _level(
        "write_file",
        annotations={"readOnlyHint": True},
        high_risk_tools=("write_file",),
    )
    assert level == SensitivityLevel.CRITICAL


# inv-a3 命名规则 [P1]
def test_adapt_naming_rule_plain_server():
    tool = _adapter("fs").adapt(make_descriptor("read_file"))

    assert tool.name == "fs__read_file"
    assert tool.full_name == "mcp_fs__read_file"
    assert tool.namespace == "mcp"


def test_adapt_naming_rule_server_with_whitespace():
    tool = _adapter("my server").adapt(make_descriptor("read_file"))

    assert tool.name == "my_server__read_file"
    assert tool.full_name == "mcp_my_server__read_file"


# inv-a5 / R-ADPT4 LOW policy 派生 [P0]
def test_low_tool_policy_derivation():
    tool = _adapter().adapt(
        make_descriptor("list_items", annotations={"readOnlyHint": True})
    )

    assert tool.sensitivity == SensitivityLevel.LOW
    assert tool.read_only is True
    assert tool.is_idempotent is True
    assert tool.is_reversible is True
    assert tool.audit_required is False
    assert tool.trust_level_required == TrustLevel.EXTERNAL
    assert tool.max_output_bytes == MCP_TOOL_MAX_OUTPUT_BYTES == 128_000


# inv-a5 / R-ADPT4 + R-SEC1 CRITICAL policy 派生 [P0]
def test_critical_tool_policy_derivation():
    tool = _adapter().adapt(
        make_descriptor("write_file", annotations={"destructiveHint": True})
    )

    assert tool.sensitivity == SensitivityLevel.CRITICAL
    assert tool.audit_required is True
    assert tool.is_reversible is False
    assert tool.read_only is False
    assert tool.is_idempotent is False


# inv-a5 MEDIUM policy 派生 [P1]
def test_medium_tool_policy_derivation():
    tool = _adapter().adapt(make_descriptor("echo"))

    assert tool.sensitivity == SensitivityLevel.MEDIUM
    assert tool.read_only is False
    assert tool.is_idempotent is False
    assert tool.is_reversible is True
    assert tool.audit_required is False


# inv-a4 / R-ADPT2 空/非法 inputSchema → 降级默认 [P1]
@pytest.mark.parametrize(
    "schema",
    [{}, None, {"properties": {}}, {"foo": 1}, {"type": ""}],
)
def test_invalid_input_schema_degrades_to_default(schema):
    tool = _adapter().adapt(make_descriptor("echo", input_schema=schema))
    assert dict(tool.input_schema) == {"type": "object", "properties": {}}


# R-ADPT2 合法 inputSchema 透传 [P1]
def test_valid_input_schema_passthrough():
    schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    tool = _adapter().adapt(make_descriptor("read_file", input_schema=schema))
    assert dict(tool.input_schema) == schema


# R-ADPT3 when_to_use 兜底非空 [P1]
def test_when_to_use_falls_back_to_server_and_tool_name():
    tool = _adapter("fs").adapt(make_descriptor("echo", description="", when_to_use=""))

    assert tool.when_to_use.strip() != ""
    assert "fs" in tool.when_to_use
    assert "echo" in tool.when_to_use


def test_when_to_use_falls_back_to_description():
    tool = _adapter("fs").adapt(make_descriptor("echo", description="d", when_to_use=""))
    assert tool.when_to_use == "d"


# R-ADPT1 executor 转发 + tool_name 回填 [P1]
@pytest.mark.asyncio
async def test_executor_forwards_and_backfills_tool_name():
    handle = RecordingHandle(returns=ToolResult(success=True, data="ok"))
    cfg = McpServerConfig(name="fs", transport="stdio", command="py")
    tool = McpToolAdapter(cfg, handle).adapt(make_descriptor("echo"))

    result = await tool.executor(None, text="hi")

    assert handle.calls == [("echo", {"text": "hi"})]
    assert result.tool_name == "mcp_fs__echo"
    assert result.success is True


# R-TOK1 snake / kebab / camel 拆分等价 [P2, property]
def test_tokenize_style_equivalence():
    assert _tokenize("read_file") == _tokenize("read-file") == _tokenize("readFile")
    assert _tokenize("read_file") == {"read", "file"}
