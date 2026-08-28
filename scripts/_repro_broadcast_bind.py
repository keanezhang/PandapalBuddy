"""临时复现脚本 2：验证 app._register_skill_hooks 的查找逻辑在双层嵌套下失效。"""
from pandaren.hook.hooks import CompositeAgentHooks
from pandapal.hooks.skill_hooks import SkillAwareHooks

# ── 模拟 run_local + builder 的真实装配 ──
# run_local.py:671-676：内层 Composite 含 SkillAwareHooks
inner = CompositeAgentHooks()
inner.add(SkillAwareHooks())

# builder.py:918-921：外层 Composite 先 add obs_adapter，再 add 应用层 hooks
class _FakeObsAdapter:
    pass

outer = CompositeAgentHooks()
outer.add(_FakeObsAdapter())  # ObservabilityHooksAdapter 占位
outer.add(inner)

# ── 模拟 app.py:208-224 的查找逻辑（逐行复刻）──
hooks = outer  # blueprint.hooks_template
skill_hooks = None
if isinstance(hooks, SkillAwareHooks):
    skill_hooks = hooks
else:
    inner_list = getattr(hooks, "_hooks", None)
    if isinstance(inner_list, list):
        for h in inner_list:
            if isinstance(h, SkillAwareHooks):
                skill_hooks = h
                break

print("hooks_template 类型:", type(hooks).__name__)
print("hooks_template._hooks 元素:", [type(h).__name__ for h in hooks._hooks])
print("内层 Composite._hooks 元素:", [type(h).__name__ for h in inner._hooks])
print("=== 结论 ===")
print("app 查找找到 SkillAwareHooks:", skill_hooks is not None)
if skill_hooks is None:
    print("→ broadcast 绑定失败 → 即使签名修好 SKILL_ACTIVATED 也不推送（_broadcast 恒 None）")
