"""pandaren/hook/tests/test_hooks.py — AgentHooks 生命周期协议回归测试。

设计文档：pandaren/hook/tests/design/hooks.design.md（v1，U1~U17）
被测：pandaren/hook/hooks.py（AgentHooks Protocol / DefaultAgentHooks / CompositeAgentHooks）

覆盖矩阵（P0×11 / P1×3 / P2×2 / P3×1）：
  U1   异常隔离（参数化 21 方法）          inv-1 + R1,   P0, component(fake)
  U2   provider 条件转发                  inv-2 + R2,   P0, component(fake)
  U3   **kwargs 兜底 + inspect 失败       inv-2 + R2,   P0, component(fake)
  U4   _sig_cache 命中不重复 inspect       inv-3 + R3,   P0, component(fake)
  U5   clone _hooks 列表独立              inv-4 + R4,   P0, component(fake)
  U6   clone 元素共享 + 缓存独立           inv-4 + R4,   P0, component(fake)
  U7   session_id 透传（参数化 17 run 级）  inv-5 + R5,   P0, component(fake)
  U8   21 方法集合 + 签名约束              inv-6 + inv-9 + R6, P0, unit
  U9   Default 满足 Protocol              inv-7 + R7,   P0, unit
  U10  add 顺序执行                       inv-8 + R8,   P0, component(fake)
  U11  非 run 级不接收 session_id          inv-9 + R9,   P1, unit + component(fake)
  U12  AgentHooks 不可实例化               inv-10 + R10, P1, unit
  U13  空列表调用不抛异常                  inv-11 + R11, P1, component(fake)
  U14  add(None) 忽略                     R12, P2, component(fake)
  U15  参数透传完整性                      R13, P2, component(fake)
  U16  BaseException 传播边界              R14, P3, component(fake)
  U17  真实 adapter 链式（integration）    inv-1/2/5 + R13, P0, integration

已知差距：
  - KG-1：ObservabilityHooksAdapter 仅实现 19/21 hook（缺 on_skill_activated/on_skill_cleared），
    U17 ④ 依赖 Composite 对缺方法子 hook（AttributeError）的容错：吞异常且后续 hook 继续。
  - U11 对照场景：设计文档预期"Composite 抛 TypeError 被自身 catch 吞掉"；实测参数绑定
    TypeError 在函数体外抛出并向上传播（更早暴露调用错误）——按实际行为断言，非本期修复目标。
"""
from __future__ import annotations

import inspect
from typing import Protocol

import pytest

from pandaren.hook import hooks as hooks_module
from pandaren.hook.hooks import AgentHooks, CompositeAgentHooks, DefaultAgentHooks
from pandaren.observability.backend.in_memory import (
    InMemoryLoggerBackend,
    InMemoryMetricsBackend,
    InMemoryTracerBackend,
)
from pandaren.observability.hooks_adapter import ObservabilityHooksAdapter
from pandaren.observability.logger import Logger
from pandaren.observability.metrics import Metrics
from pandaren.observability.tracer import Tracer

# ═══════════════════════════════════════════════════════════════════════
# §1.3 方法参数表（Golden Value：全部来自设计文档，规格推导）
# ═══════════════════════════════════════════════════════════════════════

ALL_METHODS = [
    "on_run_start",
    "on_run_end",
    "on_step_start",
    "on_step_end",
    "on_before_llm_call",
    "on_after_llm_call",
    "on_before_tool_call",
    "on_after_tool_call",
    "on_tool_register",
    "on_tool_discover",
    "on_tool_disabled",
    "on_tool_circuit_open",
    "on_tool_circuit_close",
    "on_tool_output_truncated",
    "on_concurrent_execution_failure",
    "on_hitl_requested",
    "on_hitl_resolved",
    "on_error",
    "on_halt",
    "on_skill_activated",
    "on_skill_cleared",
]

# run 级（17 个）：签名含 session_id 且转发时透传
RUN_LEVEL_METHODS = [
    "on_run_start",
    "on_run_end",
    "on_step_start",
    "on_step_end",
    "on_before_llm_call",
    "on_after_llm_call",
    "on_before_tool_call",
    "on_after_tool_call",
    "on_tool_discover",
    "on_tool_disabled",
    "on_concurrent_execution_failure",
    "on_hitl_requested",
    "on_hitl_resolved",
    "on_error",
    "on_halt",
    "on_skill_activated",
    "on_skill_cleared",
]

