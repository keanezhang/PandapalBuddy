"""pandaren/behavior/permission_guard.py — 权限校验（无状态纯函数）

设计原则：
  S2 ：最小权限，默认 deny all（仅 HIGH/CRITICAL 工具受限）。
  HC3：被 AgentLoop 硬编码调用。

权限检查逻辑（敏感度优先）：
  ① LOW / MEDIUM 工具 → 直接放行，无需任何权限声明。
  ② HIGH / CRITICAL 工具，且 sensitive_permission 为 None → 直接放行（工具开发者未声明限制）。
  ③ HIGH / CRITICAL 工具，且 sensitive_permission 不为 None：
     → identity.sensitive_permissions 中包含该值 → allow
     → 不包含 → deny（缺少所需的高敏感权限）
"""

from __future__ import annotations

import logging

from ..identity.models import SensitivePermission
from ..tool.types import SensitivityLevel

logger = logging.getLogger("pandaren.behavior.permission_guard")


class PermissionGuard:
    """权限校验器（无状态）。所有方法都是确定性纯函数。"""

    def check_permission(
        self,
        sensitive_permissions: frozenset[SensitivePermission],
        tool_sensitivity: SensitivityLevel,
        tool_permission: SensitivePermission | None,
    ) -> str:
        """校验是否有权限执行该工具。

        Args:
            sensitive_permissions: Identity 持有的高敏感权限集合。
            tool_sensitivity:      工具声明的敏感度等级（SensitivityLevel）。
            tool_permission:       工具所需的 SensitivePermission（HIGH/CRITICAL 工具才声明，
                                   None 表示工具未声明限制或为 LOW/MEDIUM 级别）。

        Returns:
            "allow" → 有权限，放行
            "deny"  → 无权限，拒绝
        """
        # ① LOW / MEDIUM 直接放行
        if tool_sensitivity <= SensitivityLevel.MEDIUM:
            return "allow"

        # ② HIGH / CRITICAL，但工具未声明所需权限 → 放行
        if tool_permission is None:
            return "allow"

        # ③ HIGH / CRITICAL，需要具体权限 → 检查 Identity 是否持有
        if tool_permission in sensitive_permissions:
            return "allow"

        logger.warning(
            "permission_guard: deny，tool_permission=%s 不在 sensitive_permissions=%s 中",
            tool_permission.value,
            [p.value for p in sorted(sensitive_permissions, key=lambda x: x.value)],
        )
        return "deny"
