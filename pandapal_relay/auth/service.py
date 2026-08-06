"""AuthService — 账号系统核心服务。

职责：
- 账号注册（register_account）
- 登录验证（validate_login_credentials）
- JWT 签发（build_jwt_token）
- 密码修改（update_password）
- 登录失败计数与锁定

设计约束：
- BL1: 密码只以 hash 存储，明文不落盘/日志
- BL2: 恒时比对防时序攻击，账号不存在也执行 dummy hash
- BL3: user_id/created_at 不可变
- BL4: 敏感数据不出现在日志（用 username_hash 替代明文）
- BL5: Fail-Safe — 不确定时拒绝优先；Fail-Fast — jwt_secret 为空时启动即失败

职责边界说明（设计文档 Step 3 / Step 5c）：
- JWT *验证*（verify_jwt_token）主要在 Relay Server 端使用（WebSocket 握手鉴权）。
- 本地 sidecar 的 IPC 通道直接信任前端传来的 user_id，不做 JWT 验证。
- 本服务同时提供签发（build_jwt_token）和验证（verify_jwt_token）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import aiosqlite
import bcrypt
import jwt

from pandapal_relay.auth.models import (
    AccountLockedError,
    AuthConfig,
    AuthConfigError,
    InvalidCredentialsError,
    InvalidInputError,
    LoginFailureReason,
    UserAccount,
    UsernameTakenError,
)

logger = logging.getLogger(__name__)

# BL2: 账号不存在时执行恒时 bcrypt 比对，防止攻击者通过响应耗时枚举账号是否存在。
# 攻击者已知的密码必然不会等于 b"dummy_password_placeholder"，因此比对结果必然为 False；
# 此处的目的只是消耗与真实路径等量的 CPU 时间，不需要、也不应该让结果可能为 True。
_DUMMY_HASH = bcrypt.hashpw(b"dummy_password_placeholder", bcrypt.gensalt(rounds=12))


class AuthService:
    """账号系统服务。

    使用方式：
        auth = AuthService(config=AuthConfig(jwt_secret="...", db_path="auth.db"))
        await auth.initialize()
        user_id = await auth.register_account("alice", "password123")
        token, expires_at = auth.build_jwt_token(user_id)
    """

    # 输入校验常量
    USERNAME_MIN = 3
    USERNAME_MAX = 32
    PASSWORD_MIN = 8
    PASSWORD_MAX = 128

    def __init__(self, config: AuthConfig) -> None:
        """
        Args:
            config: Auth 配置（含 jwt_secret 和 db_path）。

        Raises:
            AuthConfigError: jwt_secret 为空（Fail-Fast）。
        """
        if not config.jwt_secret:
            raise AuthConfigError("jwt_secret cannot be empty (Fail-Fast)")

        self._config = config
        self._conn: aiosqlite.Connection | None = None

    # ──────────────────────────────────────────────
    # 生命周期
    # ──────────────────────────────────────────────

    async def initialize(self) -> None:
        """初始化数据库（建表）。必须在任何业务方法调用前 await。"""
        self._conn = await aiosqlite.connect(self._config.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS user_accounts (
                user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                locked_until TEXT,
                failed_login_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        await self._conn.commit()
        logger.info("AuthService initialized (db=%s)", self._config.db_path)

    async def shutdown(self) -> None:
        """关闭数据库连接。"""
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ──────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────

    async def register_account(self, username: str, password: str) -> str:
        """注册新账号。

        Returns:
            user_id（= username，方案 D：username 即 user_id，
            可控渠道通过相同 username 与不可控渠道身份对齐）。

        Raises:
            InvalidInputError: 格式不符。
            UsernameTakenError: 用户名已存在。
        """
        conn = self._require_conn()

        # A1: 输入校验
        self._validate_username(username)
        self._validate_password(password)

        # Hash password (BL1) — 卸载到线程池，不阻塞事件循环
        password_hash = await asyncio.to_thread(self._hash_password, password)

        # 方案 D: username 即 user_id，不再生成 UUID
        user_id = username
        created_at = datetime.now(timezone.utc).isoformat()

        # INSERT
        try:
            await conn.execute(
                "INSERT INTO user_accounts "
                "(user_id, username, password_hash, created_at, failed_login_count) "
                "VALUES (?, ?, ?, ?, 0)",
                (user_id, username, password_hash, created_at),
            )
            await conn.commit()
        except sqlite3.IntegrityError as e:
            raise UsernameTakenError(username) from e

        logger.info("Account registered: user_id=%s (username=user_id)", user_id)
        return user_id

    async def validate_login_credentials(
        self, username: str, password: str, *, ip: str = "unknown"
    ) -> UserAccount:
        """验证登录凭据。

        Args:
            username: 用户名。
            password: 明文密码。
            ip: 请求来源 IP（用于安全日志，BL4）。默认 "unknown"。

        Returns:
            UserAccount（验证成功）。

        Raises:
            InvalidCredentialsError: 凭据无效（BL2: 账号不存在与密码错误统一响应）。
            AccountLockedError: 账号被锁定。
        """
        # 查找账号
        account = await self._find_account_by_username(username)

        if account is None:
            # BL2: 执行 dummy hash 消耗与真实比对等量时间，防止时序枚举。
            # 用真实输入的 password 与 _DUMMY_HASH 比对：
            #   - 攻击者无法构造能匹配 _DUMMY_HASH 的明文（其原文本仅本进程进程内已知）
            #   - 结果必然 False，但 CPU 耗时与真实路径同量级
            await asyncio.to_thread(
                bcrypt.checkpw, password.encode("utf-8"), _DUMMY_HASH
            )
            raise InvalidCredentialsError()

        # 检查锁定状态（可能清除已过期的锁定记录）
        if await self._check_lockout_status(account):
            raise AccountLockedError(account.locked_until)  # type: ignore

        # 验证密码（BL2: bcrypt 恒时比对，卸载到线程池）
        pw_match = await asyncio.to_thread(
            bcrypt.checkpw,
            password.encode("utf-8"),
            account.password_hash.encode("utf-8"),
        )
        if not pw_match:
            await self._record_login_failure(account, ip=ip)
            raise InvalidCredentialsError()

        # 成功 → 重置失败计数
        await self._reset_login_failure_count(account.user_id)
        # 设计文档 Step 6 观测要求：login_success 日志含 user_id + ip
        logger.info("Login success: user_id=%s, ip=%s", account.user_id, ip)
        return account

    def build_jwt_token(self, user_id: str) -> tuple[str, datetime]:
        """签发 JWT token。

        Returns:
            (jwt_token, expires_at) 元组。

        Note:
            JWT *验证* 属于 Relay Server 职责边界（设计文档 Step 5c），
            本服务不提供 verify_jwt_token()。
        """
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=self._config.jwt_expiry_seconds)

        claims = {
            "user_id": user_id,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
        }

        token = jwt.encode(claims, self._config.jwt_secret, algorithm="HS256")
        return token, expires_at

    def verify_jwt_token(self, token: str) -> str | None:
        """本地验证 JWT token，返回 user_id（有效）或 None（无效/过期）。

        Note:
            此方法仅用于 IPC 自动登录场景（Phase 1 → Phase 2 快速恢复），
            Relay Server 仍有独立的 JWT 验证逻辑。

        BL4: 不在日志中输出 token 内容或 PyJWT 异常 message（可能含载荷摘要），
             只记录异常类名。
        """
        try:
            payload = jwt.decode(
                token, self._config.jwt_secret, algorithms=["HS256"]
            )
            return payload.get("user_id")
        except jwt.ExpiredSignatureError:
            logger.info("JWT verify failed: ExpiredSignatureError")
            return None
        except jwt.InvalidTokenError as e:
            # BL4: 只输出异常类型，避免 PyJWT 在 message 中带 token/载荷信息
            logger.warning("JWT verify failed: %s", type(e).__name__)
            return None

    def get_refresh_grace_days(self) -> int:
        """refresh 宽限期天数（供 router 写 401 message 用）。"""
        return self._config.jwt_refresh_grace_days

    def verify_jwt_token_with_grace(self, token: str) -> str | None:
        """宽容验证 JWT（refresh 专用）：有效 token 直接通过；已过期但在宽限期内的也放行。

        判定逻辑：
          1. jwt.decode() 正常验证（含 exp）→ 通过则返回 user_id
          2. 捕获 ExpiredSignatureError → options={"verify_exp": False} 再解码
             （仍验签名，只跳过期）
          3. 检查 exp：now - exp <= jwt_refresh_grace_days 天 → 是则返回 user_id，否则 None
          4. 其他异常（签名无效等）→ None

        BL4: 日志只记异常类名，不记 token 内容。
        """
        try:
            payload = jwt.decode(
                token, self._config.jwt_secret, algorithms=["HS256"]
            )
            return payload.get("user_id")
        except jwt.ExpiredSignatureError:
            pass  # 进入宽限期判定
        except jwt.InvalidTokenError as e:
            # BL4: 只输出异常类型，避免 PyJWT 在 message 中带 token/载荷信息
            logger.warning("JWT grace verify failed: %s", type(e).__name__)
            return None

        try:
            payload = jwt.decode(
                token,
                self._config.jwt_secret,
                algorithms=["HS256"],
                options={"verify_exp": False},
            )
        except jwt.InvalidTokenError as e:
            logger.warning("JWT grace verify failed: %s", type(e).__name__)
            return None

        exp = payload.get("exp")
        user_id = payload.get("user_id")
        if not isinstance(exp, (int, float)) or user_id is None:
            logger.warning("JWT grace verify failed: missing exp/user_id claims")
            return None

        expired_seconds = datetime.now(timezone.utc).timestamp() - exp
        if expired_seconds <= self._config.jwt_refresh_grace_days * 86400:
            return user_id

        logger.info(
            "JWT grace verify failed: expired %.1f days ago (grace=%d days)",
            expired_seconds / 86400,
            self._config.jwt_refresh_grace_days,
        )
        return None

    async def update_password(
        self, user_id: str, old_password: str, new_password: str
    ) -> bool:
        """修改密码。

        Raises:
            InvalidCredentialsError: user_id 无效或旧密码错误。
            InvalidInputError: 新密码格式不符。
        """
        conn = self._require_conn()

        # 校验新密码格式
        self._validate_password(new_password)

        # 查找账号
        account = await self._find_account_by_user_id(user_id)
        if account is None:
            raise InvalidCredentialsError()

        # 验证旧密码（卸载到线程池）
        old_match = await asyncio.to_thread(
            bcrypt.checkpw,
            old_password.encode("utf-8"),
            account.password_hash.encode("utf-8"),
        )
        if not old_match:
            raise InvalidCredentialsError()

        # Hash 新密码并更新（BL3: 只更新 password_hash，卸载到线程池）
        new_hash = await asyncio.to_thread(self._hash_password, new_password)
        await conn.execute(
            "UPDATE user_accounts SET password_hash = ? WHERE user_id = ?",
            (new_hash, user_id),
        )
        await conn.commit()

        logger.info("Password updated: user_id=%s", user_id)
        return True

    # ──────────────────────────────────────────────
    # Private Methods
    # ──────────────────────────────────────────────

    def _require_conn(self) -> aiosqlite.Connection:
        """Guard: 确保 initialize() 已调用，否则抛出 AssertionError 给出明确指引。"""
        assert self._conn is not None, (
            "AuthService not initialized — "
            "call `await service.initialize()` before any operation"
        )
        return self._conn

    @staticmethod
    def _hash_password(password: str) -> str:
        """BL1: 密码哈希（bcrypt, work factor 12）。仅供 asyncio.to_thread 调用，勿在协程中直接调用。"""
        hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12))
        return hashed.decode("utf-8")

    async def _find_account_by_username(self, username: str) -> UserAccount | None:
        """按 username 查找账号。"""
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT user_id, username, password_hash, created_at, "
            "locked_until, failed_login_count "
            "FROM user_accounts WHERE username = ?",
            (username,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_account(row)

    async def _find_account_by_user_id(self, user_id: str) -> UserAccount | None:
        """按 user_id 查找账号。"""
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT user_id, username, password_hash, created_at, "
            "locked_until, failed_login_count "
            "FROM user_accounts WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_account(row)

    async def _check_lockout_status(self, account: UserAccount) -> bool:
        """检查账号锁定状态，过期锁定自动清除。

        Returns:
            True  — 账号仍在锁定期，拒绝登录。
            False — 账号未锁定（含锁定已过期并已自动清除的情况）。

        Side-effect:
            若锁定已过期，会执行 UPDATE 清除 locked_until 和 failed_login_count。
        """
        if account.locked_until is None:
            return False

        now = datetime.now(timezone.utc)
        if account.locked_until > now:
            return True

        # 锁定已过期 → 清除
        conn = self._require_conn()
        await conn.execute(
            "UPDATE user_accounts SET locked_until = NULL, failed_login_count = 0 "
            "WHERE user_id = ?",
            (account.user_id,),
        )
        await conn.commit()
        return False

    async def _record_login_failure(
        self, account: UserAccount, *, ip: str = "unknown"
    ) -> None:
        """记录登录失败。

        每次失败都输出 WARN 日志（设计方4要求）；达到阈值时触发账号锁定。
        BL4: 日志使用 username_hash（SHA-256 前 16 hex），不记录明文用户名。
        """
        conn = self._require_conn()
        new_count = account.failed_login_count + 1
        locked_until: str | None = None

        # BL4: SHA-256 截断替代明文，防止日志泄露用户名
        username_hash = hashlib.sha256(account.username.encode()).hexdigest()[:16]

        # 每次失败均记录（设计方4：可观测性要求）
        logger.warning(
            "Login failure: username_hash=%s, ip=%s, failed_count=%d, reason=%s",
            username_hash,
            ip,
            new_count,
            LoginFailureReason.INVALID_CREDENTIALS.value,
        )

        if new_count >= self._config.max_failed_logins:
            locked_until = (
                datetime.now(timezone.utc)
                + timedelta(seconds=self._config.lockout_duration_seconds)
            ).isoformat()
            logger.warning(
                "Account locked: user_id=%s, until=%s",
                account.user_id,
                locked_until,
            )

        await conn.execute(
            "UPDATE user_accounts SET failed_login_count = ?, locked_until = ? "
            "WHERE user_id = ?",
            (new_count, locked_until, account.user_id),
        )
        await conn.commit()

    async def _reset_login_failure_count(self, user_id: str) -> None:
        """重置登录失败计数与锁定状态（登录成功后调用）。"""
        conn = self._require_conn()
        await conn.execute(
            "UPDATE user_accounts SET failed_login_count = 0, locked_until = NULL "
            "WHERE user_id = ?",
            (user_id,),
        )
        await conn.commit()

    @staticmethod
    def _validate_username(username: str) -> None:
        """A1: username 格式校验。"""
        if not username or len(username) < 3 or len(username) > 32:
            raise InvalidInputError("用户名需 3~32 字符")

    @staticmethod
    def _validate_password(password: str) -> None:
        """A1: password 格式校验。"""
        if not password or len(password) < 8 or len(password) > 128:
            raise InvalidInputError("密码需 8~128 字符")

    @staticmethod
    def _row_to_account(row: aiosqlite.Row) -> UserAccount:
        """数据库行 → UserAccount（具名字段访问，不依赖列顺序）。"""
        locked_until = None
        if row["locked_until"]:
            locked_until = datetime.fromisoformat(row["locked_until"])

        return UserAccount(
            user_id=row["user_id"],
            username=row["username"],
            password_hash=row["password_hash"],
            created_at=datetime.fromisoformat(row["created_at"]),
            locked_until=locked_until,
            failed_login_count=row["failed_login_count"],
        )