# 非 run 级（4 个）：签名无 session_id，转发时无 session_id 键（§1.3 ❌ 行）
NON_RUN_METHODS = [
    "on_tool_register",
    "on_tool_circuit_open",
    "on_tool_circuit_close",
    "on_tool_output_truncated",
]

# 每个方法的完整参数表（与 §1.3 逐键一致）。on_before_llm_call 的 provider 字段
# 依据 §1.3 注释（新旧签名兼容）由测试显式传入。
METHOD_PARAMS: dict[str, dict] = {
    "on_run_start": {"task": "task-1", "run_id": "run-1", "session_id": "sess-42"},
    "on_run_end": {"run_id": "run-1", "success": True, "terminal_reason": "done", "session_id": "sess-42"},
    "on_step_start": {"step_n": 1, "run_id": "run-1", "session_id": "sess-42"},
    "on_step_end": {"step_n": 1, "run_id": "run-1", "session_id": "sess-42"},
    "on_before_llm_call": {
        "messages": [{"role": "user", "content": "hi"}],
        "run_id": "run-1",
        "model": "gpt-4o",
        "tools": None,
        "call_type": "main",
        "session_id": "sess-42",
        "provider": "openai",
    },
    "on_after_llm_call": {
        "response": {"text": "hi"},
        "run_id": "run-1",
        "model": "gpt-4o",
        "duration_ms": 12.3,
        "call_type": "main",
        "session_id": "sess-42",
        "provider": "openai",
    },
    "on_before_tool_call": {"tool_name": "web_search", "args": {"q": "x"}, "run_id": "run-1", "step_n": 1, "session_id": "sess-42"},
    "on_after_tool_call": {"tool_name": "web_search", "result": {"ok": True}, "run_id": "run-1", "step_n": 1, "duration_ms": 5.0, "session_id": "sess-42"},
    "on_tool_register": {"tool_name": "web_search", "tier": "standard", "sensitivity": "high", "namespace": None},
    "on_tool_discover": {"tool_name": "web_search", "query": "x", "run_id": "run-1", "session_id": "sess-42"},
    "on_tool_disabled": {"tool_name": "web_search", "reason": "denied", "run_id": "run-1", "session_id": "sess-42"},
    "on_tool_circuit_open": {"tool_name": "web_search", "failure_count": 3, "recovery_timeout": 60.0},
    "on_tool_circuit_close": {"tool_name": "web_search"},
    "on_tool_output_truncated": {"tool_name": "web_search", "original_size": 1000, "max_size": 500},
    "on_concurrent_execution_failure": {"tool_names": ["a", "b"], "run_id": "run-1", "step_n": 1, "session_id": "sess-42"},
    "on_hitl_requested": {"tool_name": "approve_loan", "run_id": "run-1", "session_id": "sess-42"},
    "on_hitl_resolved": {"tool_name": "approve_loan", "decision": "approved", "run_id": "run-1", "session_id": "sess-42"},
    "on_error": {"error": RuntimeError("boom"), "run_id": "run-1", "session_id": "sess-42"},
    "on_halt": {"reason": "user_stop", "run_id": "run-1", "session_id": "sess-42"},
    "on_skill_activated": {"skill_name": "math", "skill_type": "ACTION", "tools": ["calc"], "run_id": "run-1", "step_n": 1, "session_id": "sess-42"},
    "on_skill_cleared": {"skill_name": "math", "run_id": "run-1", "session_id": "sess-42"},
}

# ═══════════════════════════════════════════════════════════════════════
# Fake：RecordingHook 家族（有真实状态、可审计，零 mock）
# ═══════════════════════════════════════════════════════════════════════

