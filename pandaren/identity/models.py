"""pandaren/identity/models.py — Identity 层核心数据模型

Identity 层是"看得见"的锚点、"管得住"的地基。
  - agent_id → trace、审计日志的关联键
  - sensitive_permissions → 权限守卫的唯一判断依据（封闭枚举，非开放字符串）
  - 不可变性 → 保证运行时无法自我升权

HC1：Identity 所有字段创建后不可修改（__slots__ + __setattr__ 拦截；对常规赋值/删除有效，
     无法对抗恶意代码直接调用 object.__setattr__/object.__delattr__，属 Python 固有局限）
HC2：sensitive_permissions 结构深度不可变（frozenset + frozen enum）
E4 ：必填字段缺失时拒绝创建
S2 ：权限范围封闭（枚举值，不接受自由字符串）
S3 ：权限不继承（每个 Identity 独立声明 sensitive_permissions）
S4 ：信任来源不可伪造（TrustLevel 枚举，不接受 int）
O1 ：agent_id 是 trace 的必要锚点

设计原则（权限体系）：
  - LOW / MEDIUM 敏感度工具无需权限声明，PermissionGuard 直接放行
  - HIGH / CRITICAL 工具必须声明所需的 SensitivePermission
  - PERMISSION_ALL 表示授予所有高敏感权限，适用于受信任的主 Agent
  - 所有高敏感权限类型由 SensitivePermission 枚举统一管理，不允许自由扩展
"""

from __future__ import annotations

import logging
from enum import Enum, IntEnum

logger = logging.getLogger("pandaren.identity.models")

# when_to_use 建议最大长度（字符数），超出时发出警告
_WHEN_TO_USE_MAX_LENGTH = 200


# ════════════════════════════════════════════════
#  TrustLevel 枚举
# ════════════════════════════════════════════════

class TrustLevel(IntEnum):
    """Agent 信任等级（IntEnum，支持大小比较）。

    S4：信任来源不可伪造。TrustLevel 是枚举类型，不是字符串或裸 int，
       避免内容注入式攻击提升 agent 权限。

    EXTERNAL     = 1  低信任，来自外部未知来源的 agent，其指令只当数据看
    SUB_AGENT    = 2  中信任，执行具体任务的子 agent
    ORCHESTRATOR = 3  高信任，主 agent / 编排层，其发出的指令其他 agent 可完整接受
    """
    EXTERNAL = 1
    SUB_AGENT = 2
    ORCHESTRATOR = 3


# ════════════════════════════════════════════════
#  SensitivePermission — 封闭的高敏感权限枚举
# ════════════════════════════════════════════════

class SensitivePermission(str, Enum):
    """高敏感操作权限枚举（封闭集合，不允许自由扩展）。

    只有 HIGH / CRITICAL 敏感度的工具才需要声明所需的 SensitivePermission。
    LOW / MEDIUM 敏感度工具无需任何权限声明，PermissionGuard 直接放行。

    六个枚举值按"做了什么"分类，而非"操作的是什么资源"：

      DATA_WRITE   — 写入持久化数据（文件写、数据库写、云存储写等）
      DATA_DELETE  — 删除数据（不可逆，单独一档）
      CODE_EXEC    — 执行代码 / 脚本（Python、JS、Shell 脚本等）
      SYSTEM_CMD   — 执行系统命令（shell、进程管理、crontab 等）
      NETWORK_CALL — 发起网络请求（HTTP、WebSocket、外部 API 等）
      MEMORY_WRITE — 修改 Agent 自身的长期记忆（影响后续所有行为）

    读操作统一归为 LOW / MEDIUM 敏感度，不需要出现在此枚举中。
    """
    DATA_WRITE   = "data_write"
    DATA_DELETE  = "data_delete"
    CODE_EXEC    = "code_exec"
    SYSTEM_CMD   = "system_cmd"
    NETWORK_CALL = "network_call"
    MEMORY_WRITE = "memory_write"


# 授予全部高敏感权限的常量（适用于主 Agent / ORCHESTRATOR 级别）
PERMISSION_ALL: frozenset[SensitivePermission] = frozenset(SensitivePermission)


# ════════════════════════════════════════════════
#  模块级校验函数（Identity 内部专用）
# ════════════════════════════════════════════════

