"""Session 层专用异常。

BL5: 业务异常与技术异常分离。
SessionNotFoundError / SessionExpiredError 是业务异常，调用方需显式处理。
StorageTimeoutError 等技术异常不在此模块捕获，直接透传给调用方。

v003 (SessionListManager) 新增：
- SessionQuotaExceeded: 会话数达上限且淘汰失败
- GroupNameConflict / GroupQuotaExceeded / GroupNameInvalid / GroupNotFoundError: 分组相关
- InvalidPageSize: 分页参数非法
"""

from __future__ import annotations

from datetime import datetime


class SessionNotFoundError(Exception):
    """会话不存在。

    触发场景：
    - validate_session() 找不到 session_id
    - refresh_session_activity() 找不到 session_id
    - SessionListManager: 目标 session 不存在 / is_deleted=1 / 属于其他用户
      （越权保护：一律映射为 not_found，不泄漏存在性）
    """

    error_code = "session_not_found"

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"Session not found: {session_id}")


class SessionExpiredError(Exception):
    """会话已过期。

    触发场景：
    - validate_session() 发现 session 已超时
    """

    error_code = "session_expired"

    def __init__(self, session_id: str, expired_at: datetime) -> None:
        self.session_id = session_id
        self.expired_at = expired_at
        super().__init__(
            f"Session expired: {session_id} (last active: {expired_at.isoformat()})"
        )


# ─────────────────────────────────────────────────────────────
# SessionListManager 业务异常（v003 引入）
# ─────────────────────────────────────────────────────────────


class SessionQuotaExceeded(Exception):
    """可见会话数量达到上限且淘汰失败。

    触发场景：
    - create_empty_session 发现 count >= max，evict_oldest 也失败
    """

    error_code = "session_quota_exceeded_evict_failed"

    def __init__(self, user_id: str, current_count: int, max_allowed: int) -> None:
        self.user_id = user_id
        self.current_count = current_count
        self.max_allowed = max_allowed
        super().__init__(
            f"Session quota exceeded for user {user_id}: "
            f"{current_count}/{max_allowed} and eviction failed"
        )


class GroupNameConflict(Exception):
    """分组名重复。"""

    error_code = "group_name_duplicate"

    def __init__(self, user_id: str, name: str) -> None:
        self.user_id = user_id
        self.name = name
        super().__init__(f"Group name already exists: user={user_id} name={name!r}")


class GroupQuotaExceeded(Exception):
    """分组数量达到上限。"""

    error_code = "group_quota_exceeded"

    def __init__(self, user_id: str, current_count: int, max_allowed: int) -> None:
        self.user_id = user_id
        self.current_count = current_count
        self.max_allowed = max_allowed
        super().__init__(
            f"Group quota exceeded for user {user_id}: "
            f"{current_count}/{max_allowed}"
        )


class GroupNameInvalid(Exception):
    """分组名不合法（为空 / 超长 / 全空白）。"""

    error_code = "group_name_invalid"

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason
        super().__init__(f"Invalid group name {name!r}: {reason}")


class GroupNotFoundError(Exception):
    """分组不存在。"""

    error_code = "group_not_found"

    def __init__(self, group_id: str) -> None:
        self.group_id = group_id
        super().__init__(f"Group not found: {group_id}")


class InvalidPageSize(Exception):
    """分页大小非法（超过 max_page_size 或 <= 0）。"""

    error_code = "page_size_invalid"

    def __init__(self, limit: int, max_allowed: int) -> None:
        self.limit = limit
        self.max_allowed = max_allowed
        super().__init__(
            f"Invalid page size {limit}: must be in [1, {max_allowed}]"
        )