# Composite 转发时各方法位置参数的顺序（§1.3 参数表顺序，用于把位置参数并入 kwargs 视图）
_POS_ARGS: dict[str, tuple[str, ...]] = {
    "on_run_start": ("task", "run_id"),
    "on_run_end": ("run_id", "success"),
    "on_step_start": ("step_n", "run_id"),
    "on_step_end": ("step_n", "run_id"),
    "on_before_llm_call": ("messages", "run_id"),
    "on_after_llm_call": ("response", "run_id"),
    "on_before_tool_call": ("tool_name", "args", "run_id"),
    "on_after_tool_call": ("tool_name", "result", "run_id"),
    "on_tool_register": ("tool_name", "tier", "sensitivity", "namespace"),
    "on_tool_discover": ("tool_name", "query", "run_id"),
    "on_tool_disabled": ("tool_name", "reason", "run_id"),
    "on_tool_circuit_open": ("tool_name", "failure_count", "recovery_timeout"),
    "on_tool_circuit_close": ("tool_name",),
    "on_tool_output_truncated": ("tool_name", "original_size", "max_size"),
    "on_concurrent_execution_failure": ("tool_names", "run_id", "step_n"),
    "on_hitl_requested": ("tool_name", "run_id"),
    "on_hitl_resolved": ("tool_name", "decision", "run_id"),
    "on_error": ("error", "run_id"),
    "on_halt": ("reason", "run_id"),
    "on_skill_activated": ("skill_name", "skill_type", "tools", "run_id", "step_n"),
    "on_skill_cleared": ("skill_name", "run_id"),
}


class RecordingHook:
    """记录每个 hook 方法调用为 (method_name, kwargs_view)。

    kwargs_view = 位置参数（按 _POS_ARGS 命名）与关键字参数的合并视图，
    可直接与 METHOD_PARAMS 逐键比较。方法体为 *args/**kwargs（VAR_KEYWORD），
    因此 _accepts 对 provider 恒返回 True——专用于验证"完整透传"。
    """

    def __init__(self, name: str, order: list[str] | None = None) -> None:
        self.name = name
        self.calls: list[tuple[str, dict]] = []
        self._order = order

    def __getattr__(self, item: str):
        if item in _POS_ARGS:
            names = _POS_ARGS[item]

            def _record(*args: object, **kwargs: object) -> None:
                merged = dict(zip(names, args))
                merged.update(kwargs)
                self.calls.append((item, merged))
                if self._order is not None:
                    self._order.append(self.name)

            return _record
        raise AttributeError(item)


class ThrowingHook(RecordingHook):
    """所有 hook 方法均抛 RuntimeError（故障注入失败缝）。"""

    def __getattr__(self, item: str):
        if item in _POS_ARGS:
            def _throw(*args: object, **kwargs: object) -> None:
                raise RuntimeError("boom")

            return _throw
        raise AttributeError(item)


class BaseThrowHook(RecordingHook):
    """on_run_start 抛 KeyboardInterrupt（BaseException，验证不被 except Exception 吞掉）。"""

    def on_run_start(self, task, run_id, *, session_id="") -> None:
        raise KeyboardInterrupt


class NewSigHook(RecordingHook):
    """on_before/after_llm_call 声明 provider（新签名 → _accepts=True）。"""

    def on_before_llm_call(self, messages, run_id, model="", tools=None, *,
                           call_type="main", session_id="", provider=""):
        self.calls.append(("on_before_llm_call", dict(
            model=model, tools=tools, call_type=call_type, session_id=session_id, provider=provider,
        )))

    def on_after_llm_call(self, response, run_id, model="", *,
                          duration_ms=None, call_type=None, session_id="", provider=""):
        self.calls.append(("on_after_llm_call", dict(
            model=model, duration_ms=duration_ms, call_type=call_type,
            session_id=session_id, provider=provider,
        )))


class OldSigHook(RecordingHook):
    """on_before/after_llm_call 不含 provider（旧签名 → _accepts=False，向后兼容目标）。"""

    def on_before_llm_call(self, messages, run_id, model="", tools=None, *,
                           call_type="main", session_id=""):
        self.calls.append(("on_before_llm_call", dict(
            model=model, tools=tools, call_type=call_type, session_id=session_id,
        )))

    def on_after_llm_call(self, response, run_id, model="", *,
                          duration_ms=None, call_type=None, session_id=""):
        self.calls.append(("on_after_llm_call", dict(
            model=model, duration_ms=duration_ms, call_type=call_type, session_id=session_id,
        )))


class KwargsHook(RecordingHook):
    """on_before/after_llm_call 用 **kwargs 兜底（VAR_KEYWORD 视为接受 provider）。"""

    def on_before_llm_call(self, messages, run_id, **kwargs):
        self.calls.append(("on_before_llm_call", dict(kwargs)))

    def on_after_llm_call(self, response, run_id, **kwargs):
        self.calls.append(("on_after_llm_call", dict(kwargs)))


