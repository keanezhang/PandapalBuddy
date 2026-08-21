"""pandaren/identity/tests/test_models.py — Identity 层测试（依据 docs/design/identity-测试设计.md）

用例编号 C01..C25 与设计文档一一对应。
设计文档中的两处 known-gap（C06 R4 / C17 R1）已随源码修复关闭，本文件不再含 xfail。
"""

from __future__ import annotations

import logging

import pytest

from pandaren.identity.models import (
    PERMISSION_ALL,
    SensitivePermission,
    TrustLevel,
    Identity,
    _validate_fields,
)


# ════════════════════════════════════════════════════════════════
# 公共构造器（设计文档 §8 顶部）
# ════════════════════════════════════════════════════════════════

def make_identity(**overrides) -> Identity:
    base = {
        "agent_id": "alice",
        "agent_name": "Alice",
        "when_to_use": "handle user requests",
        "sensitive_permissions": frozenset({SensitivePermission.DATA_WRITE}),
        "trust_level": TrustLevel.SUB_AGENT,
    }
    base.update(overrides)
    return Identity(**base)


# ════════════════════════════════════════════════════════════════
# Group A — 构造与校验（E4 / S4 / S2 / 规范化）
# ════════════════════════════════════════════════════════════════

class TestConstruction:

    @pytest.mark.parametrize(
        "sp",
        [
            frozenset({SensitivePermission.DATA_WRITE}),
            {SensitivePermission.DATA_WRITE, SensitivePermission.CODE_EXEC},
            [SensitivePermission.DATA_WRITE],
        ],
    )
    @pytest.mark.parametrize(
        "tl",
        [TrustLevel.EXTERNAL, TrustLevel.SUB_AGENT, TrustLevel.ORCHESTRATOR],
    )
    def test_c01_happy_path_normalized(self, sp, tl):
        """C01：全字段合法创建成功，规范化生效（strip + frozenset + 枚举存储）"""
        identity = Identity(
            agent_id="  alice  ",
            agent_name="  Alice  ",
            when_to_use="  do things  ",
            sensitive_permissions=sp,
            trust_level=tl,
        )
        assert identity.agent_id == "alice"
        assert identity.agent_name == "Alice"
        assert identity.when_to_use == "do things"
        assert type(identity.sensitive_permissions) is frozenset
        assert identity.sensitive_permissions == frozenset(sp)
        assert all(isinstance(p, SensitivePermission) for p in identity.sensitive_permissions)
        assert identity.trust_level is tl

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_c02_agent_id_empty_or_blank(self, caplog, bad):
        """C02：agent_id 空串 / 纯空白 → ValueError + logger.error 留痕"""
        with caplog.at_level(logging.ERROR, logger="pandaren.identity.models"):
            with pytest.raises(ValueError, match="agent_id 不能为空"):
                make_identity(agent_id=bad)
        assert any(
            "agent_id 为空" in r.message and r.levelno == logging.ERROR
            for r in caplog.records
        )

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_c03_agent_name_empty_or_blank(self, caplog, bad):
        """C03：agent_name 空串 / 纯空白 → ValueError"""
        with caplog.at_level(logging.ERROR, logger="pandaren.identity.models"):
            with pytest.raises(ValueError, match="agent_name 不能为空"):
                make_identity(agent_name=bad)
        assert any(
            "agent_name 为空" in r.message and r.levelno == logging.ERROR
            for r in caplog.records
        )

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_c04_when_to_use_empty_or_blank(self, caplog, bad):
        """C04：when_to_use 空串 / 纯空白 → ValueError"""
        with caplog.at_level(logging.ERROR, logger="pandaren.identity.models"):
            with pytest.raises(ValueError, match="when_to_use 不能为空"):
                make_identity(when_to_use=bad)
        assert any(
            "when_to_use 为空" in r.message and r.levelno == logging.ERROR
            for r in caplog.records
        )

    @pytest.mark.parametrize("field", ["agent_id", "agent_name", "when_to_use"])
    def test_c05_none_field_rejected(self, field):
        """C05：三字符串字段传 None → ValueError"""
        with pytest.raises(ValueError, match="不能为空"):
            make_identity(**{field: None})

    def test_c06_non_str_agent_id_expected_valueerror(self):
        """C06：agent_id 传非 str 非空值（int）→ ValueError（R4 已修复：统一类型错误语义）"""
        with pytest.raises(ValueError, match="类型错误"):
            make_identity(agent_id=123)

    @pytest.mark.parametrize("bad", [None, "data_write", ("data_write",), {"a": 1}, 7])
    def test_c07_permissions_container_type_error(self, bad):
        """C07：sensitive_permissions 容器类型错误（None/str/tuple/dict/int）→ ValueError，且不抛 TypeError"""
        with pytest.raises(ValueError, match="sensitive_permissions 类型错误"):
            make_identity(sensitive_permissions=bad)

    @pytest.mark.parametrize("bad", [[{"a": 1}], [[1]]])
    def test_c08_unhashable_element_valueerror(self, bad):
        """C08：sensitive_permissions 含不可哈希元素 → ValueError，异常链完整（R2 回归）"""
        with pytest.raises(ValueError, match="不可哈希") as exc_info:
            make_identity(sensitive_permissions=bad)
        assert isinstance(exc_info.value.__cause__, TypeError)

    @pytest.mark.parametrize(
        "bad",
        [["data_write"], [TrustLevel.SUB_AGENT]],
    )
    def test_c09_non_enum_element_rejected(self, bad):
        """C09：sensitive_permissions 元素非枚举（可哈希但越界）→ ValueError（S2 封闭）"""
        with pytest.raises(ValueError, match="元素必须是 SensitivePermission"):
            make_identity(sensitive_permissions=bad)

    @pytest.mark.parametrize("bad", [1, 2, "ORCHESTRATOR", None, 1.0])
    def test_c10_trust_level_forgery_rejected(self, bad):
        """C10：trust_level 伪造（裸 int / 字符串 / None / float）→ ValueError（S4）"""
        # 反证：IntEnum 值语义成立，但构造必须拒绝裸 int
        assert TrustLevel.EXTERNAL == 1
        with pytest.raises(ValueError, match="trust_level 类型错误") as exc_info:
            make_identity(trust_level=bad)
        assert "EXTERNAL" in str(exc_info.value) and "ORCHESTRATOR" in str(exc_info.value)

    def test_c11_validate_fields_direct_contract(self, caplog):
        """C11：_validate_fields 直接调用契约（空值拒绝 / list 可迭代校验）"""
        with caplog.at_level(logging.ERROR, logger="pandaren.identity.models"):
            with pytest.raises(ValueError, match="agent_id 不能为空"):
                _validate_fields(
                    agent_id="",
                    agent_name="b",
                    when_to_use="c",
                    sensitive_permissions=frozenset(),
                    trust_level=TrustLevel.EXTERNAL,
                )
        assert any("agent_id 为空" in r.message for r in caplog.records)

        # list 传入可迭代校验（不要求已规范化 frozenset）
        _validate_fields(
            agent_id="a",
            agent_name="b",
            when_to_use="c",
            sensitive_permissions=[SensitivePermission.DATA_WRITE],
            trust_level=TrustLevel.EXTERNAL,
        )