def _validate_fields(
    *,
    agent_id: str,
    agent_name: str,
    when_to_use: str,
    sensitive_permissions: frozenset[SensitivePermission],
    trust_level: TrustLevel,
) -> None:
    """E4：Identity 必填字段校验（内部函数，不对外暴露）。

    调用方（Identity.__init__）已将 sensitive_permissions 规范化为 frozenset，
    此处做内容校验 + 三字符串字段的类型检查（R4 修复：非 str 统一 ValueError）。

    校验顺序：
      0. 必填字符串字段类型检查（非 str → ValueError，统一类型错误语义，杜绝 strip() AttributeError 泄漏）
      1. 必填字符串字段非空
      2. sensitive_permissions 元素类型（确保都是 SensitivePermission 枚举）
      3. trust_level 枚举类型
      4. when_to_use 长度警告
    """
    # ── 0. 必填字符串字段类型检查（R4）──
    for field_name, value in (
        ("agent_id", agent_id),
        ("agent_name", agent_name),
        ("when_to_use", when_to_use),
    ):
        if not isinstance(value, str):
            reason = "不能为空" if value is None else f"类型错误: {type(value).__name__}，期望 str"
            logger.error("Identity 创建失败：%s %s", field_name, reason)
            raise ValueError(f"Identity.{field_name} {reason}")

    # ── 1. 必填字符串字段 ──

    if not agent_id or not agent_id.strip():
        logger.error("Identity 创建失败：agent_id 为空")
        raise ValueError("Identity.agent_id 不能为空")

    if not agent_name or not agent_name.strip():
        logger.error("Identity 创建失败：agent_name 为空")
        raise ValueError("Identity.agent_name 不能为空")

    if not when_to_use or not when_to_use.strip():
        logger.error("Identity 创建失败：when_to_use 为空")
        raise ValueError("Identity.when_to_use 不能为空")

    # ── 2. sensitive_permissions 元素类型校验 ──
    # 注：调用方（Identity.__init__）已确保传入的是 frozenset，此处只校验元素类型。

    for perm in sensitive_permissions:
        if not isinstance(perm, SensitivePermission):
            logger.error(
                "Identity 创建失败：sensitive_permissions 包含非法元素 %r（类型=%s）",
                perm, type(perm).__name__,
            )
            raise ValueError(
                f"Identity.sensitive_permissions 元素必须是 SensitivePermission 枚举，"
                f"收到: {perm!r}（类型: {type(perm).__name__}）。"
                f"有效值: {[e.value for e in SensitivePermission]}"
            )

    # ── 3. trust_level 枚举校验（S4：信任来源不可伪造）──

    if not isinstance(trust_level, TrustLevel):
        logger.error(
            "Identity 创建失败：trust_level 类型错误，当前类型=%s，值=%s",
            type(trust_level).__name__, trust_level,
        )
        raise ValueError(
            f"Identity.trust_level 类型错误: {type(trust_level).__name__}，"
            f"期望 TrustLevel 枚举（S4：信任来源不可伪造，不接受 int）。"
            f"有效值为 {[e.name for e in TrustLevel]}"
        )

    # ── 4. when_to_use 长度警告 ──

    stripped_when = when_to_use.strip()
    if len(stripped_when) > _WHEN_TO_USE_MAX_LENGTH:
        logger.warning(
            "Identity when_to_use 过长（%d 字符，建议 ≤ %d）。"
            "过长的描述可能导致 orchestrator 路由 prompt 超出 context 窗口。"
            "agent_id='%s'",
            len(stripped_when), _WHEN_TO_USE_MAX_LENGTH, agent_id,
        )


# ════════════════════════════════════════════════
#  Identity（HC1 不可变）
# ════════════════════════════════════════════════

