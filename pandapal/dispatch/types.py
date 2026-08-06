"""pandapal/dispatch/types.py — 入站归一化核心类型。

入站是出站 broadcast 的镜像：
- 出站统一内部表示是 NormalizedEvent（业务层产出，核心全程只说普通话）；
- 入站统一内部表示是 InboundEnvelope（渠道入口产出，核心全程只说普通话）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChannelContext:
    """渠道上下文：handler 由此感知来源，回包定向的依据。"""

    channel_id: str         # __desktop_ipc__ / wecom / xiaozhi:xxx
    user_id: str            # 渠道已鉴权身份
    session_id: str | None  # 会话级消息才有；全局直通为 None
    msg_id: str             # 入站消息 ID（去重 / 应答关联）


@dataclass(frozen=True)
class InboundEnvelope:
    """统一内部表示（一级归一产物）。

    msg_type 必须是规范词汇：
      - Router 类：RouterMessageType.* 9 种字符串（user_instruction 等）
      - 直通类：现有直通字符串（SKILL_LIST 等，与 IpcMessageType 同值）

    data 为已解析 payload（方言字段原样保留，由 handler/构造器解释）。
    """

    msg_type: str
    data: dict[str, Any]
    ctx: ChannelContext
