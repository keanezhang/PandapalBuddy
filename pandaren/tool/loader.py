"""pandaren/tool/loader.py — ToolLoader（从文件系统加载 Tool）

辅助工具，支持从 Markdown 文件加载 Tool 定义（YAML Frontmatter + Markdown body）。
使用统一的 schema_inference 模块。
"""

from __future__ import annotations

import importlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable

from .definition.tool import Tool
from .definition.tool_policy import ToolPolicy
from .types import ToolTier, SensitivityLevel
from .schema_inference import infer_input_schema
from ..identity.models import SensitivePermission, TrustLevel

logger = logging.getLogger("pandaren.tool.loader")


# ════════════════════════════════════════════════
#  Frontmatter 解析
# ════════════════════════════════════════════════

def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """解析 YAML Frontmatter（简易实现）。"""
    pattern = r"^---\s*\n(.*?)\n---\s*\n(.*)$"
    match = re.match(pattern, text, re.DOTALL)
    if not match:
        return {}, text

    fm_text = match.group(1)
    body = match.group(2)

    frontmatter: dict[str, str] = {}
    for line in fm_text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            frontmatter[key.strip()] = value.strip()

    return frontmatter, body


# ════════════════════════════════════════════════
#  类型解析辅助
# ════════════════════════════════════════════════

_TIER_MAP: dict[str, ToolTier] = {
    "always": ToolTier.ALWAYS,
    "deferred": ToolTier.DEFERRED,
}

_SENSITIVITY_MAP: dict[str, SensitivityLevel] = {
    "low": SensitivityLevel.LOW,
    "medium": SensitivityLevel.MEDIUM,
    "high": SensitivityLevel.HIGH,
    "critical": SensitivityLevel.CRITICAL,
}

_TRUST_MAP: dict[str, TrustLevel] = {
    "external": TrustLevel.EXTERNAL,
    "sub_agent": TrustLevel.SUB_AGENT,
    "orchestrator": TrustLevel.ORCHESTRATOR,
}


def _parse_bool(value: str, default: bool = False) -> bool:
    return value.lower() in ("true", "yes", "1")


def _parse_sensitive_permission(raw: str) -> SensitivePermission | None:
    valid = {e.value: e for e in SensitivePermission}
    result = valid.get(raw.strip().lower())
    if result is None and raw.strip():
        logger.warning("未知的 sensitive_permission 值 '%s'，忽略", raw)
    return result


def _import_executor(module_path: str) -> Callable:
    """动态导入 executor 函数。格式: "module.path:function_name"。"""
    if ":" not in module_path:
        raise ValueError(
            f"executor 格式错误: '{module_path}'，"
            f"期望 'module.path:function_name' 格式"
        )
    module_str, func_name = module_path.rsplit(":", 1)
    module_str = module_str.strip()
    func_name = func_name.strip()

    if not module_str or not func_name:
        raise ValueError(f"executor 格式错误: '{module_path}'")

    module = importlib.import_module(module_str)
    executor = getattr(module, func_name)

    if not callable(executor):
        raise ValueError(
            f"executor '{module_path}' 不是可调用对象，类型: {type(executor).__name__}"
        )
    return executor