class Identity:
    """Agent 身份声明（创建后完全不可变）。

    5 个字段：
      agent_id             : str                                必填，全局唯一标识符（O1 trace 锚点）
      agent_name           : str                                必填，人类可读名称
      when_to_use          : str                                必填，调度描述（≤ 200 字，供 orchestrator 路由）
      sensitive_permissions: frozenset[SensitivePermission]     必填，高敏感权限集合（封闭枚举，S2）
      trust_level          : TrustLevel                         必填，信任等级（S4 不可伪造）

    设计原则：
      HC1：__slots__ + __setattr__ + __delattr__ 拦截所有运行时修改
      HC2：sensitive_permissions 存储为 frozenset（不可变，深度安全）
      E4 ：必填字段缺失 / 空值 → ValueError，拒绝创建
      S1 ：所有字段通过 @property 只读暴露
      S2 ：权限范围封闭（SensitivePermission 枚举，不接受自由字符串）
      S3 ：权限完全独立，不继承（无"继承自"字段）

    权限语义：
      - 空 frozenset() → 只能使用 LOW/MEDIUM 敏感度工具
      - PERMISSION_ALL  → 可使用所有工具（含全部 HIGH/CRITICAL 工具）
      - {SensitivePermission.DATA_WRITE, ...} → 只可使用声明了对应权限的 HIGH/CRITICAL 工具
    """

    __slots__ = (
        "_agent_id", "_agent_name", "_when_to_use",
        "_sensitive_permissions", "_trust_level",
    )

    def __init__(
        self,
        *,
        agent_id: str,
        agent_name: str,
        when_to_use: str,
        sensitive_permissions: (
            frozenset[SensitivePermission]
            | set[SensitivePermission]
            | list[SensitivePermission]
        ),
        trust_level: TrustLevel,
    ) -> None:
        # 规范化为 frozenset（E4：类型检查先行，统一 ValueError 语义，不做 TypeError 泄漏）
        if not isinstance(sensitive_permissions, (frozenset, set, list)):
            raise ValueError(
                f"Identity.sensitive_permissions 类型错误: "
                f"{type(sensitive_permissions).__name__}，"
                f"期望 frozenset/set/list[SensitivePermission]，"
                f"不接受 None / 字符串 / dict 等。"
            )
        try:
            sensitive_permissions = frozenset(sensitive_permissions)
        except TypeError as exc:
            # 集合元素不可哈希（如 dict）→ 契约要求 ValueError
            raise ValueError(
                "Identity.sensitive_permissions 包含不可哈希元素"
                "（如 dict），无法规范化为 frozenset。"
            ) from exc

        # ── E4 参数校验 ──
        _validate_fields(
            agent_id=agent_id,
            agent_name=agent_name,
            when_to_use=when_to_use,
            sensitive_permissions=sensitive_permissions,
            trust_level=trust_level,
        )

        # 使用 object.__setattr__ 绕过自定义 __setattr__ 进行初始化赋值
        object.__setattr__(self, "_agent_id", agent_id.strip())
        object.__setattr__(self, "_agent_name", agent_name.strip())
        object.__setattr__(self, "_when_to_use", when_to_use.strip())
        object.__setattr__(self, "_sensitive_permissions", sensitive_permissions)
        object.__setattr__(self, "_trust_level", trust_level)

        logger.info(
            "Identity created: agent_id='%s', trust_level=%s, "
            "sensitive_permissions=%s",
            self.agent_id, self.trust_level.name,
            [p.value for p in sorted(sensitive_permissions, key=lambda x: x.value)],
        )

    # ── HC1：拦截运行时赋值 ──
    def __setattr__(self, name: str, value: object) -> None:
        logger.warning(
            "Identity 运行时篡改尝试：agent_id='%s'，字段='%s'，被拦截",
            self._safe_agent_id(), name,
        )
        raise PermissionError(
            f"Identity is immutable: cannot modify '{name}'"
        )

    # ── HC1：拦截运行时删除 ──
    def __delattr__(self, name: str) -> None:
        logger.warning(
            "Identity 运行时删除尝试：agent_id='%s'，字段='%s'，被拦截",
            self._safe_agent_id(), name,
        )
        raise PermissionError(
            f"Identity is immutable: cannot delete '{name}'"
        )

    def _safe_agent_id(self) -> str:
        """安全获取 agent_id（防止在构造异常场景中 __setattr__ 触发时 _agent_id 不存在）。"""
        try:
            return object.__getattribute__(self, "_agent_id")
        except AttributeError:
            return "<uninitialized>"

    # ── @property 只读暴露（S1 不可变性）──

    @property
    def agent_id(self) -> str:
        """全局唯一标识符，trace / 审计日志的关联键（O1、O2）。"""
        return object.__getattribute__(self, "_agent_id")

    @property
    def agent_name(self) -> str:
        """人类可读名称（仅用于显示，不参与任何逻辑判断）。"""
        return object.__getattribute__(self, "_agent_name")

    @property
    def when_to_use(self) -> str:
        """调度描述（供 orchestrator 路由决策，建议 ≤ 200 字）。"""
        return object.__getattribute__(self, "_when_to_use")

    @property
    def sensitive_permissions(self) -> frozenset[SensitivePermission]:
        """高敏感权限集合（frozenset，外部无法修改）。

        空 frozenset → 拒绝所有 HIGH/CRITICAL 工具（Fail-Safe Default）。
        PERMISSION_ALL → 允许所有高敏感工具。
        """
        return object.__getattribute__(self, "_sensitive_permissions")

    @property
    def trust_level(self) -> TrustLevel:
        """Agent 的静态信任等级（S4：不等于运行时的 message_trust）。"""
        return object.__getattribute__(self, "_trust_level")

    def has_permission(self, perm: SensitivePermission) -> bool:
        """判断是否持有指定的高敏感权限（S2 封闭）。

        仅接受 SensitivePermission 枚举实例；非枚举输入（字符串 / None / int /
        不可哈希对象等）一律 fail-closed 返回 False，不隐式匹配、不抛 TypeError。
        """
        if not isinstance(perm, SensitivePermission):
            logger.warning(
                "has_permission 收到非枚举输入：agent_id='%s'，perm=%r（类型=%s）。"
                "S2 要求仅接受 SensitivePermission 枚举，按未持有处理（fail-closed）。",
                self._safe_agent_id(),
                perm,
                type(perm).__name__,
            )
            return False
        return perm in self.sensitive_permissions

    # ── 等值比较 & 哈希（全字段深比较：相等 ⟺ 五字段全等；hash 与 eq 一致）──

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Identity):
            return NotImplemented
        return (
            self.agent_id == other.agent_id
            and self.agent_name == other.agent_name
            and self.when_to_use == other.when_to_use
            and self.sensitive_permissions == other.sensitive_permissions
            and self.trust_level == other.trust_level
        )

    def __hash__(self) -> int:
        return hash((
            self.agent_id,
            self.agent_name,
            self.when_to_use,
            self.sensitive_permissions,
            self.trust_level,
        ))

    def __repr__(self) -> str:
        perms = sorted(self.sensitive_permissions, key=lambda x: x.value)
        return (
            f"Identity("
            f"agent_id='{self.agent_id}', "
            f"agent_name='{self.agent_name}', "
            f"trust_level={self.trust_level.name}, "
            f"sensitive_permissions={[p.value for p in perms]})"
        )
