"""pandaren/skill/tests/conftest.py — SkillRegistry 测试共享基建。

对齐 skill_registry.design.md §6 全局 fixture 约定：
  1. make_registry：构造 SkillRegistry(audit_log=..., max_description_chars=...)
  2. make_context：构造 ToolContext(metadata={"skill_registry": registry})
  3. FakeAuditLog：events 记录 write_sync 调用（event_type, detail）

注：原 clear_script_cache / write_skill_script fixture 依赖已删除的
pandaren.skill.script_loader（Action Skill 机制已随 script 字段一并移除），
随 conftest 清理。
"""
from __future__ import annotations

import os
import sys
from types import MappingProxyType

import pytest

# 保证从项目根 import pandaren.*（与仓库既有测试 pandaren/tool/tests 一致）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from pandaren.skill.registry import SkillRegistry  # noqa: E402
from pandaren.tool.definition.context import ToolContext  # noqa: E402
from pandaren.tool.facade import ToolRegistry  # noqa: E402


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


@pytest.fixture
def make_registry():
    """构造 SkillRegistry（默认 audit_log=None）。"""

    def _make(
        *,
        audit_log: FakeAuditLog | None = None,
        max_description_chars: int = 250,
    ) -> SkillRegistry:
        return SkillRegistry(
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
