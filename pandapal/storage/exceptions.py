"""Storage 层专用异常。

遵循 BL5（语义异常）原则：每个异常类名直接表达失败场景。
"""

from __future__ import annotations


class StorageInitError(Exception):
    """存储初始化失败（I1 Fail Fast）。

    触发场景：
    - DB 文件路径不可写
    - DB 文件损坏（PRAGMA integrity_check 失败）
    - 无法创建目录
    """

    def __init__(self, reason: str, path: str | None = None) -> None:
        self.reason = reason
        self.path = path
        msg = f"Storage initialization failed: {reason}"
        if path:
            msg += f" (path={path})"
        super().__init__(msg)


class SchemaMigrationError(Exception):
    """Schema 迁移失败。

    触发场景：
    - 迁移 SQL 执行出错
    - 迁移脚本文件缺失
    - 版本号不连续
    """

    def __init__(
        self, version: int, reason: str, *, rollback_success: bool = False
    ) -> None:
        self.version = version
        self.reason = reason
        self.rollback_success = rollback_success
        msg = f"Schema migration v{version:03d} failed: {reason}"
        if rollback_success:
            msg += " (rollback succeeded)"
        super().__init__(msg)


class StorageTimeoutError(Exception):
    """数据库操作超时（I5 Timeout Coverage）。

    触发场景：
    - asyncio.wait_for 超过 query_timeout_s
    - 允许调用方决定重试或降级策略
    """

    def __init__(self, operation: str, timeout_s: float) -> None:
        self.operation = operation
        self.timeout_s = timeout_s
        super().__init__(
            f"Storage operation '{operation}' timed out after {timeout_s:.1f}s"
        )


class StorageDuplicateError(Exception):
    """数据唯一性冲突。

    触发场景：
    - ApprovalRequest INSERT 时 approval_id 已存在（不允许覆盖）
    """

    def __init__(self, entity: str, key: str) -> None:
        self.entity = entity
        self.key = key
        super().__init__(f"Duplicate {entity}: {key}")
