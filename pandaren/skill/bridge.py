"""pandaren/skill/bridge.py — SkillToolBridge（Action Skill → Tool 转换器）。

职责：
- 将 Action Skill 转化为标准 Tool 对象
- 从 Python 函数签名自动推导 JSON Schema
- 构建安全的 executor 包装（异常捕获、async 适配）
- 生成的 Tool 完全符合 Tool frozen dataclass 契约

设计约束：
- 纯转换器，无状态
- 生成的 Tool 遵循 S4（注册后只读）
- executor 内部 try/except 包裹，永不向上抛异常（E4）
- 脚本加载失败时由调用方处理（SK7 Fail-Safe）
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Callable, get_type_hints

from ..tool.definition.tool import Tool, JsonSchema
from ..tool.definition.context import ToolContext
from ..tool.definition.tool_result import ToolResult
from ..tool.definition.tool_policy import ToolPolicy
from ..tool.types import ToolTier, SensitivityLevel
from .models import Skill
from .script_loader import load_skill_script, resolve_entry_function
from .exceptions import SkillScriptError

logger = logging.getLogger("pandaren.skill.bridge")

# Python 类型 → JSON Schema 类型映射（与 tool/decorator.py 对齐）
_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}

# Action Skill executor 超时（秒）
_DEFAULT_EXECUTION_TIMEOUT: int = 60


class SkillToolBridge:
    """将 Action Skill 转化为标准 Tool 定义（纯转换器，无状态）。

    使用方式（SkillRegistry 内部调用）：
        bridge = SkillToolBridge()
        tool = bridge.create_tool(skill)
        tool_registry.register_tool(tool)
    """

    def __init__(self, execution_timeout: int = _DEFAULT_EXECUTION_TIMEOUT) -> None:
        self._execution_timeout = execution_timeout

    def create_tool(self, skill: Skill) -> Tool:
        """将 Action Skill 转化为 Tool 对象。

        流程：
        1. 加载脚本模块
        2. 解析入口函数
        3. 从函数签名 + docstring 推导 JSON Schema
        4. 构建安全 executor
        5. 组装 Tool frozen dataclass

        Args:
            skill: 必须是 Action Skill（skill.is_action == True）。

        Returns:
            标准 Tool 对象，可直接注册到 ToolRegistry。

        Raises:
            SkillScriptError: 脚本加载失败、函数未找到等。
        """
        if not skill.is_action:
            raise SkillScriptError(
                f"Skill '{skill.name}' 不是 Action Skill（无 script 字段）"
            )

        # 1. 加载脚本模块
        module = load_skill_script(skill.base_path, skill.script)

        # 2. 解析入口函数
        func = resolve_entry_function(module, skill.entry_function)

        # 3. 推导 JSON Schema
        input_schema = self._build_schema(func)

        # 4. 构建 executor
        executor = self._build_executor(func, skill.name)

        # 5. 组装 Tool（DEFERRED 级，不直接注册到 ToolRegistry）
        # 由 SkillRegistry 在 search_skills 时才注册 + promote
        tool = Tool(
            name=skill.name,
            description=skill.description,
            executor=executor,
            input_schema=input_schema,
            tier=ToolTier.DEFERRED,
            when_to_use=skill.when_to_use,
            policy=ToolPolicy(
                sensitivity=SensitivityLevel.LOW,
                is_reversible=True,
                audit_required=False,
                is_idempotent=True,
                read_only=True,
            ),
            namespace="skill",
        )

        # logger.info(
        #     "Action Skill → Tool: '%s' (namespace=skill, tier=DEFERRED, params=%s)",
        #     skill.name,
        #     list(input_schema.get("properties", {}).keys()),
        # )

        return tool

    def _build_schema(self, func: Callable[..., Any]) -> JsonSchema:
        """从函数签名 + docstring 自动推导 JSON Schema。

        唯一数据来源是 Python 函数本身：
        - type hints → 参数类型
        - 默认值有无 → required 判定
        - docstring Args 段 → 参数描述

        Args:
            func: 入口函数。

        Returns:
            JSON Schema dict。
        """

        # 自动推导
        try:
            hints = get_type_hints(func)
        except Exception:
            hints = {}

        sig = inspect.signature(func)
        params = list(sig.parameters.values())

        # 跳过 ToolContext 参数（如有）和 self/cls
        if params:
            first_hint = hints.get(params[0].name)
            if first_hint is ToolContext or params[0].name in ("ctx", "context", "self", "cls"):
                params = params[1:]

        # 从 docstring 提取参数描述
        param_docs = self._parse_param_docs(func)

        properties: dict[str, Any] = {}
        required: list[str] = []

        for param in params:
            prop: dict[str, Any] = {}
            hint = hints.get(param.name)

            # 类型映射
            if hint in _TYPE_MAP:
                prop["type"] = _TYPE_MAP[hint]
            else:
                prop["type"] = "string"  # 默认 string

            # 描述
            if param.name in param_docs:
                prop["description"] = param_docs[param.name]

            properties[param.name] = prop

            # 必填判断
            if param.default is inspect.Parameter.empty:
                required.append(param.name)

        schema: JsonSchema = {
            "type": "object",
            "properties": properties,
            # "additionalProperties": False,
        }
        if required:
            schema["required"] = required

        return schema

    def _build_executor(
        self,
        func: Callable[..., Any],
        skill_name: str,
    ) -> Callable[..., ToolResult]:
        """构建安全的 executor 包装。

        - async 函数 → 直接 await
        - sync 函数 → run_in_executor 包装
        - 异常 → ToolResult(success=False)
        - 超时 → ToolResult(success=False)
        """
        timeout = self._execution_timeout
        is_async = inspect.iscoroutinefunction(func)

        def _executor(ctx: ToolContext, **kwargs: Any) -> ToolResult:
            """Action Skill executor（同步入口，内部处理 async）。"""
            try:
                if is_async:
                    # 在当前 event loop 或新 loop 中执行
                    try:
                        loop = asyncio.get_running_loop()
                        # 已有 event loop — 使用 ThreadPoolExecutor
                        import concurrent.futures
                        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                            future = pool.submit(
                                asyncio.run,
                                asyncio.wait_for(func(**kwargs), timeout=timeout),
                            )
                            result = future.result(timeout=timeout + 5)
                    except RuntimeError:
                        # 无 event loop — 直接 asyncio.run
                        result = asyncio.run(
                            asyncio.wait_for(func(**kwargs), timeout=timeout)
                        )
                else:
                    result = func(**kwargs)

                # 统一转为 str
                data = str(result) if result is not None else ""

                return ToolResult(
                    success=True,
                    data=data,
                    tool_name=f"skill.{skill_name}",
                )

            except asyncio.TimeoutError:
                logger.warning(
                    "Action Skill '%s' 执行超时 (%ds)", skill_name, timeout
                )
                return ToolResult(
                    success=False,
                    error=f"执行超时（{timeout}秒）",
                    tool_name=f"skill.{skill_name}",
                )
            except Exception as e:
                logger.error(
                    "Action Skill '%s' 执行失败: %s", skill_name, e,
                    exc_info=True,
                )
                return ToolResult(
                    success=False,
                    error=f"执行失败: {e}",
                    tool_name=f"skill.{skill_name}",
                )

        return _executor

    @staticmethod
    def _parse_param_docs(func: Callable) -> dict[str, str]:
        """从函数 docstring 提取参数描述（Google 风格）。"""
        doc = inspect.getdoc(func) or ""
        lines = doc.strip().split("\n")

        params: dict[str, str] = {}
        in_args = False
        for line in lines[1:]:
            stripped = line.strip()
            if stripped.lower().startswith(("args:", "arguments:", "parameters:")):
                in_args = True
                continue
            if stripped.lower().startswith(
                ("returns:", "raises:", "examples:", "notes:", "yields:")
            ):
                in_args = False
                continue
            if in_args and ":" in stripped:
                param_name, param_desc = stripped.split(":", 1)
                param_name = param_name.strip()
                # 跳过带类型注解的参数名（如 "path (str)"）
                if "(" in param_name:
                    param_name = param_name.split("(")[0].strip()
                params[param_name] = param_desc.strip()

        return params
