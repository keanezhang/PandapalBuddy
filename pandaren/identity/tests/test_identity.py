"""
Pandaren Agent SDK · Identity 层真实测试

覆盖约束
--------
  - HC1：Identity 所有字段创建后不可修改（__slots__ + __setattr__ 拦截）
  - HC2：Permission 结构深度不可变（tuple + frozen dataclass）
  - E4 ：必填字段缺失时拒绝创建
  - S1 ：所有字段通过 @property 只读暴露
  - S2 ：通配符权限拦截
  - S3 ：权限不继承（每个 Identity 独立声明 permission_set）
  - S4 ：信任来源不可伪造（TrustLevel 枚举，不接受 int）
  - O1 ：agent_id 是 trace 的必要锚点

运行方式
--------
  cd pandaren/identity/tests && python test_identity.py
  cd pandaren/identity/tests && python test_identity.py --section trust_level
  cd pandaren/identity/tests && python test_identity.py --section permission
  cd pandaren/identity/tests && python test_identity.py --section identity
  cd pandaren/identity/tests && python test_identity.py --section integration
"""

from __future__ import annotations

import os
import sys
import io
import logging

# Windows 控制台 UTF-8 输出
if sys.platform == "win32" and "pytest" not in sys.modules:  # pytest 下交给 capture，避免拆台
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

# ═══ SDK 导入 ═══
from pandaren.identity.models import Identity, SensitivePermission, PERMISSION_ALL, TrustLevel
from pandaren.behavior.permission_guard import PermissionGuard


# ════════════════════════════════════════════════════
#  测试框架
# ════════════════════════════════════════════════════

class TestResult:
    """轻量测试结果收集器。"""
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors: list[str] = []

    def ok(self, name: str):
        self.passed += 1
        print(f"   ✅ {name}")

    def fail(self, name: str, detail: str = ""):
        self.failed += 1
        msg = f"   ❌ {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        self.errors.append(msg)

    def summary(self, section: str = ""):
        total = self.passed + self.failed
        label = f" [{section}]" if section else ""
        print(f"\n📊{label} 通过={self.passed} / 失败={self.failed} / 总计={total}")
        if self.errors:
            print("   失败列表:")
            for e in self.errors:
                print(f"     {e}")
        return self.failed == 0


result = TestResult()


def assert_true(condition: bool, name: str, detail: str = ""):
    if condition:
        result.ok(name)
    else:
        result.fail(name, detail or "条件为 False")


def assert_raises(exc_type, name: str, detail: str = ""):
    """装饰器：断言被装饰的函数会抛出指定异常。"""
    def decorator(fn):
        try:
            fn()
            result.fail(name, f"未抛出 {exc_type.__name__}" + (f": {detail}" if detail else ""))
        except exc_type:
            result.ok(name)
        except Exception as e:
            result.fail(name, f"抛出了 {type(e).__name__}({e}) 而非 {exc_type.__name__}")
    return decorator


def assert_no_raises(name: str, detail: str = ""):
    """装饰器：断言被装饰的函数不会抛出异常。"""
    def decorator(fn):
        try:
            fn()
            result.ok(name)
        except Exception as e:
            result.fail(name, f"意外抛出 {type(e).__name__}({e})" + (f": {detail}" if detail else ""))
    return decorator


# ════════════════════════════════════════════════════
#  1. TrustLevel 枚举测试
# ════════════════════════════════════════════════════

