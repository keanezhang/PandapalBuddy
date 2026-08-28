"""临时验证：Bug A（签名对齐推送）+ Bug B（递归查找）修复验证。"""
import asyncio

from pandaren.hook.hooks import CompositeAgentHooks
from pandapal.hooks.skill_hooks import SkillAwareHooks
from pandapal.events.normalized import EventType
from pandapal.app import _find_skill_hooks_in


class FakeBroadcast:
    def __init__(self):
        self.events = []
    async def send(self, event):
        self.events.append(event)


async def main():
    # ── Bug A：真实装配下 SKILL_ACTIVATED 被推送到 broadcast ──
    skill_hooks = SkillAwareHooks()
    inner = CompositeAgentHooks()
    inner.add(skill_hooks)
    outer = CompositeAgentHooks()
    outer.add(inner)

    fake_broadcast = FakeBroadcast()
    skill_hooks.bind_broadcast(fake_broadcast)
    outer.on_skill_activated(skill_name="math", run_id="run-1", step_n=1, session_id="sess-42")
    await asyncio.sleep(0.05)  # 等 create_task 落地
    print("SKILL_ACTIVATED 推送条数:", len(fake_broadcast.events))
    assert len(fake_broadcast.events) == 1, "SKILL_ACTIVATED 未被推送"
    assert fake_broadcast.events[0].event_type == EventType.SKILL_ACTIVATED

    # ── Bug B：双层嵌套递归查找 ──
    found = _find_skill_hooks_in(outer)
    assert isinstance(found, SkillAwareHooks), "递归查找失败"
    print("双层嵌套查找: OK")
    assert _find_skill_hooks_in(CompositeAgentHooks()) is None, "未启用场景应返回 None"
    print("未启用场景: OK")
    print("=== 两个 bug 修复验证全部通过 ===")


asyncio.run(main())
