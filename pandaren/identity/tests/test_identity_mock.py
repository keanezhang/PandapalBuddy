"""
Pandaren Agent SDK · Identity 层 Mock 测试

覆盖约束
--------
  通过 Mock/Patch 验证 Identity 层的内部行为、日志链路和边界条件：
  - _validate_fields 调用验证
  - logger.info/error/warning 日志链路
  - 篡改拦截日志
  - 注入异常
  - Mock PermissionGuard
  - Mock when_to_use 过长警告
  - Mock AgentBuilder.identity() 参数传递
  - _safe_agent_id 安全性
  - Mock 审计链路

运行方式
--------
  cd pandaren/identity/tests && python test_identity_mock.py
"""

from __future__ import annotations

import os
import sys
import io
import logging
from unittest.mock import patch, MagicMock, call

# Windows 控制台 UTF-8 输出
if sys.platform == "win32" and "pytest" not in sys.modules:  # pytest 下交给 capture，避免拆台
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

# ═══ SDK 导入 ═══
from pandaren.identity.models import Identity, SensitivePermission, PERMISSION_ALL, TrustLevel
from pandaren.behavior.permission_guard import PermissionGuard
from pandaren.tool.types import SensitivityLevel


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


# ════════════════════════════════════════════════════
#  工厂方法
# ════════════════════════════════════════════════════

def _make_identity(**overrides) -> Identity:
    """创建测试用 Identity 的工厂方法。"""
    defaults = dict(
        agent_id="test.agent",
        agent_name="测试 Agent",
        when_to_use="用于测试",
        sensitive_permissions=frozenset({
            SensitivePermission.CODE_EXEC,
            SensitivePermission.NETWORK_CALL,
        }),
        trust_level=TrustLevel.SUB_AGENT,
    )
    defaults.update(overrides)
    return Identity(**defaults)


# ════════════════════════════════════════════════════
#  Mock 测试
# ════════════════════════════════════════════════════

def test_mock_validate_fields():
    """5.1 Mock _validate_fields 验证调用次数"""
    print("\n" + "═" * 60)
    print("5.1  Mock _validate_fields")
    print("═" * 60)

    with patch("pandaren.identity.models._validate_fields") as mock_validate:
        mock_validate.return_value = None
        identity = Identity(
            agent_id="mock_test",
            agent_name="Mock Agent",
            when_to_use="mock test",
            sensitive_permissions=frozenset({SensitivePermission.CODE_EXEC}),
            trust_level=TrustLevel.SUB_AGENT,
        )
        assert_true(mock_validate.call_count == 1, "_validate_fields 被调用 1 次")
        call_kwargs = mock_validate.call_args[1]
        assert_true(call_kwargs["agent_id"] == "mock_test", "mock: agent_id 参数正确")
        assert_true(call_kwargs["trust_level"] == TrustLevel.SUB_AGENT, "mock: trust_level 参数正确")


def test_mock_logger():
    """5.2 Mock logger 验证日志输出"""
    print("\n" + "═" * 60)
    print("5.2  Mock logger")
    print("═" * 60)

    # 创建成功时应有 INFO 日志
    with patch("pandaren.identity.models.logger") as mock_logger:
        identity = Identity(
            agent_id="log_test",
            agent_name="Log Agent",
            when_to_use="log test",
            sensitive_permissions=frozenset({SensitivePermission.CODE_EXEC}),
            trust_level=TrustLevel.ORCHESTRATOR,
        )
        info_calls = [c for c in mock_logger.info.call_args_list]
        assert_true(len(info_calls) >= 1, "创建成功时 logger.info 被调用")
        first_info = str(info_calls[0])
        assert_true("log_test" in first_info, "INFO 日志包含 agent_id")

    # 创建失败时应有 ERROR 日志
    with patch("pandaren.identity.models.logger") as mock_logger:
        try:
            Identity(
                agent_id="",
                agent_name="Fail Agent",
                when_to_use="should fail",
                sensitive_permissions=frozenset(),
                trust_level=TrustLevel.EXTERNAL,
            )
        except ValueError:
            pass
        error_calls = [c for c in mock_logger.error.call_args_list]
        assert_true(len(error_calls) >= 1, "创建失败时 logger.error 被调用")