def test_trust_level():
    print("\n" + "═" * 60)
    print("1️⃣  TrustLevel 枚举测试")
    print("═" * 60)

    # ── 1.1 三个枚举值定义正确 ──
    assert_true(TrustLevel.EXTERNAL == 1, "EXTERNAL = 1")
    assert_true(TrustLevel.SUB_AGENT == 2, "SUB_AGENT = 2")
    assert_true(TrustLevel.ORCHESTRATOR == 3, "ORCHESTRATOR = 3")

    # ── 1.2 IntEnum 支持大小比较（S4 不可伪造，但可比较）──
    assert_true(TrustLevel.EXTERNAL < TrustLevel.SUB_AGENT, "EXTERNAL < SUB_AGENT")
    assert_true(TrustLevel.SUB_AGENT < TrustLevel.ORCHESTRATOR, "SUB_AGENT < ORCHESTRATOR")
    assert_true(TrustLevel.EXTERNAL < TrustLevel.ORCHESTRATOR, "EXTERNAL < ORCHESTRATOR")
    assert_true(not (TrustLevel.ORCHESTRATOR < TrustLevel.SUB_AGENT), "ORCHESTRATOR ≮ SUB_AGENT")

    # ── 1.3 IntEnum 同时是 int ──
    assert_true(isinstance(TrustLevel.SUB_AGENT, int), "TrustLevel 实例是 int")
    assert_true(TrustLevel.SUB_AGENT + 1 == 3, "TrustLevel 可做算术运算（+1=3）")

    # ── 1.4 S4：TrustLevel 不接受裸 int 替代 ──
    @assert_raises(ValueError, "S4: int 1 不等于 TrustLevel.EXTERNAL")
    def _():
        Identity(
            agent_id="test_s4_int",
            agent_name="test",
            when_to_use="test",
            sensitive_permissions=frozenset(),
            trust_level=1,  # 应该用 TrustLevel.EXTERNAL
        )

    # ── 1.5 S4：TrustLevel 不接受字符串替代 ──
    @assert_raises(ValueError, "S4: 字符串 'orchestrator' 不被接受")
    def _():
        Identity(
            agent_id="test_s4_str",
            agent_name="test",
            when_to_use="test",
            sensitive_permissions=frozenset(),
            trust_level="ORCHESTRATOR",  # 应该用 TrustLevel.ORCHESTRATOR
        )

    # ── 1.6 .name 和 .value 属性 ──
    assert_true(TrustLevel.EXTERNAL.name == "EXTERNAL", ".name 属性正确")
    assert_true(TrustLevel.ORCHESTRATOR.value == 3, ".value 属性正确")

    # ── 1.7 枚举成员遍历 ──
    members = list(TrustLevel)
    assert_true(len(members) == 3, "枚举成员数为 3")
    assert_true(members == [TrustLevel.EXTERNAL, TrustLevel.SUB_AGENT, TrustLevel.ORCHESTRATOR],
                "枚举成员顺序正确")


# ════════════════════════════════════════════════════
#  2. SensitivePermission 枚举测试
# ════════════════════════════════════════════════════

def test_permission():
    print("\n" + "═" * 60)
    print("2️⃣  SensitivePermission 枚举测试")
    print("═" * 60)

    # ── 2.1 六个枚举值全部存在 ──
    assert_true(SensitivePermission.DATA_WRITE is not None, "DATA_WRITE 枚举值存在")
    assert_true(SensitivePermission.DATA_DELETE is not None, "DATA_DELETE 枚举值存在")
    assert_true(SensitivePermission.CODE_EXEC is not None, "CODE_EXEC 枚举值存在")
    assert_true(SensitivePermission.SYSTEM_CMD is not None, "SYSTEM_CMD 枚举值存在")
    assert_true(SensitivePermission.NETWORK_CALL is not None, "NETWORK_CALL 枚举值存在")
    assert_true(SensitivePermission.MEMORY_WRITE is not None, "MEMORY_WRITE 枚举值存在")

    # ── 2.2 枚举成员数量为 6 ──
    members = list(SensitivePermission)
    assert_true(len(members) == 6, "枚举成员数为 6")

    # ── 2.3 每个枚举值都是 str（str Enum）──
    for perm in SensitivePermission:
        assert_true(isinstance(perm.value, str), f"{perm.name}.value 是 str")

    # ── 2.4 PERMISSION_ALL 包含全部 6 个枚举值 ──
    assert_true(isinstance(PERMISSION_ALL, frozenset), "PERMISSION_ALL 是 frozenset")
    assert_true(len(PERMISSION_ALL) == 6, "PERMISSION_ALL 包含 6 个枚举值")
    for perm in SensitivePermission:
        assert_true(perm in PERMISSION_ALL, f"PERMISSION_ALL 包含 {perm.name}")

    # ── 2.5 frozenset 不可修改 ──
    @assert_raises(AttributeError, "PERMISSION_ALL 无 add 方法（frozenset 不可变）")
    def _():
        PERMISSION_ALL.add(SensitivePermission.CODE_EXEC)  # type: ignore[attr-defined]

    # ── 2.6 枚举成员可用于 frozenset 查找 ──
    fs = frozenset({SensitivePermission.CODE_EXEC, SensitivePermission.DATA_DELETE})
    assert_true(SensitivePermission.CODE_EXEC in fs, "frozenset 包含检查: CODE_EXEC in fs")
    assert_true(SensitivePermission.SYSTEM_CMD not in fs, "frozenset 不包含检查: SYSTEM_CMD not in fs")

    # ── 2.7 枚举 .name 和 .value 属性 ──
    assert_true(SensitivePermission.CODE_EXEC.name == "CODE_EXEC", "CODE_EXEC.name 正确")
    assert_true(isinstance(SensitivePermission.CODE_EXEC.value, str), "CODE_EXEC.value 是 str")

    # ── 2.8 枚举成员可作为 dict key ──
    d = {SensitivePermission.DATA_WRITE: "write", SensitivePermission.CODE_EXEC: "exec"}
    assert_true(d[SensitivePermission.DATA_WRITE] == "write", "枚举成员作为 dict key 正常")

    # ── 2.9 空 frozenset 合法（Fail-Safe Default）──
    empty_fs = frozenset()
    assert_true(isinstance(empty_fs, frozenset), "空 frozenset 合法")
    assert_true(len(empty_fs) == 0, "空 frozenset 长度为 0")


