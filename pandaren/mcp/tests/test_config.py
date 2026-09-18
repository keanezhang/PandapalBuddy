"""pandaren/mcp/tests/test_config.py — McpServerConfig 校验 / 序列化（unit，零 mock）。

设计依据：mcp-core.design.md 分组 A（MCP-01..13b）。
风险映射：R-CONF1 / R-CONF2（fail-fast 契约，P0）、R-SER1、R-ADPT1(slug, P2)。
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from pandaren.mcp.config import (
    DEFAULT_CALL_TIMEOUT,
    DEFAULT_CONNECT_TIMEOUT,
    McpConfigError,
    McpServerConfig,
    McpTransport,
)
from pandaren.tool.types import ToolTier


# inv-c1 字段保真 + frozen：[P2]
def test_valid_stdio_config_preserves_fields():
    cfg = McpServerConfig(name="fs", transport="stdio", command="python", args=["s.py"])

    assert cfg.transport is McpTransport.STDIO
    assert cfg.tier is ToolTier.DEFERRED
    assert cfg.enabled is True
    assert cfg.connect_timeout == DEFAULT_CONNECT_TIMEOUT == 20.0
    assert cfg.call_timeout == DEFAULT_CALL_TIMEOUT == 60.0
    assert cfg.args == ("s.py",)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.name = "x"  # type: ignore[misc]


# R-CONF1 transport 非法 fail-fast [P0]
@pytest.mark.parametrize("bad_transport", ["sse", "websocket", "tcp"])
def test_invalid_transport_raises(bad_transport):
    with pytest.raises(McpConfigError) as excinfo:
        McpServerConfig(name="x", transport=bad_transport, command="python")

    assert isinstance(excinfo.value, ValueError)


# R-CONF1 stdio 缺 command fail-fast [P0]
@pytest.mark.parametrize("command", [None, "", "   "])
def test_stdio_without_command_raises(command):
    with pytest.raises(McpConfigError, match="缺少 command"):
        McpServerConfig(name="x", transport="stdio", command=command)


# R-CONF1 http 缺 url fail-fast [P0]
@pytest.mark.parametrize("url", [None, "", "   "])
def test_http_without_url_raises(url):
    with pytest.raises(McpConfigError, match="缺少 url"):
        McpServerConfig(name="x", transport="http", url=url)


# R-CONF1 http url 非 http(s) fail-fast [P0]
@pytest.mark.parametrize("bad_url", ["ftp://h", "file:///x", "localhost:8080", "//h/p"])
def test_http_url_non_http_scheme_raises(bad_url):
    with pytest.raises(McpConfigError, match="http://"):
        McpServerConfig(name="x", transport="http", url=bad_url)


def test_http_url_https_is_accepted():
    cfg = McpServerConfig(name="x", transport="http", url="https://ok")
    assert cfg.url == "https://ok"


# R-CONF1 timeout<=0 fail-fast [P0]
@pytest.mark.parametrize(
    "kwargs",
    [
        {"connect_timeout": 0},
        {"connect_timeout": -1.0},
        {"call_timeout": 0},
        {"call_timeout": -0.5},
    ],
)
def test_non_positive_timeout_raises(kwargs):
    with pytest.raises(McpConfigError):
        McpServerConfig(name="x", transport="stdio", command="python", **kwargs)


# R-CONF1 name 空/空白/None fail-fast [P0]
@pytest.mark.parametrize("name", ["", "   ", None])
def test_blank_name_raises(name):
    with pytest.raises(McpConfigError, match="name"):
        McpServerConfig(name=name, transport="stdio", command="python")


# inv-c2 未启用时跳过传输细节校验 [P1]
def test_disabled_server_skips_transport_validation():
    cfg = McpServerConfig(name="x", transport="stdio", enabled=False)

    assert cfg.command is None
    cfg.validate()  # 幂等，不抛


# R-CONF2 from_mapping 缺必填项 fail-fast [P0]
@pytest.mark.parametrize(
    "mapping, fragment",
    [
        ({"transport": "stdio", "command": "py"}, "缺少 name"),
        ({"name": "x"}, "缺少 transport"),
    ],
)
def test_from_mapping_missing_required_raises(mapping, fragment):
    with pytest.raises(McpConfigError, match=fragment):
        McpServerConfig.from_mapping(mapping)


# R-CONF2 from_mapping 未知键 fail-fast [P0]
def test_from_mapping_unknown_key_raises():
    bad = {"name": "x", "transport": "stdio", "command": "py", "commnad": "typo"}
    with pytest.raises(McpConfigError, match="未知字段"):
        McpServerConfig.from_mapping(bad)


# R-CONF2 from_mapping 非 Mapping fail-fast [P0]
def test_from_mapping_non_mapping_raises():
    with pytest.raises(McpConfigError, match="期望 Mapping"):
        McpServerConfig.from_mapping(["name", "transport"])


# R-CONF1 tier 归一化（branch: bool/int/str/其他）[P1]
@pytest.mark.parametrize(
    "value, expected",
    [
        (1, ToolTier.ALWAYS),
        (2, ToolTier.DEFERRED),
        ("always", ToolTier.ALWAYS),
        ("DEFERRED", ToolTier.DEFERRED),
        (" always ", ToolTier.ALWAYS),
        (ToolTier.ALWAYS, ToolTier.ALWAYS),
    ],
)
def test_tier_valid_values_are_normalized(value, expected):
    cfg = McpServerConfig(name="x", transport="stdio", command="py", tier=value)
    assert cfg.tier is expected


@pytest.mark.parametrize("value", [True, 0, 99, "nope", 1.5])
def test_tier_invalid_values_raise(value):
    with pytest.raises(McpConfigError):
        McpServerConfig(name="x", transport="stdio", command="py", tier=value)


# inv-c3 往返等价 [property] + R-SER1 JSON 可序列化 [P1/P2]
def test_to_dict_roundtrip_and_json_serializable(mock_stdio_config):
    cfg = mock_stdio_config(
        name="fs",
        args=["s.py"],
        env={"K": "V"},
        high_risk_tools=("write_file",),
        safe_tools=("list_items",),
    )
    data = cfg.to_dict()

    assert json.dumps(data)  # 不抛
    assert data["transport"] == "stdio"
    assert data["tier"] == "deferred"
    assert McpServerConfig.from_mapping(data) == cfg


def test_http_config_roundtrip():
    cfg = McpServerConfig(
        name="remote",
        transport="http",
        url="https://h/mcp",
        headers={"Authorization": "Bearer x"},
    )
    assert McpServerConfig.from_mapping(cfg.to_dict()) == cfg


# R-ADPT1 slug 空白替换 [P2]
@pytest.mark.parametrize(
    "name, expected_slug",
    [("my server", "my_server"), ("plain", "plain")],
)
def test_slug_replaces_whitespace(name, expected_slug):
    cfg = McpServerConfig(name=name, transport="stdio", command="py")
    assert cfg.slug == expected_slug
