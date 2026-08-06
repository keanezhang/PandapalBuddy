"""pandaren/tool/exceptions.py — Tool 模块异常"""


class ToolRegistrationError(Exception):
    """工具注册失败（必填字段缺失、矛盾检测命中 ERROR 级别、name 重复等）。

    在开发阶段（注册时）同步抛出，让问题尽早暴露。
    """
    pass


class ToolValidationWarning(UserWarning):
    """工具注册时的矛盾检测 WARNING（不阻断注册，但记录日志）。

    例如：sensitivity=CRITICAL 且 is_reversible=True
    """
    pass
