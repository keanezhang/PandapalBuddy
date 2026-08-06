"""pandapal/dispatch/adapter.py — 渠道入站适配器 Protocol。

类比出站的 Transport Protocol：每渠道一份适配器，负责方言翻译；
渠道差异推边缘，核心（InboundDispatcher）只认识规范信封。
"""

from __future__ import annotations

from typing import Any, Protocol

from pandapal.dispatch.types import InboundEnvelope
from pandapal.router.models import InboundMessage


class InboundChannelAdapter(Protocol):
    """渠道入站适配器：归一发生在渠道入口（逐条即时翻译，非集中批处理）。"""

    @property
    def channel_id(self) -> str:
        """本适配器所属渠道 ID。"""
        ...

    @property
    def allowed_types(self) -> frozenset[str]:
        """本渠道放行的消息类型（方言 → 规范前的全集）。安全白名单：
        不在集合内的类型在适配器直接 WARN + drop，永远到不了 dispatcher。"""
        ...

    def normalize(self, raw: dict[str, Any]) -> InboundEnvelope | None:
        """方言帧 → 规范信封（结构归一 + 词汇归一，两级一次完成）。

        返回 None = 非法/不放行的消息（适配器已 WARN 留痕）。
        连接层消息（PING/ACK/pong）不进此方法，由 gate 自处理。
        """
        ...

    def build_inbound_message(self, env: InboundEnvelope) -> InboundMessage:
        """仅当 dispatcher 判定 msg_type ∈ RouterMessageType 后调用。

        含各渠道自己的校验语义（IPC 0 容忍 vs Gateway session 可选）。
        """
        ...
