"""pandapal.broadcast.channel_registry — 统一渠道注册表（精简版）。

★ 5.2.B 改造后职责：
  - 只维护"渠道元信息 + 投递能力"
  - 不持有任何 bytes/envelope 知识
  - 不再管理流式缓冲（buffer 移到 Transport 内部）
  - 不再有 _build_frame_raw 等帧构造方法（移到 Transport 层）

★ ChannelInfo 关键变化：
  - 移除 callback 字段
  - 移除 stream_buffer 字段
  - 新增 transport 字段（每个 channel 自带 transport）
  - 新增 capabilities 字段（取代 supports_stream 布尔）

★ 渠道分类（保留）：
  - LOCAL:  本地渠道（同进程 Transport 投递）
  - REMOTE: 远程渠道（经 WSS → Relay → 远端 Transport）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pandapal.broadcast.transport import Transport

logger = logging.getLogger(__name__)


class ChannelDispatchPolicy(str, Enum):
    """渠道分发策略：渠道自我声明"我想收什么"（事件本身不带策略属性）。

    ★ 分发模型（恒定规则 + 渠道策略）：
      R0 指名即达：调用方显式 target_channel_ids → 仅指名渠道收（最高优先）
      R1 echo 永不回源：USER_INPUT_ECHO 永不发给其 origin 渠道（源渠道本地已显示）
      R2 回复恒达会话主：非 echo 事件 → origin 归属渠道恒收（含 TARGET_ONLY 渠道）
    本枚举只裁决「别人的事件与全局事件我收不收」：

    - SHARED:      都收（共享模式 = 现状行为）
    - SOURCE_ONLY: 只收来源是自己的事件 + 全局事件（owner=None 如定时任务）
    - TARGET_ONLY: 只收被 target_channel_ids 显式指名的事件（其余一概不收）
    """

    SHARED = "shared"
    SOURCE_ONLY = "source_only"
    TARGET_ONLY = "target_only"


@runtime_checkable
class ChannelPolicyPredicate(Protocol):
    """自定义渠道策略谓词（可插拔扩展点）。

    枚举覆盖不了的场景（如按用户组路由）注入实现本协议的 callable 即可，
    broadcaster 分发逻辑不变。

    返回 True 表示该渠道应接收此事件。
    约定：实现抛异常时 broadcaster 会 catch 并按 SHARED 放行（fail-open）。
    """

    def __call__(
        self,
        origin: str | None,
        owner: "ChannelInfo | None",
        channel: "ChannelInfo",
    ) -> bool:
        """origin=事件来源渠道ID；owner=origin 归属解析后的渠道（None=全局事件）。"""
        ...


class ChannelRegistryError(Exception):
    """非合规渠道 ID 被拒绝注册时抛出。"""


class ChannelType(str, Enum):
    """渠道类型。"""
    LOCAL = "local"      # 本地渠道（同进程 Transport）
    REMOTE = "remote"    # 远程渠道（经 WSS → Relay）


class ChannelCapability(str, Enum):
    """渠道能力枚举（取代散落的 supports_stream 布尔）。"""
    STREAM          = "stream"            # 能实时接收 LLM_TOKEN
    TEXT            = "text"              # 能发文字
    TEMPLATE_CARD   = "template_card"     # 能发模板卡片（WeCom 专属）
    INTERACTIVE     = "interactive"       # 能发交互型问卷
    IMAGE           = "image"             # 能发图片


@dataclass(frozen=True)
class ChannelInfo:
    """渠道元信息（不可变，注册后不可改）。"""
    id:           str
    type:         ChannelType
    capabilities: frozenset[ChannelCapability]
    transport:    Optional["Transport"] = None  # ★ 关键：每个 channel 自带 transport
    # 可选元数据（用于溯源）
    user_id:      str | None = None        # 渠道绑定的用户（IPC 必有，WeCom 推送时填）
    device_id:    str | None = None        # 硬件 ID（XiaoZhi 用）
    # ★ 分发策略（渠道自我声明"我想收什么"；事件本身不带策略属性）
    dispatch_policy: "ChannelDispatchPolicy | ChannelPolicyPredicate" = (
        ChannelDispatchPolicy.SHARED
    )
    # origin 前缀别名：origin 以任一条目前缀开头即归属本渠道
    # （如 xiaozhi 渠道配 ("xiaozhi:",)，则 xiaozhi:{device_id} 的事件归属 xiaozhi 投递）
    origin_aliases: tuple[str, ...] = ()
    registered_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class ChannelRegistry:
    """改造后的 ChannelRegistry：纯元信息管理。

    移除的方法：
    - register_local_channel()        → 改为构造 ChannelInfo 传入
    - register_remote_channel()        → 同上
    - sync_remote_channels()           → 移到 Gateway 启动逻辑
    - _build_frame_raw()               → 移到 Transport 层
    - 任何与 bytes/envelope 相关的方法  → 全部移到 Transport 层
    """

    def __init__(self) -> None:
        self._channels: dict[str, ChannelInfo] = {}

    def register(self, channel: ChannelInfo) -> None:
        """注册渠道（一次性写入，注册后不可改）。"""
        if channel.id in self._channels:
            raise ValueError(f"channel {channel.id!r} already registered")
        self._channels[channel.id] = channel
        logger.info(
            "Channel registered: id=%s, type=%s, caps=%s",
            channel.id, channel.type.value, sorted(c.value for c in channel.capabilities),
        )

    def deregister(self, channel_id: str) -> ChannelInfo | None:
        """注销渠道。返回被注销的 ChannelInfo（如有）。"""
        ch = self._channels.pop(channel_id, None)
        if ch:
            logger.info("Channel deregistered: id=%s", channel_id)
        return ch

    def get(self, channel_id: str) -> ChannelInfo | None:
        return self._channels.get(channel_id)

    def list_active(
        self, capabilities: ChannelCapability | None = None
    ) -> list[ChannelInfo]:
        """列出所有活跃渠道（可按能力过滤）。"""
        all_channels = list(self._channels.values())
        if capabilities is None:
            return all_channels
        return [ch for ch in all_channels if capabilities in ch.capabilities]

    def has_capability(self, channel_id: str, cap: ChannelCapability) -> bool:
        ch = self._channels.get(channel_id)
        return ch is not None and cap in ch.capabilities

    # ── 兼容性属性（保留旧 API 以便旧 Consumer 平稳过渡）──
    @property
    def channels(self) -> dict[str, ChannelInfo]:
        """返回渠道字典的浅拷贝（向后兼容）。"""
        return dict(self._channels)

    def get_channel_info(self, channel_id: str) -> ChannelInfo | None:
        """向后兼容：get_channel_info() 别名。"""
        return self.get(channel_id)

    def is_registered(self, channel_id: str) -> bool:
        """判断渠道是否已注册。"""
        return channel_id in self._channels

    def get_all_active_channels(self) -> list[ChannelInfo]:
        """向后兼容：get_all_active_channels() 别名。"""
        return self.list_active()

    def get_all_active_channel_ids(self) -> list[str]:
        """返回所有活跃渠道 ID。"""
        return list(self._channels.keys())

    def all_transports(self) -> list["Transport"]:
        """返回所有渠道的 transport 实例（★ 根本解 2026-06-10：用于生命周期驱动）。

        返回顺序为注册顺序。
        内部 channel（如 __hitl_bridge__ / __scheduler__）的 transport=None 也会被返回，
        调用方需自行判断 None 跳过。
        """
        return [ch.transport for ch in self._channels.values()]
