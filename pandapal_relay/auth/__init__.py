"""pandapal_relay.auth — 账号系统。"""

from pandapal_relay.auth.models import (
    AccountLockedError,
    AuthConfig,
    AuthConfigError,
    AuthError,
    InvalidCredentialsError,
    InvalidInputError,
    LoginFailureReason,
    UserAccount,
    UsernameTakenError,
)
from pandapal_relay.auth.router import auth_router, init_auth_router
from pandapal_relay.auth.service import AuthService

__all__ = [
    # 服务
    "AuthService",
    # 配置
    "AuthConfig",
    # 数据模型
    "UserAccount",
    "LoginFailureReason",
    # 异常基类
    "AuthError",
    # 具体异常
    "UsernameTakenError",
    "InvalidCredentialsError",
    "AccountLockedError",
    "InvalidInputError",
    "AuthConfigError",
    # HTTP 路由（FastAPI）
    "auth_router",
    "init_auth_router",
]
