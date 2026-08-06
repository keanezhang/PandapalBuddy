"""pandapal_relay/transport_protocol.py — 渠道传输层抽象（Relay 本地副本）。

★ 来源：pandapal/broadcast/transport.py（完整复制，内部 import 已重写为本地 .normalized_events）
★ 用途：Relay 服务端独立部署，不依赖完整 pandapal 包。
★ ⚠️ 设计债务（2026-06-10）：此文件是完整副本，需与 pandapal 端保持同步。
   当前已知差异：本文件已加 is_started（2026-06-10）。
   长期方案：抽共享包（见 docs/技术债/transport-protocol-共享包.md）。

每个渠道类型（IPC / WeCom / XiaoZhi / 未来新渠道）实现自己的 Transport。
Broadcast 拿到 NormalizedEvent 后只需调 transport.send()，不关心底层协议。

★ Transport 不做任何"事件配对"或"副作用拼接"——
  "HITL_REQUEST 之前先发 REPLY_END" 这种配对是 Scheduler 转换层
  （stream_to_normalized.py）的责任，不是 Transport 的事。

生命周期契约（★ 根本解 2026-06-10）：
  1. 构造后 is_started = False
  2. start() 之后 is_started = True（无论成功失败——失败表示进入降级模式，但仍属「已尝试启动」）
  3. stop() 之后 is_started = False
  4. send() 内部不检查 is_started（O3 Never Throw 保持不变），
     但 run_relay 启动期会扫描 is_started 状态做自检日志
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .normalized_events import NormalizedEvent


@runtime_checkable
class Transport(Protocol):
    """渠道传输层抽象。

    每个渠道类型（IPC / WeCom / XiaoZhi / 未来新渠道）实现自己的 Transport。
    Broadcast 拿到 NormalizedEvent 后只需调 transport.send()，不关心底层协议。

    ★ Transport 不做任何"事件配对"或"副作用拼接"——
      "HITL_REQUEST 之前先发 REPLY_END" 这种配对是 Scheduler 转换层
      （stream_to_normalized.py）的责任，不是 Transport 的事。
    """

    @property
    def is_started(self) -> bool:
        """当前 transport 是否已 start()（用于自检/幂等控制/测试断言）。"""
        ...

    async def send(self, event: NormalizedEvent) -> None:
        """发送一条事件。失败必须内部消化，不向上抛（O3 Never Throw）。"""
        ...

    async def start(self) -> None:
        """连接建立（如 WSS 握手、OAuth 鉴权）。幂等。"""
        ...

    async def stop(self) -> None:
        """连接关闭。幂等。"""
        ...