def _parse_input_schema(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        schema = json.loads(raw)
        if isinstance(schema, dict):
            return schema
        return None
    except json.JSONDecodeError:
        return None


# ════════════════════════════════════════════════
#  公开 API
# ════════════════════════════════════════════════

def load_tool_from_file(path: str | Path) -> Tool:
    """从 Markdown 文件加载 Tool 定义。"""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Tool 文件不存在: {file_path}")

    text = file_path.read_text(encoding="utf-8")
    frontmatter, body = _parse_frontmatter(text)

    # ── 必填字段 ──
    name = frontmatter.get("name", "").strip() or file_path.stem

    description = frontmatter.get("description", "").strip()
    if not description:
        raise ValueError(f"Tool 文件 '{file_path}' 缺少 description 字段")

    executor_path = frontmatter.get("executor", "").strip()
    if not executor_path:
        raise ValueError(f"Tool 文件 '{file_path}' 缺少 executor 字段")
    executor = _import_executor(executor_path)

    # ── 可选字段 ──
    tier_str = frontmatter.get("tier", "deferred").strip().lower()
    tier = _TIER_MAP.get(tier_str, ToolTier.DEFERRED)

    sens_str = frontmatter.get("sensitivity", "low").strip().lower()
    sensitivity = _SENSITIVITY_MAP.get(sens_str, SensitivityLevel.LOW)

    is_reversible = _parse_bool(frontmatter.get("is_reversible", "true"), default=True)
    audit_required = _parse_bool(frontmatter.get("audit_required", "false"), default=False)
    is_idempotent = _parse_bool(frontmatter.get("is_idempotent", "true"), default=True)
    halt_on_failure = _parse_bool(frontmatter.get("halt_on_failure", "false"), default=False)

    # sensitive_permission
    permission_raw = frontmatter.get("permission", "").strip()
    sensitive_permission = _parse_sensitive_permission(permission_raw) if permission_raw else None

    # trust_level
    trust_str = frontmatter.get("trust_level", "sub_agent").strip().lower()
    trust_level = _TRUST_MAP.get(trust_str, TrustLevel.SUB_AGENT)

    # max_calls_per_turn
    max_calls: int | None = None
    raw_max = frontmatter.get("max_calls_per_turn", "").strip()
    if raw_max:
        try:
            max_calls = int(raw_max)
        except ValueError:
            pass

    # when_to_use
    when_to_use = frontmatter.get("when_to_use", "").strip()
    body_text = body.strip()
    if body_text:
        when_to_use = f"{when_to_use}\n\n{body_text}" if when_to_use else body_text
    if not when_to_use:
        raise ValueError(f"Tool 定义文件 '{file_path}' 缺少 when_to_use 字段")

    # namespace
    namespace = frontmatter.get("namespace", "").strip() or None

    # tags
    tags_raw = frontmatter.get("tags", "").strip()
    tags = tuple(t.strip() for t in tags_raw.split(",") if t.strip()) if tags_raw else ()

    # input_schema
    input_schema_raw = frontmatter.get("input_schema", "").strip()
    input_schema = _parse_input_schema(input_schema_raw)
    if input_schema is None:
        input_schema = infer_input_schema(executor)

    # 构建 ToolPolicy
    policy = ToolPolicy(
        sensitivity=sensitivity,
        is_reversible=is_reversible,
        audit_required=audit_required,
        is_idempotent=is_idempotent,
        halt_on_failure=halt_on_failure,
        trust_level_required=trust_level,
        sensitive_permission=sensitive_permission,
        max_calls_per_turn=max_calls,
    )

    return Tool(
        name=name,
        description=description,
        executor=executor,
        input_schema=input_schema,
        tier=tier,
        when_to_use=when_to_use,
        namespace=namespace,
        tags=tags,
        policy=policy,
    )


def load_tools_from_dir(
    directory: str | Path,
    pattern: str = "*.md",
    recursive: bool = True,
) -> list[Tool]:
    """从目录批量加载 Tool 定义。

    Fail-Safe：单个文件加载失败时跳过。
    """
    dir_path = Path(directory)
    if not dir_path.is_dir():
        logger.warning("Tool 目录不存在: %s", dir_path)
        return []

    glob_mode = f"**/{pattern}" if recursive else pattern
    tools: list[Tool] = []
    for file_path in sorted(dir_path.glob(glob_mode)):
        if file_path.is_file():
            try:
                tool_def = load_tool_from_file(file_path)
                tools.append(tool_def)
            except Exception as e:
                logger.warning("Tool 加载失败（跳过）: %s → %s", file_path, e)

    # logger.info("从 %s 加载了 %d 个 Tool", dir_path, len(tools))
    return tools
