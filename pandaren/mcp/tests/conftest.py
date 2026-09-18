"""pandaren/mcp/tests/conftest.py — MCP 核心模块测试共享 fixtures 与内存替身。

设计依据：pandaren/mcp/tests/design/mcp-core.design.md（§5 Mock 决策 / §6 文件布局）。

替身优先级（能不用 mock 就不用）：
  - 纯函数 / 纯值（config / tool_adapter / _serialize_content）→ 零替身
  - 协议边界（ClientSession）→ FakeSession（内存对象，记录入参）
  - 外部进程边界（stdio 子进程）→ mock_mcp_server.py 真实子进程
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

# pandaren/mcp/tests 无 __init__.py，pytest 只会把本目录加入 sys.path；
# 显式补仓库根，保证 `import pandaren.mcp.*` 可用。
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

MOCK_PATH = Path(__file__).parent / "mock_mcp_server.py"

from pandaren.mcp.client import McpToolDescriptor  # noqa: E402
from pandaren.mcp.config import McpServerConfig, McpTransport  # noqa: E402
from pandaren.mcp.manager import McpClientManager  # noqa: E402

# ──────────────────────────────────────────────────────────────
# 内存替身（Fake）
# ──────────────────────────────────────────────────────────────


@dataclass
class FakeContent:
    """模拟 mcp TextContent（只暴露被 `_serialize_content` 读取的 text 字段）。"""

    text: str


@dataclass
class FakeResult:
    """模拟 CallToolResult（content / isError / structuredContent 三处读取点）。"""

    content: list = field(default_factory=list)
    is_error: bool = False
    structured_content: Any = None


class FakeSession:
    """内存 ClientSession 替身：可注入返回值 / 异常 / 慢协程，并记录入参。"""

    def __init__(
        self,
        *,
        returns: Any = None,
        raises: BaseException | None = None,
        delay: float = 0.0,
    ) -> None:
        self._returns = returns
        self._raises = raises
        self._delay = delay
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        return self._returns


class RecordingHandle:
    """McpSessionHandle 替身：记录 call_tool 入参并返回固定 ToolResult。"""

    def __init__(self, returns: Any) -> None:
        self._returns = returns
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        return self._returns


class StubHandle:
    """极简句柄替身：仅需 aclose / is_alive 供 manager 生命周期分支使用。"""

    def __init__(self) -> None:
        self.closed = False

    @property
    def is_alive(self) -> bool:
        return False

    async def aclose(self) -> None:
        self.closed = True


class FakeRegistry:
    """ToolRegistry 替身（duck-typed）：仅需 register_tool(tool, *, skip_if_exists)。"""

    def __init__(self) -> None:
        self.register_calls: list[tuple[str, bool]] = []

    def register_tool(self, tool: Any, *, skip_if_exists: bool = True) -> None:
        self.register_calls.append((tool.full_name, skip_if_exists))


# ──────────────────────────────────────────────────────────────
# 构造助手（纯函数，无状态）
# ──────────────────────────────────────────────────────────────

_DEFAULT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def make_descriptor(
    name: str,
    *,
    description: str = "",
    input_schema: Any = None,
    when_to_use: str = "",
    annotations: Any = None,
) -> McpToolDescriptor:
    """构造 McpToolDescriptor（未给 schema 时用空 object schema）。"""
    return McpToolDescriptor(
        name=name,
        description=description,
        input_schema=_DEFAULT_SCHEMA if input_schema is None else input_schema,
        when_to_use=when_to_use,
        annotations=annotations,
    )


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def mock_stdio_config():
    """返回 mock stdio 配置工厂：固定 sys.executable + 真实 mock server 脚本。"""

    def _make(name: str = "mock", **overrides: Any) -> McpServerConfig:
        kwargs: dict[str, Any] = dict(
            name=name,
            transport=McpTransport.STDIO,
            command=sys.executable,
            args=[str(MOCK_PATH)],
            cwd=str(PROJECT_ROOT),
            connect_timeout=15.0,
            call_timeout=15.0,
        )
        kwargs.update(overrides)
        return McpServerConfig(**kwargs)

    return _make


@pytest_asyncio.fixture
async def connected_manager(mock_stdio_config):
    """已连接 mock server 的 manager（function-scope，结束回收子进程）。"""
    mgr = McpClientManager()
    await mgr.connect(mock_stdio_config())
    try:
        yield mgr
    finally:
        await mgr.aclose_all()