# ════════════════════════════════════════════════════════════════
# Group B — 不可变性（HC1 / HC2）
# ════════════════════════════════════════════════════════════════

class TestImmutability:

    @pytest.mark.parametrize(
        "field",
        ["agent_id", "agent_name", "when_to_use", "sensitive_permissions", "trust_level", "new_field"],
    )
    def test_c12_assignment_blocked(self, caplog, field):
        """C12：已构造实例字段赋值 → PermissionError，字段值不变，logger.warning 留痕"""
        identity = make_identity()
        with caplog.at_level(logging.WARNING, logger="pandaren.identity.models"):
            with pytest.raises(PermissionError, match="immutable"):
                setattr(identity, field, "hacked")
        assert identity.agent_id == "alice"  # 字段值保持原样
        assert any(
            "运行时篡改尝试" in r.message and "被拦截" in r.message
            and r.levelno == logging.WARNING
            for r in caplog.records
        )

    @pytest.mark.parametrize("field", ["agent_id", "agent_name", "when_to_use", "sensitive_permissions", "trust_level"])
    def test_c13_deletion_blocked(self, field):
        """C13：已构造实例字段删除 → PermissionError"""
        identity = make_identity()
        with pytest.raises(PermissionError, match="immutable"):
            delattr(identity, f"_{field}")
        # 对象未被破坏，仍可读取
        assert identity.agent_id == "alice"

    def test_c14_uninitialized_instance_safe(self):
        """C14：未初始化实例的赋值/删除不崩溃，_safe_agent_id 返回占位符（R3）"""
        u = Identity.__new__(Identity)  # 绕过 __init__，slot 均未赋值
        with pytest.raises(PermissionError):
            u.agent_id = "x"
        with pytest.raises(PermissionError):
            del u._agent_id
        assert u._safe_agent_id() == "<uninitialized>"

    def test_c15_permissions_deep_immutable(self):
        """C15：sensitive_permissions 深度不可变（原生 frozenset 封闭）"""
        identity = make_identity()
        sp = identity.sensitive_permissions
        assert type(sp) is frozenset
        assert not hasattr(sp, "add") and not hasattr(sp, "discard")
        assert sp == frozenset({SensitivePermission.DATA_WRITE})
        assert all(isinstance(p, SensitivePermission) for p in sp)
        with pytest.raises(AttributeError):
            sp.add(SensitivePermission.CODE_EXEC)  # frozenset 无 add → AttributeError，原集合不变
        assert identity.sensitive_permissions == frozenset({SensitivePermission.DATA_WRITE})


# ════════════════════════════════════════════════════════════════
# Group C — has_permission（S2 / inv-8）
# ════════════════════════════════════════════════════════════════