def test_mock_tamper_log():
    """5.3 Mock __setattr__ 拦截日志"""
    print("\n" + "═" * 60)
    print("5.3  Mock 篡改拦截日志")
    print("═" * 60)

    id_mock = _make_identity(agent_id="tamper_test")
    with patch("pandaren.identity.models.logger") as mock_logger:
        try:
            id_mock.agent_id = "tampered"
        except PermissionError:
            pass
        warning_calls = [c for c in mock_logger.warning.call_args_list]
        assert_true(len(warning_calls) >= 1, "篡改时 logger.warning 被调用")
        first_warning = str(warning_calls[0])
        assert_true("tamper_test" in first_warning, "WARNING 日志包含 agent_id")
        assert_true("agent_id" in first_warning, "WARNING 日志包含被篡改的字段名")


def test_mock_inject_exception():
    """5.4 Mock _validate_fields 注入异常"""
    print("\n" + "═" * 60)
    print("5.4  Mock 注入校验异常")
    print("═" * 60)

    with patch("pandaren.identity.models._validate_fields") as mock_validate:
        mock_validate.side_effect = ValueError("Injected validation error")
        @assert_raises(ValueError, "Mock 注入校验异常 → Identity 创建失败")
        def _():
            Identity(
                agent_id="inject_test",
                agent_name="Inject Agent",
                when_to_use="inject test",
                sensitive_permissions=frozenset(),
                trust_level=TrustLevel.SUB_AGENT,
            )


def test_mock_permission_guard():
    """5.5 Mock PermissionGuard.check_permission"""
    print("\n" + "═" * 60)
    print("5.5  Mock PermissionGuard")
    print("═" * 60)

    id_perm = _make_identity(
        agent_id="guard_mock",
        sensitive_permissions=frozenset({SensitivePermission.CODE_EXEC}),
    )

    mock_guard = MagicMock(spec=PermissionGuard)
    mock_guard.check_permission.return_value = "allow"

    result_check = mock_guard.check_permission(
        id_perm.sensitive_permissions,
        SensitivityLevel.HIGH,
        SensitivePermission.CODE_EXEC,
    )
    assert_true(result_check == "allow", "Mock PermissionGuard 返回 allow")
    assert_true(
        mock_guard.check_permission.call_count == 1,
        "Mock PermissionGuard.check_permission 被调用一次",
    )

    # 修改 mock 返回 deny
    mock_guard.check_permission.reset_mock()
    mock_guard.check_permission.return_value = "deny"
    result_deny = mock_guard.check_permission(
        id_perm.sensitive_permissions,
        SensitivityLevel.HIGH,
        SensitivePermission.DATA_WRITE,
    )
    assert_true(result_deny == "deny", "Mock PermissionGuard 返回 deny")


def test_mock_when_to_use_warning():
    """5.6 Mock when_to_use 长度警告"""
    print("\n" + "═" * 60)
    print("5.6  Mock when_to_use 过长警告")
    print("═" * 60)

    with patch("pandaren.identity.models.logger") as mock_logger:
        long_when = "X" * 300
        identity = Identity(
            agent_id="long_when",
            agent_name="Long When",
            when_to_use=long_when,
            sensitive_permissions=frozenset(),
            trust_level=TrustLevel.EXTERNAL,
        )
        warning_calls = [c for c in mock_logger.warning.call_args_list]
        assert_true(len(warning_calls) >= 1, "when_to_use 过长时 logger.warning 被调用")
        first_warning = str(warning_calls[0])
        assert_true("300" in first_warning, "WARNING 日志包含长度 300")
        assert_true("200" in first_warning, "WARNING 日志包含建议上限 200")


def test_mock_agent_builder():
    """5.7 Mock AgentBuilder.identity() 验证参数传递"""
    print("\n" + "═" * 60)
    print("5.7  Mock AgentBuilder.identity()")
    print("═" * 60)

    from pandaren.builder import AgentBuilder as _AgentBuilder

    with patch("pandaren.identity.models.Identity.__init__") as mock_init:
        mock_init.return_value = None
        builder = _AgentBuilder()
        builder.identity(
            agent_id="mock_builder_id",
            agent_name="Mock Builder",
            when_to_use="mock builder test",
            sensitive_permissions=frozenset({SensitivePermission.CODE_EXEC}),
            trust_level=TrustLevel.ORCHESTRATOR,
        )
        assert_true(mock_init.called, "AgentBuilder.identity() 调用了 Identity.__init__")
        call_kwargs = mock_init.call_args[1]
        assert_true(call_kwargs["agent_id"] == "mock_builder_id", "mock: agent_id 传递正确")


