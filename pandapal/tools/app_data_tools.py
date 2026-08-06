"""app_data_tools — 快应用数据推送工具（AI Quick App 框架通道③）。

提供 push_app_data 工具，让 AI 向已注册的快应用前端面板推送结构化数据。

设计约束：
- BL2 (Stateless): session_id 从 ToolContext 传入，不存全局状态
- BL4 (DI): broadcaster 通过 AppDataTools 构造函数注入
- active_app_id 从 ToolContext.metadata 读取，前端在 sendMessage 时附加
- 工具内部校验 app_id 匹配，不匹配时返回警告让 AI 降级到通道①
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pandaren.tool.decorator import tool
from pandaren.tool.definition.context import ToolContext
from pandaren.tool.definition.tool import Tool
from pandaren.tool.definition.tool_policy import ToolPolicy
from pandaren.tool.types import SensitivityLevel, ToolTier

from pandapal.broadcast.broadcaster import MessageBroadcast
from pandapal.events.normalized import NormalizedEvent

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# _push_app_data_event 工厂（★ 闭包绑定 broadcaster）
# ═══════════════════════════════════════════════════════════════════════════════


def _make_push_app_data_event(broadcaster: MessageBroadcast | None):
    """工厂：返回绑定 broadcaster 的推送函数。

    broadcaster 为 None 时返回 no-op 版本（Fail-Safe 兜底）。
    """
    if broadcaster is None:
        async def _push(event: NormalizedEvent) -> None:
            logger.warning(
                "push_app_data: broadcaster is None, event dropped "
                "(app_id=%s, data_type=%s, event_type=%s)",
                event.payload.get("app_id"),
                event.payload.get("data_type"),
                event.event_type.value,
            )
        return _push

    async def _push(event: NormalizedEvent) -> None:
        try:
            await broadcaster.send(event)
        except Exception as e:
            logger.warning(
                "Failed to push QUICK_APP_DATA event: %s "
                "(app_id=%s, data_type=%s)",
                e,
                event.payload.get("app_id"),
                event.payload.get("data_type"),
            )

    return _push


# ═══════════════════════════════════════════════════════════════════════════════
# 工具工厂
# ═══════════════════════════════════════════════════════════════════════════════


def _make_push_app_data(push_event) -> Tool:
    @tool.function(
        name="push_app_data",
        description=(
            "向指定前端快应用推送结构化数据，驱动快应用的独立 UI 面板渲染。\n"
            "\n"
            "【何时必须调用】AI 产出的数据目标是快应用的独立展示组件（表格、图表、代码面板等），"
            "而非聊天面板的对话气泡。\n"
            "\n"
            "【何时不要调用】\n"
            "- 数据目标是聊天面板的对话气泡 → 使用正常回复文字\n"
            "- 需要用户做出选择 → 使用 ask_user（通道④）\n"
            "- 多步骤流程进度 → 使用 AgentTask 工具（通道②）\n"
            "\n"
            "参数要求：\n"
            "- app_id: 目标快应用 ID，必须与前端当前活跃的快应用一致\n"
            "- data_type: 数据类型标签，由应用自定义（如 step_output、quote、history）\n"
            "- data: 要推送的数据对象（dict），建议控制在 4KB 以内，大内容通过 filePath 指向文件"
        ),
        when_to_use=(
            "当 AI 产出需要渲染在快应用独立面板（而非聊天气泡）时调用。"
            "典型场景：测试结果展示、股票行情卡片、设计文档预览。"
        ),
        tier=ToolTier.ALWAYS,
        progress_label='推送数据到「{app_id}」',
        policy=ToolPolicy(
            sensitivity=SensitivityLevel.LOW,
            is_reversible=True,
            audit_required=False,
            is_idempotent=False,
        ),
    )
    async def push_app_data(
        ctx: ToolContext,
        app_id: str,
        data_type: str,
        data: dict[str, Any],
    ) -> str:
        """向指定前端快应用推送结构化数据。

        Args:
            ctx: 工具执行上下文
            app_id: 目标快应用 ID，如 "test-pipeline"、"stock-query"
            data_type: 数据类型标签，如 "step_output"、"quote"、"history"
            data: 要推送的数据对象（dict），建议控制在 4KB 以内

        Returns:
            确认消息或警告
        """
        active_app_id = (ctx.metadata or {}).get("active_app_id", "")

        if not active_app_id:
            return "提示: 当前请求未指定活跃快应用，数据不会推送到前端面板"
        if active_app_id != app_id:
            return f"错误: 当前活跃的快应用是 '{active_app_id}'，但传入的 app_id 是 '{app_id}'。请先调用 start_app 启动快应用"

        if data is None:
            return "错误: push_app_data 的 data 参数为 None，请传入有效的 dict 对象"
        if not isinstance(data, dict):
            return f"错误: push_app_data 的 data 参数期望 dict 类型，实际收到 {type(data).__name__}。请传入 dict 对象而非字符串"

        # ── 大小检查 ──
        data_json = json.dumps(data)
        data_size = len(data_json.encode("utf-8"))
        warning = ""
        if data_size > 4096:
            warning = (
                f"\n⚠️ 数据大小 {data_size // 1024} KB，超过建议的 4KB 上限。"
                f"大内容请通过 filePath 字段指向文件，由前端按需 read_file。"
            )

        # ── 构造并推送事件 ──
        session_id = ctx.session_id
        run_id = ctx.run_id or ""
        event = NormalizedEvent.quick_app_data(
            app_id=app_id,
            data_type=data_type,
            data=data,
            session_id=session_id,
            run_id=run_id or None,
        )
        logger.info(
            "push_app_data: app_id=%s data_type=%s data_size=%d run_id=%s session_id=%s",
            app_id, data_type, data_size, run_id, session_id,
        )
        await push_event(event)

        return (
            f"✅ 数据已推送至快应用 '{app_id}' (data_type: {data_type})"
            f"{warning}"
        )

    return push_app_data


# ═══════════════════════════════════════════════════════════════════════════════
# Provider 类（★ 层次 3：依赖通过 __init__ 显式注入）
# ═══════════════════════════════════════════════════════════════════════════════


class AppDataTools:
    """快应用数据推送工具组 Provider。

    构造时显式注入 broadcaster，get_tools() 返回绑定依赖的 Tool 列表。
    """

    def __init__(
        self,
        broadcaster: MessageBroadcast | None = None,
    ) -> None:
        self._broadcaster = broadcaster
        self._push_event = _make_push_app_data_event(broadcaster)

    def get_tools(self) -> list[Tool]:
        """返回快应用数据推送工具（绑定了 broadcaster 的 Tool 列表）。"""
        return [
            _make_push_app_data(self._push_event),
        ]
