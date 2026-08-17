"""真实集成验证：子 Agent LLM 配置在真实链路生效（真实 LLM，非 mock）

验证目标（对应用户确认的 v2 merge 语义）：
  1. settings 继承 —— 子 Agent 蓝图不写 llm_settings → 委派时实际收到父级 settings
  2. settings 覆盖 —— 子 Agent 蓝图写 temperature=0.2 → 该字段被覆盖，其余字段仍继承父级
  3. model 路由   —— 父级用 LLMRouter 注册两个真实 client，子 Agent 蓝图 model= 指定其一
                     → 委派时 router 按 target_model 路由到对应 client，真实响应成功

链路（与生产一致，不走手动 register）：
  AgentBuilder.sub_agents([SubAgentBlueprint]) → build() → _resolve_agent_registry()
    → _build_sub_agent_from_blueprint()（merge 逻辑所在）→ SubAgentRegistry
  委派：agent._loop._agent_registry.call_agent(...) → materialize → AgentLoop → client

实现手段：RecordingClient —— 透传探针。
  它包装真实 OpenAICompatibleClient，不伪造、不修改任何请求/响应，
  只是在每次 call/stream 时把实际收到的 (model_name, settings 快照) 记录下来。
  断言的是「真实链路上 client 实际收到的 settings」，不是 mock 行为。

运行：
  DEEPSEEK_API_KEY=sk-xxx python pandaren/sub_agent/tests/real_llm_config_integration.py

退出码：0=全部通过，1=有失败。
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from dataclasses import dataclass, fields
from typing import Any, AsyncGenerator

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _discover_api_key() -> str:
    """发现 DeepSeek/OpenAI API key。

    优先级：
      1. 环境变量 DEEPSEEK_API_KEY / OPENAI_API_KEY
      2. 项目根目录 .env.development（宽容处理注释前缀——
         项目惯例是 key 以 `# DEEPSEEK_API_KEY=...` 注释形态存放在 .env.development）
    找不到返回 ""（调用方自行失败）。
    """
    for env_name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        val = os.getenv(env_name, "").strip()
        if val:
            return val

    # 项目根 = 本文件上溯 3 级（tests → sub_agent → pandaren → 项目根）
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    for env_file in (".env.development", ".env"):
        path = os.path.join(project_root, env_file)
        try:
            with open(path, encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    # 宽容：允许行首 `# ` 注释前缀（项目 .env.development 即此形态）
                    if line.startswith("#"):
                        line = line.lstrip("#").strip()
                    if not line:
                        continue
                    if "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key, value = key.strip(), value.strip().strip('"').strip("'")
                    if key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY") and value:
                        return value
        except OSError:
            continue
    return ""


from pandaren.builder import AgentBuilder
from pandaren.identity.models import PERMISSION_ALL, TrustLevel
from pandaren.llm.client import OpenAICompatibleClient
from pandaren.llm.router import LLMRouter
from pandaren.llm.types import LLMResponse, LLMStreamChunk, ModelSettings
from pandaren.sub_agent.models import SubAgentBlueprint
from pandaren.sub_agent.tests.test_isolation import _make_context


# ════════════════════════════════════════════════════════════════
# 透传探针：记录每次真实调用收到的 model + settings 快照
# ════════════════════════════════════════════════════════════════
@dataclass
class CallRecord:
    model_name: str
    settings: dict[str, Any]  # settings 非 None 字段的快照


class RecordingClient:
    """包装真实 client：请求/响应原样透传，仅记录观测快照。"""

    def __init__(self, inner: OpenAICompatibleClient) -> None:
        self._inner = inner
        self.calls: list[CallRecord] = []

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    def _record(self, settings: ModelSettings | None) -> None:
        snap = {
            f.name: getattr(settings, f.name)
            for f in fields(ModelSettings)
            if getattr(settings, f.name) is not None
        } if settings is not None else {}
        self.calls.append(CallRecord(model_name=self._inner.model_name, settings=snap))

    async def call(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        settings: ModelSettings | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self._record(settings)
        return await self._inner.call(messages, tools, settings, **kwargs)

    async def stream_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        settings: ModelSettings | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[LLMStreamChunk, None]:
        self._record(settings)
        async for chunk in self._inner.stream_response(messages, tools, settings, **kwargs):
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


def _llm() -> OpenAICompatibleClient:
    api_key = _discover_api_key()
    if not api_key:
        raise SystemExit("未找到 API key：请设置 DEEPSEEK_API_KEY / OPENAI_API_KEY 环境变量，或配置项目 .env.development")
    return OpenAICompatibleClient.for_deepseek(api_key=api_key, model_name="deepseek-v4-flash", timeout=60.0)


def _check(label: str, ok: bool, detail: str = "") -> int:
    print(f"  {'✅' if ok else '❌'} {label}" + (f"  {detail}" if detail else ""))
    return 0 if ok else 1


def _bp(agent_id: str, name: str, *, model: str | None = None, llm_settings: ModelSettings | None = None) -> SubAgentBlueprint:
    """构造 SubAgentBlueprint（真实链路入口：builder.sub_agents 消费）。"""
    return SubAgentBlueprint(
        agent_id=agent_id,
        agent_name=name,
        when_to_use="配置验证子代理",
        system_prompt="你是配置验证子代理。只回答数字。",
        trust_level=TrustLevel.SUB_AGENT,
        sensitive_permissions=PERMISSION_ALL,
        model=model,
        llm_settings=llm_settings,
    )


async def _main() -> int:
    failed = 0
    api_key = _discover_api_key()
    if not api_key:
        raise SystemExit("未找到 API key：请设置 DEEPSEEK_API_KEY / OPENAI_API_KEY 环境变量，或配置项目 .env.development")

    # ═══ 场景 1 + 2：父级 settings 继承/覆盖（单 client + 探针）═══
    print("═══ 1+2. settings 继承/覆盖：builder.sub_agents 真实链路 ═══")
    probe = RecordingClient(_llm())
    parent = (
        AgentBuilder()
        .identity(
            agent_id="real.cfg.parent",
            agent_name="真实配置父代理",
            when_to_use="配置继承验证",
            sensitive_permissions=PERMISSION_ALL,
            trust_level=TrustLevel.ORCHESTRATOR,
        )
        .llm(probe)
        .llm_settings(temperature=0.7, max_tokens=2048)          # 父级基准
        .sub_agents(
            [
                _bp("real.cfg.inherit", "继承配置子代理"),          # 不写 llm_settings
                _bp("real.cfg.override", "覆盖配置子代理",
                    llm_settings=ModelSettings(temperature=0.2)),  # 只覆盖 temperature
            ],
            llm_client=probe,  # 子 Agent 复用同一真实 client（生产用法）
        )
        .behavior(max_steps=3)
        .build()
    )
    registry = parent._loop._agent_registry  # 与生产一致的 registry（build 内部构建）
    assert registry is not None, "sub_agents 注册后 registry 不应为 None"
    print(f"  registry 子 Agent 数: {registry.agent_count()}")

    # ── 1. 继承 ──
    before = len(probe.calls)
    r1 = await registry.call_agent("继承配置子代理", "1 + 1 等于几？只回答数字。", _make_context(f"real-cfg-1-{uuid.uuid4()}"))
    new_calls = probe.calls[before:]
    out1 = str(r1.data) if r1.success else f"ERR:{r1.error}"
    print(f"  继承委派输出: {out1!r}")
    last = new_calls[-1] if new_calls else None
    s1 = last.settings if last else {}
    print(f"  探针记录 settings: {s1}")
    failed += _check("继承：temperature=0.7 来自父级", s1.get("temperature") == 0.7, f"实际={s1.get('temperature')!r}")
    failed += _check("继承：max_tokens=2048 来自父级", s1.get("max_tokens") == 2048, f"实际={s1.get('max_tokens')!r}")
    failed += _check("继承：真实委派成功", r1.success and "2" in str(out1), f"output={out1!r}")

    # ── 2. 覆盖 ──
    before = len(probe.calls)
    r2 = await registry.call_agent("覆盖配置子代理", "2 + 2 等于几？只回答数字。", _make_context(f"real-cfg-2-{uuid.uuid4()}"))
    new_calls = probe.calls[before:]
    out2 = str(r2.data) if r2.success else f"ERR:{r2.error}"
    print(f"  覆盖委派输出: {out2!r}")
    last = new_calls[-1] if new_calls else None
    s2 = last.settings if last else {}
    print(f"  探针记录 settings: {s2}")
    failed += _check("覆盖：temperature=0.2 蓝图覆盖生效", s2.get("temperature") == 0.2, f"实际={s2.get('temperature')!r}")
    failed += _check("覆盖：max_tokens=2048 未写 → 继承父级", s2.get("max_tokens") == 2048, f"实际={s2.get('max_tokens')!r}")
    failed += _check("覆盖：真实委派成功", r2.success and "4" in str(out2), f"output={out2!r}")
    await probe.aclose()

    # ═══ 场景 3：model 指定 vs 继承（对比验证真实调用的模型）═══
    print("\n═══ 3. model：指定 model= 走 chat；不给 model 继承父级 → 走 default flash ═══")
    flash = RecordingClient(OpenAICompatibleClient.for_deepseek(api_key=api_key, model_name="deepseek-v4-flash", timeout=60.0))
    chat = RecordingClient(OpenAICompatibleClient.for_deepseek(api_key=api_key, model_name="deepseek-chat", timeout=60.0))
    router = LLMRouter()
    router.register("deepseek-v4-flash", flash)
    router.register("deepseek-chat", chat)
    router.set_default(flash)  # 父级未指定 target_model 时默认走 flash

    router_parent = (
        AgentBuilder()
        .identity(
            agent_id="real.cfg.router.parent",
            agent_name="真实路由父代理",
            when_to_use="路由验证",
            sensitive_permissions=PERMISSION_ALL,
            trust_level=TrustLevel.ORCHESTRATOR,
        )
        .llm(router)
        .llm_settings(temperature=0.5)  # 父级基准：target_model=None
        .sub_agents(
            [
                _bp("real.cfg.model", "指定模型子代理", model="deepseek-chat"),  # 显式指定
                _bp("real.cfg.inherit.model", "继承模型子代理"),                   # 不给 model → 继承父级(None)
            ],
            llm_client=router,
        )
        .behavior(max_steps=3)
        .build()
    )
    registry3 = router_parent._loop._agent_registry
    n_chat_before = len(chat.calls)
    n_flash_before = len(flash.calls)

    r3 = await registry3.call_agent("指定模型子代理", "3 + 3 等于几？只回答数字。", _make_context(f"real-cfg-3-{uuid.uuid4()}"))
    out3 = str(r3.data) if r3.success else f"ERR:{r3.error}"
    print(f"  指定 model=deepseek-chat 委派输出: {out3!r}")

    r4 = await registry3.call_agent("继承模型子代理", "4 + 4 等于几？只回答数字。", _make_context(f"real-cfg-4-{uuid.uuid4()}"))
    out4 = str(r4.data) if r4.success else f"ERR:{r4.error}"
    print(f"  不给 model 委派输出: {out4!r}")

    # 对比：指定 model → 只调 chat；不给 model → 只调 flash（default）
    chat_called = len(chat.calls) > n_chat_before
    flash_called = len(flash.calls) > n_flash_before
    failed += _check("指定 model=deepseek-chat → 真实调用 deepseek-chat", chat_called)
    failed += _check("不给 model → 继承父级 target_model=None → 走 default flash", flash_called)
    failed += _check("指定委派成功", r3.success and "6" in str(out3), f"output={out3!r}")
    failed += _check("继承委派成功", r4.success and "8" in str(out4), f"output={out4!r}")

    await flash.aclose()
    await chat.aclose()

    print(f"\n结果: {'✅ 全部通过' if failed == 0 else f'❌ {failed} 项失败'}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
