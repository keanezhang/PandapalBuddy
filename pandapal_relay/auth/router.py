"""pandapal_relay/auth/router.py — Auth 模块 HTTP 路由层。

实现设计文档 Step 6 定义的 REST 接口：
- POST /auth/register  → 201 { token, user_id, username, expires_at } / 409 / 422 / 503
- POST /auth/login     → 200 { token, user_id, username, expires_at } / 401 / 423 / 503
- PUT  /auth/password  → 200 { success: true }                        / 401 / 422 / 503
                         需携带 Authorization: Bearer <jwt_token>，user_id 来自 JWT claims（A2/Step 7 失败 8）
- POST /auth/refresh   → 200 { token, user_id, username, expires_at } / 401 / 429
                         宽限期内（过期 ≤ jwt_refresh_grace_days 天）换发新 JWT，带审计日志 + rate limit

统一错误响应格式：{ "error": "<CODE>", "message": "<说明>" }
错误码：
  - USERNAME_TAKEN / INVALID_CREDENTIALS / ACCOUNT_LOCKED
  - INVALID_INPUT / INVALID_TOKEN / DB_UNAVAILABLE

使用方式（在 FastAPI app 中注册）：
    from pandapal_relay.auth.router import auth_router, init_auth_router
    init_auth_router(service)
    app.include_router(auth_router)
"""

from __future__ import annotations

import logging
import sqlite3
import time

import aiosqlite
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel

from pandapal_relay.auth.models import (
    AccountLockedError,
    InvalidCredentialsError,
    InvalidInputError,
    UsernameTakenError,
)
from pandapal_relay.auth.service import AuthService

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Pydantic 请求 / 响应模型
# ──────────────────────────────────────────────


class RegisterRequest(BaseModel):
    username: str
    password: str


class RegisterResponse(BaseModel):
    token: str
    user_id: str
    username: str
    expires_at: str  # ISO 8601 UTC


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    user_id: str
    username: str
    expires_at: str  # ISO 8601 UTC


class UpdatePasswordRequest(BaseModel):
    """PUT /auth/password 请求体。

    user_id 不再来自请求体——由 Authorization: Bearer JWT 提取（A2 + 失败 8）。
    """

    old_password: str
    new_password: str


class UpdatePasswordResponse(BaseModel):
    success: bool


class RefreshResponse(BaseModel):
    """POST /auth/refresh 响应（结构同 LoginResponse）。

    方案 D：username 即 user_id，无需查 DB 即可填充。
    """

    token: str
    user_id: str
    username: str
    expires_at: str  # ISO 8601 UTC


class ErrorDetail(BaseModel):
    """统一错误响应（设计文档 A3 / Step 6）。

    字段语义：
      - error:   错误码（机器可读，UPPER_SNAKE_CASE）
      - message: 用户可见说明（文案，可本地化）
    """

    error: str
    message: str


def _err(code: str, message: str) -> dict:
    """构造标准错误 detail。"""
    return {"error": code, "message": message}


# ──────────────────────────────────────────────
# 依赖注入：AuthService 单例
# ──────────────────────────────────────────────

_auth_service: AuthService | None = None


def init_auth_router(service: AuthService) -> None:
    """在应用启动时调用，注入已初始化的 AuthService 实例。"""
    global _auth_service
    _auth_service = service
    logger.info("[AuthRouter] AuthService injected")


def _get_service() -> AuthService:
    """FastAPI 依赖项：返回已注入的 AuthService，未注入时快速失败。"""
    assert _auth_service is not None, (
        "AuthService not injected — call init_auth_router(service) at startup"
    )
    return _auth_service


