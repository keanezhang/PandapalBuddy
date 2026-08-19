"""pandaren/skill/tests/conftest.py — SkillRegistry 测试共享基建。

对齐 skill_registry.design.md §6 全局 fixture 约定：
  1. clear_script_cache（autouse）：每个测试后 script_loader.clear_cache()
  2. write_skill_script：写 Action Skill 脚本（entry_function="run"），返回 base_path
  3. make_registry：构造 SkillRegistry(tool_registry=..., audit_log=...)
  4. make_context：构造 ToolContext(metadata={"skill_registry": registry})
  5. FakeAuditLog：events 记录 write_sync 调用（event_type, detail）
"""
from __future__ import annotations

import os
import sys
from types import MappingProxyType

import pytest

# 保证从项目根 import pandaren.*（与仓库既有测试 pandaren/tool/tests 一致）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from pandaren.skill.registry import SkillRegistry  # noqa: E402
from pandaren.skill.script_loader import clear_cache  # noqa: E402
from pandaren.tool.definition.context import ToolContext  # noqa: E402
from pandaren.tool.facade import ToolRegistry  # noqa: E402

_SCRIPT_TEMPLATE = '''\
def run(query: str) -> str:
    """Args:
    query: 查询词
    """
    return {return_expr}
'''


class FakeAuditLog:
    """内存审计桩：记录 (event_type, detail)，不落盘。"""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def write_sync(self, event_type, **kwargs) -> None:
        self.events.append((event_type, kwargs.get("detail", "")))


@pytest.fixture
def fake_audit() -> FakeAuditLog:
    return FakeAuditLog()


@pytest.fixture
def tool_registry() -> ToolRegistry:
    return ToolRegistry()


@pytest.fixture(autouse=True)
def clear_script_cache():
    """每个测试后清空 script_loader 全局模块缓存（隔离 tmp_path 加载）。"""
    yield
    clear_cache()


@pytest.fixture
def write_skill_script(tmp_path):
    """写 Action Skill 脚本，返回 base_path 字符串。

    默认脚本：def run(query: str) -> str: 返回 f"processed:{query}"。
    可通过 return_expr 定制返回表达式以区分多版脚本。
    """

    def _write(
        script_name: str = "tool.py",
        return_expr: str = 'f"processed:{query}"',
    ) -> str:
        (tmp_path / script_name).write_text(
            _SCRIPT_TEMPLATE.format(return_expr=return_expr),
            encoding="utf-8",
        )
        return str(tmp_path)

    return _write


@pytest.fixture
def make_registry():
    """构造 SkillRegistry（默认 tool_registry=None / audit_log=None）。"""

    def _make(
        *,
        tool_registry: ToolRegistry | None = None,
        audit_log: FakeAuditLog | None = None,
        max_description_chars: int = 250,
    ) -> SkillRegistry:
        return SkillRegistry(
            tool_registry=tool_registry,
            audit_log=audit_log,
            max_description_chars=max_description_chars,
        )

    return _make


@pytest.fixture
def make_context():
    """构造 ToolContext，metadata 注入 {"skill_registry": registry}。"""

    def _make(registry: SkillRegistry) -> ToolContext:
        return ToolContext(
            run_id="r1",
            step_n=1,
            agent_id="a1",
            session_id="s1",
            metadata=MappingProxyType({"skill_registry": registry}),
        )

    return _make
