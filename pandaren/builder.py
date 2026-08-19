"""pandaren/builder.py — AgentBuilder 链式构建 API

应用层通过 Builder 配置"要不要、用什么"，SDK 内部负责组装。

Observability 显式四态（适用于 log / tracer / metrics，audit 见 HC4）：
  _UNSET（初始值）— 未调用 .observability()，build 时解析为 False（关闭）
  False           — 显式关闭（audit 除外，HC4 不可关闭，传 False 降级为 InMemory）
  "mem"           — 使用 SDK 内置 InMemory 后端（显式开启）
  Backend 实例    — 使用应用层传入的自定义后端

调用约定：
  不调用 .observability()                    → 全关（零噪音，适合测试/CI）
  .observability()                           → 全关（参数全为 _UNSET → False）
  .observability(log="mem", audit="mem")     → 精细控制（传了的开，没传的关）
  .observability(audit=MyAuditBackend())     → 自定义后端

Memory backend（由应用层提供实现，SDK 只定义 Protocol，并提供
SQLiteRawLogBackend 内置实现）：
  raw_log_backend:  None = 不持久化原始日志（默认）
  db_path:          str | Path = 启用 SQLite 落盘的快捷方式
                    （内部构造 SQLiteRawLogBackend(db_path=...)；与 raw_log_backend 互斥）

示例：
    agent = (
        AgentBuilder()
        .identity(agent_id="coder", ...)
        .llm(client=openai_client)
        .tools(ALL_TOOLS)
        .system_prompt("你是代码助手")
        .behavior(max_steps=50)
        .memory(
            db_path="./pandapal.db",                    # SQLite 快捷方式
        )
        .observability(
            audit=MarkdownAuditBackend("./data/obs"),  # 自定义后端
            metrics=False,                              # 显式关闭
            log="mem",                                  # InMemory
        )
        .build()
    )

.identity() → .llm() → .tools() → .skills() → .behavior() → .memory() → .observability() → .build() → Agent
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from .identity.models import Identity, SensitivePermission, TrustLevel
from .tool import ToolRegistry, create_tool_registry, Tool
from .behavior.harness.executor import HarnessExecutor
from .behavior.permission_guard import PermissionGuard
from .behavior.hitl_controller import HITLController
from .behavior.execution_limits import ExecutionLimits, DEFAULT_STEP_TIMEOUT, DEFAULT_TOTAL_TIMEOUT
from .behavior.error_policy import ErrorPolicy
from .behavior.step_guard import StepGuard
from .behavior.harness.tool_feedback import ToolFeedbackProvider
from .behavior.context_window_budget import ContextWindowBudget
from .observability.config import ObservabilityConfig
from .observability.provider import ObservabilityProvider
from .observability.audit import AuditLog
from .observability.types import LogLevel, TraceLevel
from .llm.protocol import LLMClient
from .llm.types import ModelSettings
from .memory.memory import Memory
from .memory.protocols import (
    RawLogBackend,
    CompactionPolicy,
    DropSummarizer,
    PostCompactSource,
    TokenEstimator,
    WorkingMemoryBackend,
)
from .memory.backends import SQLiteRawLogBackend

if TYPE_CHECKING:
    # 仅供类型注解解析：运行时这三者仍走 build() 内的延迟 import（见 build/build_blueprint），
    # 放这里让 ruff/IDE/类型检查器能解析字符串注解，且不引入 import 期开销。
    from .agent import Agent
    from .agent.blueprint import AgentBlueprint
    from .skill.models import SkillSource


# 模块级标记：防止子 Agent 构建时重复加载内置蓝图
_default_agents_loaded: bool = False

# Sentinel：区分"未配置"与"显式传 None（= 用默认后端）"
# 类似 dataclasses.MISSING、attrs.NOTHING 的惯用模式
_UNSET = object()

logger = logging.getLogger("pandaren.builder")


class AgentBuilder:
    """链式 API 构建 Agent。

    Observability 四态（适用于 log / tracer / metrics，audit 见 HC4）：
      _UNSET（初始值）— 未配置 → build 时解析为 False（关闭）
      False           — 显式关闭（audit 除外，HC4 不可关闭，传 False 降级为 InMemory）
      "mem"           — 使用 SDK 内置 InMemory 后端（显式开启）
      Backend 实例    — 使用传入的自定义对象

    调用约定：
      不调用 .observability()                  → 全关（零噪音，适合测试/CI）
      .observability()                         → 全关（参数全为 _UNSET → False）
      .observability(log="mem", tracer="mem")  → 精细开启指定子系统
      .observability(audit=MyBackend())        → 自定义后端

    Memory backend（应用层实现 RawLogBackend Protocol，或用 db_path 启用 SQLite）：
      raw_log_backend:  None = 不持久化原始日志（默认）
      db_path:          str | Path = 启用 SQLite 落盘的快捷方式
                        （内部构造 SQLiteRawLogBackend(db_path=...)；与 raw_log_backend 互斥）

    示例：
        agent = (
            AgentBuilder()
            .identity(agent_id="coder", ...)
            .llm(client=openai_client)
            .tools(ALL_TOOLS)
            .system_prompt("你是代码助手")
            .behavior(max_steps=50)
            .memory(
                db_path="./pandapal.db",                   # SQLite 快捷方式
            )
            .observability(
                audit=MarkdownAuditBackend("./data/obs"),  # 自定义后端
                metrics=False,                              # 显式关闭
                log="mem",                                  # InMemory
            )
            .build()
        )
    """

    def __init__(self) -> None:
        # ── Identity ──────────────────────────────────────────────────────────
        self._identity: Identity | None = None

        # ── LLM ───────────────────────────────────────────────────────────────
        self._llm_client: LLMClient | None = None
        self._llm_settings: ModelSettings | None = None

        # ── Tools（build() 时统一构造 ToolRegistry）──────────────────────────
        self._tool_list: list[Tool] = []

        # ── Skills（build() 时统一构造 SkillRegistry）─────────────────────────
        self._skill_list: list[Any] = []
        
        # ── sub_agent─────────────────────────
        self._sub_agent_blueprints: list[tuple[Any, LLMClient, list, list]] = []

        # ── System Prompt ─────────────────────────────────────────────────────
        self._system_prompt: str = "You are a helpful assistant."

        # ── Behavior ──────────────────────────────────────────────────────────
        self._permission_guard: PermissionGuard = PermissionGuard()
        self._hitl_controller: HITLController | None = None
        self._execution_limits: ExecutionLimits | None = None
        self._error_policy: ErrorPolicy | None = None
        self._step_guard: StepGuard | None = None
        self._tool_feedback_providers: list[ToolFeedbackProvider] = []
        self._tool_budget_ratio: float | None = None
        self._tool_max_always_count: int | None = None
        self._tool_max_discovered: int | None = None
        self._stream: bool = True

        # ── Context Window Budget（上下文 token 配额）─────────────────────────
        self._context_window_budget: ContextWindowBudget | None = None

        # ── Memory ────────────────────────────────────────────────────────────
        # 持久化（应用层注入 backend，或用 db_path 快捷方式启用 SQLite）
        self._raw_log_backend: RawLogBackend | None = None
        self._db_path: str | Path | None = None
        self._session_mode: str = "multi_turn"

        # 切分策略（None = 默认 WindowedKeepPolicy；应用层可注入自定义 CompactionPolicy）
        self._compaction_policy: CompactionPolicy | None = None

        # 被丢弃消息的脉络摘要（None = 不摘要；应用层可注入 LLM-driven 实现）
        self._drop_summarizer: DropSummarizer | None = None

        # Token 估算器（None = CharBasedTokenEstimator；应用层可注入真实 tokenizer
        # 实现如 TiktokenEstimator，使压缩触发判据与实际 LLM token 同量纲）
        self._token_estimator: TokenEstimator | None = None

        # MicroCompact（SDK 算法 + 应用层提供工具白名单）
        self._microcompact_tools: frozenset[str] | set[str] | None = None
        self._microcompact_keep_recent: int | None = None
        self._microcompact_single_result_max_tokens: int | None = None

        # PostCompact 回注（应用层注入 sources，默认空 = 不启用）
        self._post_compact_sources: list[PostCompactSource] | None = None
        self._post_compact_token_budget: int | None = None

        # 工作记忆持久化
        self._working_memory_backend: WorkingMemoryBackend | None = None

        # ── Plan Mode ──
        self._plan_dir: str | None = None                 # 自定义计划文件存放目录

        # ── Observability ─────────────────────────────────────────────────────
        # 四态：_UNSET = 未配置(→全关) / None = 用默认 / False = 关闭 / 实例 = 自定义
        self._audit: Any = _UNSET
        self._tracer: Any = _UNSET
        self._metrics: Any = _UNSET
        self._log: Any = _UNSET
        self._log_level: LogLevel = LogLevel.INFO
        self._trace_level: TraceLevel = TraceLevel.SUMMARY
        self._sanitizer: Any = None
        self._hooks: Any = None

    # ── Identity ──

    def identity(
        self,
        *,
        agent_id: str,
        agent_name: str,
        when_to_use: str,
        sensitive_permissions: (
            frozenset["SensitivePermission"]
            | set["SensitivePermission"]
            | list["SensitivePermission"]
        ),
        trust_level: TrustLevel,
    ) -> "AgentBuilder":
        """设置 Identity（身份声明）。

        E4 失败安全：when_to_use、sensitive_permissions、trust_level 为必填字段，
        不提供默认值，强制开发者显式声明，避免配置遗漏被静默掩盖。
        """
        self._identity = Identity(
            agent_id=agent_id,
            agent_name=agent_name,
            when_to_use=when_to_use,
            sensitive_permissions=sensitive_permissions,
            trust_level=trust_level,
        )
        return self

    # ── LLM ──

    def llm(self, client: LLMClient) -> "AgentBuilder":
        """设置 LLM 客户端。"""
        self._llm_client = client
        return self

    def llm_settings(
        self,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        frequency_penalty: float | None = None,
        presence_penalty: float | None = None,
        stop: list[str] | None = None,
        seed: int | None = None,
        response_format: dict[str, Any] | type | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        parallel_tool_calls: bool | None = None,
        include_usage: bool | None = None,
        reasoning: dict[str, Any] | None = None,
        extra_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        extra_query: dict[str, str] | None = None,
    ) -> "AgentBuilder":
        """设置 LLM 调参（temperature、response_format 等）。

        所有参数均可选，仅传入需要覆盖的字段，其余由 provider 默认决定。
        与 .llm() 分离，职责清晰：.llm() 配"用哪个模型"，.llm_settings() 配"怎么调用"。

        Args:
            temperature:         采样温度，0~2。越高越随机，越低越确定。0 为贪婪解码。
            max_tokens:          单次回复最大 token 数。
            top_p:               核采样概率阈值，0~1。与 temperature 二选一即可。
            frequency_penalty:   频率惩罚，-2~2。正值降低已出现 token 的重复概率。
            presence_penalty:    存在惩罚，-2~2。正值鼓励讨论新话题。
            stop:                停止序列列表，模型遇到其中任一字符串时停止生成。
            seed:                随机种子，设固定值可使输出尽可能确定性（provider 支持时）。
            response_format:     输出格式控制，支持三种形式：
                                   - dict: 手动指定，如 {"type": "json_object"}
                                   - type: 传 dataclass 或 Pydantic BaseModel，SDK 自动转为 json_schema
                                   - None: 不设置
            tool_choice:         工具调用策略：
                                   - "none": 不调用工具
                                   - "auto": 由模型决定（默认）
                                   - "required": 必须调用至少一个工具
                                   - {"type":"function","function":{"name":"..."}}: 强制调用指定工具
            parallel_tool_calls: 是否允许并行调用多个工具（OpenAI 1106+）。
            include_usage:       流式模式下是否注入 stream_options.include_usage。
                                   - None（默认）：不注入（最大兼容性，任何 provider 都不会因此报错）
                                   - True：请求 provider 在流末尾返回 usage 统计
                                   - False：等价于 None，用于在代码中显式表达"不需要"
            reasoning:           推理模型（o1/o3/deepseek-r1 等）的推理强度配置。
                                 示例：{"effort": "low"} / {"effort": "medium"} / {"effort": "high"}
                                 注意：普通模型传入此字段可能报 400，调用方须自行保证仅在推理模型下使用。
            extra_body:          Provider 专属顶层 body 参数，如千问的 enable_search=True。
            extra_headers:       附加到 HTTP 请求头的自定义字段（覆盖同名键）。
                                 如某些网关的自定义 header：{"X-DashScope-Plugin": "vl"}。
            extra_query:         附加到请求 URL 的 query 参数。
                                 如 Azure OpenAI 的版本号：{"api-version": "2024-02-01"}。

        示例：
            # 基础调参
            .llm_settings(temperature=0.7, max_tokens=4096)

            # 结构化输出 — 手动 dict
            .llm_settings(response_format={"type": "json_object"})

            # 结构化输出 — 传类型，SDK 自动转换（推荐）
            @dataclass
            class UserInfo:
                name: str = field(metadata={"description": "姓名"})
                age: int  = field(metadata={"description": "年龄"})

            .llm_settings(response_format=UserInfo)

            # 推理模型 + 流式 usage 统计
            .llm_settings(reasoning={"effort": "medium"}, include_usage=True)

            # Azure OpenAI 版本号
            .llm_settings(extra_query={"api-version": "2024-02-01"})
        """
        self._llm_settings = ModelSettings(
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            frequency_penalty=frequency_penalty,
            presence_penalty=presence_penalty,
            stop=stop,
            seed=seed,
            response_format=response_format,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
            include_usage=include_usage,
            reasoning=reasoning,
            extra_body=extra_body,
            extra_headers=extra_headers,
            extra_query=extra_query,
        )
        return self

    def tools(self, tool_list: list[Tool]) -> "AgentBuilder":
        """注册工具列表。"""
        self._tool_list.extend(tool_list)
        return self

    # ── Skills ──

    def skills(self, skill_list: list) -> "AgentBuilder":
        """注册 Skill 列表。

        传入 Skill 对象列表，build() 时自动注册到 SkillRegistry。
        """
        self._skill_list.extend(skill_list)
        return self

    def skills_from_dir(
        self,
        directory,
        recursive: bool = True,
        source: "SkillSource | None" = None,
    ) -> "AgentBuilder":
        """从目录加载并注册所有 Skill（SKILL.md 文件）。

        等价于 load_skills_from_dir(directory) + skills(result)，
        但应用层只需传路径，SDK 内部完成加载。

        Args:
            directory: Skill 目录路径（str 或 Path，递归扫描 SKILL.md）。
            recursive: 是否递归子目录，默认 True。
            source: Skill 来源，默认 PROJECT。传 USER 可让用户 Skill 获得更高覆盖优先级。

        Returns:
            self（链式调用）。
        """
        from .skill.loader import load_skills_from_dir
        from .skill.models import SkillSource

        _source = source if source is not None else SkillSource.PROJECT
        loaded = load_skills_from_dir(
            directory, source=_source, recursive=recursive,
        )
        self._skill_list.extend(loaded)
        return self

    # ── Plan Mode ──

    def plan_mode(
        self,
        *,
        plan_dir: str | None = None,
    ) -> "AgentBuilder":
        """Plan Mode 目录（可选）。

        - ``plan_dir`` — 自定义计划文件存放目录（绝对路径）。
          不传则使用 ``{cwd}/.pandaren/plans/``。

        示例::

            agent = (
                AgentBuilder()
                .identity(...)
                .llm(...)
                .plan_mode(
                    plan_dir="/home/user/my-plans",
                )
                .build()
            )
        """
        self._plan_dir = plan_dir
        return self

    # ── Agent Registry（多 Agent 场景）──

    def sub_agents(
        self,
        blueprints: list,
        llm_client: LLMClient,
        tools: list | None = None,
        skills: list | None = None,
    ) -> "AgentBuilder":
        """追加用户自定义子 Agent（通过 SubAgentBlueprint 列表）。

        Args:
            blueprints:  SubAgentBlueprint 对象列表
            llm_client:  子 Agent 使用的 LLM 客户端（想复用主 Agent 的直接传同一实例）
            tools:       父级工具池（蓝图按名过滤；None = 不传入工具）
            skills:      父级 Skill 池（None = 不传入 Skill）
        """
        for bp in blueprints:
            self._sub_agent_blueprints.append((bp, llm_client, tools or [], skills or []))
        return self

    def sub_agents_from_dir(
        self,
        directory: str | Any,
        llm_client: LLMClient,
        tools: list | None = None,
        skills: list | None = None,
        pattern: str = "*.md",
        recursive: bool = True,
    ) -> "AgentBuilder":
        """从目录加载 Agent 蓝图，构建时自动创建 sub-agent 并注册。

        便捷方法，等价于手动调用 load_agents_from_dir + AgentBuilder + registry.register。

        蓝图中可声明：
          tools:       工具名列表（从 tools 池过滤），"*" = 继承全部，空 = 不用工具
          skills:      Skill 名列表（从 skills 池过滤），"*" = 继承全部，空 = 不继承
          sub_agents:  子 Agent 名列表（从蓝图中过滤），"*" = 可委派全部，空 = 不委派

        Args:
            directory:  Agent 定义目录（如 ".agent/"）
            llm_client: sub-agent 使用的 LLM 客户端（想复用主 Agent 的直接传同一实例）
            tools:      父级工具池（蓝图按名过滤；None = 空池）
            skills:     父级 Skill 池（蓝图按 skills 字段过滤；None = 空池）
            pattern:    文件匹配模式（默认 "*.md"）
            recursive:  是否递归扫描子目录，默认 True
        """
        from .sub_agent import load_agents_from_dir
        blueprints = load_agents_from_dir(directory, pattern=pattern, recursive=recursive)
        logger.info(
            "[user sub-agent loader] 从 '%s' 加载了 %d 个 sub-agent blueprint",
            directory, len(blueprints),
        )
        for bp in blueprints:
            self._sub_agent_blueprints.append((bp, llm_client, tools or [], skills or []))
            logger.debug(
                "[user sub-agent loader]   agent_id='%s'  name='%s'  tools=%s  skills=%s  sub_agents=%s",
                bp.agent_id, bp.agent_name, bp.tools, bp.skills, bp.sub_agents,
            )
        return self

    def with_default_sub_agents(self) -> "AgentBuilder":
        """加载 SDK 内置子 Agent 蓝图（pandaren/agents/ 下的 .md 文件）。

        幂等：同一进程内多次调用只加载一次。
        使用主 Agent 的 LLM、工具池、Skill 池，追加到已有蓝图列表。
        """
        global _default_agents_loaded
        if _default_agents_loaded:
            return self

        from pathlib import Path
        from .sub_agent import load_agents_from_dir

        default_dir = Path(__file__).parent / "agents"
        if not default_dir.is_dir():
            return self

        try:
            blueprints = load_agents_from_dir(str(default_dir))
        except Exception as e:
            logger.warning("[pandaren builtin sub-agents] 加载失败: %s", e)
            return self

        if not blueprints:
            return self

        _default_agents_loaded = True

        for bp in blueprints:
            self._sub_agent_blueprints.append(
                (bp, self._llm_client, self._tool_list, self._skill_list)
            )
            logger.info(
                "[pandaren builtin sub-agents]   agent_id='%s'  name='%s'  tools=%s  skills=%s  sub_agents=%s",
                bp.agent_id, bp.agent_name, bp.tools, bp.skills, bp.sub_agents,
            )
        # logger.info(
        #     "[pandaren builtin sub-agents] 共加载 %d 个内置子 Agent", len(blueprints),
        # )
        return self

    # ── System Prompt ──

    def system_prompt(self, prompt: str) -> "AgentBuilder":
        """设置系统提示词。"""
        self._system_prompt = prompt
        return self

    # ── Behavior ──

    def behavior(
        self,
        *,
        max_steps: int = 30,
        step_timeout: float = DEFAULT_STEP_TIMEOUT,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
        step_guard: "StepGuard | None" = None,
        tool_feedback_providers: "list[ToolFeedbackProvider] | None" = None,
        auto_confirm_high: bool = False,
        max_retries: int = 3,
        base_delay_s: float = 1.0,
        max_delay_s: float = 30.0,
        tool_budget_ratio: float | None = None,
        tool_max_always_count: int | None = None,
        tool_max_discovered: int | None = None,
        stream: bool = True,
    ) -> "AgentBuilder":
        """设置行为策略。

        Args:
            max_steps:             最大 step 数（HC5 有界循环）
            step_timeout:          单 step 超时（秒）
            total_timeout:         整个 run 总超时（秒）
            step_guard:            通用每步停机守卫（应用层实现 StepGuard 协议）。
                                   SDK 不知道停机理由——每步 LLM 调用后把用量事实交给它，
                                   由它按自身策略（如净费用累加超预算）裁决 halt + 理由。
                                   None = 永不因守卫停机。价格/预算全归应用层。
            tool_feedback_providers: 工具执行后的反馈贡献者（应用层实现 ToolFeedbackProvider）。
                                   典型用途：写完 .py 就跑 lint，把诊断随工具结果回灌给 LLM。
                                   SDK 不含任何领域判断——「检查什么」全在实现方。
                                   None/[] = 不收反馈（默认，整个 stage 跳过、零开销）。
            auto_confirm_high:     HIGH 敏感度操作自动放行（HC6 CRITICAL 仍强制 HITL）
            max_retries:           工具重试上限
            base_delay_s:          重试基础延迟（秒）
            max_delay_s:           重试最大延迟（秒）
            tool_budget_ratio:     工具 schema 占 context 的比例
            tool_max_always_count: ALWAYS tier 工具最大数量
            tool_max_discovered:   每会话最多发现的 DEFERRED 工具数
            stream:                是否使用流式 LLM 调用
        """
        self._execution_limits = ExecutionLimits(
            max_steps=max_steps,
            step_timeout=step_timeout,
            total_timeout=total_timeout,
        )
        self._hitl_controller = HITLController(auto_confirm_high=auto_confirm_high)
        self._error_policy = ErrorPolicy(
            max_retries=max_retries,
            base_delay_s=base_delay_s,
            max_delay_s=max_delay_s,
        )
        self._step_guard = step_guard
        self._tool_feedback_providers = list(tool_feedback_providers or ())
        self._tool_budget_ratio = tool_budget_ratio
        self._tool_max_always_count = tool_max_always_count
        self._tool_max_discovered = tool_max_discovered
        self._stream = stream
        return self

    def context_budget(
        self,
        *,
        context_window: int | None = None,
        system_prompt_ratio: float | None = None,
        tool_schema_ratio: float | None = None,
        conversation_ratio: float | None = None,
        recall_ratio: float | None = None,
    ) -> "AgentBuilder":
        """设置上下文窗口 Token 预算分配。

        提供模型 context window 的 token 预算配额，作为所有消费方的单一真相源。
        设置后 Memory 的 compact_threshold 将自动使用 conversation_tokens 配额，
        ToolBudget 将使用 tool_schema_tokens 配额。

        所有参数均可选，未传入时使用 ContextWindowBudget 内部默认值。

        注意：LLM 最大输出 token 数请通过 .llm_settings(max_tokens=...) 配置。

        Args:
            context_window:       模型输入上下文窗口大小（token），建议查阅模型文档后传入
            system_prompt_ratio:  system prompt 占比
            tool_schema_ratio:    工具 schema 占比
            conversation_ratio:   对话历史占比
            recall_ratio:         召回内容占比
        """
        # 只透传用户显式传入的参数，其余由 ContextWindowBudget 默认值兜底
        kwargs = {
            k: v for k, v in {
                "context_window": context_window,
                "system_prompt_ratio": system_prompt_ratio,
                "tool_schema_ratio": tool_schema_ratio,
                "conversation_ratio": conversation_ratio,
                "recall_ratio": recall_ratio,
            }.items() if v is not None
        }
        self._context_window_budget = ContextWindowBudget(**kwargs)
        return self

    # ── Memory ──

    def memory(
        self,
        *,
        # ── 持久化（默认不启用） ──
        raw_log_backend: RawLogBackend | None = None,
        db_path: str | Path | None = None,
        session_mode: str = "multi_turn",
        # ── 切分策略 ──
        compaction_policy: CompactionPolicy | None = None,
        # ── Token 估算（压缩触发判据的量纲；None = CharBasedTokenEstimator）──
        token_estimator: TokenEstimator | None = None,
        # ── 摘要扩展点（异步、可调 LLM、应用层注入） ──
        drop_summarizer: DropSummarizer | None = None,
        # ── MicroCompact（清旧工具结果，与切分正交，本次重构不动） ──
        microcompact_tools: frozenset[str] | set[str] | None = None,
        microcompact_keep_recent: int | None = None,
        microcompact_single_result_max_tokens: int | None = None,
        # ── PostCompact 回注 ──
        post_compact_sources: list[PostCompactSource] | None = None,
        post_compact_token_budget: int | None = None,
        # ── 工作记忆持久化 ──
        working_memory_backend: WorkingMemoryBackend | None = None,
    ) -> "AgentBuilder":
        """设置 Memory 配置。

        SDK 只提供 Protocol 定义和默认切分 / 回注算法。
        Backend（RawLog）、被丢弃消息的脉络摘要（DropSummarizer，可调 LLM）、
        MicroCompact 白名单都由应用层注入。

        ──────────────── 持久化（raw_log）默认不启用 ────────────────

        v1.4 重构：去 summary 化后，raw_log 的语义重新定位为**离线分析数据源**——
        应用层定时任务通过 ``RawLogBackend.load_all()`` 提炼 User Model /
        Episodic Archive。是一个有部署成本的能力，必须显式声明开启：

          - 不传任何参数        → ``raw_log_backend=None``，运行时不落盘
          - 传 ``db_path=...``  → 自动 ``SQLiteRawLogBackend(db_path=...)``（最常用）
          - 传 ``raw_log_backend=...`` → 用户显式构造任意 Protocol 实现
          - **同时传 ``raw_log_backend`` 与 ``db_path`` → ValueError**（语义冲突）

        ──────────────── 上下文管理四层（compact 流程） ────────────────

        Layer 1. **MicroCompact（SDK 内置算法）**：
          - ``add_tool_result`` 入口：单条工具结果超
            ``microcompact_single_result_max_tokens`` 立即截断（"防爆炸"）。
          - ``compact_if_needed`` 入口：清空早期、白名单内的工具结果正文。
            如果清后已 < 阈值就不进 Layer 2（省一次切分）。
          *白名单* ``microcompact_tools`` 必须由应用层提供。

        Layer 2. **CompactionPolicy（默认 WindowedKeepPolicy）**：
          - 默认从对话尾向前扩，按 (min_keep_tokens, min_keep_text_messages) 双下限
            和 max_keep_tokens 上限三维度切窗口。
          - 必须返回 ``CompactionSplit(kept, dropped)``；
            SDK 内部强制叠加 ``ensure_tool_pair_integrity`` 兜底（API 硬约束）。

        Layer 3. **DropSummarizer（应用层注入，可选）**：
          - 对 Layer 2 的 ``dropped`` 做 LLM 脉络摘要，产物 ``role=system`` 消息
            插入到 ``kept`` 之前。
          - 默认 None = 不摘要，``dropped`` 直接抛弃。

        Layer 4. **PostCompact 回注**：
          - 压缩成功后，按顺序调每个 Source.collect() 收集"必须回注"的状态片段
            （最近读过的文件、当前激活的技能、当前 plan 状态等）。

        Args:
            raw_log_backend:        原始日志后端（None = 不持久化；与 db_path 互斥）
            db_path:                SQLite 快捷方式：等价于
                                    ``raw_log_backend=SQLiteRawLogBackend(db_path=...)``。
                                    禁止 ``":memory:"``。
            session_mode:           "multi_turn"（默认）| "single_turn"
            compaction_policy:      自定义 CompactionPolicy（None = WindowedKeepPolicy）
            token_estimator:        Token 估算器（None = CharBasedTokenEstimator chars/4.0；
                                    中文/代码场景建议注入 TiktokenEstimator 等真实 tokenizer
                                    实现，否则压缩触发判据与实际 LLM token 差可达 ~2x）
            drop_summarizer:        被丢弃消息的脉络摘要策略（None = 不摘要，默认）
            microcompact_tools:     可清旧结果的工具白名单（None / 空集 = 不启用清理）
            microcompact_keep_recent: 预清理时保留最近 N 条
            microcompact_single_result_max_tokens: 单条工具结果上限
            post_compact_sources:   PostCompactSource 列表（None / 空 = 不启用回注）
            post_compact_token_budget: 回注总 token 预算
            working_memory_backend: 工作记忆持久化后端（None = 纯内存，不持久化）

        Raises:
            ValueError: 同时传 ``raw_log_backend`` 与 ``db_path``。
        """
        if raw_log_backend is not None and db_path is not None:
            raise ValueError(
                "AgentBuilder.memory: cannot specify both raw_log_backend and db_path; "
                "use db_path for the SQLite shortcut, "
                "or raw_log_backend to inject any RawLogBackend instance."
            )
        self._raw_log_backend = raw_log_backend
        self._db_path = db_path
        self._session_mode = session_mode
        self._compaction_policy = compaction_policy
        self._token_estimator = token_estimator
        self._drop_summarizer = drop_summarizer
        self._microcompact_tools = microcompact_tools
        self._microcompact_keep_recent = microcompact_keep_recent
        self._microcompact_single_result_max_tokens = microcompact_single_result_max_tokens
        self._post_compact_sources = post_compact_sources
        self._post_compact_token_budget = post_compact_token_budget
        self._working_memory_backend = working_memory_backend
        return self

    # ── Observability ──

    def observability(
        self,
        *,
        audit: Any = _UNSET,
        tracer: Any = _UNSET,
        metrics: Any = _UNSET,
        log: Any = _UNSET,
        log_level: LogLevel | None = None,
        trace_level: TraceLevel | None = None,
        sanitizer: Any = None,
        hooks: Any | None = None,
    ) -> "AgentBuilder":
        """设置可观测配置（完全显式语义）。

        调用语义：
          不调用 .observability()                   — 全关（字段保持 _UNSET → build 时解析为 False）
          .observability() 不传参                    — 全关（参数全为 _UNSET → False）
          .observability(log="mem", audit="mem")    — 精细控制（传了的开，没传的关）

        每个 Backend 参数支持四种值：
          _UNSET（默认）  — 不传该参数，build 时解析为 False（关闭）
          False           — 显式关闭（audit 除外，HC4 不可关闭）
          "mem"           — 使用 SDK 内置 InMemory 后端
          Backend 实例    — 使用自定义后端

        Args:
            audit:       AuditBackend 实例 / False / "mem"（HC4：传 False 降级为 InMemory + WARN）
            tracer:      TracerBackend 实例 / False / "mem"
            metrics:     MetricsBackend 实例 / False / "mem"
            log:         LoggerBackend 实例 / False / "mem"
            log_level:   日志级别（默认 INFO）
            trace_level: 追踪粒度（默认 SUMMARY）
            sanitizer:   脱敏器实例 / None
            hooks:       AgentHooks 实例（None = 自动构建 ObservabilityHooksAdapter）
        """
        self._audit = audit
        self._tracer = tracer
        self._metrics = metrics
        self._log = log
        if log_level is not None:
            self._log_level = log_level
        if trace_level is not None:
            self._trace_level = trace_level
        if sanitizer is not None:
            self._sanitizer = sanitizer
        if hooks is not None:
            self._hooks = hooks
        return self

    def hooks(self, hooks: Any) -> "AgentBuilder":
        """设置 Loop Hooks（等价于 .observability(hooks=...)）。"""
        self._hooks = hooks
        return self

    # ── Build ──

    def build(self) -> "Agent":
        """构建单个 Agent 实例（向后兼容入口）。

        语义等价于 ``self.build_blueprint().materialize()``——
        通过 Blueprint 中转，SDK 内部只维护一条组装路径。
        """
        return self.build_blueprint().materialize()

    def build_blueprint(self) -> "AgentBlueprint":
        """构建 AgentBlueprint（多 session 场景的入口）。

        产出后可通过 ``blueprint.materialize()`` 多次实例化 Agent，
        每个 Agent 拥有独立 Memory / Hooks，其余共享。

        组装顺序（6 阶段）：
          0. 前置校验
          1. 基础设施（Behavior + Observability）— 零依赖，最先就绪
          2. 能力层（ToolRegistry + HarnessExecutor）— hooks 在注册前注入
          3. 高层注册（SkillRegistry / SubAgentRegistry）— 依赖 tool_registry + audit_log
          4. Cost 计算 + memory_factory 闭包
          5. 打包 AgentBlueprint（无 AgentLoop 构造，materialize 时才建）
        """
        from .agent import Agent  # noqa: F401  — 类型别名一致性引用
        from .agent.blueprint import AgentBlueprint

        # ── 0. 前置校验 ─────────────────────────────────────────────────────
        if self._identity is None:
            raise ValueError("AgentBuilder: 必须调用 .identity() 设置 Identity")
        if self._llm_client is None:
            raise ValueError("AgentBuilder: 必须调用 .llm() 设置 LLM 客户端")

        agent_id = self._identity.agent_id

        # ── 1. 基础设施（无外部依赖，最先创建）───────────────────────────────
        execution_limits, hitl_controller, error_policy = self._build_behavior_defaults()
        audit_log, hooks = self._build_observability_and_hooks(agent_id)

        # ── 2. 工具层（hooks 已就位，工具注册事件可被审计）───────────────────
        logger.info("────────────────── [PANDAREN] TOOLS REGISTER  [%s] ──────────────────", agent_id)
        tool_registry, harness_executor = self._build_tool_layer(
            audit_log, hooks
        )

        # ── 3. Skill 注册 ────────────────────────────────────────────────
        logger.info("────────────────── [PANDAREN] SKILLS REGISTER [%s] ──────────────────", agent_id)
        skill_registry = self._resolve_skill_registry(audit_log, tool_registry)

        # ── 4. 子 Agent 注册 ─────────────────────────────────────────────
        self.with_default_sub_agents()  # 幂等，仅首调用生效
        logger.info("────────────────── [PANDAREN] SUB-AGENTS REGISTER [%s] ──────────────────", agent_id)
        agent_registry = self._resolve_agent_registry(audit_log, tool_registry)

        # ── 5. Memory 工厂 + 通用每步停机守卫（应用层注入，SDK 不知停机理由）──────
        memory_factory = self._build_memory_factory(skill_registry=skill_registry)
        step_guard = self._step_guard

        # ── 6. 组装 Blueprint（materialize 时才构造 Memory / AgentLoop / Agent）─
        blueprint = AgentBlueprint(
            identity=self._identity,
            llm_client=self._llm_client,
            llm_settings=self._llm_settings,
            tool_registry=tool_registry,
            skill_registry=skill_registry,
            agent_registry=agent_registry,
            permission_guard=self._permission_guard,
            hitl_controller=hitl_controller,
            harness_executor=harness_executor,
            audit_log=audit_log,
            execution_limits=execution_limits,
            error_policy=error_policy,
            step_guard=step_guard,
            context_window_budget=self._context_window_budget,
            system_prompt=self._system_prompt,
            stream=self._stream,
            memory_factory=memory_factory,
            hooks_template=hooks,
        )

        skill_count = skill_registry.skill_count() if skill_registry else 0
        agent_count = agent_registry.agent_count() if agent_registry else 0
        logger.info(
            "✓ AgentBlueprint 构建完成 [%s]: tools=%d, skills=%d, agents=%d",
            agent_id,
            len(tool_registry.list_tools()),
            skill_count,
            agent_count,
        )
        return blueprint

    # ════════════════════════════════════════════════
    #  build() 内部阶段方法
    # ════════════════════════════════════════════════

    def _build_behavior_defaults(self) -> tuple[ExecutionLimits, HITLController, ErrorPolicy]:
        """Phase 1a: Behavior 默认值兜底。无外部依赖。"""
        return (
            self._execution_limits or ExecutionLimits(),
            self._hitl_controller or HITLController(),
            self._error_policy or ErrorPolicy(),
        )

    def _build_observability_and_hooks(self, agent_id: str) -> tuple[AuditLog, Any]:
        """Phase 1b: 构建 Observability + Hooks。

        前移到工具注册之前，确保后续所有注册事件都能被审计/hook 捕获。
        依赖：仅 agent_id + builder 自身配置字段。
        """
        from .hook.hooks import CompositeAgentHooks

        obs_config = self._build_obs_config()
        # SDK 不计价：traces 的 llm_call span 只记 token/命中等**事实**，不再挂金额 cost_usd。
        # 费用由应用层从 tokens + 价格表自算（运行时停机走 StepGuard、看板走 cost_of_call）。
        obs_provider = ObservabilityProvider(
            obs_config,
            agent_id=agent_id,
        )
        audit_log = obs_provider.audit_log

        # 组合模式：ObservabilityHooksAdapter（框架内置） + 用户自定义 hooks 共存
        hooks = CompositeAgentHooks()
        hooks.add(obs_provider.hooks_adapter)  # 底座：logs.md / traces.md / metrics.md
        if self._hooks is not None:
            hooks.add(self._hooks)              # 顶层：SkillAwareHooks 等应用层 hooks
        return audit_log, hooks

    def _build_tool_layer(
        self, audit_log: AuditLog, hooks: Any
    ) -> tuple[ToolRegistry, HarnessExecutor]:
        """Phase 2: 组装 Tool 层。

        内部分 5 步：
          A. 创建 ToolRegistry（带预算配置）+ 早期 hooks 注入
          B. 内置工具注册（Phase 1：不依赖 Skill/Agent）
          C. SDK 内置通用工具注册（glob / grep / read_file / write_file / edit_file / bash / time）
          D. 用户工具注册
          E. HarnessExecutor 创建（包裹已完成注册的 registry）
        """
        from .tool.exposure.budget import (
            ToolBudget as _ToolBudget,
            DEFAULT_MAX_ALWAYS_COUNT,
            DEFAULT_MAX_DISCOVERED,
        )
        from .tool.builtin import SearchToolFactory, PlanToolFactory
        from .constants import DEFAULT_TOOL_SCHEMA_RATIO

        # ─ A. 创建 ToolRegistry + 早期 hooks 注入 ─
        tool_budget = _ToolBudget(
            budget_ratio=self._tool_budget_ratio if self._tool_budget_ratio is not None else DEFAULT_TOOL_SCHEMA_RATIO,
            max_always_count=self._tool_max_always_count if self._tool_max_always_count is not None else DEFAULT_MAX_ALWAYS_COUNT,
            max_discovered_per_session=self._tool_max_discovered if self._tool_max_discovered is not None else DEFAULT_MAX_DISCOVERED,
        )
        tool_registry = create_tool_registry(budget=tool_budget)
        tool_registry.set_hooks(hooks)  # 早期注入：后续注册都能触发 on_tool_register

        # ─ B. 内置工具注册（Phase 1：不依赖 SkillRegistry / SubAgentRegistry）─
        phase1_factories = [
            SearchToolFactory(),
            PlanToolFactory(
                plan_dir=self._plan_dir,
            ),
        ]
        tool_registry.register_builtin_factories(phase1_factories)

        # ─ D. SDK 内置通用工具注册（glob / grep / read_file / write_file / edit_file / bash / time）─
        # SDK 自带的基础工具，始终可用，无需应用层手动注入
        from .tools import get_builtin_tools
        for tool in get_builtin_tools():
            tool_registry.register_tool(tool)

        # ─ E. 用户工具注册 ─
        for tool in self._tool_list:
            tool_registry.register_tool(tool, skip_if_exists=True)

        # ─ F. HarnessExecutor（behavior 层包裹 capability 层）─
        harness_executor = HarnessExecutor(
            tool_registry,
            feedback_providers=self._tool_feedback_providers,
        )
        harness_executor._audit_log = audit_log
        harness_executor.set_hooks(hooks)
        for tool in self._tool_list:
            if tool.circuit_breaker:
                harness_executor.register_circuit_breaker(tool.full_name, tool.circuit_breaker)

        return tool_registry, harness_executor

    def _build_memory_factory(
        self, skill_registry: Any | None = None,
    ) -> Callable[[], Memory]:
        """Phase 3a: 组装 Memory 构造参数快照 → 返回工厂闭包。

        每次调用工厂返回一个全新的 Memory 实例，供 Blueprint.materialize()
        为每个 session 独立分配 STM / WorkingMemory。共享的 backend 引用
        （raw_log_backend / working_memory_backend）由后端内部按 session_id 分片。

        SDK 默认切分策略：WindowedKeepPolicy（三维度窗口）。
        应用层若注入 ``compaction_policy=`` 则覆盖默认。
        ``drop_summarizer`` 默认 None（不摘要），应用层可注入 LLM-driven 实现。
        Memory.compact_if_needed 的四层管线（MicroCompact → CompactionPolicy.split
        → DropSummarizer → PostCompact）由 Memory Facade 自身编排。

        ``raw_log_backend`` / ``db_path`` 互斥规则已在 ``.memory()`` 入口校验，
        本方法只负责把最终的 backend 实例注入 Memory kwargs：
          - 显式传 ``raw_log_backend`` → 直接使用
          - 显式传 ``db_path``         → 这里构造 ``SQLiteRawLogBackend(db_path=...)``
          - 都不传                     → ``raw_log_backend=None``（不持久化）

        Args:
            skill_registry: 由 ``_resolve_skill_registry`` 返回；
                            ActiveSkillsSource 需要它读取激活技能。
                            None = 应用没启用 skill 层 / 还没创建 → 该 source 空跑。

        Returns:
            factory: 无参 callable，每次调用返回一个新的 Memory 实例。
        """
        # raw_log_backend 与 db_path 二选一（互斥校验已在 .memory() 中完成）
        # ★ 只解析一次，多次 materialize 共享同一个 backend 实例
        raw_log_backend: RawLogBackend | None
        if self._raw_log_backend is not None:
            raw_log_backend = self._raw_log_backend
        elif self._db_path is not None:
            raw_log_backend = SQLiteRawLogBackend(db_path=self._db_path)
        else:
            raw_log_backend = None

        # Memory 构造参数快照（闭包捕获，多次 materialize 复用）
        memory_kwargs: dict = dict(
            system_prompt=self._system_prompt,
            # ── 持久化 ──
            raw_log_backend=raw_log_backend,
            session_mode=self._session_mode,
            # ── 切分策略（None → Memory 内默认 WindowedKeepPolicy）──
            compaction_policy=self._compaction_policy,
            # ── 摘要扩展点 ──
            drop_summarizer=self._drop_summarizer,
            # ── 工作记忆持久化 ──
            working_memory_backend=self._working_memory_backend,
            # ── PostCompact ActiveSkillsSource 需要的引用 ──
            skill_registry=skill_registry,
        )

        # Token 估算器（None → Memory 内默认 CharBasedTokenEstimator）
        if self._token_estimator is not None:
            memory_kwargs["token_estimator"] = self._token_estimator

        # MicroCompact 参数（None 表示用 Memory 内默认）
        if self._microcompact_tools is not None:
            memory_kwargs["microcompact_tools"] = self._microcompact_tools
        if self._microcompact_keep_recent is not None:
            memory_kwargs["microcompact_keep_recent"] = self._microcompact_keep_recent
        if self._microcompact_single_result_max_tokens is not None:
            memory_kwargs["microcompact_single_result_max_tokens"] = (
                self._microcompact_single_result_max_tokens
            )

        # PostCompact 参数
        if self._post_compact_sources is not None:
            memory_kwargs["post_compact_sources"] = self._post_compact_sources
        if self._post_compact_token_budget is not None:
            memory_kwargs["post_compact_token_budget"] = self._post_compact_token_budget

        # 压缩阈值：优先用 ContextWindowBudget 的 conversation 配额
        if self._context_window_budget is not None:
            memory_kwargs["compact_threshold"] = (
                self._context_window_budget.get_slot_tokens("conversation")
            )

        def factory() -> Memory:
            return Memory(**memory_kwargs)

        return factory

    # ════════════════════════════════════════════════
    #  内部解析方法
    # ════════════════════════════════════════════════

    def _build_obs_config(self) -> ObservabilityConfig:
        """将 Builder 收集的四态值解析为 ObservabilityConfig。

        解析规则（完全显式语义）：
          log / tracer / metrics：
            _UNSET / None → False（关闭）
            False         → False（关闭）
            "mem"         → "mem"（InMemory）
            实例           → 原样传入
          audit（HC4 特殊处理）：
            _UNSET        → None（静默使用 InMemory，不 warning）
            None          → None（同上）
            False         → False（HC4 降级为 InMemory + WARNING）
            "mem"         → "mem"（显式 InMemory，无 warning）
            实例           → 原样传入
        """
        def _resolve(val: Any) -> Any:
            """log/tracer/metrics：_UNSET/None 都映射为 False（关闭）。"""
            return False if (val is _UNSET or val is None) else val

        def _resolve_audit(val: Any) -> Any:
            """audit HC4：_UNSET/None → None（静默 InMemory），其余原样。"""
            return None if (val is _UNSET or val is None) else val

        return ObservabilityConfig(
            log_backend=_resolve(self._log),
            log_level=self._log_level,
            tracer_backend=_resolve(self._tracer),
            trace_level=self._trace_level,
            metrics_backend=_resolve(self._metrics),
            audit_backend=_resolve_audit(self._audit),
            sanitizer=self._sanitizer,
        )

    def _resolve_skill_registry(self, audit_log: AuditLog, tool_registry: ToolRegistry) -> Any:
        """解析 Skill 配置，构建 SkillRegistry。"""
        if not self._skill_list:
            return None

        from .skill.registry import SkillRegistry

        registry = SkillRegistry(
            audit_log=audit_log,
        )

        for skill in self._skill_list:
            try:
                registry.register_skill(skill)
            except Exception as e:
                logger.warning("Skill 注册失败（跳过）: %s", e)

        # 内置工具统一注册（Phase 2）
        from .tool.builtin import SkillToolFactory
        tool_registry.register_builtin_factories([SkillToolFactory()])

        return registry

    def _resolve_agent_registry(self, audit_log: AuditLog, tool_registry: ToolRegistry) -> Any:
        """解析 Agent Registry：从蓝图构建所有子 Agent 并注册（仅一层，不嵌套）。

        工具池修复：子 Agent 蓝图的 tools 字段按名称从父级 tool_registry 过滤，
        而非从预存的 self._tool_list 快照（当时基础工具尚未注册）。
        """
        if not self._sub_agent_blueprints:
            return None

        from .sub_agent import SubAgentRegistry

        registry = SubAgentRegistry(
            tool_registry=tool_registry,
            audit_log=audit_log,
        )

        # 使用已完成注册的父级 tool_registry 作为工具池，确保基础工具可见
        full_tools_pool = tool_registry.list_tools()
        summary_parts: list[str] = []

        for bp, bp_llm_client, _tools_pool, skills_pool in self._sub_agent_blueprints:
            try:
                sub_agent = self._build_sub_agent_from_blueprint(
                    bp=bp,
                    llm_client=bp_llm_client,
                    tools_pool=full_tools_pool,
                    skills_pool=skills_pool,
                    audit_log=audit_log,
                )
                registry.register(sub_agent)
                # 从蓝图直接计算 tool/skill 数量
                if not bp.tools:
                    t_cnt = 0
                elif "*" in bp.tools:
                    t_cnt = len(full_tools_pool)
                else:
                    pool_names = {t.name for t in full_tools_pool if hasattr(t, "name")}
                    t_cnt = len([n for n in bp.tools if n in pool_names])
                if not bp.skills:
                    s_cnt = 0
                elif "*" in bp.skills:
                    s_cnt = len(skills_pool) if skills_pool else 0
                else:
                    pool_names = {s.name for s in (skills_pool or []) if hasattr(s, "name")}
                    s_cnt = len([n for n in bp.skills if n in pool_names])
                summary_parts.append(f"{bp.agent_id}(tools={t_cnt}, skills={s_cnt})")
            except Exception as e:
                logger.warning("Sub-agent 蓝图构建失败（跳过）: %s → %s", bp.agent_id, e)

        if summary_parts:
            logger.info("子 Agent 蓝图加载完成: %s", ", ".join(summary_parts))

        # 内置工具统一注册
        from .tool.builtin import AgentToolFactory
        tool_registry.register_builtin_factories([AgentToolFactory()])

        return registry

    def _build_sub_agent_from_blueprint(
        self,
        bp: Any,
        llm_client: LLMClient,
        tools_pool: list,
        skills_pool: list,
        audit_log: AuditLog,
    ) -> Any:
        """从蓝图构建一层子 Agent（不嵌套，bp.sub_agents 为权限声明）。

        工具过滤（最小权限）：
          bp.tools 为空 tuple    → 不传入任何工具
          bp.tools == ("*",)     → 继承 tools_pool 全部
          bp.tools == (name,...) → 从 tools_pool 中按名称过滤

        Skill 过滤（与 Tool 对称）：
          bp.skills 为空 tuple   → 不从父级继承 Skill
          bp.skills == ("*",)    → 继承 skills_pool 全部
          bp.skills == (name,...)→ 从 skills_pool 中按名称过滤
        """
        # ── 工具过滤 ──
        if not bp.tools:
            filtered_tools: list = []
        elif "*" in bp.tools:
            filtered_tools = list(tools_pool)
        else:
            pool_by_name = {t.name: t for t in tools_pool if hasattr(t, "name")}
            filtered_tools = [pool_by_name[name] for name in bp.tools if name in pool_by_name]
            missing = [name for name in bp.tools if name not in pool_by_name]
            if missing:
                logger.warning(
                    "Agent '%s' 声明的工具在父级工具池中未找到（已忽略）: %s",
                    bp.agent_id, missing,
                )

        # ── Skill 过滤 ──
        filtered_skills: list = []
        if not bp.skills:
            pass
        elif "*" in bp.skills:
            filtered_skills = list(skills_pool)
        else:
            pool_by_name = {s.name: s for s in skills_pool if hasattr(s, "name")}
            filtered_skills = [pool_by_name[name] for name in bp.skills if name in pool_by_name]
            missing = [name for name in bp.skills if name not in pool_by_name]
            if missing:
                logger.warning(
                    "Agent '%s' 声明的 Skill 在父级 Skill 池中未找到（已忽略）: %s",
                    bp.agent_id, missing,
                )

        logger.info(
            "[sub-agent skills] agent_id='%s' → 最终 Skill 数量=%d, 列表=%s",
            bp.agent_id, len(filtered_skills),
            [getattr(s, 'name', '?') for s in filtered_skills],
        )

        # ── 构建子 Agent ──
        builder = (
            AgentBuilder()
            .identity(
                agent_id=bp.agent_id,
                agent_name=bp.agent_name,
                when_to_use=bp.when_to_use,
                trust_level=bp.trust_level,
                sensitive_permissions=bp.sensitive_permissions,
            )
            .llm(llm_client)
            .tools(filtered_tools)
            .skills(filtered_skills)
            .system_prompt(bp.system_prompt)
            .behavior(
                step_timeout=self._execution_limits.step_timeout if self._execution_limits else DEFAULT_STEP_TIMEOUT,
                total_timeout=self._execution_limits.total_timeout if self._execution_limits else DEFAULT_TOTAL_TIMEOUT,
                step_guard=self._step_guard,  # 子 Agent 继承父级停机守卫
                # 子 Agent 也继承反馈通道：它同样会写文件，没有理由不受同一把尺子约束。
                # 注意子 Agent **不继承** .hooks()，故 provider 的 on_run_end 不会因子 Agent
                # 收尾而触发 —— 父 run 的熔断计数不会被子 Agent 提前清掉。
                tool_feedback_providers=self._tool_feedback_providers,
            )
        )
        # 继承父级的上下文窗口预算，确保子 Agent 与父 Agent 使用一致的 compact_threshold
        if self._context_window_budget is not None:
            builder = builder.context_budget(
                context_window=self._context_window_budget.context_window,
                system_prompt_ratio=self._context_window_budget.system_prompt_ratio,
                tool_schema_ratio=self._context_window_budget.tool_schema_ratio,
                conversation_ratio=self._context_window_budget.conversation_ratio,
                recall_ratio=self._context_window_budget.recall_ratio,
            )
        # 继承父级的 Token 估算器：与 context_budget 同理——阈值一致了，
        # 尺子也必须一致，否则子 Agent 的压缩触发判据退回 chars/4.0 量纲。
        if self._token_estimator is not None:
            builder = builder.memory(token_estimator=self._token_estimator)
        builder = (
            builder
            .observability(
                audit=self._audit if self._audit is not _UNSET else None,
                tracer=self._tracer if self._tracer is not _UNSET else False,
                metrics=self._metrics if self._metrics is not _UNSET else False,
                log=self._log if self._log is not _UNSET else False,
            )
            
        )

        # ── 子 Agent LLM 调参：默认继承父级 settings，蓝图显式字段逐字段覆盖；
        #    model 顶层字段只覆盖 target_model（其余继续继承）。──
        # 走 builder._llm_settings 而非 public .llm_settings()：后者无 target_model
        # 参数（子 Agent 需经 LLMRouter 路由到指定模型）。
        sub_settings: ModelSettings | None = self._llm_settings          # 1. 父级作底
        if bp.llm_settings is not None:                                  # 2. 蓝图字段覆盖
            override_kw = {
                f.name: getattr(bp.llm_settings, f.name)
                for f in dataclasses.fields(bp.llm_settings)
                if getattr(bp.llm_settings, f.name) is not None
            }
            sub_settings = dataclasses.replace(sub_settings, **override_kw) if sub_settings is not None \
                else bp.llm_settings
        if bp.model:                                                     # 3. model → target_model
            sub_settings = dataclasses.replace(sub_settings, target_model=bp.model) if sub_settings is not None \
                else ModelSettings(target_model=bp.model)
        if sub_settings is not None:
            builder._llm_settings = sub_settings

        # 返回 AgentBlueprint 而非 Agent 实例：registry 委派时每次 materialize
        # 全新实例（独立 Memory / Hooks）→ 多会话并发隔离（见 sub_agent/registry.py）。
        return builder.build_blueprint()
