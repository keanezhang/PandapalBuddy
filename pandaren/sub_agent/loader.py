"""pandaren/agent/loader.py — AgentLoader（从文件系统加载 SubAgentBlueprint）

辅助工具，不是 SubAgentRegistry 的核心职责。
支持从 Markdown 文件加载 Agent 蓝图（YAML Frontmatter + Markdown body）。

与 SkillLoader 对称设计。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .models import SubAgentBlueprint, SubAgentSource
from ..llm.types import ModelSettings

if TYPE_CHECKING:
    from ..identity.models import TrustLevel

logger = logging.getLogger("pandaren.sub_agent.loader")


# ModelSettings 全量字段白名单（frontmatter 顶层展开，逐字段收集）
# 不含 target_model —— 它由顶层 `model` 字段映射，避免双入口。
_LLM_SETTINGS_FIELDS: frozenset[str] = frozenset({
    "temperature",
    "max_tokens",
    "top_p",
    "frequency_penalty",
    "presence_penalty",
    "stop",
    "seed",
    "response_format",
    "tool_choice",
    "parallel_tool_calls",
    "include_usage",
    "reasoning",
    "extra_body",
    "extra_headers",
    "extra_query",
})


def load_agent_from_file(
    path: str | Path,
    source: SubAgentSource = SubAgentSource.DIRECTORY,
) -> SubAgentBlueprint:
    """从 Markdown 文件加载 SubAgentBlueprint。

    文件格式（YAML Frontmatter + Markdown body）：
        ---
        agent_id: reviewer
        agent_name: 代码审查专家
        when_to_use: 审查代码质量，发现潜在问题，给出改进建议
        trust_level: sub_agent
        permissions: code:read, code:review
        tools: grep_search, read_file       # 工具名列表；"*" = 继承全部；空 = 不用工具
        skills: code-review                 # Skill 名列表；"*" = 继承全部；空 = 不从父级继承
        sub_agents: reviewer, tester        # 可委派的子 Agent 名；"*" = 可委派全部；空 = 不委派
        ---

        你是一位经验丰富的代码审查专家...（Markdown 正文 = system prompt）

    Args:
        path: Agent 蓝图文件路径。
        source: 蓝图来源（默认 DIRECTORY）。

    Returns:
        解析后的 SubAgentBlueprint 对象。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 文件格式错误（frontmatter 解析失败、when_to_use 缺失、正文为空）。
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Agent 蓝图文件不存在: {file_path}")

    text = file_path.read_text(encoding="utf-8")

    # 解析 YAML Frontmatter
    frontmatter, body = _parse_frontmatter(text)

    if not frontmatter:
        raise ValueError(
            f"Agent 蓝图文件 '{file_path}' 缺少 YAML Frontmatter（--- ... --- 区块）"
        )

    # ── agent_id（必填，缺失时用文件名 stem）──
    agent_id = _as_str(frontmatter.get("agent_id")).strip()
    if not agent_id:
        agent_id = file_path.stem

    # ── agent_name（必填，缺失时等于 agent_id）──
    agent_name = _as_str(frontmatter.get("agent_name")).strip()
    if not agent_name:
        agent_name = agent_id

    # ── when_to_use（必填，缺失 → 抛 ValueError）──
    when_to_use = _as_str(frontmatter.get("when_to_use")).strip()
    if not when_to_use:
        raise ValueError(
            f"Agent 蓝图文件 '{file_path}' 缺少 when_to_use 字段"
        )

    # ── system_prompt（Markdown 正文，为空 → 抛 ValueError）──
    system_prompt = body.strip()
    if not system_prompt:
        raise ValueError(
            f"Agent 蓝图文件 '{file_path}' 正文为空（正文 = Agent 的 system prompt）"
        )

    # ── trust_level（可选，默认 sub_agent）──
    trust_level = _parse_trust_level(
        _as_str(frontmatter.get("trust_level")), file_path,
    )

    # ── sensitive_permissions（可选，格式: ["data_write", "network_call", ...]）──
    sensitive_permissions = _parse_sensitive_permissions(frontmatter.get("permissions"))

    # ── tools（可选，逗号分隔的工具名；"*" = 继承全部；空 = 不用工具）──
    tools = _parse_comma_list(frontmatter.get("tools"))

    # ── skills（可选，逗号分隔的 Skill 名；"*" = 继承全部；空 = 不从父级继承）──
    skills = _parse_comma_list(frontmatter.get("skills"))

    # ── sub_agents（可选，逗号分隔的 agent_id；"*" = 可委派全部；空 = 不委派）──
    sub_agents = _parse_comma_list(frontmatter.get("sub_agents"))

    # ── model（可选，顶层字段 → 构建时映射 ModelSettings.target_model）──
    model = _as_str(frontmatter.get("model")).strip() or None

    # ── llm_settings（可选，顶层展开的 ModelSettings 白名单字段）──
    llm_settings = _parse_llm_settings(frontmatter)

    return SubAgentBlueprint(
        agent_id=agent_id,
        agent_name=agent_name,
        when_to_use=when_to_use,
        system_prompt=system_prompt,
        trust_level=trust_level,
        sensitive_permissions=sensitive_permissions,
        source=source,
        source_path=str(file_path),
        tools=tools,
        skills=skills,
        sub_agents=sub_agents,
        model=model,
        llm_settings=llm_settings,
    )


