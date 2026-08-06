"""AuthService 测试。"""

from __future__ import annotations

import pytest
import pytest_asyncio

from pandapal.auth.models import (
    AccountLockedError,
    AuthConfig,
    AuthConfigError,
    InvalidCredentialsError,
    InvalidInputError,
    UsernameTakenError,
)
from pandapal.auth.service import AuthService


@pytest_asyncio.fixture
async def auth_service(tmp_path):
    """提供初始化完成的 AuthService。"""
    config = AuthConfig(
        jwt_secret="test-secret-key-for-testing",
        db_path=str(tmp_path / "auth.db"),
        jwt_expiry_seconds=3600,
        max_failed_logins=3,
        lockout_duration_seconds=60,
    )
    service = AuthService(config=config)
    await service.initialize()
    yield service
    await service.shutdown()


# ──────────────────────────────────────────────
# Config Tests
# ──────────────────────────────────────────────


def test_empty_jwt_secret_raises():
    """jwt_secret 为空时拒绝启动（Fail-Fast）。"""
    with pytest.raises(AuthConfigError):
        AuthService(config=AuthConfig(jwt_secret=""))


# ──────────────────────────────────────────────
# Registration Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_success(auth_service):
    """正常注册返回 user_id。"""
    user_id = await auth_service.register_account("alice", "password123")
    assert user_id is not None
    assert len(user_id) == 36  # UUID v4 format


@pytest.mark.asyncio
async def test_register_duplicate_username_raises(auth_service):
    """重复用户名抛出 UsernameTakenError。"""
    await auth_service.register_account("alice", "password123")
    with pytest.raises(UsernameTakenError):
        await auth_service.register_account("alice", "different_pass")


@pytest.mark.asyncio
async def test_register_short_username_raises(auth_service):
    """用户名太短抛出 InvalidInputError。"""
    with pytest.raises(InvalidInputError):
        await auth_service.register_account("ab", "password123")


@pytest.mark.asyncio
async def test_register_short_password_raises(auth_service):
    """密码太短抛出 InvalidInputError。"""
    with pytest.raises(InvalidInputError):
        await auth_service.register_account("alice", "short")


# ──────────────────────────────────────────────
# Login Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_login_success(auth_service):
    """正确凭据登录成功。"""
    await auth_service.register_account("alice", "password123")
    account = await auth_service.validate_login_credentials("alice", "password123")
    assert account.username == "alice"
    assert account.user_id is not None


@pytest.mark.asyncio
async def test_login_wrong_password_raises(auth_service):
    """错误密码抛出 InvalidCredentialsError。"""
    await auth_service.register_account("alice", "password123")
    with pytest.raises(InvalidCredentialsError):
        await auth_service.validate_login_credentials("alice", "wrongpassword")


@pytest.mark.asyncio
async def test_login_nonexistent_user_raises(auth_service):
    """不存在的用户抛出 InvalidCredentialsError（BL2: 与密码错误统一）。"""
    with pytest.raises(InvalidCredentialsError):
        await auth_service.validate_login_credentials("noone", "password123")


@pytest.mark.asyncio
async def test_login_lockout_after_max_failures(auth_service):
    """连续失败达到阈值后锁定账号。"""
    await auth_service.register_account("alice", "password123")

    # 3 次失败（max_failed_logins=3）
    for _ in range(3):
        with pytest.raises(InvalidCredentialsError):
            await auth_service.validate_login_credentials("alice", "wrong")

    # 第 4 次应抛出 AccountLockedError
    with pytest.raises(AccountLockedError):
        await auth_service.validate_login_credentials("alice", "password123")


@pytest.mark.asyncio
async def test_login_success_resets_failure_count(auth_service):
    """登录成功后重置失败计数。"""
    await auth_service.register_account("alice", "password123")

    # 2 次失败（未达阈值）
    for _ in range(2):
        with pytest.raises(InvalidCredentialsError):
            await auth_service.validate_login_credentials("alice", "wrong")

    # 正确登录 → 重置
    account = await auth_service.validate_login_credentials("alice", "password123")
    assert account is not None

    # 再 2 次失败后不应锁定（计数已重置）
    for _ in range(2):
        with pytest.raises(InvalidCredentialsError):
            await auth_service.validate_login_credentials("alice", "wrong")

    # 仍能登录（因为计数只到 2，未达 3）
    account = await auth_service.validate_login_credentials("alice", "password123")
    assert account is not None


# ──────────────────────────────────────────────
# JWT Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_jwt(auth_service):
    """签发 JWT 返回非空 token 和未来的过期时间（本服务只负责签发，不负责验证）。"""
    user_id = await auth_service.register_account("alice", "password123")
    token, expires_at = auth_service.build_jwt_token(user_id)

    assert token is not None and len(token) > 0
    assert expires_at is not None

    # token 格式应为三段 base64（header.payload.signature）
    assert token.count(".") == 2


# ──────────────────────────────────────────────
# Password Update Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_password_success(auth_service):
    """修改密码成功。"""
    user_id = await auth_service.register_account("alice", "oldpass123")
    result = await auth_service.update_password(user_id, "oldpass123", "newpass456")
    assert result is True

    # 旧密码不可用
    with pytest.raises(InvalidCredentialsError):
        await auth_service.validate_login_credentials("alice", "oldpass123")

    # 新密码可用
    account = await auth_service.validate_login_credentials("alice", "newpass456")
    assert account.user_id == user_id


@pytest.mark.asyncio
async def test_update_password_wrong_old_raises(auth_service):
    """旧密码错误抛出 InvalidCredentialsError。"""
    user_id = await auth_service.register_account("alice", "password123")
    with pytest.raises(InvalidCredentialsError):
        await auth_service.update_password(user_id, "wrongold", "newpass456")


@pytest.mark.asyncio
async def test_update_password_invalid_new_raises(auth_service):
    """新密码格式错误抛出 InvalidInputError。"""
    user_id = await auth_service.register_account("alice", "password123")
    with pytest.raises(InvalidInputError):
        await auth_service.update_password(user_id, "password123", "short")