class TestHasPermission:

    def test_c16_hold_vs_missing_vs_empty(self):
        """C16：持有 → True；未持有 → False；空集合 fail-safe → False"""
        holder = make_identity(sensitive_permissions={SensitivePermission.DATA_WRITE})
        assert holder.has_permission(SensitivePermission.DATA_WRITE) is True
        assert holder.has_permission(SensitivePermission.CODE_EXEC) is False

        empty = make_identity(sensitive_permissions=frozenset())
        assert empty.has_permission(SensitivePermission.DATA_DELETE) is False

    @pytest.mark.parametrize("bad", ["data_write", {"a": 1}])
    def test_c17_non_enum_fail_closed(self, caplog, bad):
        """C17：has_permission 非枚举输入（字符串 / 不可哈希）→ fail-closed False（R1 已修复）+ warning 留痕"""
        with caplog.at_level(logging.WARNING, logger="pandaren.identity.models"):
            holder = make_identity(sensitive_permissions={SensitivePermission.DATA_WRITE})
            assert holder.has_permission(bad) is False
        assert any(
            "has_permission 收到非枚举输入" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        )

    @pytest.mark.parametrize("bad", [None, 1])
    def test_c18_none_int_fail_closed(self, bad):
        """C18：has_permission 非枚举输入（None / int）→ False（现状即符合，锁定回归）"""
        assert make_identity().has_permission(bad) is False

    def test_c19_permission_all_contract(self):
        """C19：PERMISSION_ALL 常量契约 + 全权限判定"""
        expected = {
            SensitivePermission.DATA_WRITE: "data_write",
            SensitivePermission.DATA_DELETE: "data_delete",
            SensitivePermission.CODE_EXEC: "code_exec",
            SensitivePermission.SYSTEM_CMD: "system_cmd",
            SensitivePermission.NETWORK_CALL: "network_call",
            SensitivePermission.MEMORY_WRITE: "memory_write",
        }
        assert len(SensitivePermission) == 6
        for member, value in expected.items():
            assert member.value == value

        assert PERMISSION_ALL == frozenset(SensitivePermission)
        assert type(PERMISSION_ALL) is frozenset
        assert len(PERMISSION_ALL) == 6

        id_all = make_identity(sensitive_permissions=PERMISSION_ALL)
        assert all(id_all.has_permission(p) for p in SensitivePermission)

        empty = make_identity(sensitive_permissions=frozenset())
        assert empty.has_permission(SensitivePermission.DATA_WRITE) is False


# ════════════════════════════════════════════════════════════════
# Group D — 等值与哈希（inv-7）
# ════════════════════════════════════════════════════════════════

class TestEqualityAndHash:

    def test_c20_equal_instances_eq_hash_dict_set(self):
        """C20：全字段相同 → == True 且 hash 一致，可用作 dict key / set 元素"""
        a = make_identity()
        b = make_identity()
        assert a == b
        assert hash(a) == hash(b)
        assert {a: "x"}[b] == "x"
        assert len({a, b}) == 1

    @pytest.mark.parametrize(
        "field, diff",
        [
            ("agent_id", "bob"),
            ("agent_name", "Bob"),
            ("when_to_use", "other"),
            ("sensitive_permissions", {SensitivePermission.CODE_EXEC}),
            ("trust_level", TrustLevel.ORCHESTRATOR),
        ],
    )
    def test_c21_single_field_diff_not_equal(self, field, diff):
        """C21：任一字段不同 → != （5 变体 parametrize）"""
        a = make_identity()
        b = make_identity(**{field: diff})
        assert a != b
        assert (a == b) is False

    @pytest.mark.parametrize("other", [None, 5, "alice"])
    def test_c22_non_identity_comparison(self, other):
        """C22：与非 Identity 比较 → False / != True（NotImplemented 路径）"""
        a = make_identity()
        assert (a == other) is False
        assert (a != other) is True

    def test_c23_hash_deterministic(self):
        """C23：hash 确定性（同一实例多次一致 + 等值实例一致）"""
        a = make_identity()
        h1, h2, h3 = hash(a), hash(a), hash(a)
        assert h1 == h2 == h3
        b = make_identity()
        assert h1 == hash(b)


# ════════════════════════════════════════════════════════════════
# Group E — when_to_use 长度警告（inv-9）
# ════════════════════════════════════════════════════════════════

class TestWhenToUseLengthWarning:

    def test_c24_200_chars_no_warning(self, caplog):
        """C24：strip 后恰好 200 字符 → 创建成功且无 warning"""
        with caplog.at_level(logging.WARNING, logger="pandaren.identity.models"):
            identity = make_identity(when_to_use="  " + "x" * 200 + "  ")
        assert len(identity.when_to_use) == 200
        assert not any("when_to_use 过长" in r.message for r in caplog.records)

    def test_c25_201_chars_warning_not_blocking(self, caplog):
        """C25：strip 后 201 字符 → logger.warning，且不阻断创建"""
        with caplog.at_level(logging.WARNING, logger="pandaren.identity.models"):
            identity = make_identity(when_to_use="  " + "x" * 201 + "  ")
        assert len(identity.when_to_use) == 201
        assert any(
            "when_to_use 过长" in r.message and "agent_id='alice'" in r.message
            and r.levelno == logging.WARNING
            for r in caplog.records
        )