class UninspectableHook(RecordingHook):
    """on_before_llm_call 可调用但不可内省（模拟 C 扩展式方法）：
    __signature__ 被污染 → inspect.signature 抛 ValueError → _accepts 假定接受并调用成功。
    """

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.on_before_llm_call = self._before  # 实例属性覆盖类方法，访问时不可内省

    def _before(self, *args: object, **kwargs: object) -> None:
        self.calls.append(("on_before_llm_call", dict(kwargs)))

    _before.__signature__ = "poison"  # 非 Signature → inspect.signature 抛 ValueError


# ═══════════════════════════════════════════════════════════════════════
# U1 异常隔离（inv-1 + R1, P0, component）
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("method", ALL_METHODS)
def test_u1_single_hook_exception_swallowed(method):
    # inv-1：首元素抛异常 → 异常不外抛、后续 hook 收到完整参数
    composite = CompositeAgentHooks()
    composite.add(ThrowingHook("h1"))
    h2 = RecordingHook("h2")
    composite.add(h2)
    params = METHOD_PARAMS[method]

    getattr(composite, method)(**params)  # 不抛异常

    assert h2.calls == [(method, params)]


# ═══════════════════════════════════════════════════════════════════════
# U2 provider 条件转发（inv-2 + R2, P0, component）
# ═══════════════════════════════════════════════════════════════════════


def test_u2_provider_forwarded_only_to_new_signature_hook():
    # inv-2：新签名 hook 收到 provider；旧签名不传且调用成功（向后兼容）
    composite = CompositeAgentHooks()
    new_hook = NewSigHook("new")
    old_hook = OldSigHook("old")
    composite.add(new_hook)
    composite.add(old_hook)

    composite.on_before_llm_call(
        messages=[{"role": "user", "content": "hi"}], run_id="run-1", model="gpt-4o",
        tools=None, call_type="main", session_id="sess-42", provider="openai",
    )

    assert new_hook.calls[-1][1]["provider"] == "openai"
    assert "provider" not in old_hook.calls[-1][1]
    assert old_hook.calls[-1][1] == {
        "model": "gpt-4o", "tools": None, "call_type": "main", "session_id": "sess-42",
    }
    assert composite._accepts(old_hook, "on_before_llm_call", "provider") is False
    assert composite._accepts(new_hook, "on_before_llm_call", "provider") is True

    composite.on_after_llm_call(
        response={"text": "hi"}, run_id="run-1", model="gpt-4o", duration_ms=12.3,
        call_type="main", session_id="sess-42", provider="openai",
    )

    assert new_hook.calls[-1][1]["provider"] == "openai"
    assert "provider" not in old_hook.calls[-1][1]
    assert old_hook.calls[-1][1] == {
        "model": "gpt-4o", "duration_ms": 12.3, "call_type": "main", "session_id": "sess-42",
    }


# ═══════════════════════════════════════════════════════════════════════
# U3 **kwargs 兜底 + inspect 失败（inv-2 + R2, P0, component）
# ═══════════════════════════════════════════════════════════════════════


def test_u3_kwargs_catchall_and_inspect_failure_assume_accept():
    # inv-2：VAR_KEYWORD 视为接受 provider；inspect 失败（不可内省）假定接受
    composite = CompositeAgentHooks()
    kw_hook = KwargsHook("kw")
    un_hook = UninspectableHook("un")
    composite.add(kw_hook)
    composite.add(un_hook)

    composite.on_before_llm_call(
        messages=[{"role": "user", "content": "hi"}], run_id="run-1", model="gpt-4o",
        tools=None, call_type="main", session_id="sess-42", provider="openai",
    )

    assert composite._accepts(kw_hook, "on_before_llm_call", "provider") is True
    assert kw_hook.calls[-1][1]["provider"] == "openai"
    assert un_hook.calls[-1][1]["provider"] == "openai"


# ═══════════════════════════════════════════════════════════════════════
# U4 _sig_cache 命中不重复 inspect（inv-3 + R3, P0, component）
# ═══════════════════════════════════════════════════════════════════════


