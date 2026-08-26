"""pandaren/engine/tests/test_denied_calls_feedback.py — 全部 tool_calls 被拒时反馈闭环回归测试。

背景（run_core._run_stream_core 修复）：
  LLM 一轮发出的 tool_calls 全部被拒（未注册 / 权限拒绝）时，旧代码 `if not approved_calls: pass`
  不提交任何 tool 结果 → LLM 下一轮看不到「调用被拒」反馈，只会盲目重试同一批调用，
  直至 3 轮后 PERMISSION_EXHAUSTED（白耗 2 轮 LLM 调用，体验像卡死）。
  修复后：为未写入结果的 tool_call 补占位并原子提交 → LLM 下一轮能看到反馈并正常收尾。

风险映射：
  - P0：全部被拒时 LLM 必须收到反馈（否则盲目重试 / PERMISSION_EXHAUSTED）
  - P1：Agent 必须正常结束（不卡死、不误报成功）
  - P1：正常路径（有结果）不得受影响 —— 由存量 451 测试覆盖

确定性：ScriptedLLM 固定剧本，零网络 / 零随机 / 零时间依赖。
"""
from __future__ import annotations

import pytest

from pandaren.builder import AgentBuilder
from pandaren.identity.models import Identity, TrustLevel
from pandaren.llm.types import LLMResponse


class ScriptedLLM:
    """固定剧本 mock LLM：第一轮发 tool_call（未注册工具），第二轮收尾。"""

    model_name = "mock"

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def call(self, messages, tools=None, settings=None, **kwargs):
        self.calls.append(messages)
        if len(self.calls) == 1:
            return LLMResponse(
                content=None,
                finish_reason="tool_calls",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                id="resp-1",
                model="mock",
                created=1,
                tool_calls=[
                    {
                        "id": "call_denied_1",
                        "type": "function",
                        "function": {"name": "nonexistent_tool", "arguments": "{}"},
                    }
                ],
            )
        return LLMResponse(
            content="done",
            finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            id="resp-2",
            model="mock",
            created=2,
        )


@pytest.fixture
def agent():
    llm = ScriptedLLM()
    identity = Identity(
        agent_id="test-denied-feedback",
        agent_name="test",
        when_to_use="test only",
        sensitive_permissions=frozenset(),
        trust_level=TrustLevel.ORCHESTRATOR,
    )
    builder = (
        AgentBuilder()
        .identity(agent_id=identity.agent_id, agent_name=identity.agent_name,
                  when_to_use=identity.when_to_use,
                  sensitive_permissions=identity.sensitive_permissions,
                  trust_level=identity.trust_level)
        .llm(llm)
        .tools([])
        .behavior(max_steps=5)
        .system_prompt("You are a test agent.")
    )
    return builder.build(), llm


@pytest.mark.asyncio
async def test_unregistered_tool_denied_feedback_closes_loop(agent):
    """P0：全部 tool_calls 被拒（未注册）时，LLM 第二轮必须收到反馈并正常收尾。"""
    built, llm = agent
    result = await built.run("test", session_id="test-denied-001")

    # LLM 必须收到两轮调用（第一轮发 tool_call，第二轮基于反馈收尾）
    assert len(llm.calls) == 2, f"期望 2 轮 LLM 调用，实际 {len(llm.calls)}（修复未生效则卡死在重试循环）"
    second_round_messages = llm.calls[1]
    tool_msgs = [m for m in second_round_messages if m.get("role") == "tool"]
    assert tool_msgs, "第二轮 LLM 未收到任何 tool 结果反馈（修复未生效）"
    denied = [m for m in tool_msgs if m.get("tool_call_id") == "call_denied_1"]
    assert denied, f"未找到 call_denied_1 的结果条目，实际 tool 消息: {tool_msgs}"
    assert "nonexistent_tool" in denied[0]["content"], denied[0]

    # Agent 必须正常结束（不卡死、不误报）
    assert result.success, f"意外终止: {result.terminal_reason} / {result.error}"
