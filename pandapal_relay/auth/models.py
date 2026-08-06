"""Auth 模块数据模型与异常。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


@dataclass(frozen=True)
class UserAccount:
    """用户账号（持久化实体）。

    BL3: user_id 和 created_at 创建后不可变。
    """

    user_id: str
    username: str
    password_hash: str
    created_at: datetime
    locked_until: datetime | None = None
    failed_login_count: int = 0


@dataclass(frozen=True)
class AuthConfig:
    """Auth 启动配置。

    db_path: SQLite 数据库路径（与 AuthService 一起封装，统一配置管理）。
    """

    jwt_secret: str
    db_path: str = "auth.db"
    jwt_expiry_seconds: int = 86400  # 24h
    max_failed_logins: int = 5
    lockout_duration_seconds: int = 900  # 15 min
    # refresh 宽限期：「过期不超过 N 天」（基于 exp），不是「签发不超过 N 天」（基于 iat）。
    # 当前 token 24h 有效，"iat 7 天"实际语义是"过期 6 天"，基于 exp 语义直接、不易算错。
    jwt_refresh_grace_days: int = 7


class LoginFailureReason(str, Enum):
    """登录失败原因。"""

    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    DB_ERROR = "DB_ERROR"


# ──────────────────────────────────────────────
# 异常类
# ──────────────────────────────────────────────


class AuthError(Exception):
    """Auth 基础异常。"""

    pass


class UsernameTakenError(AuthError):
    """用户名已存在（409）。"""

    def __init__(self, username: str) -> None:
        self.username = username
        super().__init__(f"Username already taken: {username}")


class InvalidCredentialsError(AuthError):
    """凭据无效（401）— 账号不存在或密码错误统一返回（BL2 防枚举）。"""

    pass


class AccountLockedError(AuthError):
    """账号被锁定（423）。"""

    def __init__(self, unlock_at: datetime) -> None:
        self.unlock_at = unlock_at
        super().__init__(f"Account locked until {unlock_at.isoformat()}")


class InvalidInputError(AuthError):
    """输入格式错误（422）。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class AuthConfigError(AuthError):
    """Auth 配置缺失（启动时 Fail-Fast）。"""

    pass
