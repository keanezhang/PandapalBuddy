"""pandaren/mcp/client.py — 单个 MCP 服务器会话（连接 / 生命周期 / 工具调用）。

设计依据：docs/design/mcp-capability.md（§6.1 client.py、§7.4 签名、§12.1 已实测的 2.x API）。

核心难点与取舍：
  MCP 的 ``ClientSession`` 必须存活在一个**长驻任务**里（stdio 依赖后台进程，
  且 anyio 上下文要求同任务进出）。故这里采用「后台任务 + 就绪事件」模式：
  ``connect()`` 起一个后台任务跑 ``async with transport / ClientSession``，
  握手 + 列工具完成后 set ``_ready``，随后任务阻塞在 ``_stop`` 事件上保活；
  ``aclose()`` set ``_stop`` → 上下文退出 → 子进程/连接被正确回收。

本模块是唯一 import ``mcp`` SDK 的地方之一（另一处是 manager 的间接调用）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

from ..tool.definition.tool_result import ToolResult
from .config import McpConnectError, McpServerConfig, McpTransport

logger = logging.getLogger("pandaren.mcp.client")

#: connect() 外层等待的上限 = 内层握手超时 + 余量（让内层 wait_for 先触发，错误更精确）。
_READY_WAIT_MARGIN = 5.0
#: aclose() 等待后台任务自然退出的宽限期（秒）；超时后强制 cancel。
_CLOSE_GRACE = 5.0


@dataclass(frozen=True)
class McpToolDescriptor:
    """MCP 服务器暴露的一个工具（归一化后的只读描述）。

    input_schema 已归一为 dict；annotations 原样保留（可能是 pydantic 对象或 dict）。
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    when_to_use: str
    annotations: Any = None


def _json_dump(obj: Any) -> str:
    """尽力把对象序列化为紧凑 JSON 字符串（展示类字段，失败回落 str + 留痕）。"""
    try:
        if hasattr(obj, "model_dump"):
            return json.dumps(obj.model_dump(), ensure_ascii=False, default=str)
        if hasattr(obj, "dict"):
            return json.dumps(obj.dict(), ensure_ascii=False, default=str)
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001 - 展示类兜底，必须留痕不静默
        logger.warning("MCP 内容序列化失败，回落 str()：%s", exc)
        return str(obj)


def _extract_field(obj: Any, *names: str) -> Any:
    """按候选属性/键名取值（兼容 pydantic 对象与 dict 两种形态）。"""
    if obj is None:
        return None
    for name in names:
        if isinstance(obj, dict):
            if name in obj:
                return obj[name]
        elif hasattr(obj, name):
            return getattr(obj, name)
    return None


def _serialize_content(result: Any) -> str:
    """把 ``CallToolResult.content`` 序列化为给 LLM 的文本。

    文本块（TextContent）优先拼接；非文本块（Image/Audio/Resource）以紧凑 JSON
    结构化序列化，保证字段不丢。无内容块时回落到 structuredContent。
    """
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")

    parts: list[str] = []
    for item in (content or []):
        text = _extract_field(item, "text")
        if text is not None:
            parts.append(str(text))
        else:
            parts.append(_json_dump(item))

    if parts:
        return "\n".join(parts)

    structured = _extract_field(result, "structuredContent", "structured_content")
    if structured is not None:
        return _json_dump(structured)
    return ""