def load_agents_from_dir(
    directory: str | Path,
    source: SubAgentSource = SubAgentSource.DIRECTORY,
    pattern: str = "*.md",
    recursive: bool = True,
) -> list[SubAgentBlueprint]:
    """从目录批量加载 SubAgentBlueprint。

    AR-FS1 Fail-Safe：单个文件加载失败时跳过，不阻塞其他文件。
    与 SkillLoader.load_skills_from_dir() 对称设计。

    Args:
        directory: 目录路径。
        source: 蓝图来源。
        pattern: 文件匹配模式（默认 "*.md"）。
        recursive: 是否递归扫描子目录，默认 True。

    Returns:
        成功加载的 SubAgentBlueprint 列表。
    """
    dir_path = Path(directory)
    if not dir_path.is_dir():
        logger.warning("Agent 蓝图目录不存在: %s", dir_path)
        return []

    glob_mode = f"**/{pattern}" if recursive else pattern
    blueprints: list[SubAgentBlueprint] = []
    for file_path in sorted(dir_path.glob(glob_mode)):
        if file_path.is_file():
            try:
                bp = load_agent_from_file(file_path, source=source)
                blueprints.append(bp)
            except Exception as e:
                logger.warning(
                    "Agent 蓝图加载失败（跳过）: %s → %s", file_path, e,
                )

    # logger.info(
    #     "从 %s 加载了 %d 个 Agent 蓝图（来源: %s, 递归: %s）",
    #     dir_path, len(blueprints), source.name, recursive,
    # )
    return blueprints


# ════════════════════════════════════════════════
#  内部解析工具
# ════════════════════════════════════════════════

def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """解析 YAML Frontmatter（使用 PyYAML，支持折叠块 `>`、保留块 `|`、列表等完整语法）。

    与 SkillLoader._parse_frontmatter() 对称设计，行为一致。

    Returns:
        (frontmatter_dict, body_text) 二元组。frontmatter 的 value 可能是
        str / list / dict / bool / int / None，调用方需用 ``_as_str()`` 等
        helper 规范化。
    """
    pattern = r"^---\s*\n(.*?)\n---\s*\n(.*)$"
    match = re.match(pattern, text, re.DOTALL)
    if not match:
        # 无 frontmatter，整个文本作为 body
        return {}, text

    fm_text = match.group(1)
    body = match.group(2)

    try:
        parsed = yaml.safe_load(fm_text)
    except yaml.YAMLError as e:
        logger.warning("YAML Frontmatter 解析失败（退回空 dict）: %s", e)
        return {}, body

    if parsed is None:
        return {}, body
    if not isinstance(parsed, dict):
        logger.warning(
            "YAML Frontmatter 顶层不是映射（type=%s），退回空 dict",
            type(parsed).__name__,
        )
        return {}, body

    frontmatter: dict[str, Any] = {str(k): v for k, v in parsed.items()}
    return frontmatter, body


