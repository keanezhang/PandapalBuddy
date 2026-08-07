"""pandaren/skill/models.py — Skill 层核心数据模型

Skill:        知识包定义（frozen dataclass，SK1 不可变）
SkillResult:  调用结果（frozen dataclass，allowed_tools 有安全意义）
SkillSummary: 摘要（frozen dataclass，注入 system prompt）
SkillSource:  来源枚举（IntEnum，优先级排序）
SkillType:    类型枚举（KNOWLEDGE / ACTION）
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class SkillSource(IntEnum):
    """Skill 来源（优先级从低到高）。

    同名 Skill 注册时，高优先级覆盖低优先级。
    """
    BUILTIN = 1       # SDK 内置
    PROJECT = 2       # 项目级：随应用分发、只读
    USER = 3          # 用户级：持久化，不随 rebuild/upgrade 丢失
    PROGRAMMATIC = 4  # 代码注册（AgentBuilder.skills()）

    # 注：本枚举只表达「来源与优先级」，**不规定目录** —— 目录由应用层在
    # `skills_from_dir(dir, source=...)` 时决定。
    # 参考：pandapal 装配为 PROJECT={resources}/skills/system（随 sidecar 打包）、
    #      USER={data_dir}/skills（data_dir 默认 .pandapal，打包后即用户级目录；
    #      直接子目录即 skill，如 {data_dir}/skills/<name>/SKILL.md）。
    # 此处曾硬编码 `.pandaren/skills/` / `~/.pandaren/skills/`，是错的：那两个路径
    # 从未存在过，SDK 也无权规定应用把 skill 放哪。skillify 照抄了这条注释，
    # 于是把错误传播到了 skill 生成流程里。


class SkillType(IntEnum):
    """Skill 类型。

    KNOWLEDGE: 纯知识注入（content → system prompt），LLM 按文本指引行事。
    ACTION:    可执行动作（script → 自动生成 Tool），LLM 直接调用带参数的工具。
    """
    KNOWLEDGE = 1
    ACTION = 2


@dataclass(frozen=True)
class Skill:
    """Skill 知识包定义（frozen，SK1 不可变）。

    注册后所有字段不可修改。修改 Skill 需要重新注册（替换）。
    容器类型使用 tuple 保证深度不可变。

    Action Skill 标识：script 字段非 None 时自动识别为 ACTION 类型，
    SDK 将基于 script 指定的 Python 函数自动生成带完整参数 schema 的 Tool。
    """
    # ── 必填字段 ──
    name: str                                      # 唯一标识，同时作为调用命令
    description: str                               # ≤250 字符，供 LLM 匹配判断
    when_to_use: str                               # ≤200 字符，调度描述（供 LLM 判断何时使用）
    content: str                                   # Markdown 正文（Skill 的核心载体）

    # ── 可选字段（安全默认值）──
    source: SkillSource = SkillSource.BUILTIN      # 安全默认：最低优先级，外部传入需显式覆盖
    allowed_tools: tuple[str, ...] | None = None   # None=继承 Agent 默认工具集（SK2），指定工具名列表，则Skill 激活期间的工具白名单，同一 Turn 内激活了多个skills去工具的并集
    allow_auto_trigger: bool = True                # False=必须手动触发（SK3），控制 LLM 是否可以在对话中自行判断、自动触发该 Skill
    argument_hint: str | None = None               # 参数提示（UX 优化），纯 UX（用户体验）优化——给用户提示这个 Skill 需要什么参数。
    tags: tuple[str, ...] = ()                     # 搜索辅助标签
    base_path: str | None = None                   # 辅助资源基础路径

    # ── Action Skill 字段（全部可选，向后兼容）──
    script: str | None = None                      # 脚本文件相对路径（相对 base_path）
    entry_function: str | None = None              # 入口函数名（None=自动检测模块中唯一 public function）

    @property
    def is_action(self) -> bool:
        """是否为 Action Skill（有可执行脚本）。"""
        return self.script is not None

    @property
    def skill_type(self) -> SkillType:
        """Skill 类型（根据 script 字段自动推断）。"""
        return SkillType.ACTION if self.is_action else SkillType.KNOWLEDGE


@dataclass(frozen=True)
class SkillResult:
    """Skill 调用结果（frozen=True）。

    frozen 原因：allowed_tools 有安全意义——传递给 ToolRegistry 做执行期过滤，
    如果中途被篡改等于运行时权限提升。
    """
    success: bool
    content: str | None = None                     # 渲染后的 Skill 正文
    error: str | None = None
    skill_name: str = ""
    allowed_tools: tuple[str, ...] | None = None   # 传递给 ToolRegistry
    content_tokens: int = 0                        # 注入的 token 估算


@dataclass(frozen=True)
class SkillSummary:
    """Skill 摘要（注入 system prompt，供 LLM 感知可用 Skill）。"""
    name: str
    when_to_use: str  # 已截断到 max_description_chars（取自 Skill.when_to_use）