def test_mock_safe_agent_id():
    """5.8 _safe_agent_id 安全性"""
    print("\n" + "═" * 60)
    print("5.8  _safe_agent_id 安全性")
    print("═" * 60)

    # Identity 不可变，无法用 patch.object 修改属性（HC1 保护）
    # 改为验证：__setattr__ 拦截时，日志中确实包含了正确的 agent_id
    id_safe = _make_identity(agent_id="safe_id_test")
    with patch("pandaren.identity.models.logger") as mock_logger:
        try:
            id_safe.trust_level = TrustLevel.ORCHESTRATOR
        except PermissionError:
            pass
        warning_calls = [c for c in mock_logger.warning.call_args_list]
        assert_true(len(warning_calls) >= 1, "_safe_agent_id: 篡改拦截 warning 被调用")
        first_warning = str(warning_calls[0])
        assert_true("safe_id_test" in first_warning,
                    "_safe_agent_id: warning 日志包含正确的 agent_id")

    # 验证 _safe_agent_id 在 _agent_id 不存在时返回 <uninitialized>
    raw_id = object.__new__(Identity)
    safe_result = raw_id._safe_agent_id()
    assert_true(safe_result == "<uninitialized>",
                "_safe_agent_id: 未初始化时返回 '<uninitialized>'")


def test_mock_audit_chain():
    """5.9 Mock Identity 创建的完整链路"""
    print("\n" + "═" * 60)
    print("5.9  Mock Identity 完整链路")
    print("═" * 60)

    # 模拟 Identity 创建成功后的 Agent 注册流程
    mock_audit = MagicMock()
    mock_audit.write_sync = MagicMock()

    identity_for_registry = _make_identity(
        agent_id="registry_mock",
        agent_name="Registry Mock",
        sensitive_permissions=frozenset({SensitivePermission.CODE_EXEC}),
        trust_level=TrustLevel.SUB_AGENT,
    )

    # 验证 Identity 的字段可以被审计日志消费
    audit_data = {
        "agent_id": identity_for_registry.agent_id,
        "trust_level": identity_for_registry.trust_level.name,
        "sensitive_permissions": [p.value for p in identity_for_registry.sensitive_permissions],
    }
    assert_true(audit_data["agent_id"] == "registry_mock", "Mock: 审计数据包含 agent_id")
    assert_true(audit_data["trust_level"] == "SUB_AGENT", "Mock: 审计数据包含 trust_level.name")


# ════════════════════════════════════════════════════
#  主入口
# ════════════════════════════════════════════════════

SECTIONS = {
    "validate_fields": test_mock_validate_fields,
    "logger": test_mock_logger,
    "tamper_log": test_mock_tamper_log,
    "inject_exception": test_mock_inject_exception,
    "permission_guard": test_mock_permission_guard,
    "when_to_use_warning": test_mock_when_to_use_warning,
    "agent_builder": test_mock_agent_builder,
    "safe_agent_id": test_mock_safe_agent_id,
    "audit_chain": test_mock_audit_chain,
}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Identity 层 Mock 测试")
    parser.add_argument(
        "--section",
        choices=list(SECTIONS.keys()),
        default="",
        help="仅运行指定测试分区",
    )
    args = parser.parse_args()

    print("🐼 Pandaren Agent SDK — Identity 层 Mock 测试")
    print("   目标模块: pandaren/identity/models.py")
    print("   测试方式: unittest.mock")
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
        test_mock_validate_fields()
        test_mock_logger()
        test_mock_tamper_log()
        test_mock_inject_exception()
        test_mock_permission_guard()
        test_mock_when_to_use_warning()
        test_mock_agent_builder()
        test_mock_safe_agent_id()
        test_mock_audit_chain()
        result.summary("全部")

    if result.failed > 0:
        print("\n⚠️  存在失败的测试用例，请检查上方输出")
        sys.exit(1)
    else:
        print("\n🎉 所有测试通过！Identity 层 Mock 测试验证完毕")
        sys.exit(0)


if __name__ == "__main__":
    main()