# ════════════════════════════════════════════════════
#  3. Identity 核心类测试
# ════════════════════════════════════════════════════

def _make_identity(**overrides) -> Identity:
    """创建测试用 Identity 的工厂方法。"""
    defaults = dict(
        agent_id="test.agent",
        agent_name="测试 Agent",
        when_to_use="用于测试",
        sensitive_permissions=PERMISSION_ALL,
        trust_level=TrustLevel.SUB_AGENT,
    )
    defaults.update(overrides)
    return Identity(**defaults)


def test_identity():
    print("\n" + "═" * 60)
    print("3️⃣  Identity 核心类测试")
    print("═" * 60)

    # ────────────────────────────────────────
    #  3.1 正常创建（全部必填字段）
    # ────────────────────────────────────────
    identity = _make_identity()
    assert_true(identity.agent_id == "test.agent", "agent_id 正确")
    assert_true(identity.agent_name == "测试 Agent", "agent_name 正确")
    assert_true(identity.when_to_use == "用于测试", "when_to_use 正确")
    assert_true(identity.trust_level == TrustLevel.SUB_AGENT, "trust_level 正确")
    assert_true(len(identity.sensitive_permissions) == 6, "sensitive_permissions 长度正确（PERMISSION_ALL = 6）")

    # ────────────────────────────────────────
    #  3.2 基本字段有值
    # ────────────────────────────────────────
    assert_true(identity.agent_id == "test.agent", "agent_id 正确")
    assert_true(identity.agent_name == "测试 Agent", "agent_name 正确")
    assert_true(identity.when_to_use == "用于测试", "when_to_use 正确")
    assert_true(identity.trust_level == TrustLevel.SUB_AGENT, "trust_level 正确")

    # ────────────────────────────────────────
    #  3.4 HC1：__setattr__ 拦截运行时修改
    # ────────────────────────────────────────
    id_hc1 = _make_identity(agent_id="hc1_test")

    @assert_raises(PermissionError, "HC1: 修改 agent_id 抛出 PermissionError")
    def _():
        id_hc1.agent_id = "hacked"

    @assert_raises(PermissionError, "HC1: 修改 agent_name 抛出 PermissionError")
    def _():
        id_hc1.agent_name = "hacked"

    @assert_raises(PermissionError, "HC1: 修改 when_to_use 抛出 PermissionError")
    def _():
        id_hc1.when_to_use = "hacked"

    @assert_raises(PermissionError, "HC1: 修改 sensitive_permissions 抛出 PermissionError")
    def _():
        id_hc1.sensitive_permissions = frozenset()

    @assert_raises(PermissionError, "HC1: 修改 trust_level 抛出 PermissionError")
    def _():
        id_hc1.trust_level = TrustLevel.ORCHESTRATOR

    # ── 3.5 HC1：修改不存在的属性也被拦截 ──
    @assert_raises(PermissionError, "HC1: 设置不存在的新属性也抛出 PermissionError")
    def _():
        id_hc1.new_attr = "injected"

    # ────────────────────────────────────────
    #  3.6 HC1：__delattr__ 拦截运行时删除
    # ────────────────────────────────────────
    id_del = _make_identity(agent_id="del_test")

    @assert_raises(PermissionError, "HC1: 删除 agent_id 抛出 PermissionError")
    def _():
        del id_del._agent_id

    # ────────────────────────────────────────
    #  3.7 __slots__ 防止动态添加属性
    # ────────────────────────────────────────
    @assert_raises(PermissionError, "__slots__: 动态添加属性被拦截")
    def _():
        id_hc1.extra_field = "should_fail"

    # ────────────────────────────────────────
    #  3.8 E4：必填字段缺失 / 空值
    # ────────────────────────────────────────
    @assert_raises(ValueError, "E4: agent_id 为空字符串抛出 ValueError")
    def _():
        _make_identity(agent_id="")

    @assert_raises(ValueError, "E4: agent_id 为纯空格抛出 ValueError")
    def _():
        _make_identity(agent_id="   ")

    @assert_raises(ValueError, "E4: agent_name 为空字符串抛出 ValueError")
    def _():
        _make_identity(agent_name="")

    @assert_raises(ValueError, "E4: when_to_use 为空字符串抛出 ValueError")
    def _():
        _make_identity(when_to_use="")

    @assert_raises(ValueError, "E4: when_to_use 为纯空格抛出 ValueError")
    def _():
        _make_identity(when_to_use="   ")

    # ────────────────────────────────────────
    #  3.9 E4：sensitive_permissions 类型校验
    # ────────────────────────────────────────
    @assert_raises(ValueError, "E4: sensitive_permissions=None 抛出 ValueError")
    def _():
        _make_identity(sensitive_permissions=None)

    @assert_raises(ValueError, "E4: sensitive_permissions 为字符串抛出 ValueError")
    def _():
        _make_identity(sensitive_permissions="code:read")

    @assert_raises(ValueError, "E4: sensitive_permissions 包含非 SensitivePermission 元素抛出 ValueError")
    def _():
        _make_identity(sensitive_permissions=frozenset({"not_a_permission"}))

    @assert_raises(ValueError, "E4: sensitive_permissions 包含 dict 抛出 ValueError")
    def _():
        _make_identity(sensitive_permissions=frozenset({{"resource": "file"}}))

    # ────────────────────────────────────────
    #  3.10 空 sensitive_permissions 是合法的（Fail-Safe Default）
    # ────────────────────────────────────────
    @assert_no_raises("空 sensitive_permissions=frozenset() 合法（Fail-Safe）")
    def _():
        _make_identity(sensitive_permissions=frozenset())

    id_empty_perm = _make_identity(sensitive_permissions=frozenset())
    assert_true(len(id_empty_perm.sensitive_permissions) == 0, "空 sensitive_permissions 长度为 0")

    # ────────────────────────────────────────
    #  3.11 S4：trust_level 类型校验
    # ────────────────────────────────────────
    @assert_raises(ValueError, "S4: trust_level=1 (int) 抛出 ValueError")
    def _():
        _make_identity(trust_level=1)

    @assert_raises(ValueError, "S4: trust_level='SUB_AGENT' (str) 抛出 ValueError")
    def _():
        _make_identity(trust_level="SUB_AGENT")

    @assert_raises(ValueError, "S4: trust_level=None 抛出 ValueError")
    def _():
        _make_identity(trust_level=None)

    # ────────────────────────────────────────
    #  3.13 HC2：sensitive_permissions 转为 frozenset（深度不可变）
    # ────────────────────────────────────────
    perms_set = {SensitivePermission.CODE_EXEC, SensitivePermission.NETWORK_CALL}
    id_frozen = _make_identity(sensitive_permissions=perms_set)
    assert_true(isinstance(id_frozen.sensitive_permissions, frozenset), "sensitive_permissions 是 frozenset 类型")

    # 修改原 set 不影响 Identity
    perms_set.add(SensitivePermission.DATA_WRITE)
    assert_true(len(id_frozen.sensitive_permissions) == 2, "HC2: 修改原 set 不影响 Identity")

    # ── 3.14 sensitive_permissions 返回的 frozenset 不允许 add ──
    @assert_raises(AttributeError, "HC2: sensitive_permissions frozenset 无 add 方法")
    def _():
        id_frozen.sensitive_permissions.add(SensitivePermission.DATA_WRITE)

    # ────────────────────────────────────────
    #  3.15 strip 规范化
    # ────────────────────────────────────────
    id_strip = _make_identity(
        agent_id="  stripped_id  ",
        agent_name="  stripped_name  ",
        when_to_use="  stripped_when  ",
    )
    assert_true(id_strip.agent_id == "stripped_id", "agent_id strip 规范化")
    assert_true(id_strip.agent_name == "stripped_name", "agent_name strip 规范化")
    assert_true(id_strip.when_to_use == "stripped_when", "when_to_use strip 规范化")

    # ────────────────────────────────────────
    #  3.16 when_to_use 过长警告
    # ────────────────────────────────────────
    long_when = "A" * 201
    @assert_no_raises("when_to_use > 200 字符只发 warning，不抛异常")
    def _():
        _make_identity(when_to_use=long_when)

    # ────────────────────────────────────────
    #  3.17 has_permission 便捷方法
    # ────────────────────────────────────────
    id_res = _make_identity(sensitive_permissions=frozenset({
        SensitivePermission.CODE_EXEC,
        SensitivePermission.NETWORK_CALL,
    }))
    assert_true(id_res.has_permission(SensitivePermission.CODE_EXEC),
                "has_permission: CODE_EXEC → True")
    assert_true(id_res.has_permission(SensitivePermission.NETWORK_CALL),
                "has_permission: NETWORK_CALL → True")
    assert_true(not id_res.has_permission(SensitivePermission.DATA_WRITE),
                "has_permission: DATA_WRITE → False")

    id_empty_res = _make_identity(sensitive_permissions=frozenset())
    assert_true(not id_empty_res.has_permission(SensitivePermission.CODE_EXEC),
                "空 sensitive_permissions 的 has_permission 始终为 False")

    # ────────────────────────────────────────
    #  3.18 __eq__ 等值比较
    # ────────────────────────────────────────
    id_a = _make_identity(agent_id="eq_test")
    id_b = _make_identity(agent_id="eq_test")
    assert_true(id_a == id_b, "__eq__: 相同字段的 Identity 相等")

    id_c = _make_identity(agent_id="eq_test", agent_name="不同名称")
    assert_true(id_a != id_c, "__eq__: 不同字段的 Identity 不等")

    assert_true(id_a != "not_identity", "__eq__: 与非 Identity 比较返回不等")
    assert_true(id_a != 42, "__eq__: 与 int 比较返回不等")

    # ────────────────────────────────────────
    #  3.19 __hash__ 哈希（支持 set/dict 使用）
    # ────────────────────────────────────────
    id_h1 = _make_identity(agent_id="hash_test")
    id_h2 = _make_identity(agent_id="hash_test")
    assert_true(hash(id_h1) == hash(id_h2), "__hash__: 相等 Identity 哈希相同")

    s = {id_h1, id_h2}
    assert_true(len(s) == 1, "__hash__: 相等 Identity 在 set 中去重")

    d = {id_h1: "value"}
    assert_true(d[id_h2] == "value", "__hash__: 相等 Identity 作为 dict key 可互查")

    # ────────────────────────────────────────
    #  3.20 __repr__
    # ────────────────────────────────────────
    id_repr = _make_identity(agent_id="repr_test", agent_name="展示Agent")
    repr_str = repr(id_repr)
    assert_true("repr_test" in repr_str, "__repr__ 包含 agent_id")
    assert_true("展示Agent" in repr_str, "__repr__ 包含 agent_name")
    assert_true("SUB_AGENT" in repr_str, "__repr__ 包含 trust_level.name")

    # ────────────────────────────────────────
    #  3.21 S3：权限不继承（无"继承自"字段）
    # ────────────────────────────────────────
    parent_perms = PERMISSION_ALL
    child_perms = frozenset({SensitivePermission.CODE_EXEC})

    parent_id = _make_identity(agent_id="parent", sensitive_permissions=parent_perms,
                               trust_level=TrustLevel.ORCHESTRATOR)
    child_id = _make_identity(agent_id="child", sensitive_permissions=child_perms,
                              trust_level=TrustLevel.SUB_AGENT)

    assert_true(len(child_id.sensitive_permissions) == 1, "S3: 子 agent 权限独立声明")
    assert_true(SensitivePermission.CODE_EXEC in child_id.sensitive_permissions,
                "S3: 子 agent 只有 CODE_EXEC")
    assert_true(parent_id.sensitive_permissions != child_id.sensitive_permissions,
                "S3: 父子 sensitive_permissions 不同")

    assert_true(not hasattr(parent_id, "inherits_from"), "S3: 无 inherits_from 字段")
    assert_true(not hasattr(parent_id, "parent_id"), "S3: 无 parent_id 字段")

    # ────────────────────────────────────────
    #  3.22 O1：agent_id 是 trace 的必要锚点
    # ────────────────────────────────────────
    id_trace = _make_identity(agent_id="trace_anchor_001")
    assert_true(id_trace.agent_id == "trace_anchor_001",
                "O1: agent_id 作为 trace 锚点可正确读取")

    # ────────────────────────────────────────
    #  3.23 三个 TrustLevel 都能正确创建 Identity
    # ────────────────────────────────────────
    @assert_no_raises("TrustLevel.EXTERNAL 创建 Identity")
    def _():
        _make_identity(trust_level=TrustLevel.EXTERNAL)

    @assert_no_raises("TrustLevel.SUB_AGENT 创建 Identity")
    def _():
        _make_identity(trust_level=TrustLevel.SUB_AGENT)

    @assert_no_raises("TrustLevel.ORCHESTRATOR 创建 Identity")
    def _():
        _make_identity(trust_level=TrustLevel.ORCHESTRATOR)

    # ────────────────────────────────────────
    #  3.24 只读属性验证（S1）
    # ────────────────────────────────────────
    id_ro = _make_identity()
    assert_true(type(Identity.__dict__.get("agent_id")) is property, "S1: agent_id 是 property")
    assert_true(type(Identity.__dict__.get("agent_name")) is property, "S1: agent_name 是 property")
    assert_true(type(Identity.__dict__.get("when_to_use")) is property, "S1: when_to_use 是 property")
    assert_true(type(Identity.__dict__.get("sensitive_permissions")) is property, "S1: sensitive_permissions 是 property")
    assert_true(type(Identity.__dict__.get("trust_level")) is property, "S1: trust_level 是 property")

    _ = id_ro.agent_id
    _ = id_ro.agent_name
    _ = id_ro.when_to_use
    _ = id_ro.sensitive_permissions
    _ = id_ro.trust_level
    result.ok("S1: 所有 @property 只读属性可正常读取")