class McpSessionHandle:
    """单个 MCP 服务器的会话句柄（一个 server ↔ 一个长驻任务）。"""

    def __init__(self, config: McpServerConfig) -> None:
        self._cfg = config
        self._session: Any = None
        self._tools: list[McpToolDescriptor] = []
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()
        self._error: BaseException | None = None
        self._call_lock = asyncio.Lock()

    # ── 只读视图 ──────────────────────────────────────────

    @property
    def config(self) -> McpServerConfig:
        return self._cfg

    @property
    def is_alive(self) -> bool:
        """会话是否处于可用状态（已握手且后台任务未退出）。"""
        return (
            self._session is not None
            and self._error is None
            and self._task is not None
            and not self._task.done()
        )

    def list_tools(self) -> list[McpToolDescriptor]:
        """返回握手期缓存的工具描述（连接后有效）。"""
        return list(self._tools)

    # ── 生命周期 ──────────────────────────────────────────

    async def connect(self) -> None:
        """建立连接并完成握手 + 列工具。失败抛 McpConnectError。"""
        cfg = self._cfg
        cfg.validate()

        # 幂等：直接二次 connect 时先回收旧会话，避免子进程 / 连接泄漏。
        # （manager 层已保证同名 server 先 disconnect；这里让 handle 自身也安全。）
        if self._task is not None and not self._task.done():
            logger.warning("MCP server '%s' 已有存活会话，先关闭旧会话再重连", cfg.name)
            await self.aclose()

        self._stop = asyncio.Event()
        self._ready = asyncio.Event()
        self._error = None
        self._tools = []
        self._task = asyncio.create_task(self._run(), name=f"mcp-session:{cfg.name}")

        try:
            await asyncio.wait_for(
                asyncio.shield(self._ready.wait()),
                timeout=cfg.connect_timeout + _READY_WAIT_MARGIN,
            )
        except asyncio.TimeoutError:
            await self.aclose()
            raise McpConnectError(
                f"MCP server '{cfg.name}' 连接超时（{cfg.connect_timeout}s）"
            ) from None

        if self._error is not None:
            err = self._error
            await self.aclose()
            raise McpConnectError(
                f"MCP server '{cfg.name}' 连接失败：{err}"
            ) from err

    async def _run(self) -> None:
        """后台任务体：持有 transport + ClientSession，阻塞保活至 _stop。"""
        cfg = self._cfg
        try:
            async with self._open_transport() as (read, write):
                from mcp import ClientSession

                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(
                        session.initialize(), timeout=cfg.connect_timeout
                    )
                    self._tools = await asyncio.wait_for(
                        self._fetch_tools(session), timeout=cfg.connect_timeout
                    )
                    self._session = session
                    self._ready.set()
                    await self._stop.wait()
        except asyncio.CancelledError:
            # 关闭路径下的主动取消：不视为错误，但需唤醒 connect() 等待者。
            self._ready.set()
            raise
        except BaseException as exc:  # noqa: BLE001 - 握手段是故障隔离点，转成错误上抛
            self._error = exc
            self._ready.set()
            logger.warning("MCP server '%s' 会话异常：%s", cfg.name, exc)
        finally:
            self._session = None

    async def aclose(self) -> None:
        """关闭会话：通知后台任务退出并回收（超时则强制取消）。幂等。"""
        self._stop.set()
        task = self._task
        self._task = None
        self._session = None
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=_CLOSE_GRACE)
        except Exception:  # noqa: BLE001 - 宽限期内的超时/异常都走强制取消
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 - 取消后的收尾异常一律吞掉（已留痕在上游）
                pass

    # ── 传输 ──────────────────────────────────────────────

    @asynccontextmanager
    async def _open_transport(self) -> AsyncIterator[tuple[Any, Any]]:
        """打开底层传输，yield (read_stream, write_stream)。"""
        cfg = self._cfg
        if cfg.transport is McpTransport.STDIO:
            from mcp import StdioServerParameters, stdio_client

            params = StdioServerParameters(
                command=cfg.command,
                args=list(cfg.args),
                env=dict(cfg.env) if cfg.env else None,
                cwd=cfg.cwd,
            )
            async with stdio_client(params) as streams:
                yield streams[0], streams[1]
        else:
            from mcp.client.streamable_http import streamable_http_client

            if cfg.headers:
                import httpx2

                async with httpx2.AsyncClient(headers=dict(cfg.headers)) as http_client:
                    async with streamable_http_client(
                        cfg.url, http_client=http_client, terminate_on_close=True
                    ) as streams:
                        yield streams[0], streams[1]
            else:
                async with streamable_http_client(
                    cfg.url, terminate_on_close=True
                ) as streams:
                    yield streams[0], streams[1]

    async def _fetch_tools(self, session: Any) -> list[McpToolDescriptor]:
        """调用 ``tools/list`` 并归一化为 McpToolDescriptor 列表。"""
        listed = await session.list_tools()
        raw_tools = getattr(listed, "tools", None) or []
        out: list[McpToolDescriptor] = []
        for raw in raw_tools:
            name = getattr(raw, "name", None)
            if not name:
                # name 属身份类字段，缺失即该工具不可寻址 → 丢弃并留痕（绝不编造）。
                logger.warning(
                    "MCP server '%s' 返回了无名工具，已跳过", self._cfg.name
                )
                continue

            schema = getattr(raw, "inputSchema", None)
            if schema is None:
                schema = getattr(raw, "input_schema", None)
            if hasattr(schema, "model_dump"):
                schema = schema.model_dump()
            if not isinstance(schema, dict):
                schema = {}

            description = getattr(raw, "description", None) or ""
            out.append(
                McpToolDescriptor(
                    name=name,
                    description=description,
                    input_schema=schema,
                    when_to_use=description,
                    annotations=getattr(raw, "annotations", None),
                )
            )
        return out

    # ── 工具调用 ──────────────────────────────────────────

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> ToolResult:
        """调用 MCP 工具，永远返回 ToolResult（O3 精神，不外抛）。

        Args:
            name: MCP 原生工具名（非 pandaren 侧 full_name）。
            arguments: 工具入参。
            timeout: 覆盖默认 call_timeout。
        """
        session = self._session
        if session is None:
            return ToolResult(
                success=False,
                error=f"MCP server '{self._cfg.name}' 未连接，工具 '{name}' 不可用",
            )

        budget = self._cfg.call_timeout if timeout is None else timeout
        async with self._call_lock:
            try:
                result = await asyncio.wait_for(
                    session.call_tool(name, arguments), timeout=budget
                )
            except asyncio.TimeoutError:
                return ToolResult(
                    success=False,
                    error=f"MCP 工具 '{name}' 调用超时（{budget}s）",
                )
            except Exception as exc:  # noqa: BLE001 - 远端调用失败，转 ToolResult
                logger.warning("MCP 工具 '%s' 调用失败：%s", name, exc)
                return ToolResult(
                    success=False,
                    error=f"MCP 工具 '{name}' 调用失败：{exc}",
                )

        # CallToolResult 有 content；多轮交互类结果（InputRequiredResult 等）没有，
        # 目前不支持 → 显式失败，绝不假装成功（避免静默丢语义）。
        if not hasattr(result, "content"):
            return ToolResult(
                success=False,
                error=(
                    f"MCP 工具 '{name}' 返回了暂不支持的响应类型"
                    f"（{type(result).__name__}，可能为 multi-turn elicitation）"
                ),
            )

        text = _serialize_content(result)
        if bool(_extract_field(result, "isError", "is_error")):
            return ToolResult(
                success=False,
                error=text or f"MCP 工具 '{name}' 返回 isError=true",
            )
        return ToolResult(success=True, data=text or "OK")