def _client_ip(request: Request) -> str:
    """从 Request 中提取客户端 IP（兼容 X-Forwarded-For 代理头）。"""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _require_user_id(
    authorization: str | None = Header(default=None, alias="Authorization"),
    service: AuthService = Depends(_get_service),
) -> str:
    """认证依赖：从 Authorization: Bearer <jwt> 解析 user_id。

    设计文档 A2：凭据通过 Authorization Header 携带。
    设计文档 Step 7 失败 8：JWT 无效/过期 → 401 INVALID_TOKEN。
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_err("INVALID_TOKEN", "缺少或非法的 Authorization Header"),
        )

    token = authorization[len("Bearer ") :].strip()
    user_id = service.verify_jwt_token(token)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_err("INVALID_TOKEN", "JWT 无效或已过期"),
        )
    return user_id


def _db_unavailable() -> HTTPException:
    """构造 503 DB_UNAVAILABLE 响应（设计文档失败 5）。"""
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=_err("DB_UNAVAILABLE", "存储后端暂不可用，请稍后重试"),
    )


# ──────────────────────────────────────────────
# Router
# ──────────────────────────────────────────────

auth_router = APIRouter(prefix="/auth", tags=["Auth"])


@auth_router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=RegisterResponse,
    responses={
        409: {"model": ErrorDetail, "description": "用户名已存在"},
        422: {"model": ErrorDetail, "description": "输入格式错误"},
        503: {"model": ErrorDetail, "description": "存储后端不可用"},
    },
    summary="注册新账号",
)
async def register(
    body: RegisterRequest,
    service: AuthService = Depends(_get_service),
) -> RegisterResponse:
    """注册新账号（注册即登录），返回 token + user_id + username。"""
    try:
        user_id = await service.register_account(body.username, body.password)
        # 注册即登录：立即签发 JWT token
        token, expires_at = service.build_jwt_token(user_id)
        return RegisterResponse(
            token=token,
            user_id=user_id,
            username=body.username,
            expires_at=expires_at.isoformat(),
        )
    except UsernameTakenError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_err("USERNAME_TAKEN", str(exc)),
        ) from exc
    except InvalidInputError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_err("INVALID_INPUT", exc.reason),
        ) from exc
    except (sqlite3.DatabaseError, aiosqlite.Error) as exc:
        # 失败 5（BL5 Fail-Safe）：DB 不可用一律 503，不做"降级放行"
        logger.error("[Auth] db_error during register: %s", type(exc).__name__)
        raise _db_unavailable() from exc


@auth_router.post(
    "/login",
    status_code=status.HTTP_200_OK,
    response_model=LoginResponse,
    responses={
        401: {"model": ErrorDetail, "description": "凭据无效"},
        423: {"model": ErrorDetail, "description": "账号已锁定"},
        503: {"model": ErrorDetail, "description": "存储后端不可用"},
    },
    summary="登录并签发 JWT",
)
async def login(
    body: LoginRequest,
    request: Request,
    service: AuthService = Depends(_get_service),
) -> LoginResponse:
    """验证凭据，成功后签发 JWT token 和过期时间。"""
    ip = _client_ip(request)
    try:
        account = await service.validate_login_credentials(
            body.username, body.password, ip=ip
        )
        token, expires_at = service.build_jwt_token(account.user_id)
        return LoginResponse(
            token=token,
            user_id=account.user_id,
            username=account.username,
            expires_at=expires_at.isoformat(),
        )
    except AccountLockedError as exc:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=_err("ACCOUNT_LOCKED", str(exc)),
        ) from exc
    except InvalidCredentialsError as exc:
        # BL2: 账号不存在与密码错误统一响应，不区分两种情况（防枚举）
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_err("INVALID_CREDENTIALS", "用户名或密码错误"),
        ) from exc
    except (sqlite3.DatabaseError, aiosqlite.Error) as exc:
        logger.error("[Auth] db_error during login: %s", type(exc).__name__)
        raise _db_unavailable() from exc


@auth_router.put(
    "/password",
    status_code=status.HTTP_200_OK,
    response_model=UpdatePasswordResponse,
    responses={
        401: {"model": ErrorDetail, "description": "JWT 无效/过期 或 旧密码错误"},
        422: {"model": ErrorDetail, "description": "新密码格式错误"},
        503: {"model": ErrorDetail, "description": "存储后端不可用"},
    },
    summary="修改密码",
)
async def update_password(
    body: UpdatePasswordRequest,
    user_id: str = Depends(_require_user_id),
    service: AuthService = Depends(_get_service),
) -> UpdatePasswordResponse:
    """修改密码（A2 + 失败 8）：

    - 必须携带 Authorization: Bearer <jwt_token>
    - user_id 来自 JWT claims（不接受请求体携带，防止越权）
    - 验证旧密码后更新（BL3：只更新 password_hash，不变更 user_id/created_at）
    """
    try:
        await service.update_password(user_id, body.old_password, body.new_password)
        return UpdatePasswordResponse(success=True)
    except InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_err("INVALID_CREDENTIALS", "旧密码验证失败"),
        ) from exc
    except InvalidInputError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_err("INVALID_INPUT", exc.reason),
        ) from exc
    except (sqlite3.DatabaseError, aiosqlite.Error) as exc:
        logger.error("[Auth] db_error during update_password: %s", type(exc).__name__)
        raise _db_unavailable() from exc


# ──────────────────────────────────────────────
# POST /auth/refresh — 宽限期内换发新 JWT
# ──────────────────────────────────────────────

# 单用户成功 refresh 的最小间隔（秒）。进程内 dict 记录，单实例单用户桌面场景足够；
# 防止被盗 token 高频刷取 / 脚本失控刷日志。
_REFRESH_MIN_INTERVAL_SECONDS = 60.0
_last_successful_refresh: dict[str, float] = {}


@auth_router.post(
    "/refresh",
    status_code=status.HTTP_200_OK,
    response_model=RefreshResponse,
    responses={
        401: {"model": ErrorDetail, "description": "token 无效或已超出宽限期"},
        429: {"model": ErrorDetail, "description": "refresh 过于频繁"},
    },
    summary="宽限期内换发新 JWT（过期自动续期）",
)
async def refresh(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    service: AuthService = Depends(_get_service),
) -> RefreshResponse:
    """接受已过期但仍在宽限期（jwt_refresh_grace_days，基于 exp）内的 JWT，换发新 JWT。

    - 从 Authorization: Bearer <jwt> 提取旧 token（与 _require_user_id 同款解析）
    - verify_jwt_token_with_grace() 判定（签名有效 + 过期 ≤ 宽限期）
    - 审计日志：每次 refresh 记录 user_id + ip + 结果，成功 INFO / 失败 WARNING
    - Rate limit：单 user_id 成功 refresh 最小间隔 60s，间隔内重复请求 → 429
    """
    ip = _client_ip(request)

    if not authorization or not authorization.startswith("Bearer "):
        logger.warning("[Auth] refresh failed: ip=%s, result=missing_header", ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_err("INVALID_TOKEN", "缺少或非法的 Authorization Header"),
        )

    old_token = authorization[len("Bearer ") :].strip()
    user_id = service.verify_jwt_token_with_grace(old_token)
    if user_id is None:
        # 失败原因细分（签名无效 / 超出宽限期）已在 service 层记日志，此处记结果即可
        logger.warning(
            "[Auth] refresh failed: ip=%s, result=invalid_or_beyond_grace", ip
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_err(
                "INVALID_TOKEN",
                f"登录已过期，请重新登录（宽限期 {service.get_refresh_grace_days()} 天）",
            ),
        )

    # Rate limit：单 user_id 成功 refresh 最小间隔
    now = time.monotonic()
    last = _last_successful_refresh.get(user_id)
    if last is not None and (now - last) < _REFRESH_MIN_INTERVAL_SECONDS:
        logger.warning(
            "[Auth] refresh rate-limited: user_id=%s, ip=%s, elapsed=%.1fs",
            user_id,
            ip,
            now - last,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_err("RATE_LIMITED", "刷新过于频繁，请稍后重试"),
        )

    token, expires_at = service.build_jwt_token(user_id)
    _last_successful_refresh[user_id] = now
    logger.info("[Auth] refresh success: user_id=%s, ip=%s", user_id, ip)
    return RefreshResponse(
        token=token,
        user_id=user_id,
        username=user_id,  # 方案 D：username 即 user_id
        expires_at=expires_at.isoformat(),
    )