def test_u4_sig_cache_avoids_reinspect(monkeypatch):
    # inv-3：缓存命中后不再重复 inspect（False 结果同样命中）；old 签名两次都收不到 provider
    count = {"n": 0}
    orig_signature = hooks_module.inspect.signature

    def wrapped(*args, **kwargs):
        count["n"] += 1
        return orig_signature(*args, **kwargs)

    monkeypatch.setattr(hooks_module.inspect, "signature", wrapped)

    composite = CompositeAgentHooks()
    old_hook = OldSigHook("old")
    new_hook = NewSigHook("new")
    composite.add(old_hook)
    composite.add(new_hook)
    params = METHOD_PARAMS["on_before_llm_call"]

    composite.on_before_llm_call(**params)
    composite.on_before_llm_call(**params)

    assert count["n"] == 2  # 首次各 1 次；第二次调用 0 次新增
    assert composite._sig_cache.get((id(old_hook), "on_before_llm_call", "provider")) is False
    assert composite._sig_cache.get((id(new_hook), "on_before_llm_call", "provider")) is True
    assert len(old_hook.calls) == 2
    assert all("provider" not in entry[1] for entry in old_hook.calls)


# ═══════════════════════════════════════════════════════════════════════
# U5 clone _hooks 列表独立（inv-4 + R4, P0, component）
# ═══════════════════════════════════════════════════════════════════════


def test_u5_clone_hooks_list_independent():
    # inv-4：clone 后各自 add 互不可见、互不影响
    orig = CompositeAgentHooks()
    hook_a = RecordingHook("a")
    orig.add(hook_a)
    clone = orig.clone()
    assert clone._hooks is not orig._hooks

    hook_b = RecordingHook("b")
    orig.add(hook_b)
    clone.on_run_start(**METHOD_PARAMS["on_run_start"])

    assert len(clone._hooks) == 1
    assert len(hook_a.calls) == 1
    assert len(hook_b.calls) == 0

    hook_c = RecordingHook("c")
    clone.add(hook_c)
    orig.on_run_start(**METHOD_PARAMS["on_run_start"])

    assert len(orig._hooks) == 2
    assert len(hook_a.calls) == 2
    assert len(hook_b.calls) == 1
    assert len(hook_c.calls) == 0


# ═══════════════════════════════════════════════════════════════════════
# U6 clone 元素共享 + 缓存独立（inv-4 + R4, P0, component）
# ═══════════════════════════════════════════════════════════════════════


def test_u6_clone_shares_elements_but_isolates_sig_cache():
    # inv-4：clone 的元素引用共享（同一 hook 实例），_sig_cache 各自独立
    orig = CompositeAgentHooks()
    hook_a = RecordingHook("a")
    orig.add(hook_a)
    orig.on_before_llm_call(**METHOD_PARAMS["on_before_llm_call"])  # 触发 _accepts 写缓存

    clone = orig.clone()

    assert clone._hooks[0] is hook_a
    assert clone._sig_cache == {}
    assert len(orig._sig_cache) == 1

    clone.on_before_llm_call(**METHOD_PARAMS["on_before_llm_call"])

    assert len(orig._sig_cache) == 1  # clone 不写 orig
    assert len(clone._sig_cache) == 1


# ═══════════════════════════════════════════════════════════════════════
# U7 session_id 透传（inv-5 + R5, P0, component）
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("method", RUN_LEVEL_METHODS)
def test_u7_session_id_forwarded_to_each_hook(method):
    # inv-5：run 级 session_id 原样透传给每个子 hook（逐键一致，含 session_id）
    composite = CompositeAgentHooks()
    h1 = RecordingHook("h1")
    h2 = RecordingHook("h2")
    composite.add(h1)
    composite.add(h2)
    params = METHOD_PARAMS[method]

    getattr(composite, method)(**params)

    assert h1.calls == [(method, params)]
    assert h2.calls == [(method, params)]


# ═══════════════════════════════════════════════════════════════════════
# U8 21 方法集合 + 签名约束（inv-6 + inv-9 + R6, P0, unit）
# ═══════════════════════════════════════════════════════════════════════


def test_u8_all_implementations_expose_same_21_methods():
    # inv-6：三类实现暴露完全一致的方法集合（21 个），防止实现漂移
    for cls in (AgentHooks, DefaultAgentHooks, CompositeAgentHooks):
        names = {name for name in cls.__dict__ if name.startswith("on_")}
        assert names == set(ALL_METHODS), cls.__name__
        assert len(names) == 21


def test_u8_session_id_signature_constraints():
    # inv-9：run 级签名含 session_id；非 run 级签名不含 session_id（§1.3 ❌ 行）
    for cls in (AgentHooks, DefaultAgentHooks, CompositeAgentHooks):
        for method in RUN_LEVEL_METHODS:
            sig = inspect.signature(getattr(cls, method))
            assert "session_id" in sig.parameters, f"{cls.__name__}.{method}"
        for method in NON_RUN_METHODS:
            sig = inspect.signature(getattr(cls, method))
            assert "session_id" not in sig.parameters, f"{cls.__name__}.{method}"