# ════════════════════════════════════════════════════
#  4. 集成测试（PermissionGuard + AgentBuilder）
# ════════════════════════════════════════════════════

def test_integration():
    print("\n" + "═" * 60)
    print("4️⃣  集成测试")
    print("═" * 60)

    # ── 4.1 PermissionGuard + Identity.sensitive_permissions ──
    print("\n   ── 4.1 PermissionGuard 集成 ──")

    from pandaren.tool.types import SensitivityLevel

    orch_identity = _make_identity(
        agent_id="orch_1",
        sensitive_permissions=frozenset({
            SensitivePermission.CODE_EXEC,
            SensitivePermission.NETWORK_CALL,
        }),
        trust_level=TrustLevel.ORCHESTRATOR,
    )

    guard = PermissionGuard()

    result_guard = guard.check_permission(
        orch_identity.sensitive_permissions,
        SensitivityLevel.HIGH,
        SensitivePermission.CODE_EXEC,
    )
    assert_true(result_guard == "allow", "PermissionGuard: 有权限 → allow")

    result_guard2 = guard.check_permission(
        orch_identity.sensitive_permissions,
        SensitivityLevel.HIGH,
        SensitivePermission.DATA_WRITE,
    )
    assert_true(result_guard2 == "deny", "PermissionGuard: 无权限 → deny")

    result_guard3 = guard.check_permission(
        orch_identity.sensitive_permissions,
        SensitivityLevel.LOW,
        None,
    )
    assert_true(result_guard3 == "allow", "PermissionGuard: LOW 敏感度无需权限 → allow")

    result_guard4 = guard.check_permission(
        orch_identity.sensitive_permissions,
        SensitivityLevel.HIGH,
        SensitivePermission.NETWORK_CALL,
    )
    assert_true(result_guard4 == "allow", "PermissionGuard: 持有 NETWORK_CALL → allow")

    # ── 4.2 空 sensitive_permissions = 拒绝高敏感操作（Fail-Safe）──
    empty_identity = _make_identity(
        agent_id="empty_perm",
        sensitive_permissions=frozenset(),
        trust_level=TrustLevel.EXTERNAL,
    )
    result_empty = guard.check_permission(
        empty_identity.sensitive_permissions,
        SensitivityLevel.HIGH,
        SensitivePermission.CODE_EXEC,
    )
    assert_true(result_empty == "deny", "Fail-Safe: 空 sensitive_permissions + HIGH 工具 → deny")

    # 但 LOW 敏感度工具仍然可以使用
    result_low = guard.check_permission(
        empty_identity.sensitive_permissions,
        SensitivityLevel.LOW,
        None,
    )
    assert_true(result_low == "allow", "Fail-Safe: 空 sensitive_permissions + LOW 工具 → allow")

    # ── 4.3 S3：权限不继承在 Guard 中体现 ──
    parent_id = _make_identity(agent_id="parent", sensitive_permissions=PERMISSION_ALL,
                               trust_level=TrustLevel.ORCHESTRATOR)
    child_id = _make_identity(agent_id="child",
                              sensitive_permissions=frozenset({SensitivePermission.CODE_EXEC}),
                              trust_level=TrustLevel.SUB_AGENT)

    assert_true(guard.check_permission(parent_id.sensitive_permissions,
                 SensitivityLevel.HIGH, SensitivePermission.DATA_WRITE) == "allow",
                "S3 集成: 父 agent 可 DATA_WRITE")

    assert_true(guard.check_permission(child_id.sensitive_permissions,
                 SensitivityLevel.HIGH, SensitivePermission.DATA_WRITE) == "deny",
                "S3 集成: 子 agent 不可 DATA_WRITE（权限独立）")

    # ── 4.4 AgentBuilder.identity() 创建 Identity ──
    print("\n   ── 4.4 AgentBuilder 集成 ──")

    from pandaren.builder import AgentBuilder

    @assert_no_raises("AgentBuilder.identity() 正常创建 Identity")
    def _():
        AgentBuilder().identity(
            agent_id="builder_test",
            agent_name="Builder 测试",
            when_to_use="Builder 集成测试",
            sensitive_permissions=frozenset({SensitivePermission.CODE_EXEC}),
            trust_level=TrustLevel.SUB_AGENT,
        )

    @assert_raises(ValueError, "AgentBuilder.identity() 缺少 when_to_use → ValueError")
    def _():
        AgentBuilder().identity(
            agent_id="builder_e4",
            agent_name="Builder E4",
            when_to_use="",
            sensitive_permissions=frozenset(),
            trust_level=TrustLevel.SUB_AGENT,
        )

    @assert_raises(ValueError, "AgentBuilder.identity() trust_level=int → ValueError")
    def _():
        AgentBuilder().identity(
            agent_id="builder_s4",
            agent_name="Builder S4",
            when_to_use="测试",
            sensitive_permissions=frozenset(),
            trust_level=2,
        )

    @assert_raises(ValueError, "AgentBuilder.identity() 非法 sensitive_permissions → ValueError")
    def _():
        AgentBuilder().identity(
            agent_id="builder_s2",
            agent_name="Builder S2",
            when_to_use="测试",
            sensitive_permissions=frozenset({"invalid_perm"}),
            trust_level=TrustLevel.SUB_AGENT,
        )

    # ── 4.5 SubAgentBlueprint → Identity 流程 ──
    print("\n   ── 4.5 SubAgentBlueprint 加载流程 ──")

    from pandaren.sub_agent.loader import _parse_trust_level, _parse_sensitive_permissions
    from pathlib import Path

    tl = _parse_trust_level("ORCHESTRATOR", Path("test.md"))
    assert_true(tl == TrustLevel.ORCHESTRATOR, "_parse_trust_level 解析 ORCHESTRATOR")

    tl2 = _parse_trust_level("sub_agent", Path("test.md"))
    assert_true(tl2 == TrustLevel.SUB_AGENT, "_parse_trust_level 大小写不敏感")

    @assert_raises(ValueError, "_parse_trust_level 空值 → ValueError")
    def _():
        _parse_trust_level("", Path("test.md"))

    @assert_raises(ValueError, "_parse_trust_level 非法值 → ValueError")
    def _():
        _parse_trust_level("admin", Path("test.md"))

    perms = _parse_sensitive_permissions("code_exec, data_write, network_call")
    assert_true(len(perms) == 3, "_parse_sensitive_permissions 解析 3 个权限")
    assert_true(SensitivePermission.CODE_EXEC in perms, "_parse_sensitive_permissions 包含 CODE_EXEC")
    assert_true(SensitivePermission.DATA_WRITE in perms, "_parse_sensitive_permissions 包含 DATA_WRITE")

    empty_perms = _parse_sensitive_permissions("")
    assert_true(len(empty_perms) == 0, "_parse_sensitive_permissions 空值 → 空 frozenset")

    # ── 4.6 ObservabilityProvider 消费 identity.agent_id ──
    print("\n   ── 4.6 ObservabilityProvider 消费 ──")

    from pandaren.observability.provider import ObservabilityProvider
    from pandaren.observability.config import ObservabilityConfig

    obs_config = ObservabilityConfig()
    obs_provider = ObservabilityProvider(obs_config, agent_id="obs_test_001")
    assert_true(obs_provider.logger._agent_id == "obs_test_001",
                "O1 集成: ObservabilityProvider 透传 agent_id 给 Logger")

    assert_true(obs_provider.audit_log is not None, "ObservabilityProvider 创建 audit_log")


