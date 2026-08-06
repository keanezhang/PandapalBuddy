"""SkillManager 数量上限用例。

覆盖：
  - create_skill 达上限抛 ValueError
  - import_and_build_event（非覆盖）达上限 → SKILL_IMPORTED success=False
  - overwrite 导入不受上限拦截（在 dispatch 阶段因 source_path 缺失才失败）
"""
from pathlib import Path

import pytest

from pandapal.resources import skill_manager as sm
from pandapal.resources.skill_manager import SkillManager, MAX_USER_SKILLS


@pytest.fixture
def mgr(tmp_path: Path) -> SkillManager:
    system_dir = tmp_path / "system"
    user_dir = tmp_path / "user"
    system_dir.mkdir()
    user_dir.mkdir()
    return SkillManager(system_dir=system_dir, user_dir=user_dir)


def _fill_to_limit(monkeypatch, count: int) -> None:
    """把 _load_user_skills 伪造成返回 count 个用户 Skill。"""
    fake = {f"skill-{i}": object() for i in range(count)}
    monkeypatch.setattr(SkillManager, "_load_user_skills", lambda self: fake)


def test_create_skill_blocked_at_limit(mgr, monkeypatch):
    _fill_to_limit(monkeypatch, MAX_USER_SKILLS)
    with pytest.raises(ValueError, match="数量上限"):
        mgr.create_skill("new-skill", "d", "w", "content", [])


def test_create_skill_ok_below_limit(mgr, monkeypatch):
    _fill_to_limit(monkeypatch, MAX_USER_SKILLS - 1)
    # 未达上限 → 不因配额报错（真正写入走正常路径）
    dto = mgr.create_skill("brand-new", "desc", "when", "content", [])
    assert dto.name == "brand-new"


@pytest.mark.asyncio
async def test_import_blocked_at_limit(mgr, monkeypatch):
    _fill_to_limit(monkeypatch, MAX_USER_SKILLS)
    event = await mgr.import_and_build_event(
        fmt="zip", overwrite=False, source_path="/tmp/x.zip",
    )
    payload = event.payload
    assert payload["success"] is False
    assert "数量上限" in payload["error"]
