"""pandaren/behavior/harness/tool_feedback.py — 工具执行后的反馈贡献协议

控制面的第四条链。既有三条（ToolGate / ExecutionGuard / StepGuard）**全在「执行前」**，
工具执行**后**是一片空白：结果一旦产生，没有任何一等机制能对它贡献东西。本模块填这个空白。

形状对齐 pandaren/behavior/step_guard.py 的 StepGuard：
  应用层实现 · 专用 builder 参数注入 · frozen 值对象 · O3 约定写进 docstring

值对象 ToolFeedback / FeedbackSeverity 落在 tool/definition/tool_result.py 而非本文件——
因为 ToolResult.feedback 要引用它，而 capability 层不能反向 import behavior 层
（依赖方向：engine → behavior → capability → identity）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ...tool.definition.context import ToolContext
from ...tool.definition.tool_result import ToolFeedback, ToolResult

__all__ = ["ToolFeedbackProvider"]


@runtime_checkable
class ToolFeedbackProvider(Protocol):
    """工具执行后贡献反馈（应用层实现，`.behavior(tool_feedback_providers=[...])` 注入）。

    典型实现：代码质量门控（写完 .py 就跑 lint，把诊断回灌给 LLM）、密钥泄漏扫描、
    敏感词检查。框架不含任何领域判断——「检查什么」「什么算问题」全在实现方。

    约定：
      - 返回 None = 无反馈（零打扰，框架不追加任何文本）。
      - **只能贡献反馈，不能改写 ToolResult**：`result` 入参仅供只读判断，
        挂载由 HarnessExecutor 独占，保证「审计记录的 = Agent 看到的」。
        若实现方就地改了 result 的字段，会让工具的真实结果与审计日志（HC4）分歧——
        这是本仓立身之本「看得见、管得住」不可接受的破口。
      - **Fail-Safe（O3）**：实现内部任何异常都应吞掉并返回 None，
        绝不因反馈自身问题把 run 炸断。（框架侧另有兜底，但实现方不得依赖它）
      - **不阻塞（B）**：子进程/网络等阻塞 I/O 必须走 async，
        绝不在事件循环上同步执行——否则冻结所有并发 session 的流式输出。
      - 框架对每个 provider 套硬超时（默认 10s），超时即丢弃该条反馈。
        实现方应对自己的耗时操作另设更短的超时，不要依赖框架这层兜底。

    多 provider 时：按注册序各自 await，返回的反馈**全部拼接**（不取首个），
    severity 取最高。一个 provider 崩溃只丢它自己那条，其余照常合并。
    """

    async def provide(
        self,
        tool_name: str,
        args: dict,
        result: ToolResult,
        ctx: ToolContext,
    ) -> ToolFeedback | None:
        """为刚执行完的工具贡献一段反馈。

        Args:
            tool_name: 刚执行的工具名（实现方自行筛选关心哪些工具）
            args: 该次调用的入参（如 write_file 的 file_path）
            result: 执行结果，**只读**——改它不会生效，且违反本协议约定
            ctx: 工具上下文，含 session_id（状态归属的唯一凭证，不得自造）

        Returns:
            ToolFeedback 或 None（无反馈）
        """
        ...
