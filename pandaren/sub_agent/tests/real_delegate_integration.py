"""真实集成验证：子 Agent 委派并发隔离（真实 LLM，非 mock）

验证目标（对应用户确认的两点）：
  1. 主 Agent 不受影响——主 Agent 走 SessionAgentPool → blueprint.materialize()，
     与 SubAgentRegistry 完全无关；此处用真实 LLM 跑一个主 Agent run() 确认。
  2. 子 Agent 委派真实隔离——registry 每次委派 materialize 全新实例（独立 Memory），
     两个 session 并发委派同一子 Agent，各自上下文互不串扰（真实 LLM 输出验证）。

运行：
  DEEPSEEK_API_KEY=sk-xxx python pandaren/sub_agent/tests/real_delegate_integration.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from pandaren.agent import AgentStatus
from pandaren.builder import AgentBuilder
from pandaren.identity.models import Identity, PERMISSION_ALL, TrustLevel
from pandaren.llm.client import OpenAICompatibleClient
from pandaren.sub_agent.registry import SubAgentRegistry
from pandaren.sub_agent.tests.test_isolation import _FakeBlueprint, _make_context


def _llm():
    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise SystemExit("需要 DEEPSEEK_API_KEY / OPENAI_API_KEY 环境变量")
    return OpenAICompatibleClient.for_deepseek(api_key=api_key, model_name="deepseek-v4-flash", timeout=60.0)


async def _main() -> int:
    failed = 0

    # ═══ 1. 主 Agent 真实 run（证明主 Agent 路径不受影响）═══
    print("\n═══ 1. 主 Agent 真实 run()（走 blueprint.materialize，与 registry 无关）═══")
    main_bp = (
        AgentBuilder()
        .identity(
            agent_id="real.main.agent",
            agent_name="真实主代理",
            when_to_use="主代理集成验证",
            sensitive_permissions=PERMISSION_ALL,
            trust_level=TrustLevel.ORCHESTRATOR,
        )
        .llm(_llm())
        .system_prompt("你是集成测试主代理。请用一句话简洁回答。")
        .behavior(max_steps=3)
        .build_blueprint()
    )
    main_agent = main_bp.materialize()
    r = await main_agent.run("1 + 1 等于几？只回答数字。", session_id="real-main-1")
    ok = r.success and "2" in str(r.output)
    print(f"  主 Agent run: success={r.success}, output={str(r.output)[:60]!r}")
    print(f"  {'✅ 通过' if ok else '❌ 失败'}")
    failed += 0 if ok else 1

    # ═══ 2. 子 Agent 真实委派——并发两 session，任务结果互不串扰 ═══
    print("\n═══ 2. 子 Agent 真实委派——并发两 session 不同任务，结果互不串扰 ═══")

    # 子 Agent 蓝图：无记忆（默认），专注计算
    child_bp = (
        AgentBuilder()
        .identity(
            agent_id="real.child.agent",
            agent_name="真实子代理",
            when_to_use="被主代理委派、有记忆的子代理",
            sensitive_permissions=PERMISSION_ALL,
            trust_level=TrustLevel.SUB_AGENT,
        )
        .llm(_llm())
        .system_prompt(
            "你是集成测试子代理。请只回答数学问题的最终数字，不要任何解释。"
        )
        .behavior(max_steps=4)
        .build_blueprint()
    )

    reg = SubAgentRegistry()
    reg.register(child_bp)
    assert reg.get_status("real.child.agent") == AgentStatus.HEALTHY
    # 每次委派 materialize 全新实例
    a1, a2 = child_bp.materialize(), child_bp.materialize()
    assert a1 is not a2, "materialize 应产出不同实例"
    print("  ✅ materialize 每次产出新实例（独立 Memory / Hooks）")

    # 并发两个 session：A 问 5×7，B 问 8×3，断言各自拿到正确数字、互不串扰
    async def session(session_id: str, question: str):
        ctx = _make_context(session_id)
        return await reg.call_agent("真实子代理", question, ctx)

    sess_a = f"real-sess-{uuid.uuid4()}"
    sess_b = f"real-sess-{uuid.uuid4()}"
    ra, rb = await asyncio.gather(
        session(sess_a, "5 × 7 等于多少？只回答数字。"),
        session(sess_b, "8 × 3 等于多少？只回答数字。"),
    )

    a_out = str(ra.data) if ra.success else f"ERR:{ra.error}"
    b_out = str(rb.data) if rb.success else f"ERR:{rb.error}"
    print(f"  session A (5×7) → {a_out!r}")
    print(f"  session B (8×3) → {b_out!r}")

    ok_a = "35" in a_out and "24" not in a_out
    ok_b = "24" in b_out and "35" not in b_out
    ok_first = ra.success and rb.success
    print(f"  {'✅ A 结果正确且不含 B 的答案（无串扰）' if ok_a else '❌ A 串扰/错误!'}")
    print(f"  {'✅ B 结果正确且不含 A 的答案（无串扰）' if ok_b else '❌ B 串扰/错误!'}")
    failed += 0 if (ok_a and ok_b and ok_first) else 1

    # ═══ 2b. 开启 memory 持久化后：同 session 记忆串联 + 跨 session 隔离 ═══
    print("\n═══ 2b. 开启 memory(db_path) 后——同 session 记忆串联，跨 session 不串 ═══")
    import tempfile
    mem_db = os.path.join(tempfile.gettempdir(), f"real-delegate-mem-{uuid.uuid4()}.db")
    mem_child_bp = (
        AgentBuilder()
        .identity(
            agent_id="real.mem.child",
            agent_name="有记忆子代理",
            when_to_use="记忆隔离验证",
            sensitive_permissions=PERMISSION_ALL,
            trust_level=TrustLevel.SUB_AGENT,
        )
        .llm(_llm())
        .memory(db_path=mem_db, session_mode="multi_turn")
        .system_prompt(
            "你是集成测试子代理。请记住用户告诉你的名字。"
            "被问到'我叫什么'时，只回答你记住的名字。如果不知道，回答'不知道'。"
        )
        .behavior(max_steps=4)
        .build_blueprint()
    )
    reg.register(mem_child_bp)

    async def mem_session(session_id: str, my_name: str):
        ctx = _make_context(session_id)
        await reg.call_agent("有记忆子代理", f"我的名字是 {my_name}。", ctx)
        return await reg.call_agent("有记忆子代理", "我叫什么？", ctx)

    ma, mb = await asyncio.gather(
        mem_session(f"mem-sess-{uuid.uuid4()}", "Alice"),
        mem_session(f"mem-sess-{uuid.uuid4()}", "Bob"),
    )
    m_a = str(ma.data) if ma.success else f"ERR:{ma.error}"
    m_b = str(mb.data) if mb.success else f"ERR:{mb.error}"
    print(f"  记忆 session A 问名字 → {m_a!r}")
    print(f"  记忆 session B 问名字 → {m_b!r}")
    ok_ma = "Alice" in m_a and "Bob" not in m_a
    ok_mb = "Bob" in m_b and "Alice" not in m_b
    print(f"  {'✅ A 记住 Alice 且不含 Bob（同 session 串联 + 跨 session 隔离）' if ok_ma else '❌ A 记忆失败/串扰!'}")
    print(f"  {'✅ B 记住 Bob 且不含 Alice' if ok_mb else '❌ B 记忆失败/串扰!'}")
    failed += 0 if (ok_ma and ok_mb) else 1
    try:
        os.remove(mem_db)
    except OSError:
        pass

    # ═══ 3. 契约：Agent 实例直接注册 → TypeError ═══
    print("\n═══ 3. register(Agent 实例) → TypeError（兼容路径已移除）═══")
    reg2 = SubAgentRegistry()
    try:
        reg2.register(main_agent)  # Agent 实例
        print("  ❌ 失败：未抛 TypeError")
        failed += 1
    except TypeError as e:
        print(f"  ✅ TypeError: {str(e)[:60]}...")
    except Exception as e:
        print(f"  ❌ 失败：抛了 {type(e).__name__}")
        failed += 1

    print(f"\n{'=' * 50}\n结果: {'✅ 全部通过' if failed == 0 else f'❌ {failed} 项失败'}")
    return failed


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
