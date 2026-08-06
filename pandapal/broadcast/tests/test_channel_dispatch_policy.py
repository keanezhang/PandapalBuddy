"""渠道分发策略（ChannelDispatchPolicy）测试。

分发模型（2026-06 渠道策略重构）：
  R0 指名即达：调用方显式 target_channel_ids → 仅指名渠道收（最高优先）
  R1 echo 永不回源：USER_INPUT_ECHO 永不发给其 origin 归属渠道
  R2 回复恒达会话主：非 echo 事件 → origin 归属渠道恒收（覆盖 TARGET_ONLY）
  渠道策略：SHARED / SOURCE_ONLY / TARGET_ONLY / 自定义谓词（fail-open）

本文件验证：
  1. R1：echo 不回源，SHARED 渠道收他人 echo，SOURCE_ONLY/TARGET_ONLY 不收
  2. R2：非 echo 事件会话主恒收（含 TARGET_ONLY 渠道）
  3. 三策略矩阵：对他人的事件与全局事件的收发行为
  4. R0：显式指名 → 仅指名渠道收（豁免一切策略）
  5. origin_aliases：xiaozhi:{device} 前缀归属到 wecom 渠道（R2 生效）
  6. 全局事件（origin=None）：SHARED/SOURCE_ONLY 收，TARGET_ONLY 不收
  7. 自定义谓词：正常裁决 + 抛异常时 fail-open 按 SHARED
  8. 默认 SHARED：不配置时行为 = 旧 BROADCAST 兼容
"""

from __future__ import annotations

import pytest

from pandapal.broadcast.broadcaster import MessageBroadcast
from pandapal.broadcast.channel_registry import (
    ChannelCapability,
    ChannelDispatchPolicy,
    ChannelInfo,
    ChannelRegistry,
    ChannelType,
)
from pandapal.events.normalized import NormalizedEvent


# ══════════════════════════════════════════════════════════════════════════════
# Test Helpers
# ══════════════════════════════════════════════════════════════════════════════


class MockTransport:
    """记录收到的事件。"""

    def __init__(self) -> None:
        self._started = True
        self.sent_events: list[NormalizedEvent] = []

    @property
    def is_started(self) -> bool:
        return self._started

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    async def send(self, event: NormalizedEvent) -> None:
        self.sent_events.append(event)


def _make_broadcast(*channels: ChannelInfo) -> MessageBroadcast:
    registry = ChannelRegistry()
    for ch in channels:
        registry.register(ch)
    return MessageBroadcast(registry=registry)


def _channel(
    channel_id: str,
    policy=ChannelDispatchPolicy.SHARED,
    aliases: tuple[str, ...] = (),
) -> tuple[ChannelInfo, MockTransport]:
    transport = MockTransport()
    ch = ChannelInfo(
        id=channel_id,
        type=ChannelType.LOCAL,
        capabilities=frozenset({ChannelCapability.TEXT}),
        transport=transport,
        dispatch_policy=policy,
        origin_aliases=aliases,
    )
    return ch, transport


def _reply_event() -> NormalizedEvent:
    return NormalizedEvent.reply_start(reply_id="r1", run_id="run1")


def _echo_event() -> NormalizedEvent:
    return NormalizedEvent.user_input_echo(
        user_id="u1", content="你好", session_id="s1",
    )


# ══════════════════════════════════════════════════════════════════════════════
# R1：echo 永不回源
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_r1_echo_never_returns_to_source():
    """echo 事件不发回 origin 归属渠道（源渠道本地已显示）。"""
    desktop, t_desktop = _channel("desktop")
    wecom, t_wecom = _channel("wecom")
    bc = _make_broadcast(desktop, wecom)

    await bc.send(_echo_event(), origin_channel_id="desktop")

    assert t_desktop.sent_events == []
    assert len(t_wecom.sent_events) == 1  # SHARED 渠道收他人 echo


@pytest.mark.asyncio
async def test_r1_echo_only_shared_channels_receive():
    """echo 对其他渠道按策略过滤：SHARED 收；SOURCE_ONLY/TARGET_ONLY 不收。"""
    desktop, t_desktop = _channel("desktop")
    shared, t_shared = _channel("shared_ch")
    src_only, t_src = _channel("src_ch", policy=ChannelDispatchPolicy.SOURCE_ONLY)
    tgt_only, t_tgt = _channel("tgt_ch", policy=ChannelDispatchPolicy.TARGET_ONLY)
    bc = _make_broadcast(desktop, shared, src_only, tgt_only)

    await bc.send(_echo_event(), origin_channel_id="desktop")

    assert t_desktop.sent_events == []   # R1 不回源
    assert len(t_shared.sent_events) == 1
    assert t_src.sent_events == []       # echo 的 owner 是别人 → 不收
    assert t_tgt.sent_events == []       # 未被指名 → 不收


