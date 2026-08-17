"""pandaren/sub_agent/models.py — 子 Agent 层核心数据模型

SubAgentSummary:       摘要（frozen dataclass，注入 system prompt）
SubAgentDelegateResult: 委派执行结果（frozen dataclass）
SubAgentBlueprint:     蓝图（frozen dataclass，从 .agent/ 目录加载的中间结构）
SubAgentSource:        来源枚举（IntEnum，优先级排序）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..identity.models import TrustLevel, SensitivePermission
    from ..llm.types import ModelSettings


# ════════════════════════════════════════════════
#  SubAgentSource（来源枚举）
# ════════════════════════════════════════════════

class SubAgentSource(IntEnum):
    """Agent 蓝图来源（优先级从低到高）。

    与 SkillSource 对称设计。
    同 agent_id 注册时，高优先级覆盖低优先级。
    """
    DIRECTORY = 1      # 从 .agent/ 目录加载
    PROGRAMMATIC = 2   # 代码直接构建（AgentBuilder 手动创建）


# ════════════════════════════════════════════════
#  SubAgentSummary（摘要，注入 system prompt）
# ════════════════════════════════════════════════

@dataclass(frozen=True)
class SubAgentSummary:
    """Agent 摘要（注入 system prompt，供 LLM 感知可委派的 Agent）。

    与 SkillSummary 对称设计。
    agent_id 不暴露给 LLM，仅在内部用于路由。
    """
    agent_name: str
    when_to_use: str               # 已截断到 max_description_chars


# ════════════════════════════════════════════════
#  SubAgentDelegateResult（委派结果）
# ════════════════════════════════════════════════

@dataclass(frozen=True)
class SubAgentDelegateResult:
    """Agent 委派执行结果。

    供 delegate_task 的 executor 返回给 ToolResult.data。
    """
    success: bool
    output: Any = None             # 目标 Agent 的 AgentResult.output
    error: str | None = None
    target_agent_id: str = ""
    target_run_id: str = ""        # 目标 Agent 的 run_id（trace 关联）
    duration_ms: float = 0.0


# ════════════════════════════════════════════════
#  SubAgentBlueprint（蓝图，目录加载的中间结构）
# ════════════════════════════════════════════════

@dataclass(frozen=True)
class SubAgentBlueprint:
    """Agent 蓝图（从 Markdown 文件加载的身份声明 + system prompt）。

    蓝图 ≠ Agent 实例。蓝图只包含声明式信息（纯数据），
    不包含运行时依赖（LLM Client、Tools 等）。
    开发者拿到蓝图后，通过 AgentBuilder 注入运行时依赖，构建完整 Agent 实例。

    与 Skill 的区别：
      Skill = 纯数据（content 就是最终产物），可以直接注册
      SubAgentBlueprint = 半成品（只有声明，缺运行时依赖），需要 Builder 加工

    设计原则：frozen 不可变，加载后不允许修改。
    E4：trust_level 为必填字段，不允许空值或非法值静默降级。

    资源声明（tools / skills / sub_agents，三层对称过滤）：
      tools:          声明该 Agent 需要哪些工具（工具名列表，"*" 表示继承全部）。
                      空 tuple = 不使用任何工具（Fail-Safe，不静默继承）。
      skills:         声明该 Agent 需要哪些 Skill（Skill 名列表，"*" 表示继承全部）。
                      空 tuple = 不从父级继承 Skill（Fail-Safe 默认）。
      sub_agents:     权限声明——该 Agent 可委派哪些同级子 Agent（agent_id 列表，"*" 表示可委派全部）。
                      空 tuple = 不可委派子 Agent（Fail-Safe 默认）。
                      仅一层，不做递归嵌套构建；运行时由 SubAgentRegistry 执行权限校验。
    """
    # ── 必填字段（从 Frontmatter 解析）──
    agent_id: str                                      # 唯一标识
    agent_name: str                                    # 人类可读名称
    when_to_use: str                                   # 调度描述（≤200 字）
    system_prompt: str                                 # Markdown 正文 → Agent 的 system prompt
    trust_level: TrustLevel                            # 信任等级（必填，E4 不允许缺失）

    # ── 可选字段（从 Frontmatter 解析，安全默认值）──
    sensitive_permissions: frozenset[SensitivePermission] = field(default_factory=frozenset)  # 默认空权限（Fail-Safe）
    source: SubAgentSource = SubAgentSource.DIRECTORY        # 来源标记
    source_path: str | None = None                     # 源文件路径（调试 / 审计用）

    # ── LLM 配置（可选；None = 默认继承父级 settings / provider 默认）──
    model: str | None = None                           # 顶层 model 字段 → 构建时映射 ModelSettings.target_model
    llm_settings: "ModelSettings | None" = None        # 蓝图显式 LLM 调参；逐字段覆盖父级（None = 全继承父级）

    # ── 资源声明（最小权限，Fail-Safe 默认值）──
    tools: tuple[str, ...] = ()                        # 工具名列表；空=不用工具；("*",)=继承全部
    skills: tuple[str, ...] = ()                       # Skill名列表；空=不从父级继承；("*",)=继承全部
    sub_agents: tuple[str, ...] = ()                   # 子Agent名列表；空=不委派；("*",)=可委派全部
