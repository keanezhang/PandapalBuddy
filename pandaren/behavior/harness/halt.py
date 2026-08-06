"""pandaren/behavior/harness/halt.py — S6 失败硬停止

halt_on_failure=True 的工具执行失败时，
ToolResult.halt=True → agent_loop 终止整个 run。

注意：halt 不等于 crash。
触发 on_run_halt hook，记录原因，返回结构化错误给调用方。
"""


class HaltChecker:
    """S6 原则检查器。"""

    @staticmethod
    def should_halt(success: bool, halt_on_failure: bool) -> bool:
        """判断是否应该硬停止。"""
        return not success and halt_on_failure