# ════════════════════════════════════════════════════
#  主入口
# ════════════════════════════════════════════════════

SECTIONS = {
    "trust_level": test_trust_level,
    "permission": test_permission,
    "identity": test_identity,
    "integration": test_integration,
}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Identity 层真实测试")
    parser.add_argument(
        "--section",
        choices=list(SECTIONS.keys()),
        default="",
        help="仅运行指定测试分区",
    )
    args = parser.parse_args()

    print("🐼 Pandaren Agent SDK — Identity 层真实测试")
    print("   目标模块: pandaren/identity/models.py")
    print("   包含类: Identity, SensitivePermission, TrustLevel")
    print()

    logging.getLogger("pandaren.identity.models").setLevel(logging.WARNING)

    if args.section:
        section_name = args.section
        section_fn = SECTIONS[section_name]
        section_result = TestResult()

        global result
        old_result = result
        result = section_result

        section_fn()

        result = old_result
        result.passed += section_result.passed
        result.failed += section_result.failed
        result.errors.extend(section_result.errors)

        section_result.summary(section_name)
    else:
        test_trust_level()
        test_permission()
        test_identity()
        test_integration()
        result.summary("全部")

    if result.failed > 0:
        print("\n⚠️  存在失败的测试用例，请检查上方输出")
        sys.exit(1)
    else:
        print("\n🎉 所有测试通过！Identity 层真实测试验证完毕")
        sys.exit(0)


if __name__ == "__main__":
    main()
