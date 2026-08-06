"""pandapal.desktop_ipc — IPC 协议层（5.2 重写版）。

公开 API：
- IpcMessageType: IPC 消息类型字符串常量（与前端 types/api.ts 保持一致）
- InboundIpcMessage / OutboundIpcMessage: 兼容旧结构体（新代码建议用 NormalizedEvent）
- StdioIpcServer: 薄层 IPC server
- IpcStdoutTransport: 出站 Transport（被 StdioIpcServer 持有）
"""

from pandapal.desktop_ipc.ipc_transport import IpcStdoutTransport
from pandapal.desktop_ipc.message_codec import (
    IpcMessageType,
    InboundIpcMessage,
    OutboundIpcMessage,
)
from pandapal.desktop_ipc.stdio_ipc import StdioIpcServer

__all__ = [
    "IpcMessageType",
    "InboundIpcMessage",
    "OutboundIpcMessage",
    "StdioIpcServer",
    "IpcStdoutTransport",
]