def _as_str(value: Any) -> str:
    """把 YAML 还原的任意值规范化为 str（None → ""，其余走 str(...) 兜底）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _parse_trust_level(raw: str, file_path: Path) -> "TrustLevel":
    """解析 trust_level 字段（大小写不敏感，空值/非法值抛出 ValueError）。

    E4 失败安全：trust_level 无法识别时拒绝创建，不静默降级。
    原因：Identity 是构造时一次性校验，静默降级会掩盖配置错误。
    """
    from ..identity.models import TrustLevel

    if not raw.strip():
        raise ValueError(
            f"Agent 蓝图 '{file_path}' 缺少 trust_level 字段。"
            f"有效值为 {[e.name for e in TrustLevel]}"
        )

    mapping = {
        "orchestrator": TrustLevel.ORCHESTRATOR,
        "sub_agent": TrustLevel.SUB_AGENT,
        "external": TrustLevel.EXTERNAL,
    }
    result = mapping.get(raw.strip().lower())
    if result is None:
        raise ValueError(
            f"Agent 蓝图 '{file_path}' 的 trust_level 值 '{raw}' 不在合法枚举中。"
            f"有效值为 {[e.name for e in TrustLevel]}"
        )
    return result


def _parse_comma_list(raw: Any) -> tuple[str, ...]:
    """解析逗号分隔的字符串列表。

    兼容 YAML 的多种来源：
    - ``None`` / 空串 → ``()``
    - ``str`` → 按逗号切
    - ``list`` / ``tuple`` → 逐项 ``str().strip()``
    """
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple)):
        return tuple(str(t).strip() for t in raw if str(t).strip())
    if isinstance(raw, str):
        if not raw.strip():
            return ()
        return tuple(t.strip() for t in raw.split(",") if t.strip())
    # 其他类型（int/bool 等）不合法，静默退回空
    return ()


def _parse_sensitive_permissions(raw: Any) -> frozenset:
    """解析 permissions 字段（格式: ["data_write", "network_call", ...]）。

    兼容 YAML 还原出的多种形态：
    - ``None`` / 空串 → ``frozenset()``
    - ``str`` → 按逗号切，每项映射到 SensitivePermission
    - ``list`` → 逐项视为枚举值字符串
    """
    from ..identity.models import SensitivePermission

    valid = {e.value: e for e in SensitivePermission}

    # 归一化为 entries: list[str]
    if raw is None:
        return frozenset()
    if isinstance(raw, (list, tuple)):
        entries = [str(e).strip().lower() for e in raw if str(e).strip()]
    elif isinstance(raw, str):
        if not raw.strip():
            return frozenset()
        entries = [e.strip().lower() for e in raw.split(",") if e.strip()]
    else:
        return frozenset()

    perms: list[SensitivePermission] = []
    for entry in entries:
        perm = valid.get(entry)
        if perm is not None:
            perms.append(perm)
        else:
            logger.warning(
                "未知的 SensitivePermission 值（跳过）: '%s'。有效值: %s",
                entry, list(valid.keys()),
            )

    return frozenset(perms)


def _parse_llm_settings(frontmatter: dict[str, Any]) -> "ModelSettings | None":
    """从 frontmatter 顶层解析 ModelSettings 白名单字段。

    逐字段收集非 None 值构造 ModelSettings 对象；
    全部字段都未写 → 返回 None（表示未显式配置，由 builder 决定继承父级）。

    target_model 不在此白名单内——它由顶层 ``model`` 字段映射（避免双入口）。
    """
    subset: dict[str, Any] = {}
    for key in _LLM_SETTINGS_FIELDS:
        value = frontmatter.get(key)
        if value is not None:
            subset[key] = value
    if not subset:
        return None
    return ModelSettings(**subset)