# ══════════════════════════════════════════════════════════════════════════════
# R2：回复恒达会话主
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_r2_reply_always_reaches_owner_even_target_only():
    """非 echo 事件：origin 归属渠道恒收，即使它是 TARGET_ONLY。"""
    wecom, t_wecom = _channel("wecom", policy=ChannelDispatchPolicy.TARGET_ONLY)
    desktop, t_desktop = _channel("desktop", policy=ChannelDispatchPolicy.TARGET_ONLY)
    bc = _make_broadcast(wecom, desktop)

    await bc.send(_reply_event(), origin_channel_id="wecom")

    assert len(t_wecom.sent_events) == 1   # 会话主恒收（R2 覆盖 TARGET_ONLY）
    assert t_desktop.sent_events == []     # 别人的事件，TARGET_ONLY 不收


@pytest.mark.asyncio
async def test_r2_owner_receives_and_shared_also_receives():
    """会话主恒收 + SHARED 渠道也收他人事件（默认共享行为）。"""
    desktop, t_desktop = _channel("desktop")
    wecom, t_wecom = _channel("wecom")
    bc = _make_broadcast(desktop, wecom)

    await bc.send(_reply_event(), origin_channel_id="wecom")

    assert len(t_wecom.sent_events) == 1
    assert len(t_desktop.sent_events) == 1


# ══════════════════════════════════════════════════════════════════════════════
# 三策略矩阵：他人的事件（owner 是别的渠道）
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_policy_matrix_foreign_event():
    """他人事件：SHARED 收；SOURCE_ONLY 不收；TARGET_ONLY 不收。"""
    owner, t_owner = _channel("owner")
    shared, t_shared = _channel("shared_ch")
    src_only, t_src = _channel("src_ch", policy=ChannelDispatchPolicy.SOURCE_ONLY)
    tgt_only, t_tgt = _channel("tgt_ch", policy=ChannelDispatchPolicy.TARGET_ONLY)
    bc = _make_broadcast(owner, shared, src_only, tgt_only)

    await bc.send(_reply_event(), origin_channel_id="owner")

    assert len(t_owner.sent_events) == 1   # R2
    assert len(t_shared.sent_events) == 1
    assert t_src.sent_events == []
    assert t_tgt.sent_events == []


@pytest.mark.asyncio
async def test_policy_matrix_own_event():
    """自己的事件：三种策略都收（R2 恒达）。"""
    for policy in (
        ChannelDispatchPolicy.SHARED,
        ChannelDispatchPolicy.SOURCE_ONLY,
        ChannelDispatchPolicy.TARGET_ONLY,
    ):
        ch, transport = _channel("self_ch", policy=policy)
        bc = _make_broadcast(ch)
        await bc.send(_reply_event(), origin_channel_id="self_ch")
        assert len(transport.sent_events) == 1, f"{policy} 应收自己的事件"


# ══════════════════════════════════════════════════════════════════════════════
# 全局事件（origin=None，如定时任务）
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_global_event_shared_and_source_only_receive():
    """全局事件（owner=None）：SHARED/SOURCE_ONLY 收；TARGET_ONLY 不收。"""
    shared, t_shared = _channel("shared_ch")
    src_only, t_src = _channel("src_ch", policy=ChannelDispatchPolicy.SOURCE_ONLY)
    tgt_only, t_tgt = _channel("tgt_ch", policy=ChannelDispatchPolicy.TARGET_ONLY)
    bc = _make_broadcast(shared, src_only, tgt_only)

    await bc.send(_reply_event(), origin_channel_id=None)

    assert len(t_shared.sent_events) == 1
    assert len(t_src.sent_events) == 1     # SOURCE_ONLY 收全局事件（定时任务场景）
    assert t_tgt.sent_events == []


@pytest.mark.asyncio
async def test_unknown_origin_treated_as_global():
    """未知 origin（无渠道命中、无别名命中）保守按全局处理，保证可达性。"""
    src_only, t_src = _channel("src_ch", policy=ChannelDispatchPolicy.SOURCE_ONLY)
    tgt_only, t_tgt = _channel("tgt_ch", policy=ChannelDispatchPolicy.TARGET_ONLY)
    bc = _make_broadcast(src_only, tgt_only)

    await bc.send(_reply_event(), origin_channel_id="ghost_channel")

    assert len(t_src.sent_events) == 1     # 按全局事件放行
    assert t_tgt.sent_events == []


