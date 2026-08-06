"""pandaren/tool/registry/validator.py — 注册时校验逻辑。

REQUIRED_FIELDS + CONFLICT_CHECKS
从原 validation.py 迁移。
"""

from __future__ import annotations

import types as builtin_types
import warnings
from dataclasses import replace

from ..types import SensitivityLevel
from ..definition.tool import Tool
from ..exceptions import ToolRegistrationError, ToolValidationWarning


def validate_required_fields(tool: Tool) -> None:
    """校验必填字段完整性。缺失则抛出 ToolRegistrationError。"""
    # name 非空校验
    if not tool.name or not tool.name.strip():
        raise ToolRegistrationError("工具 name 不能为空字符串")

    # description 非空校验
    if not tool.description:
        raise ToolRegistrationError(f"工具 '{tool.name}' 缺少 description")

    # executor 非空校验
    if tool.executor is None:
        raise ToolRegistrationError(f"工具 '{tool.name}' 缺少 executor")

    # input_schema 必填校验（LLM 填参数的依据；正常路径由 decorator/loader 自动推导填充）
    # 注意：__post_init__ 已将 dict 包装为 MappingProxyType
    if tool.input_schema is None:
        raise ToolRegistrationError(
            f"工具 '{tool.name}' 缺少 input_schema"
            f"（正常路径由 @tool.function 或文件加载器自动推导，无需手动填写）"
        )
    if not isinstance(tool.input_schema, (dict, builtin_types.MappingProxyType)):
        raise ToolRegistrationError(
            f"工具 '{tool.name}' 的 input_schema 类型无效（期望 dict，实际 {type(tool.input_schema).__name__}）"
        )
    if not tool.input_schema:
        raise ToolRegistrationError(
            f"工具 '{tool.name}' 的 input_schema 为空"
        )

    # when_to_use 必填校验（不可为空字符串）
    if not tool.when_to_use or not tool.when_to_use.strip():
        raise ToolRegistrationError(
            f"工具 '{tool.name}' 缺少 when_to_use"
            f"（必须描述工具的适用场景，供 LLM 判断何时需要使用此工具）"
        )

    # sensitivity 必填校验（E4: 无默认值，强制开发者显式声明）
    if not isinstance(tool.sensitivity, SensitivityLevel):
        raise ToolRegistrationError(
            f"工具 '{tool.name}' 缺少 sensitivity"
            f"（E4: 无默认值，必须显式声明 LOW/MEDIUM/HIGH/CRITICAL）"
        )

    # description 长度校验（警告级别）
    # llm_guide 会追加到 description 尾部，因此允许较长的合并描述
    MAX_DESC_LENGTH = 1000
    if len(tool.description) > MAX_DESC_LENGTH:
        warnings.warn(
            ToolValidationWarning(
                f"工具 '{tool.name}' 的 description 超过 {MAX_DESC_LENGTH} 字"
                f"（当前 {len(tool.description)} 字），建议精简"
            )
        )


def validate_conflicts(tool: Tool) -> Tool:
    """矛盾检测。WARNING 级别只警告，ERROR 级别阻断注册。

    返回可能被自动修正后的 Tool 副本（如 policy.sensitivity 自动升级）。
    """
    policy = tool.policy

    # ── ERROR 级别 ──
    if policy.circuit_breaker and policy.circuit_breaker.failure_threshold <= 0:
        raise ToolRegistrationError(
            f"工具 '{tool.name}' 的 circuit_breaker.failure_threshold "
            f"必须 > 0，当前值: {policy.circuit_breaker.failure_threshold}"
        )

    # ── 自动升级 ──
    new_policy = policy

    # is_reversible=False 且 sensitivity < HIGH → 自动升级为 HIGH
    if not policy.is_reversible and policy.sensitivity < SensitivityLevel.HIGH:
        new_policy = replace(new_policy, sensitivity=SensitivityLevel.HIGH)
        warnings.warn(
            ToolValidationWarning(
                f"不可逆操作 '{tool.name}' 的 sensitivity 已自动从 "
                f"{policy.sensitivity.name} 升级为 HIGH"
            )
        )

    # CRITICAL 工具强制 audit_required=True
    if new_policy.sensitivity == SensitivityLevel.CRITICAL and not new_policy.audit_required:
        new_policy = replace(new_policy, audit_required=True)
        warnings.warn(
            ToolValidationWarning(
                f"CRITICAL 工具 '{tool.name}' 的 audit_required 已自动设为 True"
            )
        )

    # ── WARNING 级别（只警告，不阻断）──

    # CRITICAL 且 is_reversible=True → 矛盾
    if new_policy.sensitivity == SensitivityLevel.CRITICAL and new_policy.is_reversible:
        warnings.warn(
            ToolValidationWarning(
                f"工具 '{tool.name}': sensitivity=CRITICAL 但 is_reversible=True"
            )
        )

    # is_idempotent=True 且 CRITICAL → 可疑
    if new_policy.is_idempotent and new_policy.sensitivity == SensitivityLevel.CRITICAL:
        warnings.warn(
            ToolValidationWarning(
                f"工具 '{tool.name}': is_idempotent=True 但 sensitivity=CRITICAL"
            )
        )

    # halt_on_failure=True 且 is_reversible=True → 可疑
    if new_policy.halt_on_failure and new_policy.is_reversible:
        warnings.warn(
            ToolValidationWarning(
                f"工具 '{tool.name}': halt_on_failure=True 但 is_reversible=True"
            )
        )

    # 不可逆 + 非审计 → 建议启用审计
    if (not new_policy.is_reversible
            and not new_policy.audit_required
            and new_policy.sensitivity < SensitivityLevel.CRITICAL):
        warnings.warn(
            ToolValidationWarning(
                f"工具 '{tool.name}': 不可逆操作但 audit_required=False，建议启用审计"
            )
        )

    # 如果 policy 有变化，返回新 Tool
    if new_policy is not policy:
        return replace(tool, policy=new_policy)
    return tool
