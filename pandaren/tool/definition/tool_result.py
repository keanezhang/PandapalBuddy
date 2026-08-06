"""pandaren/tool/definition/tool_result.py — 工具执行结果模型"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ValidationResult:
    """Pre-validate 校验结果。

    与 ToolResult 的区别：
    - ToolResult 是执行后的完整结果（含 data, duration_ms 等）
    - ValidationResult 是执行前的轻量校验（只有 error 信息）
    - 两者都返回给 LLM，但语义不同：
        ToolResult.error  = "执行失败了"
        ValidationResult  = "不应该执行，因为..."
    """
    valid: bool                    # True = 通过，False = 拒绝
    message: str                   # 拒绝原因 + 建议（如 "Path not found. Did you mean /path/to/x?"）
    error_code: int | None = None  # 可选错误码（让 LLM 区分"路径不存在" vs "权限不足"）


@runtime_checkable
class HasLLMFormat(Protocol):
    """工具返回的结构化对象可实现此协议以自定义 LLM 视角的格式化。

    ToolExecutor 中的优先级链：
      data.__tool_format_for_llm__() → lifecycle.format_result_for_llm(data, name) → str(data)
    """
    def __tool_format_for_llm__(self) -> str: ...


@dataclass(frozen=True)
class DiscoveredToolEntry:
    """ToolSearch sentinel 字段的元素类型。

    用于在消息历史中标记 LLM 通过 search_tools 发现的工具。
    frozen=True 保证存储后的记录不可被外部修改。
    """
    name: str
    turn: int


#: 多源反馈合并后的 source 取值。语义：「text 里各分段已自带 [source] 标签」——
#: 渲染时据此不再加外层标签，避免 `[composite] [code_quality_gate] ...` 的双重前缀。
#: 提成常量而非字面量：合并方（behavior 层 executor）与渲染方（engine 层 run_core）
#: 靠它对齐，散落的字符串一改就断。
COMPOSITE_SOURCE = "composite"


class FeedbackSeverity(IntEnum):
    """反馈严重度。

    数值可比较：合并多源反馈时取 max（见 HarnessExecutor._run_feedback_stage）。
    """
    INFO = 1
    WARNING = 2
    ERROR = 3


@dataclass(frozen=True)
class ToolFeedback:
    """要并入工具结果的一段反馈。

    由 ToolFeedbackProvider（应用层实现）产出，HarnessExecutor 独占挂载到
    ToolResult.feedback。**有两个受众**，各走各的通路：

        render_tool_result_for_llm  → 拼进 tool 消息 → LLM（受 llm_visible 门控）
        feedback_to_event_data      → 进 StreamEvent → 用户屏幕（永远发）

    frozen=True：反馈一旦产出即不可变，杜绝下游改写导致「审计记录的」与
    「Agent 看到的」分歧（HC4）。
    """
    text: str                  # 反馈正文
    severity: FeedbackSeverity
    source: str                # 反馈来源标识，如 "code_quality_gate"（留痕/分段/去重用）

    #: False = **只上 UI，不进 LLM 上下文**（render_tool_result_for_llm 跳过它）。
    #:
    #: 为「检查通过」这类状态播报而生。若无此字段，provider 只能在通过时返回 None
    #: —— 而降级（工具没装/超时/崩溃）返回的**也是** None，两者在线上坍缩成同一个
    #: 信号，UI 无从区分。据此亮绿灯 = 在检查根本没跑时告诉用户「通过」，
    #: 是比不显示糟得多的**假绿灯**。有了它，三态才分得开：
    #:
    #:     llm_visible=False + severity=INFO  → 查过了，干净   → UI 绿灯
    #:     llm_visible=True  + severity=ERROR → 查过了，有问题 → UI 红灯 + LLM 收到诊断
    #:     feedback is None                   → **没查**       → UI 不作任何声明
    #:
    #: 默认 True：既有 provider 行为不变。
    #: 「零打扰」原则由此保持完整 —— 它约束的是 **LLM 的 token 预算**，而非用户的屏幕；
    #: 一个绿色角标对 LLM 是 0 token，因为它压根不进 messages。
    llm_visible: bool = True


@dataclass
class ToolResult:
    """工具执行结果。

    这是 execute_tool() 的统一返回类型，永远不抛异常（O3 原则）。
    agent_loop 不需要 try/except，只需检查 success 和 halt 字段。

    字段说明：
      success:      执行是否成功
      data:         成功时的返回数据（Any → 序列化后给 LLM）。
                    有数据返回数据内容；无数据时留空或给 "OK" 等提示文本，
                    不使用 None。默认 ""。
      error:        失败时的错误信息（给 LLM 的可读文本）。
                    失败时必填；成功时留空。默认 ""。
      halt:         S6 硬停止标记，True 时 agent_loop 终止整个 run
      deduplicated: R4 幂等标记，True 表示本次返回是缓存结果（非重新执行）
      truncated:    R2 截断标记，True 表示输出被截断
      tool_name:    执行的工具名（追踪用）
      duration_ms:  执行耗时（毫秒）
      warnings:     来自工具的警告消息列表（破坏性命令提示等，不影响执行）
      feedback:     工具执行后由 ToolFeedbackProvider 贡献的反馈，None=无反馈（默认）
      _discovered_tools: search_tool 专用 sentinel 字段（内部使用，LLM 不感知）
    """
    success: bool
    data: Any = ""
    error: str = ""
    halt: bool = False
    deduplicated: bool = False
    truncated: bool = False
    tool_name: str = ""
    duration_ms: float = 0.0
    warnings: list[str] | None = None
    feedback: ToolFeedback | None = None
    _discovered_tools: tuple[DiscoveredToolEntry, ...] | None = None

    # Plan Mode 信号字段
    plan_complete: bool = False
    plan_path: str | None = None
