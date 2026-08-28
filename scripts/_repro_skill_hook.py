"""临时复现脚本：验证 SkillAwareHooks（4 参旧签名）经 Composite 转发被静默吞掉。

模拟生产链路：
  run_core._safe_hook("on_skill_activated", skill_name, run_id, step_n)
  -> builder 外层 Composite（恒传 6 参）
  -> run_local 内层 Composite（恒传 6 参）
  -> SkillAwareHooks.on_skill_activated（4 参）→ TypeError → 被吞
"""
import io
import logging

from pandaren.hook.hooks import CompositeAgentHooks
from pandapal.hooks.skill_hooks import SkillAwareHooks

stream = io.StringIO()
handler = logging.StreamHandler(stream)
logger = logging.getLogger("pandaren.hook.hooks")
logger.addHandler(handler)
logger.setLevel(logging.DEBUG)

# 生产链路：两层 Composite 嵌套（builder 外层 + run_local 内层）
inner = CompositeAgentHooks()
inner.add(SkillAwareHooks())
outer = CompositeAgentHooks()
outer.add(inner)

# 模拟 run_core._safe_hook 的生产调用（3 参 + session_id 注入）
outer.on_skill_activated(skill_name="math", run_id="r1", step_n=1, session_id="s1")

out = stream.getvalue()
print("=== composite 日志 ===")
print(out if out.strip() else "(无日志)")
print("=== 结论 ===")
print("SkillAwareHooks 被 TypeError 静默吞掉" if "failed" in out else "正常转发")
