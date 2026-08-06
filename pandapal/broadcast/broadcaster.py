"""MessageBroadcast — NormalizedEvent-based 广播器（渠道策略版）。

★ 分发模型（2026-06 渠道策略重构）：
  事件本身不再携带"目标范围"属性。发给哪些渠道 = 恒定规则 + 渠道自我声明：

  R0 指名即达：调用方显式 target_channel_ids → 仅指名渠道收（最高优先）
  R1 echo 永不回源：USER_INPUT_ECHO 永不发给其 origin 归属渠道
  R2 回复恒达会话主：非 echo 事件 → origin 归属渠道恒收（覆盖 TARGET_ONLY）
  渠道策略：裁决「别人的事件与全局事件我收不收」
    - SHARED:      都收（默认，= 旧 BROADCAST 行为）
    - SOURCE_ONLY: 只收来源是自己的事件 + 全局事件（owner=None 如定时任务）
    - TARGET_ONLY: 只收被 R0 显式指名的事件（其余一概不收，R2 除外）
    - 自定义谓词:  ChannelPolicyPredicate 实现，异常时 fail-open 按 SHARED

  渲染维度（流式 buffer / 卡片 / 文本）仍由各 Transport 自决，
  broadcaster 不做 capability 过滤（与现状一致）。

设计约束：
- BL1: 流式 & 非流式统一走 send(NormalizedEvent) → 渠道策略过滤 → transport.send
- BL3: NormalizedEvent frozen=True dataclass
- O3: Transport 失败必须内部消化（永不向上抛异常）
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Protocol, runtime_checkable

from pandapal.broadcast.channel_registry import (
    ChannelDispatchPolicy,
    ChannelInfo,
    ChannelRegistry,
)
from pandapal.events.normalized import EventType, NormalizedEvent

logger = logging.getLogger(__name__)


@runtime_checkable
class BroadcastGatewayProtocol(Protocol):
    """Broadcast 层依赖的 Gateway 最小接口契约。

    ★ 改造后：Gateway 仍负责把远程 channel 的 NormalizedEvent 序列化后
      通过 WSS 发送给 Relay。Broadcast 调 gateway.send(event) 时，
      Gateway 内部把 event 序列化为 JSON dict（而非 bytes envelope）。
    """

    async def send(self, event: NormalizedEvent) -> None:
        """发送 NormalizedEvent 到远程渠道。"""
        ...


class BroadcastConfigError(Exception):
    """广播层配置错误。"""

    def __init__(self, message: str) -> None:
        super().__init__(f"Broadcast config error: {message}")


class MessageBroadcast:
    """NormalizedEvent-based 出站广播器。

    使用方式：
        broadcast = MessageBroadcast(registry=registry, gateway=gateway)

        # 任何事件都走 send()
        await broadcast.send(NormalizedEvent.reply_start(reply_id="r1", run_id="r1"))
        await broadcast.send(NormalizedEvent.llm_token(
            delta="你", snapshot="你", reply_id="r1", run_id="r1"
        ))
        await broadcast.send(NormalizedEvent.reply_end(
            reply_id="r1", output="你好", status="ok", run_id="r1"
        ))
    """

    def __init__(
        self,
        registry: ChannelRegistry,
        gateway: BroadcastGatewayProtocol | None = None,
    ) -> None:
        self._registry = registry
        self._gateway = gateway
        # 是否为「目标渠道」策略（仅指定渠道）
        # 绝大多数事件都用不到 target_channel_ids，保留接口

    # ══════════════════════════════════════════════════════════════════════════════
    # Public API
    # ══════════════════════════════════════════════════════════════════════════════

    async def send(
        self,
        event: NormalizedEvent,
        origin_channel_id: str | None = None,
        target_channel_ids: tuple[str, ...] | None = None,
    ) -> None:
        """发送一条 NormalizedEvent（统一入口）。

        Args:
            event: 归一化事件
            origin_channel_id: 来源渠道（用于 EXCLUDE_SOURCE 策略排除 + 写入 event.origin_channel_id）
            target_channel_ids: 目标渠道白名单（仅 TARGET_ONLY 策略时使用）

        ★ 5.2：把 origin_channel_id 写入 event 后再分发，
          这样所有 Transport（WSSGateway/WeComRestTransport/IpcStdoutTransport）
          都能从 event.origin_channel_id 读到。
          由于 NormalizedEvent 是 frozen=True，我们用 object.__setattr__ 绕过。
        """
        # 1. 把 origin_channel_id 写入 event（让 Transport 可读）
        if origin_channel_id is not None and event.origin_channel_id != origin_channel_id:
            try:
                object.__setattr__(event, "origin_channel_id", origin_channel_id)
            except Exception as e:
                # frozen 兜底几乎不可达；真失败会让 Transport 读到错误 origin，留痕（O2）。
                logger.warning("Broadcast: 写入 origin_channel_id 失败: %s", e)

        # 2. 查表确定目标渠道
        target_channels = self._resolve_targets(
            event.event_type, origin_channel_id, target_channel_ids
        )
        if not target_channels:
            logger.debug(
                "Broadcast: no targets for event_type=%s, origin=%s",
                event.event_type.value, origin_channel_id,
            )
            return

        # 3. 分发到各 Transport（永不抛异常——O3 Never Throw）
        for ch in target_channels:
            await self._safe_send(ch, event)

    def bind_gateway(self, gateway: BroadcastGatewayProtocol) -> None:
        """绑定 Gateway（远程渠道的事件经过 Gateway 序列化发送）。"""
        self._gateway = gateway

    @property
    def channel_registry(self) -> ChannelRegistry:
        return self._registry

    # ══════════════════════════════════════════════════════════════════════════════
    # 生命周期驱动（★ 根本解 2026-06-10）
    # ══════════════════════════════════════════════════════════════════════════════

    async def start(self) -> None:
        """启动所有已注册 transport（幂等）。

        ★ 关键架构改造：把"启动每个 transport"的责任集中到 Broadcast。
          之前是 PandaPalApp 自己去调用每个 transport.start()，
          容易遗漏（我们刚刚就被这个 bug 坑了）。现在 Broadcast 是唯一入口。
          新增 transport 类型时，PandaPalApp 完全不需要修改。

        失败处理（HC3 Fail-Safe）：
          - 单个 transport.start() 失败不影响其他 transport
          - 也不阻塞 Broadcast 整体启动
          - 仅记 warning 日志
        """
        for ch in self._registry.list_active():
            t = ch.transport
            if t is None:
                continue
            if getattr(t, "is_started", False):
                # 幂等：已启动则跳过
                continue
            try:
                await t.start()
                logger.info(
                    "Broadcast.start: transport started (channel_id=%s, type=%s)",
                    ch.id, type(t).__name__,
                )
            except Exception as e:
                # HC3 Fail-Safe：单个 transport 失败不阻塞整体
                logger.warning(
                    "Broadcast.start: transport start failed (channel_id=%s, type=%s, err=%s) "
                    "— channel will run in offline mode",
                    ch.id, type(t).__name__, e,
                )

    async def stop(self) -> None:
        """停止所有已启动的 transport（幂等）。

        与 start() 对称。失败同样内部消化。
        """
        for ch in self._registry.list_active():
            t = ch.transport
            if t is None:
                continue
            if not getattr(t, "is_started", False):
                continue  # 幂等：未启动则跳过
            try:
                await t.stop()
                logger.info(
                    "Broadcast.stop: transport stopped (channel_id=%s, type=%s)",
                    ch.id, type(t).__name__,
                )
            except Exception as e:
                logger.warning(
                    "Broadcast.stop: transport stop error (channel_id=%s, type=%s, err=%s)",
                    ch.id, type(t).__name__, e,
                )

    def get_lifecycle_snapshot(self) -> list[dict[str, Any]]:
        """★ 自检用：返回所有 transport 的状态快照。

        PandaPalApp 启动后调一次，把"构造≠已启动"的事实写到日志里——
        避免之前那种"log 说 ready 但实际是 DISCONNECTED"的尴尬。

        Returns:
            list of {"channel_id": str, "transport": str, "is_started": bool}
        """
        snapshot: list[dict[str, Any]] = []
        for ch in self._registry.list_active():
            t = ch.transport
            snapshot.append({
                "channel_id": ch.id,
                "transport": type(t).__name__ if t is not None else "<None>",
                "is_started": bool(getattr(t, "is_started", False)) if t is not None else False,
            })
        return snapshot

    # ══════════════════════════════════════════════════════════════════════════════
    # Private Methods
    # ══════════════════════════════════════════════════════════════════════════════

    def _resolve_targets(
        self,
        event_type: EventType,
        origin_channel_id: str | None,
        target_channel_ids: tuple[str, ...] | None,
    ) -> list[ChannelInfo]:
        """五步过滤：R0指名 → R1 echo不回源 → R2会话主恒收 → 渠道策略 → 收集。"""
        active = self._registry.list_active()

        # ── R0 指名即达，指名发给谁：调用方显式指名 → 仅指名渠道收（最高优先，豁免一切策略）
        if target_channel_ids:
            return [ch for ch in active if ch.id in target_channel_ids]

        # ── origin 归属解析：直接命中渠道 id，或经 origin_aliases 前缀归属
        owner = self._resolve_owner(origin_channel_id, active)

        is_echo = event_type == EventType.USER_INPUT_ECHO
        targets: list[ChannelInfo] = []
        for ch in active:
            # ── R1 echo 永不回源（源渠道本地已显示，回发=重复）
            if is_echo and owner is not None and ch.id == owner.id:
                continue
            # ── R2 回复恒达会话主：origin 归属渠道恒收（覆盖 TARGET_ONLY）
            if owner is not None and ch.id == owner.id:
                targets.append(ch)
                continue
            # ── 渠道策略裁决：「别人的事件与全局事件我收不收」
            if self._channel_accepts(ch, origin_channel_id, owner):
                targets.append(ch)
        return targets

    @staticmethod
    def _resolve_owner(
        origin_channel_id: str | None,
        active: list[ChannelInfo],
    ) -> ChannelInfo | None:
        """解析 origin 归属渠道。

        origin=None/"" → None（全局事件，如定时任务）；
        直接命中渠道 id → 该渠道；
        命中某渠道的 origin_aliases 前缀 → 该渠道（如 xiaozhi:{device} 归属 xiaozhi）。
        未知 origin → None（保守按全局处理，保证可达性）。
        """
        if not origin_channel_id:
            return None
        for ch in active:
            if ch.id == origin_channel_id:
                return ch
        for ch in active:
            for prefix in ch.origin_aliases:
                if origin_channel_id.startswith(prefix):
                    return ch
        return None

    def _channel_accepts(
        self,
        ch: ChannelInfo,
        origin_channel_id: str | None,
        owner: ChannelInfo | None,
    ) -> bool:
        """渠道策略裁决：该渠道是否接收「别人的事件 / 全局事件」。"""
        policy = ch.dispatch_policy
        if isinstance(policy, ChannelDispatchPolicy):
            if policy == ChannelDispatchPolicy.SHARED:
                return True
            if policy == ChannelDispatchPolicy.SOURCE_ONLY:
                # 自己的事件已在 R2 处理；此处只剩全局事件（owner=None）放行
                return owner is None
            # TARGET_ONLY：只收 R0 指名（已短路）与 R2 会话主（已处理），其余拒收
            return False
        # 自定义谓词：异常 fail-open 按 SHARED 放行
        try:
            return bool(policy(origin_channel_id, owner, ch))
        except Exception as e:
            logger.warning(
                "Broadcast: channel %s policy predicate raised (%s) — fail-open as SHARED",
                ch.id, e,
            )
            return True

    async def _safe_send(self, ch: ChannelInfo, event: NormalizedEvent) -> None:
        """向单个渠道投递事件，失败必须内部消化（O3 Never Throw）。"""
        try:
            if ch.transport is not None:
                await ch.transport.send(event)
            else:
                # 渠道没有 transport（如 __hitl_bridge__ 内部 channel）—— 跳过
                logger.debug(
                    "Broadcast: channel %s has no transport, skipping %s",
                    ch.id, event.event_type.value,
                )
        except Exception as e:
            logger.warning(
                "Broadcast: channel=%s event=%s send error: %s",
                ch.id, event.event_type.value, e,
            )

    # ══════════════════════════════════════════════════════════════════════════════
    # Internal Helpers
    # ══════════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _generate_msg_id() -> str:
        """生成唯一消息 ID。"""
        return str(uuid.uuid4())
