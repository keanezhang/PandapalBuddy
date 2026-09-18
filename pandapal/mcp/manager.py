"""pandapal/mcp/manager.py — MCP 生命周期编排（配置 → 连接 → 动态注册 → 出站事件）。

设计依据：docs/design/mcp-capability.md §6.2 / §7 / §10；实施计划
`mcp-capability-stage2.md`（方案 A：McpManager 独占事件发射）。

职责边界（谁生产 → 经哪层 → 谁消费）：
  - 生产：本模块是 ``MCP_*`` 出站事件的**唯一 Owner**（含应答类 ``MCP_LIST_RESULT``
    与推送类 ``MCP_STATUS_CHANGED``）。app.py 的 IPC handler 只解析 payload → 调本模块
    方法 → ``return None``（dispatcher 对 ``None`` 不重复广播），因此无 handler/manager
    双写、无漏发。
  - 依赖：复用阶段 1 的 ``pandaren.mcp.McpClientManager``（协议客户端），自身只做
    「编排 + 注册 + 事件」。工具注册进调用方传入的共享 ``ToolRegistry``，令
    ``registry.version++`` → 现存会话下一轮 ``_build_static_context`` 脏检查命中，
    工具当轮/下轮即生效（无需重建 Agent）。
  - 隔离：连接失败**绝不向上抛**（失败隔离，不中断 ``start_all``、不影响其他 server），
    统一走 ``pandapal.degradation`` 留痕（§九 统一降级通道）。

降级红线（§九）：配置非法 / server 未知 = 决策·ID 类字段缺失 → 出站 ``ERROR`` 事件 +
``report_degradation`` 留痕，**绝不静默回落默认值**。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import TYPE_CHECKING

from pandaren.mcp.config import McpConfigError, McpServerConfig, McpServerStatus
from pandaren.mcp.manager import McpClientManager
from pandaren.tool.definition.tool import Tool

from pandapal.degradation import report_degradation
from pandapal.events.normalized import NormalizedEvent

if TYPE_CHECKING:
    from pandapal.broadcast.broadcaster import MessageBroadcast
    from pandapal.mcp.config_store import McpConfigStore
    from pandaren.tool.facade import ToolRegistry

logger = logging.getLogger(__name__)


# ── 降级 event_code 常量集（B5：契约字符串收编，禁止在 call 点散写自由文本）──
#    公开：app.py 的 IPC handler 兜底降级与本模块同源复用（单一真相源）。
class McpDegradeEvent:
    CONNECT_FAILED = "mcp.connect_failed"
    DISCONNECT_FAILED = "mcp.disconnect_failed"
    CONFIG_INVALID = "mcp.config_invalid"
    SERVER_UNKNOWN = "mcp.server_unknown"
    HANDLER_ERROR = "mcp.handler_error"  # IPC handler 意外异常兜底（app.py）


# 出站 ERROR 事件的 error_code（前端按此分流文案）
_ERR_CONFIG_INVALID = "mcp_config_invalid"
_ERR_SERVER_UNKNOWN = "mcp_server_unknown"


class McpManager:
    """编排 MCP 服务器生命周期 + 动态工具注册 + 出站事件发射。"""

    def __init__(
        self,
        *,
        config_store: "McpConfigStore",
        client_manager: McpClientManager,
        tool_registry: "ToolRegistry",
        broadcast: "MessageBroadcast",
    ) -> None:
        self._config_store = config_store
        self._client_manager = client_manager
        self._tool_registry = tool_registry
        self._broadcast = broadcast
        # 全局串行化「配置写 + 连接管理」（单进程低频操作；替代设计里每 server 一把锁，
        # 一把全局锁更简单且无死锁面）。
        self._lock = asyncio.Lock()
        # 每 server 最近一次连接错误文本（供 list_servers 展示；连上即清空）。
        self._errors: dict[str, str] = {}

    # ══════════════════════════════════════════════════════════════════════════
    # 生命周期
    # ══════════════════════════════════════════════════════════════════════════

    async def start(self) -> None:
        """连接所有 ``enabled`` 的服务器（失败隔离：单个失败不中断其余）。"""
        try:
            configs = self._config_store.load_all()
        except Exception:  # noqa: BLE001 - 配置层故障，留痕不炸启动
            report_degradation(
                McpDegradeEvent.CONFIG_INVALID,
                category="id", source="mcp.manager.start", exc_info=True,
            )
            return
        for cfg in configs:
            if not cfg.enabled:
                continue
            await self._connect_one(cfg, emit=False)

    async def stop(self) -> None:
        """关闭所有会话（终止 stdio 子进程）；exit 路径绝不抛。"""
        try:
            await self._client_manager.aclose_all()
        except Exception:  # noqa: BLE001 - 退出路径兜底，留痕不抛
            report_degradation(
                McpDegradeEvent.DISCONNECT_FAILED,
                category="id", source="mcp.manager.stop", exc_info=True,
            )

    # ══════════════════════════════════════════════════════════════════════════
    # 服务器命令（全局锁串行化）
    # ══════════════════════════════════════════════════════════════════════════

    async def connect_server(self, name: str) -> None:
        """手动连接一个已配置的服务器。"""
        async with self._lock:
            cfg = self._find_config(name)
            if cfg is None:
                await self._emit_unknown(name, "mcp.manager.connect_server")
                return
            await self._connect_one(cfg)

    async def disconnect_server(self, name: str) -> None:
        """手动断开一个服务器（注销其全部工具）。"""
        async with self._lock:
            await self._unregister_and_close(name)
            await self._emit(
                NormalizedEvent.mcp_status_changed(
                    name, McpServerStatus.DISCONNECTED.value
                )
            )

    async def set_enabled(self, name: str, enabled: bool) -> None:
        """启用/禁用服务器（**保留配置**，仅切换工具加载）。

        禁用 = 注销该服务器全部工具 + 断开连接，等价于「系统没有这些工具」；
        启用 = 重新连接并注册工具。配置始终保留在 servers.toml 中，
        **绝不经由删除配置**来达到停用目的。
        """
        cfg = self._find_config(name)
        if cfg is None:
            await self._emit_unknown(name, "mcp.manager.set_enabled")
            return
        updated = dataclasses.replace(cfg, enabled=enabled)
        # 复用 save_server 的热生效语义（落盘 + 摘旧工具 + 按 enabled 重连/断开），
        # 其内部自持锁，故此处不再套锁。
        await self.save_server(updated)

    async def save_server(self, cfg: McpServerConfig) -> None:
        """保存/更新配置并**热生效**（先摘旧工具，再按 enabled 重连/断开）。"""
        async with self._lock:
            try:
                cfg.validate()
            except McpConfigError as exc:
                report_degradation(
                    McpDegradeEvent.CONFIG_INVALID,
                    category="id", source="mcp.manager.save_server",
                    expected="valid config", fallback="rejected", exc_info=True,
                )
                await self._emit(
                    NormalizedEvent.global_error(_ERR_CONFIG_INVALID, str(exc))
                )
                return  # 非法配置不落盘

            self._config_store.save(cfg)
            # 热生效：先幂等摘掉旧工具，再按 enabled 重新连接或标记断开。
            await self._unregister_and_close(cfg.name)
            if cfg.enabled:
                await self._connect_one(cfg)
            else:
                await self._emit(
                    NormalizedEvent.mcp_status_changed(
                        cfg.name, McpServerStatus.DISCONNECTED.value
                    )
                )
            await self._emit(NormalizedEvent.mcp_saved(cfg.name))

    async def delete_server(self, name: str) -> None:
        """删除一个服务器配置（先断开 + 注销工具，再删配置）。"""
        async with self._lock:
            await self._unregister_and_close(name)
            self._config_store.delete(name)
            self._errors.pop(name, None)
            await self._emit(NormalizedEvent.mcp_deleted(name))

    async def test_server(self, cfg: McpServerConfig) -> None:
        """连接测试：临时会话，**不持久化、不注册工具、不污染主 client_manager**。"""
        try:
            cfg.validate()
        except McpConfigError as exc:
            await self._emit(NormalizedEvent.mcp_test_result(False, None, str(exc)))
            return

        probe = McpClientManager()
        try:
            tools = await probe.connect(cfg)
            await self._emit(
                NormalizedEvent.mcp_test_result(
                    True, [self._tool_dict(t) for t in tools]
                )
            )
        except Exception as exc:  # noqa: BLE001 - 测试失败是正常结果，不外抛
            await self._emit(NormalizedEvent.mcp_test_result(False, None, str(exc)))
        finally:
            try:
                await probe.aclose_all()
            except Exception:  # noqa: BLE001 - 清理失败仅留痕
                report_degradation(
                    McpDegradeEvent.DISCONNECT_FAILED,
                    category="id", source="mcp.manager.test_server.cleanup", exc_info=True,
                )

    # ══════════════════════════════════════════════════════════════════════════
    # 查询 / 应答发射
    # ══════════════════════════════════════════════════════════════════════════

    def list_servers(self) -> list[dict]:
        """所有已配置服务器的摘要列表（含未连接者）。"""
        return [self._summary(cfg) for cfg in self._config_store.load_all()]

    def get_server_detail(self, name: str) -> dict | None:
        """单服务器详情（摘要 + 完整 config + 工具清单）；未配置返回 None。"""
        cfg = self._find_config(name)
        if cfg is None:
            return None
        detail = self._summary(cfg)
        detail["config"] = cfg.to_dict()
        detail["tools"] = self._tools_payload(name)
        return detail

    async def emit_list(self) -> None:
        """发射 ``MCP_LIST_RESULT``（供 handler 调用，保持事件 Owner 唯一）。"""
        await self._emit(NormalizedEvent.mcp_list_result(self.list_servers()))

    async def emit_get(self, name: str) -> None:
        """发射 ``MCP_GET_RESULT``（服务器未知则发全局 ERROR）。"""
        detail = self.get_server_detail(name)
        if detail is None:
            await self._emit_unknown(name, "mcp.manager.emit_get")
            return
        await self._emit(NormalizedEvent.mcp_get_result(detail))

    # ══════════════════════════════════════════════════════════════════════════
    # 私有
    # ══════════════════════════════════════════════════════════════════════════

    async def _connect_one(self, cfg: McpServerConfig, *, emit: bool = True) -> bool:
        """连接单个服务器并注册其工具；**失败隔离**（绝不抛，返回是否成功）。"""
        try:
            tools = await self._client_manager.connect(cfg)
        except Exception as exc:  # noqa: BLE001 - 失败隔离点：留痕 + 事件 + 继续
            self._errors[cfg.name] = str(exc)
            report_degradation(
                McpDegradeEvent.CONNECT_FAILED,
                category="id", source="mcp.manager._connect_one",
                expected="connected", fallback="error", exc_info=True,
            )
            logger.warning("MCP server '%s' 连接失败：%s", cfg.name, exc)
            if emit:
                await self._emit(
                    NormalizedEvent.mcp_status_changed(
                        cfg.name, McpServerStatus.ERROR.value, str(exc)
                    )
                )
            return False

        self._errors.pop(cfg.name, None)
        self._register_tools(tools)
        if emit:
            await self._emit(
                NormalizedEvent.mcp_status_changed(
                    cfg.name, McpServerStatus.CONNECTED.value
                )
            )
            await self._emit(
                NormalizedEvent.mcp_tools_result(
                    cfg.name, self._tools_payload(cfg.name)
                )
            )
        return True

    def _register_tools(self, tools: list[Tool]) -> None:
        """把适配后的 MCP 工具注册进共享 registry（``version++`` → 会话脏检查命中）。

        ``skip_if_exists=True``：重连/命名冲突时不覆盖同名内置工具。
        """
        for tool in tools:
            self._tool_registry.register_tool(tool, skip_if_exists=True)

    async def _unregister_and_close(self, name: str) -> None:
        """注销某服务器的全部工具并断开（幂等：未连接则 no-op）。"""
        for tool in self._client_manager.tools_for(name):
            self._tool_registry.unregister_tool(tool.full_name)
        try:
            await self._client_manager.disconnect(name)
        except Exception:  # noqa: BLE001 - 断开失败留痕，不阻塞调用方
            report_degradation(
                McpDegradeEvent.DISCONNECT_FAILED,
                category="id", source="mcp.manager._unregister_and_close", exc_info=True,
            )

    async def _emit_unknown(self, name: str, source: str) -> None:
        """server 未知：留痕 + 全局 ERROR（ID 类字段缺失，绝不静默）。"""
        report_degradation(
            McpDegradeEvent.SERVER_UNKNOWN,
            category="id", source=source,
            expected="known server", fallback="none",
        )
        await self._emit(
            NormalizedEvent.global_error(
                _ERR_SERVER_UNKNOWN, f"MCP server '{name}' 不存在"
            )
        )

    def _find_config(self, name: str) -> McpServerConfig | None:
        for cfg in self._config_store.load_all():
            if cfg.name == name:
                return cfg
        return None

    def _summary(self, cfg: McpServerConfig) -> dict:
        tools = self._client_manager.tools_for(cfg.name)
        return {
            "name": cfg.name,
            "transport": cfg.transport.value,
            "enabled": cfg.enabled,
            "tier": cfg.tier.name.lower(),
            "status": self._client_manager.status(cfg.name).value,
            "tool_count": len(tools),
            "error": self._errors.get(cfg.name),
        }

    def _tools_payload(self, name: str) -> list[dict]:
        return [self._tool_dict(t) for t in self._client_manager.tools_for(name)]

    @staticmethod
    def _tool_dict(tool: Tool) -> dict:
        sensitivity = tool.sensitivity
        return {
            "name": tool.full_name,
            "description": tool.description,
            "when_to_use": tool.when_to_use,
            "sensitivity": getattr(sensitivity, "name", str(sensitivity)).lower(),
        }

    async def _emit(self, event: NormalizedEvent) -> None:
        """广播一条事件（``broadcast.send`` 自身 Never-Throw；这里再兜一层确保不外抛）。"""
        try:
            await self._broadcast.send(event)
        except Exception:  # noqa: BLE001 - 出站边界，绝不向业务抛
            logger.warning(
                "MCP 事件广播失败：%s", event.event_type.value, exc_info=True
            )


__all__ = ["McpManager", "McpDegradeEvent"]