# ══════════════════════════════════════════════════════════════════════════════
# R0：指名即达（最高优先）
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_r0_explicit_targets_bypass_all_policies():
    """显式指名 → 仅指名渠道收（连 TARGET_ONLY 也能被指名触达）。"""
    shared, t_shared = _channel("shared_ch")
    tgt_only, t_tgt = _channel("tgt_ch", policy=ChannelDispatchPolicy.TARGET_ONLY)
    other, t_other = _channel("other_ch")
    bc = _make_broadcast(shared, tgt_only, other)

    await bc.send(
        _reply_event(),
        origin_channel_id="other_ch",
        target_channel_ids=("tgt_ch",),
    )

    assert t_shared.sent_events == []      # R0 之下 SHARED 也不收
    assert len(t_tgt.sent_events) == 1     # 指名即达
    assert t_other.sent_events == []       # origin 也不在指名集合则不收


@pytest.mark.asyncio
async def test_r0_targets_including_origin():
    """executor 参与者集合模式：{origin} ∪ targets 一并指名。"""
    desktop, t_desktop = _channel("desktop")
    wecom, t_wecom = _channel("wecom", policy=ChannelDispatchPolicy.TARGET_ONLY)
    bc = _make_broadcast(desktop, wecom)

    await bc.send(
        _reply_event(),
        origin_channel_id="desktop",
        target_channel_ids=("desktop", "wecom"),
    )

    assert len(t_desktop.sent_events) == 1
    assert len(t_wecom.sent_events) == 1


# ══════════════════════════════════════════════════════════════════════════════
# origin_aliases：前缀归属
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_origin_alias_resolves_owner():
    """xiaozhi:{device} 经别名归属 wecom → R2 恒达 wecom；isolated 的桌面不收。"""
    desktop, t_desktop = _channel(
        "desktop", policy=ChannelDispatchPolicy.SOURCE_ONLY,
    )
    wecom, t_wecom = _channel(
        "wecom",
        policy=ChannelDispatchPolicy.SOURCE_ONLY,
        aliases=("xiaozhi:",),
    )
    bc = _make_broadcast(desktop, wecom)

    await bc.send(_reply_event(), origin_channel_id="xiaozhi:device-001")

    assert len(t_wecom.sent_events) == 1   # 别名归属 → 会话主恒收
    assert t_desktop.sent_events == []     # 别人的事件，SOURCE_ONLY 不收


@pytest.mark.asyncio
async def test_origin_alias_echo_not_returned():
    """别名归属同样适用 R1：xiaozhi 的 echo 不回 wecom。"""
    desktop, t_desktop = _channel("desktop")
    wecom, t_wecom = _channel("wecom", aliases=("xiaozhi:",))
    bc = _make_broadcast(desktop, wecom)

    await bc.send(_echo_event(), origin_channel_id="xiaozhi:device-001")

    assert t_wecom.sent_events == []       # R1：echo 不回源（含别名归属源）
    assert len(t_desktop.sent_events) == 1


# ══════════════════════════════════════════════════════════════════════════════
# 自定义谓词
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_custom_predicate_controls_acceptance():
    """自定义谓词：返回 False 拒收他人事件，返回 True 接收。"""
    rejecting, t_reject = _channel("reject_ch", policy=lambda o, w, c: False)
    accepting, t_accept = _channel("accept_ch", policy=lambda o, w, c: True)
    owner, t_owner = _channel("owner")
    bc = _make_broadcast(rejecting, accepting, owner)

    await bc.send(_reply_event(), origin_channel_id="owner")

    assert t_reject.sent_events == []
    assert len(t_accept.sent_events) == 1
    assert len(t_owner.sent_events) == 1


@pytest.mark.asyncio
async def test_custom_predicate_exception_fail_open():
    """谓词抛异常 → fail-open 按 SHARED 放行（可用性优先 + warning 留痕）。"""
    def _boom(origin, owner, channel):
        raise RuntimeError("predicate exploded")

    fragile, t_fragile = _channel("fragile_ch", policy=_boom)
    owner, _t_owner = _channel("owner")
    bc = _make_broadcast(fragile, owner)

    await bc.send(_reply_event(), origin_channel_id="owner")

    assert len(t_fragile.sent_events) == 1  # fail-open 放行


# ══════════════════════════════════════════════════════════════════════════════
# 默认 SHARED 兼容
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_default_policy_is_shared_backward_compatible():
    """不显式配置 dispatch_policy → 默认 SHARED（= 旧 BROADCAST 行为）。"""
    ch = ChannelInfo(
        id="plain",
        type=ChannelType.LOCAL,
        capabilities=frozenset({ChannelCapability.TEXT}),
        transport=MockTransport(),
    )
    assert ch.dispatch_policy == ChannelDispatchPolicy.SHARED
    assert ch.origin_aliases == ()


@pytest.mark.asyncio
async def test_no_targets_returns_empty():
    """没有任何渠道时 send 静默返回（不抛异常）。"""
    bc = _make_broadcast()
    await bc.send(_reply_event(), origin_channel_id="desktop")  # 不抛即通过