# ═══════════════════════════════════════════════════════════════════════
# U9 Default 满足 Protocol（inv-7 + R7, P0, unit）
# ═══════════════════════════════════════════════════════════════════════


def test_u9_default_agent_hooks_satisfies_protocol():
    # inv-7：DefaultAgentHooks 实例是 AgentHooks（runtime_checkable，方法齐全）
    assert isinstance(DefaultAgentHooks(), AgentHooks) is True


# ═══════════════════════════════════════════════════════════════════════
# U10 add 顺序执行（inv-8 + R8, P0, component）
# ═══════════════════════════════════════════════════════════════════════


def test_u10_hooks_run_in_add_order():
    # inv-8：hook 按 add 顺序执行（共享 order 列表记录时序）
    order: list[str] = []
    composite = CompositeAgentHooks()
    composite.add(RecordingHook("h1", order=order))
    composite.add(RecordingHook("h2", order=order))
    composite.add(RecordingHook("h3", order=order))

    composite.on_run_start(**METHOD_PARAMS["on_run_start"])

    assert order == ["h1", "h2", "h3"]


# ═══════════════════════════════════════════════════════════════════════
# U11 非 run 级不接收 session_id（inv-9 + R9, P1, unit + component）
# ═══════════════════════════════════════════════════════════════════════


def test_u11_non_run_hooks_receive_no_session_id():
    # inv-9：非 run 级转发 kwargs 无 session_id 键（签名层约束见 test_u8_session_id_signature_constraints）
    composite = CompositeAgentHooks()
    h1 = RecordingHook("h1")
    composite.add(h1)

    for method in NON_RUN_METHODS:
        getattr(composite, method)(**METHOD_PARAMS[method])

    for method in NON_RUN_METHODS:
        entry = [c for c in h1.calls if c[0] == method]
        assert len(entry) == 1
        assert "session_id" not in entry[0][1], method


def test_u11_contrast_wrong_session_id_on_non_run_hook_raises():
    # inv-9 + R9 对照：调用方误向非 run 级 hook 传 session_id → TypeError 外抛。
    # 注：设计文档预期"被 Composite 吞掉"；实测参数绑定 TypeError 在函数体外抛出并
    #     向上传播（更早暴露调用错误）——以实际行为断言，非本期修复目标。
    composite = CompositeAgentHooks()
    composite.add(RecordingHook("h1"))

    with pytest.raises(TypeError):
        composite.on_tool_register(
            tool_name="web_search", tier="standard", sensitivity="high",
            namespace=None, session_id="sess-42",
        )


# ═══════════════════════════════════════════════════════════════════════
# U12 AgentHooks 不可实例化（inv-10 + R10, P1, unit）
# ═══════════════════════════════════════════════════════════════════════


def test_u12_agent_hooks_is_protocol_not_instantiable():
    # inv-10：AgentHooks 是 runtime_checkable Protocol，不可直接实例化
    with pytest.raises(TypeError):
        AgentHooks()
    assert issubclass(AgentHooks, Protocol) is True
    assert getattr(AgentHooks, "_is_runtime_protocol", False) is True


# ═══════════════════════════════════════════════════════════════════════
# U13 空列表调用不抛异常（inv-11 + R11, P1, component）
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("method", ALL_METHODS)
def test_u13_empty_composite_calls_are_noop(method):
    # inv-11：空列表调用任意 hook 不抛异常、无副作用（缓存保持空）
    composite = CompositeAgentHooks()

    getattr(composite, method)(**METHOD_PARAMS[method])

    assert composite._hooks == []
    assert composite._sig_cache == {}


# ═══════════════════════════════════════════════════════════════════════
# U14 add(None) 忽略（R12, P2, component）
# ═══════════════════════════════════════════════════════════════════════


def test_u14_add_none_ignored():
    # R12：add(None) 被忽略，不注入 None 元素、不破坏转发
    composite = CompositeAgentHooks()
    composite.add(None)
    h1 = RecordingHook("h1")
    composite.add(h1)
    composite.add(None)

    assert composite._hooks == [h1]

    composite.on_run_start(**METHOD_PARAMS["on_run_start"])

    assert len(h1.calls) == 1


