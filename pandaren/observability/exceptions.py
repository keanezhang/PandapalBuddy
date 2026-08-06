"""pandaren/observability/exceptions.py — Observability 模块异常"""


class ObservabilityError(Exception):
    """Observability 基础异常（非 AuditLog 子系统）。"""
    pass


class AuditWriteError(Exception):
    """AuditLog 写入失败异常（HC4：必须传播到 Loop）。

    与 ObservabilityError 不共享基类，防止上层意外吞掉。
    """
    pass


class SanitizeError(ObservabilityError):
    """脱敏执行异常。"""
    pass
