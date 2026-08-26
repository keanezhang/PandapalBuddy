"""pandaren/sub_agent/tests/test_registry_loader.py

设计文档: tests/design/registry_loader.design.md
用例: REG-1..6 / DEL-1..5 / TOK-1..4 / FM-1..4 / CL-1..4 / TY-1
技术栈: pytest + pytest-asyncio（pyproject 已配 asyncio_mode=auto，async 用例免装饰器）
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from pandaren.agent import AgentStatus
from pandaren.identity.models import TrustLevel
from pandaren.observability.types import AuditEventType
from pandaren.sub_agent.exceptions import SubAgentRegistrationError
from pandaren.sub_agent.loader import (
    _parse_comma_list,
    _parse_frontmatter,
    load_agent_from_file,
    load_agents_from_dir,
)
from pandaren.sub_agent.models import SubAgentSource
from pandaren.sub_agent.registry import SubAgentRegistry
from pandaren.tool.definition.context import ToolContext


# ════════════════════════════════════════════════════════════════
#  Fake / 工厂夹具
# ════════════════════════════════════════════════════════════════

class FakeAuditLog:
    """纯内存审计日志：write_sync 记录 (event_type, agent_id, run_id, detail, step_n)。"""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def write_sync(self, event_type, *, agent_id, run_id, detail, step_n):
        self.events.append((event_type, agent_id, run_id, detail, step_n))

    @property
    def registered_pairs(self) -> list[tuple]:
        return [(e[0], e[1]) for e in self.events]

    @property
    def delegate_events(self) -> list[AuditEventType]:
        """仅委派域事件序列（过滤 fixture 注册产生的 AGENT_REGISTERED 噪音）。"""
        return [e[0] for e in self.events if e[0] in _DELEGATE_EVENTS]


_DELEGATE_EVENTS = frozenset({
    AuditEventType.AGENT_DELEGATED,
    AuditEventType.AGENT_DELEGATE_COMPLETED,
    AuditEventType.AGENT_DELEGATE_DEPTH_EXCEEDED,
    AuditEventType.AGENT_DELEGATE_CYCLE,
})


def make_blueprint(
    agent_id: str,
    agent_name: str,
    source: SubAgentSource = SubAgentSource.DIRECTORY,
    *,
    marker: str | None = None,
    materialize=None,
) -> SimpleNamespace:
    """鸭子类型蓝图：materialize + identity(agent_id/agent_name/trust_level) + source。"""
    if materialize is None:
        materialize = lambda: SimpleNamespace(marker=marker)
    return SimpleNamespace(
        materialize=materialize,
        identity=SimpleNamespace(
            agent_id=agent_id,
            agent_name=agent_name,
            trust_level=TrustLevel.SUB_AGENT,
        ),
        source=source,
    )


class CountingFactory:
    """计数 materialize 工厂：记录产出次数，返回固定假 Agent。"""

    def __init__(self, agent) -> None:
        self.agent = agent
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.agent


class FakeAgent:
    """假 Agent：run() 记录收到的 session_id，返回固定结果。"""

    def __init__(self, result) -> None:
        self.result = result
        self.received_session_id = None
        self.run_calls = 0

    async def run(self, task, *, session_id, metadata=None):
        self.received_session_id = session_id
        self.run_calls += 1
        return self.result


def make_ctx(session_id="sess-1") -> ToolContext:
    """ORCHESTRATOR 信任级（绕过信任校验干扰）的 ToolContext。"""
    return ToolContext(
        run_id="r-caller",
        step_n=3,
        agent_id="caller",
        session_id=session_id,
        trust_level=TrustLevel.ORCHESTRATOR,
    )


def make_ctx_without_session_id() -> SimpleNamespace:
    """session_id 属性缺失的 context 变体（getattr 默认 None）。"""
    return SimpleNamespace(
        run_id="r-caller",
        step_n=3,
        agent_id="caller",
        trust_level=TrustLevel.ORCHESTRATOR,
    )


def _register_coder() -> tuple[SubAgentRegistry, FakeAuditLog, CountingFactory, FakeAgent]:
    """注册 (coder, 代码审查, DIRECTORY)，附带计数工厂与成功假 Agent。"""
    audit = FakeAuditLog()
    registry = SubAgentRegistry(audit_log=audit)
    agent = FakeAgent(SimpleNamespace(success=True, output="ok", error=None, run_id="run-1"))
    factory = CountingFactory(agent)
    registry.register(make_blueprint("coder", "代码审查", SubAgentSource.DIRECTORY, materialize=factory))
    return registry, audit, factory, agent


# ════════════════════════════════════════════════════════════════
#  REG — register()（C1/C2）
# ════════════════════════════════════════════════════════════════

def test_reg1_new_agent_registers_successfully():
    """REG-1 新 agent_id + 唯一 agent_name 注册成功。"""
    # inv-1 / inv-2 / inv-4（成功路径基线）
    audit = FakeAuditLog()
    registry = SubAgentRegistry(audit_log=audit)

    registry.register(make_blueprint("coder", "代码审查", SubAgentSource.DIRECTORY))

    assert registry.agent_count() == 1
    assert registry.get_identity("coder").agent_name == "代码审查"
    assert registry.get_status("coder") == AgentStatus.HEALTHY
    assert registry.version == 1
    assert audit.registered_pairs == [(AuditEventType.AGENT_REGISTERED, "coder")]


@pytest.mark.parametrize("existing,new", [
    (SubAgentSource.DIRECTORY, SubAgentSource.DIRECTORY),      # 同优先级
    (SubAgentSource.PROGRAMMATIC, SubAgentSource.DIRECTORY),   # 低优先级
    (SubAgentSource.DIRECTORY, SubAgentSource.BUILTIN),        # 内置不能覆盖用户（低优先级）
    (SubAgentSource.BUILTIN, SubAgentSource.BUILTIN),          # 内置同优先级
])
def test_reg2_same_id_lower_or_equal_source_rejected(existing, new):
    """REG-2 同 agent_id 二次注册，source 不高于现有 → 拒绝且状态零变化。"""
    # R2 / R3 / inv-3 / inv-4
    audit = FakeAuditLog()
    registry = SubAgentRegistry(audit_log=audit)
    registry.register(make_blueprint("coder", "代码审查", existing))
    version_before = registry.version
    audit_len = len(audit.events)

    with pytest.raises(SubAgentRegistrationError) as ei:
        registry.register(make_blueprint("coder", "新名称", new))

    assert "已注册" in str(ei.value)
    assert "coder" in str(ei.value)
    assert registry.agent_count() == 1
    assert registry.get_identity("coder").agent_name == "代码审查"
    assert registry.version == version_before
    assert len(audit.events) == audit_len


def test_reg4_user_directory_overrides_builtin():
    """REG-4 用户目录蓝图（DIRECTORY）可直接覆盖内置（BUILTIN），无需先 unregister。

    2026 新增 BUILTIN 档：pandaren/agents/ 与 pandapal resources/agents/system
    内置蓝图标 BUILTIN（最低优先级），用户同名蓝图自动胜出。
    """
    audit = FakeAuditLog()
    registry = SubAgentRegistry(audit_log=audit)
    registry.register(make_blueprint("coder", "内置审查", SubAgentSource.BUILTIN, marker="builtin"))

    registry.register(make_blueprint("coder", "用户审查", SubAgentSource.DIRECTORY, marker="user"))

    assert registry.agent_count() == 1
    assert registry.get_identity("coder").agent_name == "用户审查"
    assert registry._factories["coder"]().marker == "user"  # 白盒：factory 已替换
    assert registry._sources["coder"] == SubAgentSource.DIRECTORY  # 来源已升级为用户档
    registered = [e for e in audit.events if e[0] == AuditEventType.AGENT_REGISTERED]
    assert len(registered) == 2  # 首次 + 覆盖各一条


def test_reg3_high_source_overrides_and_clears_stale_state():
    """REG-3 同 agent_id，高优先级覆盖低优先级 → 替换且无残留（DRAINING 清场）。"""
    # R2 / R3 / inv-3 / inv-4
    audit = FakeAuditLog()
    registry = SubAgentRegistry(audit_log=audit)
    registry.register(make_blueprint("coder", "旧名称", SubAgentSource.DIRECTORY, marker="old"))
    registry.set_status("coder", AgentStatus.DRAINING)  # 制造残留，验证覆盖清场
    version_before = registry.version

    registry.register(make_blueprint("coder", "新名称", SubAgentSource.PROGRAMMATIC, marker="new"))

    assert registry.agent_count() == 1
    assert registry.get_identity("coder").agent_name == "新名称"
    assert registry.get_status("coder") == AgentStatus.HEALTHY  # 旧 DRAINING 被清场
    assert registry._factories["coder"]().marker == "new"        # 白盒：factory 已替换
    assert registry.version == version_before + 1
    registered = [e for e in audit.events if e[0] == AuditEventType.AGENT_REGISTERED]
    assert len(registered) == 2  # 首次 + 覆盖各一条


@pytest.mark.parametrize("name_a,name_b", [
    ("代码审查", "代码审查"),
    ("Code Review", "code review"),
    ("  Code Review  ", "code review"),
])
def test_reg4_duplicate_agent_name_rejected_with_zero_change(name_a, name_b):
    """REG-4 不同 agent_id、同名（exact / 大小写 / 首尾空白）→ 拒绝且状态零变化。"""
    # R1 / inv-2 / inv-4
    audit = FakeAuditLog()
    registry = SubAgentRegistry(audit_log=audit)
    registry.register(make_blueprint("a", name_a, SubAgentSource.DIRECTORY))
    version_before = registry.version
    audit_len = len(audit.events)

    with pytest.raises(SubAgentRegistrationError) as ei:
        registry.register(make_blueprint("b", name_b, SubAgentSource.DIRECTORY))

    assert name_b.strip() in str(ei.value)
    assert "a" in str(ei.value)
    assert registry.agent_count() == 1
    assert registry.version == version_before
    assert len(audit.events) == audit_len
    assert registry.get_identity("b") is None


def test_reg5_override_with_same_name_does_not_self_block():
    """REG-5 同 agent_id 高 source 覆盖 + 同名 → 不自我阻塞（顺序回归守卫）。"""
    # R1 / R3 / inv-2 / inv-3
    registry = SubAgentRegistry(audit_log=FakeAuditLog())
    registry.register(make_blueprint("coder", "代码审查", SubAgentSource.DIRECTORY))

    registry.register(make_blueprint("coder", "代码审查", SubAgentSource.PROGRAMMATIC))

    assert registry.agent_count() == 1
    assert registry.version == 2
    assert registry._sources["coder"] == SubAgentSource.PROGRAMMATIC  # 注册表内部 source 已被覆盖


def test_reg6_draining_agent_still_occupies_name():
    """REG-6 现有 agent 为 DRAINING 仍占用 agent_name → 同名被拒（全局唯一性）。"""
    # R1 / inv-2
    registry = SubAgentRegistry(audit_log=FakeAuditLog())
    registry.register(make_blueprint("a", "审查专家", SubAgentSource.DIRECTORY))
    registry.set_status("a", AgentStatus.DRAINING)
    version_before = registry.version

    with pytest.raises(SubAgentRegistrationError):
        registry.register(make_blueprint("b", "审查专家", SubAgentSource.DIRECTORY))

    assert registry.agent_count() == 1
    assert registry.version == version_before


# ════════════════════════════════════════════════════════════════
#  DEL — _execute_delegate() session_id 校验（C3）
# ════════════════════════════════════════════════════════════════

_MISSING_SESSION = object()


@pytest.mark.parametrize("session_id", [
    pytest.param(_MISSING_SESSION, id="attr-missing"),
    pytest.param(None, id="none"),
    pytest.param("", id="empty"),
    pytest.param("   ", id="spaces"),
    pytest.param("\t\n", id="tab-newline"),
])
async def test_del1_empty_session_id_fails_with_zero_side_effects(session_id):
    """DEL-1 session_id 缺失/None/空串/纯空白 → 显式失败，零副作用。"""
    # R5 / R6 / inv-5 / inv-6（MC-DC：A=T,B=T / A=T,B=F / A=F,B=T 三组合）
    registry, audit, factory, agent = _register_coder()
    version_before = registry.version
    stack_before = SubAgentRegistry._delegate_stack.get(None)

    ctx = make_ctx_without_session_id() if session_id is _MISSING_SESSION else make_ctx(session_id=session_id)
    result = await registry._execute_delegate("coder", "task", ctx)

    assert result.success is False
    assert "session_id" in result.error
    assert result.tool_name == "call_agent"
    assert result.data == ""
    assert factory.calls == 0  # 未产出目标 Agent
    assert agent.run_calls == 0  # 未执行委派
    assert audit.delegate_events == [AuditEventType.AGENT_DELEGATED]  # Step6 先于校验；无 COMPLETED
    assert SubAgentRegistry._delegate_stack.get(None) == stack_before  # stack 未被污染
    assert registry.version == version_before
    assert registry.agent_count() == 1


@pytest.mark.parametrize("session_id,expected_str", [
    ("0", "0"),    # 非空但 falsy 的字符串 → 放行
    (123, "123"),  # 非 str 非空（str() 后非空）→ 放行
])
async def test_del2_nonempty_falsy_or_non_str_session_allowed(session_id, expected_str):
    """DEL-2 session_id="0"/123 → 不误伤，正常进入执行（0 容忍的是空不是 falsy）。"""
    # R8 / inv-6（MC-DC：A=F, B=F → 放行分支）
    registry, audit, factory, agent = _register_coder()

    result = await registry._execute_delegate("coder", "task", make_ctx(session_id=session_id))

    assert result.success is True
    assert factory.calls == 1
    assert agent.run_calls == 1
    assert str(agent.received_session_id) == expected_str
    assert str(agent.received_session_id).strip() != ""
    assert audit.delegate_events == [
        AuditEventType.AGENT_DELEGATED,
        AuditEventType.AGENT_DELEGATE_COMPLETED,
    ]


async def test_del3_valid_session_full_success_audit_pair_and_stack_popped():
    """DEL-3 session_id 合法 → 全链路成功，审计成对，stack 弹出。"""
    # R6 / inv-5 / inv-6（happy path 基线）
    registry, audit, _, agent = _register_coder()

    result = await registry._execute_delegate("coder", "task", make_ctx(session_id="sess-1"))

    assert result.success is True
    assert "执行完成" in result.data
    assert result.tool_name == "call_agent"
    assert result.duration_ms >= 0
    assert agent.received_session_id == "sess-1"  # session_id 透传目标 Agent，无魔数兜底
    assert audit.delegate_events == [
        AuditEventType.AGENT_DELEGATED,
        AuditEventType.AGENT_DELEGATE_COMPLETED,
    ]
    # finally pop：ContextVar 持有已清空的 list（无栈残留）
    assert SubAgentRegistry._delegate_stack.get(None) == []


async def test_del4_depth_exceeded_audits_depth_event_not_cycle():
    """DEL-4 委派深度超限 → 失败 + 审计事件类型为 AGENT_DELEGATE_DEPTH_EXCEEDED（非 CYCLE）。"""
    # R7 / inv-7（C5 核心断言）
    registry, audit, factory, _ = _register_coder()
    token = SubAgentRegistry._delegate_stack.set(["reviewer"])  # 深度 1 >= 上限 1；不含目标 id
    try:
        result = await registry._execute_delegate("coder", "task", make_ctx(session_id="sess-1"))
    finally:
        SubAgentRegistry._delegate_stack.reset(token)

    assert result.success is False
    assert "深度超限" in result.error
    assert audit.delegate_events == [AuditEventType.AGENT_DELEGATE_DEPTH_EXCEEDED]
    assert audit.delegate_events[0] is not AuditEventType.AGENT_DELEGATE_CYCLE  # 防回归到旧映射
    assert factory.calls == 0


async def test_del5_cycle_detection_audits_cycle_event():
    """DEL-5 循环委派 → 审计事件类型为 AGENT_DELEGATE_CYCLE（映射表回归守卫）。"""
    # R7 / inv-7（C5 改动的是同一张映射表，须证明 CYCLE 行未被误伤）
    registry, audit, _, _ = _register_coder()
    token = SubAgentRegistry._delegate_stack.set(["coder"])  # 目标已在委派链中
    try:
        result = await registry._execute_delegate("coder", "task", make_ctx(session_id="sess-1"))
    finally:
        SubAgentRegistry._delegate_stack.reset(token)

    assert result.success is False
    assert "循环" in result.error
    assert audit.delegate_events == [AuditEventType.AGENT_DELEGATE_CYCLE]
    assert audit.delegate_events[0] is not AuditEventType.AGENT_DELEGATE_DEPTH_EXCEEDED


# ════════════════════════════════════════════════════════════════
#  TOK — _estimate_entry_tokens()（C4）
# ════════════════════════════════════════════════════════════════

class FakeTokenEstimator:
    """可配置返回/抛异常的 TokenEstimator，记录调用参数。"""

    def __init__(self, return_value=None, raise_exc=None) -> None:
        self.return_value = return_value
        self.raise_exc = raise_exc
        self.calls: list[list] = []

    def estimate(self, messages):
        self.calls.append(messages)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.return_value


FAKE_IDENTITY = SimpleNamespace(agent_id="a", agent_name="bb")
DESC9 = "ccccccccc"  # 9 字符 → fallback (1+2+9)//int(4.0)+5 = 3+5 = 8（手算推导）


def test_tok1_estimator_normal_return_adopted():
    """TOK-1 注入 estimator 正常返回 int>0 → 采用其值，调用参数精确。"""
    # R10 / inv-8
    est = FakeTokenEstimator(return_value=123)
    registry = SubAgentRegistry(token_estimator=est)

    tokens = registry._estimate_entry_tokens(FAKE_IDENTITY, "cc")

    assert tokens == 123  # 采用 estimator，非 fallback 值 8
    assert est.calls == [[{
        "role": "system",
        "content": "agent_name: bb\nwhen_to_use: cc",
    }]]  # 恰 1 条 system message，与上下文预算同一把尺


def test_tok2_estimator_raises_falls_back_with_debug_log(caplog):
    """TOK-2 estimator 抛异常 → fallback + debug 留痕。"""
    # R9 / inv-8
    est = FakeTokenEstimator(raise_exc=RuntimeError("boom"))
    registry = SubAgentRegistry(token_estimator=est)

    with caplog.at_level(logging.DEBUG, logger="pandaren.sub_agent.registry"):
        tokens = registry._estimate_entry_tokens(FAKE_IDENTITY, DESC9)

    assert tokens == 8  # fallback，异常不阻断摘要构建
    assert any("TokenEstimator 估算失败" in r.message for r in caplog.records)  # inv-8 不静默


@pytest.mark.parametrize("bad_value", [0, -5, 5.5])
def test_tok3_invalid_estimator_return_falls_back(bad_value):
    """TOK-3 estimator 返回 0/负/非 int → 非法值不采用，fallback。"""
    # R10 / inv-8
    est = FakeTokenEstimator(return_value=bad_value)
    registry = SubAgentRegistry(token_estimator=est)

    assert registry._estimate_entry_tokens(FAKE_IDENTITY, DESC9) == 8  # 一律 fallback，估算恒为正


def test_tok4_no_estimator_fallback_golden_int():
    """TOK-4 未注入 estimator → fallback 字符粗估 golden 值（int）。"""
    # R11 / inv-8
    registry = SubAgentRegistry()

    tokens = registry._estimate_entry_tokens(FAKE_IDENTITY, DESC9)

    assert tokens == 8  # (1+2+9)//int(4.0)+5 = 12//4+5，手算推导
    assert isinstance(tokens, int)  # fallback 返回 int（非 float）


# ════════════════════════════════════════════════════════════════
#  FM — _parse_frontmatter()（C6）
# ════════════════════════════════════════════════════════════════

FM_BASE = "---\nagent_id: reviewer\nagent_name: 审查专家\nwhen_to_use: 审查代码\n---\n你是一位审查专家"
FM_EXPECTED = {
    "agent_id": "reviewer",
    "agent_name": "审查专家",
    "when_to_use": "审查代码",
}


def test_fm1_strict_first_line_regression():
    """FM-1 严格 --- 首行（回归基线，证明改动未破坏原行为）。"""
    # R12 / inv-9
    fm, body = _parse_frontmatter(FM_BASE)
    assert fm == FM_EXPECTED
    assert body == "你是一位审查专家"


@pytest.mark.parametrize("prefix", [
    "\ufeff",           # 仅 BOM
    "\ufeff\n\n  \n",   # BOM + 前导空行 + 空格
    "\n\n  \n",         # 无 BOM，仅前导空行 + 空格
])
def test_fm2_bom_and_leading_whitespace_all_parse(prefix):
    """FM-2 BOM / BOM+前导空行 / 仅前导空行 → 均正常解析（C6 核心）。"""
    # R12 / inv-9
    fm, body = _parse_frontmatter(prefix + FM_BASE)
    assert fm == FM_EXPECTED
    assert body == "你是一位审查专家"


@pytest.mark.parametrize("text", ["纯正文文本", "\ufeff纯正文文本"])
def test_fm3_no_frontmatter_returns_empty_dict(text):
    """FM-3 无 frontmatter（含/不含 BOM）→ ({}, body)，BOM 不泄漏进 body。"""
    # R12 / inv-9（回归守卫）
    fm, body = _parse_frontmatter(text)
    assert fm == {}
    assert body == "纯正文文本"
    assert body.startswith("\ufeff") is False  # BOM 已剥离且不残留


@pytest.mark.parametrize("text,expected_warning", [
    ("---\nbad: [unclosed\n---\nbody", "YAML Frontmatter 解析失败"),
    ("---\n- a\n- b\n---\nbody", "顶层不是映射"),
])
def test_fm4_broken_yaml_or_non_mapping_falls_back_with_warning(text, expected_warning, caplog):
    """FM-4 YAML 损坏 / 顶层非 dict → ({}, body) + warning 留痕。"""
    # R12 / inv-9（异常路径不崩溃、留痕）
    fm, body = _parse_frontmatter(text)
    assert fm == {}
    assert body == "body"
    assert any(expected_warning in r.message for r in caplog.records)


# ════════════════════════════════════════════════════════════════
#  CL — _parse_comma_list()（C7）
# ════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("raw", [None, "", "   "])
def test_cl1_empty_values_return_empty_tuple_without_warning(raw, caplog):
    """CL-1 None/空串/纯空白 → () 且不告警。"""
    # inv-10（正常空态基线）
    assert _parse_comma_list(raw, field_name="tools") == ()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("raw,expected", [
    ("a, b ,c", ("a", "b", "c")),        # str 逗号分隔 + strip
    ("a,,b", ("a", "b")),                # 空项过滤
    (["a", " b ", ""], ("a", "b")),      # list 逐项 + 空项过滤
    ((1, "x"), ("1", "x")),              # tuple 含非 str 项 → str() 化
])
def test_cl2_valid_inputs_golden_tuples(raw, expected):
    """CL-2 str/list/tuple 合法输入 → golden tuple。"""
    # inv-10（合法路径）
    assert _parse_comma_list(raw, field_name="tools") == expected


@pytest.mark.parametrize("raw,type_name", [
    (42, "int"),
    (3.14, "float"),
    (True, "bool"),  # bool 是 int 子类，必须单独覆盖
])
def test_cl3_illegal_types_warn_with_field_and_type(raw, type_name, caplog):
    """CL-3 int/float/bool（含 bool=int 子类边界）→ () + warning 含字段名与类型名（C7 核心）。"""
    # R13 / inv-10
    assert _parse_comma_list(raw, field_name="tools") == ()
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "tools" in warnings[0].message
    assert type_name in warnings[0].message


def test_cl4_loader_component_tools_int_ends_empty_with_warning(caplog, tmp_path):
    """CL-4 loader 组件级：frontmatter tools:123 → 蓝图 tools 为空 + warning 传播。"""
    # R13 / inv-10（C7 经真实 YAML 解析链路的端到端证明）
    agent_file = tmp_path / "agent.md"
    agent_file.write_text(
        "---\n"
        "agent_id: t1\n"
        "agent_name: 测试\n"
        "when_to_use: 测试用\n"
        "trust_level: sub_agent\n"
        "tools: 123\n"
        "---\n"
        "正文内容\n",
        encoding="utf-8",
    )

    bp = load_agent_from_file(str(agent_file))

    assert bp.agent_id == "t1"
    assert bp.tools == ()  # 非法类型按空列表处理，最小权限 Fail-Safe 语义不放大
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("tools" in r.message and "int" in r.message for r in warnings)


# ════════════════════════════════════════════════════════════════
#  LD-SRC — load_agents_from_dir source 透传（BUILTIN 档）
# ════════════════════════════════════════════════════════════════

def test_ldsrc_source_parameter_propagates_to_blueprints(tmp_path):
    """LD-SRC-1 source 参数透传到每个蓝图；默认 DIRECTORY（用户目录语义）。"""
    # 内置加载路径（with_default_sub_agents / run_local system/）必须显式传 BUILTIN，
    # 否则全部坍缩为 DIRECTORY，注册覆盖语义无法区分内置与用户。
    for agent_id in ("a1", "b1"):
        (tmp_path / f"{agent_id}.md").write_text(
            "---\n"
            f"agent_id: {agent_id}\n"
            f"agent_name: 蓝图{agent_id}\n"
            "when_to_use: 测试用\n"
            "trust_level: sub_agent\n"
            "---\n"
            "正文\n",
            encoding="utf-8",
        )

    blueprints = load_agents_from_dir(str(tmp_path), source=SubAgentSource.BUILTIN)
    assert {bp.source for bp in blueprints} == {SubAgentSource.BUILTIN}

    defaults = load_agents_from_dir(str(tmp_path))
    assert {bp.source for bp in defaults} == {SubAgentSource.DIRECTORY}


# ════════════════════════════════════════════════════════════════
#  TY — AuditEventType（C8）
# ════════════════════════════════════════════════════════════════

def test_ty1_depth_exceeded_enum_member_exists_and_stable():
    """TY-1 AGENT_DELEGATE_DEPTH_EXCEEDED 成员存在、值稳定、与 CYCLE 不同。"""
    # R14 / inv-11（防成员缺失 → registry 映射兜底到 AGENT_REGISTERED）
    e = AuditEventType.AGENT_DELEGATE_DEPTH_EXCEEDED  # 访问不抛 AttributeError
    assert e.value == "agent_delegate_depth_exceeded"
    assert e is not AuditEventType.AGENT_DELEGATE_CYCLE  # 取值唯一