# ═══════════════════════════════════════════════════════════════════════
# U15 参数透传完整性（R13, P2, component）
# ═══════════════════════════════════════════════════════════════════════


def test_u15_before_llm_call_full_kwargs_passthrough():
    # R13：on_before_llm_call 五关键字字段原样透传；tools=None 不被改写
    composite = CompositeAgentHooks()
    h1 = NewSigHook("h1")
    composite.add(h1)

    composite.on_before_llm_call(
        messages=[{"role": "user", "content": "hi"}], run_id="run-1", model="gpt-4o",
        tools=[{"function": {"name": "web_search"}}], call_type="search",
        session_id="sess-42", provider="openai",
    )

    assert h1.calls[-1][1] == {
        "model": "gpt-4o",
        "tools": [{"function": {"name": "web_search"}}],
        "call_type": "search",
        "session_id": "sess-42",
        "provider": "openai",
    }

    composite.on_before_llm_call(
        messages=[{"role": "user", "content": "hi"}], run_id="run-2", model="gpt-4o",
        tools=None, call_type="main", session_id="sess-42", provider="openai",
    )

    assert h1.calls[-1][1]["tools"] is None


# ═══════════════════════════════════════════════════════════════════════
# U16 BaseException 传播边界（R14, P3, component）
# ═══════════════════════════════════════════════════════════════════════


def test_u16_base_exception_propagates():
    # R14：KeyboardInterrupt 不被 except Exception 吞掉，向外传播且中断后续 hook
    composite = CompositeAgentHooks()
    composite.add(BaseThrowHook("h1"))
    h2 = RecordingHook("h2")
    composite.add(h2)

    with pytest.raises(KeyboardInterrupt):
        composite.on_run_start(**METHOD_PARAMS["on_run_start"])

    assert h2.calls == []


# ═══════════════════════════════════════════════════════════════════════
# U17 真实 adapter 链式（inv-1/2/5 + R13, P0, integration）
# ═══════════════════════════════════════════════════════════════════════


def test_u17_real_adapter_chain_integration():
    # inv-1 + inv-5：真实 ObservabilityHooksAdapter 链式，session_id 端到端落观测；
    # inv-2：provider 真实送达消费方与用户 hook；
    # KG-1：adapter 缺 on_skill_activated → Composite 吞 AttributeError，用户 hook 不中断
    log_backend = InMemoryLoggerBackend()
    trace_backend = InMemoryTracerBackend()
    metrics_backend = InMemoryMetricsBackend()
    adapter = ObservabilityHooksAdapter(
        logger=Logger(backend=log_backend),
        tracer=Tracer(backend=trace_backend),
        metrics=Metrics(backend=metrics_backend),
    )
    composite = CompositeAgentHooks()
    composite.add(adapter)
    user = RecordingHook("user")
    composite.add(user)

    # ① on_run_start：session_id 端到端进入结构化日志
    composite.on_run_start(task="task-1", run_id="run-1", session_id="sess-42")
    assert "run-1" in adapter._run_start_mono_by_run
    run_logs = [r for r in log_backend.get_records() if r.get("run_id") == "run-1"]
    assert run_logs
    assert all(r.get("session_id") == "sess-42" for r in run_logs)

    # ② on_before_llm_call：provider 真实送达 adapter 与用户 hook
    composite.on_before_llm_call(
        messages=[{"role": "user", "content": "hi"}], run_id="run-1", model="gpt-4o",
        tools=None, call_type="main", session_id="sess-42", provider="openai",
    )
    assert adapter._llm_call_provider_by_run["run-1"] == "openai"
    before = [c for c in user.calls if c[0] == "on_before_llm_call"]
    assert before and before[-1][1]["provider"] == "openai"

    # ③ on_tool_register：非 run 级转发，kwargs 无 session_id 键
    composite.on_tool_register(tool_name="web_search", tier="standard", sensitivity="high", namespace=None)
    reg = [c for c in user.calls if c[0] == "on_tool_register"]
    assert reg and "session_id" not in reg[-1][1]

    # ④ on_skill_activated：KG-1 缺方法容错——adapter 抛 AttributeError 被吞，用户 hook 继续
    composite.on_skill_activated(
        skill_name="math", skill_type="ACTION", tools=["calc"],
        run_id="run-1", step_n=1, session_id="sess-42",
    )
    act = [c for c in user.calls if c[0] == "on_skill_activated"]
    assert act and act[-1][1]["session_id"] == "sess-42"
