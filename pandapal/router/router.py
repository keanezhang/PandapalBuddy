"""MessageRouter — 本地注入消息路由与去重（架构第 2 层）。

═════════════════════════════════════════════════════════════════════════════
  定位（v2.0 瘦身后）：本地注入消息总线 + 路由 + 去重
═════════════════════════════════════════════════════════════════════════════

  v2.0 变更：远程 Gateway 入站链路已迁出本模块——
    - frame 解析/校验 → gateway/inbound_adapter.py（GatewayInboundAdapter）
    - 分类分发        → dispatch/dispatcher.py（InboundDispatcher）
  远程消息经 GatewayInboundAdapter 归一 → dispatcher 分流后，Router 类型最终仍
  回落本模块的 inject_inbound_message()。本模块不再接触原始 frame / Gateway。

  核心职责：
  ┌─────────────────────────────────────────────────────────────────┐
  │  1. 本地注入：inject_inbound_message（HITL/Scheduler 消息总线） │
  │  2. 去重保护：防止重复处理（I3 幂等）                            │
  │  3. 类型路由：按 message_type 分发到对应 handler                │
  │  4. 超时保护：所有 handler 调用都有超时熔断（I5）               │
  └─────────────────────────────────────────────────────────────────┘

  架构位置：
  ┌──────────────────┐   ┌──────────────────┐
  │ GatewayInbound   │   │ HITLBridge /     │ ← 本地注入源
  │ Adapter→Dispatcher│  │ Scheduler /      │
  │ （远程入站）      │   │ TaskScheduler    │
  └────────┬─────────┘   └────────┬─────────┘
           │ Router 类型           │ inject_inbound_message
           └──────────┬────────────┘
                      ▼
              ┌──────────┐
              │  Router  │ ← 本模块（去重 + 路由），按消息类型调用业务 handler
              └────┬─────┘
                   │ _route_and_execute()
                   ▼
              ┌──────────┐
              │ Scheduler│ ← 业务逻辑层（Agent 调度）
              └──────────┘

设计约束详解：
- I1 (Fail Fast): freeze() 时路由表为空 → 立即抛出 RouterConfigError
  - 目的：提前暴露配置错误，避免运行时无 handler 可用
- I3 (Idempotent): 同 msg_id 在 TTL 内只处理一次
  - 实现：_dedup_window (OrderedDict) + TTL 过期 + 容量驱逐
- I4 (Externalized Config): TTL/max_size/timeout 均可配置
  - dedup_ttl_seconds: 去重窗口 TTL（默认 600s）
  - dedup_max_size: 去重窗口最大条目数（默认 10000）
  - handler_timeout_seconds: handler 超时时间（默认 600s）
- I5 (Timeout Everywhere): 所有 handler 调用都有超时保护
  - 实现：asyncio.wait_for(handler(msg), timeout=...)
- BL1 (Single Responsibility): 只做 去重+路由，不解析 frame、不执行业务逻辑

v1.1 收紧的约束（保留）：
- register_route_handler 在 freeze() 之后调用 → raise RouterStateError（避免运行时竞态）
- inject_inbound_message 强校验 source_channel_id 白名单 → 防权限旁路
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from pandapal.broadcast.channel_ids import (
    ALLOWED_REMOTE_CHANNEL_PATTERNS,
    LOCAL_DESKTOP_IPC_CHANNEL_ID,
    LOCAL_HITL_CHANNEL_ID,
    LOCAL_SCHEDULER_CHANNEL_ID,
)
from pandapal.router.models import (
    InboundMessage,
    RouterConfigError,
    RouterPermissionError,
    RouterStateError,
)

logger = logging.getLogger(__name__)

# message_type 格式规则：英文字母+下划线+数字，1-64 字符
_MESSAGE_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# inject_inbound_message 允许的本地虚拟渠道集合（来自 broadcast.channel_ids）
# 远程渠道通过 ALLOWED_REMOTE_CHANNEL_PATTERNS 走精确/前缀匹配
_LOCAL_INJECT_WHITELIST: frozenset[str] = frozenset({
    LOCAL_DESKTOP_IPC_CHANNEL_ID, # "__desktop_ipc__"
    LOCAL_HITL_CHANNEL_ID,        # "__hitl_bridge__"
    LOCAL_SCHEDULER_CHANNEL_ID,   # "__scheduler__"
})


def _is_valid_inject_source_channel(channel_id: str) -> bool:
    """判断 inject_inbound_message 的 source_channel_id 是否合法。

    合法定义（与 broadcast.channel_ids 单一权威来源对齐）：
      ① 三个本地虚拟渠道之一（`__desktop_ipc__` / `__hitl_bridge__` / `__scheduler__`）
      ② 符合远程白名单模式（精确：`"wecom"`；前缀：`"xiaozhi:..."`）

    背景：inject_inbound_message 是同进程的本地短路径，被 HITLBridge、
    Scheduler 共同使用作为内部消息总线。该校验防御未在白名单中的伪造 channel_id
    （如 "fake_channel"、"test"），避免成为权限旁路。
    """
    # ① 本地虚拟渠道
    if channel_id in _LOCAL_INJECT_WHITELIST:
        return True
    # ② 远程白名单（精确匹配 + 前缀匹配，复用 ChannelRegistry 的语义）
    for pattern in ALLOWED_REMOTE_CHANNEL_PATTERNS:
        if pattern.endswith(":"):  # 前缀匹配
            if channel_id.startswith(pattern) and len(channel_id) > len(pattern):
                return True
        else:                       # 精确匹配
            if channel_id == pattern:
                return True
    return False


class MessageRouter:
    """本地注入消息路由器（去重 + 路由 + 超时保护）。

    核心数据结构：
    - _route_table: dict[str, Callable] — 消息类型 → handler 映射表
    - _dedup_window: OrderedDict[str, datetime] — 去重窗口（msg_id → 处理时间）
    - _frozen: bool — 注册冻结标志（freeze() 后禁止再注册）

    使用方式：
        # Step1: 创建路由器
        router = MessageRouter(dedup_ttl_seconds=600)

        # Step2: 注册消息处理器
        async def handle_user_instruction(msg: InboundMessage) -> None:
            ...
        router.register_route_handler(RouterMessageType.USER_INSTRUCTION, handle_user_instruction)

        # Step3: 冻结注册表（所有 register 完成后）
        router.freeze()

        # Step4: 启动后，inject_inbound_message() 把本地消息路由到对应 handler
    """

    def __init__(
        self,
        dedup_ttl_seconds: int = 600,
        dedup_max_size: int = 10000,
        handler_timeout_seconds: float = 600.0,
        max_payload_bytes: int = 1_048_576,
    ) -> None:
        # ══════════════════════════════════════════════════════════════════
        # 路由表：message_type → handler 映射
        # ══════════════════════════════════════════════════════════════════
        # key:   message_type 字符串（如 "user_instruction", "hitl_decision"）
        # value:  async 回调函数，签名：async (InboundMessage) -> None
        self._route_table: dict[str, Callable[[InboundMessage], Awaitable[None]]] = {}

        # ══════════════════════════════════════════════════════════════════
        # 去重窗口：防止重复处理同一条消息
        # ══════════════════════════════════════════════════════════════════
        # 使用 OrderedDict 实现 FIFO 淘汰：
        #   - 新消息追加到末尾（move_to_end）
        #   - 超容量时淘汰最旧的（popitem(last=False)）
        #   - O(1) 查找 + O(1) 淘汰
        self._dedup_window: OrderedDict[str, datetime] = OrderedDict()
        self._dedup_ttl_seconds = dedup_ttl_seconds      # 去重窗口 TTL（默认 600s）
        self._dedup_max_size = dedup_max_size            # 去重窗口最大容量（默认 10000）
        self._handler_timeout_seconds = handler_timeout_seconds  # handler 超时时间（默认 600s）

        # ══════════════════════════════════════════════════════════════════
        # 注册冻结标志（v2.0：替代旧 attach_to_gateway 的 _gateway 判据）
        # ══════════════════════════════════════════════════════════════════
        # freeze() 置 True 后，register_route_handler 拒绝再注册（RouterStateError），
        # 防止运行时动态注册与并发执行的 inject/_route_and_execute 产生查表竞态。
        self._frozen: bool = False

        # ══════════════════════════════════════════════════════════════════
        # 历史兼容参数（v2.0 起不再使用）
        # ══════════════════════════════════════════════════════════════════
        # payload 大小检查已随 Gateway 入站迁至 GatewayInboundAdapter（远程不可信
        # 来源需防 OOM）；本地 inject_inbound_message 来自可信内部模块，不做大小检查。
        # 保留该参数仅为不破坏既有构造方签名，router 内部不再消费。
        _ = max_payload_bytes

    # ──────────────────────────────────────────────
    # Public Methods — 注册与冻结
    # ──────────────────────────────────────────────

    def register_route_handler(
        self,
        message_type: str,
        handler: Callable[[InboundMessage], Awaitable[None]],
    ) -> None:
        """注册消息类型与其处理器的绑定。

        调用时机：Bootstrap 装配阶段（_init_scheduler, _init_hitl）
        - Scheduler 注册：TASK_INSTRUCTION → scheduler.handle_task_instruction
        - HITL Bridge 注册：HITL_DECISION → hitl_bridge.handle_decision

        参数：
        - message_type: 消息类型字符串（⚠️ 建议使用 RouterMessageType.* 常量）
        - handler: 异步回调函数，签名：async (InboundMessage) -> None

        Raises:
            ValueError: message_type 格式非法（必须匹配 [a-z][a-z0-9_]{0,63}）
            RouterStateError: 在 freeze() 之后再次调用（v1.1 收紧）
                — 避免运行时动态注册与正在并发执行的 inject/_route_and_execute 产生竞态。

        注意：
        - 重复注册同 message_type 会覆盖旧 handler（WARN 日志含原/新 handler 的 __qualname__）
        - 必须在 freeze() 之前完成所有注册
        """
        # v1.1: 生命周期约束 — freeze 之后禁止再注册（防止与 dispatch 并发竞态）
        if self._frozen:
            raise RouterStateError(
                "register_route_handler must be called before freeze "
                f"(attempted to register message_type='{message_type}')"
            )

        # 校验 message_type 格式（防止手滑打错）
        if not _MESSAGE_TYPE_PATTERN.match(message_type):
            raise ValueError(
                f"Invalid message_type format: '{message_type}'. "
                f"Must match [a-z][a-z0-9_]{{0,63}}"
            )

        if message_type in self._route_table:
            old_handler = self._route_table[message_type]
            old_qualname = getattr(old_handler, "__qualname__", repr(old_handler))
            new_qualname = getattr(handler, "__qualname__", repr(handler))
            logger.warning(
                "Duplicate message_type registration: '%s' overwritten (%s → %s)",
                message_type,
                old_qualname,
                new_qualname,
            )

        self._route_table[message_type] = handler
        logger.debug("Route handler registered: %s", message_type)

    def freeze(self) -> None:
        """冻结注册表，进入运行期只读（I1 快速失败）。

        调用时机：Bootstrap 装配阶段，所有 register_route_handler 完成后。

        Raises:
            RouterConfigError: 路由表为空（没有注册任何 handler）

        幂等性：重复调用仅记录日志，不抛异常。
        """
        # I1: 快速失败 — 路由表为空说明配置错误
        if not self._route_table:
            raise RouterConfigError(
                "No handlers registered. "
                "Call register_route_handler before freeze."
            )
        if self._frozen:
            logger.debug("MessageRouter already frozen, skipping")
            return
        self._frozen = True
        logger.info(
            "MessageRouter frozen, registered types: %s",
            frozenset(self._route_table.keys()),
        )

    def get_registered_message_types(self) -> frozenset[str]:
        """获取已注册的消息类型集合（只读）。

        用途：
        - 调试：查看当前路由表注册了哪些消息类型
        - 测试：验证 handler 注册是否成功
        """
        return frozenset(self._route_table.keys())

    def get_dedup_window_size(self) -> int:
        """获取当前去重窗口条目数（可观测性）。

        用途：
        - 监控：查看去重窗口当前大小
        - 调试：判断是否需要调整 dedup_max_size
        """
        return len(self._dedup_window)

    async def inject_inbound_message(self, msg: InboundMessage) -> None:
        """本地直接注入入站消息（同进程内部消息总线）。

        设计定位（v1.1 修正 — 与 channel_ids 单一白名单对齐）：
            inject_inbound_message 是"同进程本地短路径"，被以下模块共同使用：
              - Desktop IPC 层（source_channel_id = "__desktop_ipc__"，经 dispatcher 回落）
              - HITL Bridge（source_channel_id = "__hitl_bridge__"）
              - Scheduler（注入审批消息时使用真实外部渠道 ID，如 "wecom"、"xiaozhi:xxx"）

        强校验（v1.1，复用 broadcast.channel_ids 白名单）：
            msg.source_channel_id 必须满足以下条件之一，否则 raise RouterPermissionError：
              ① 三个本地虚拟渠道（__desktop_ipc__ / __hitl_bridge__ / __scheduler__）
              ② 远程白名单匹配（"wecom" 精确 / "xiaozhi:..." 前缀）
            目的：防御伪造的 channel_id（如 "test"、"fake_channel"）通过 inject 路径
                 绕过 Gateway 鉴权，伪造任意 user_id / message_type。

        流程：
        1. 强校验 source_channel_id 在白名单内
        2. 去重检查（_check_and_mark_processed）
        3. 路由并执行（_route_and_execute）

        Raises:
            RouterPermissionError: msg.source_channel_id 不在 channel_ids 白名单内。
        """
        # v1.1: 强校验 source_channel_id 在 channel_ids 白名单内（防伪造旁路）
        if not _is_valid_inject_source_channel(msg.source_channel_id):
            raise RouterPermissionError(
                "inject_inbound_message rejected: source_channel_id "
                f"'{msg.source_channel_id}' is not in channel_ids whitelist "
                f"(allowed locals: {sorted(_LOCAL_INJECT_WHITELIST)}, "
                f"allowed remote patterns: {sorted(ALLOWED_REMOTE_CHANNEL_PATTERNS)}); "
                f"msg_id={msg.msg_id}"
            )

        # Step 1: 去重检查
        if self._check_and_mark_processed(msg.msg_id):
            logger.debug("msg_id %s is duplicate, skipped", msg.msg_id)
            return

        # Step 2: 路由并执行
        await self._route_and_execute(msg)

    # ──────────────────────────────────────────────
    # Private Methods — 路由与执行
    # ──────────────────────────────────────────────

    async def _route_and_execute(self, msg: InboundMessage) -> None:
        """按 message_type 路由到 handler 并执行（含超时和异常隔离）。

        设计要点：
        - I5 (Timeout Everywhere): handler 调用有超时保护
        - 异常隔离：单个 handler 异常不影响其他消息处理

        流程：
        1. 查找 handler（按 message_type）
        2. 未找到 → 记录 warning 并丢弃
        3. 找到 → 调用 handler(msg)（带超时）
        4. 超时 → 记录 error 日志
        5. 异常 → 记录 exception 日志（包含堆栈）
        """
        # Step 1: 查找 handler
        handler = self._route_table.get(msg.message_type)
        if handler is None:
            logger.warning(
                "Unknown message_type '%s' from channel '%s', discarding",
                msg.message_type,
                msg.source_channel_id,
            )
            return  # 未注册的消息类型，丢弃

        logger.info(
            "[Router] Dispatching to handler: msg_id=%s, type=%s, handler=%s",
            msg.msg_id, msg.message_type, handler.__name__ if hasattr(handler, '__name__') else str(handler),
        )

        # Step 2-5: 执行 handler（带超时和异常隔离）
        try:
            # I5: 超时保护（防止 handler 卡死）
            await asyncio.wait_for(
                handler(msg), timeout=self._handler_timeout_seconds
            )
            logger.debug(
                "Message '%s' routed to '%s' handler",
                msg.msg_id,
                msg.message_type,
            )
        except asyncio.TimeoutError:
            # handler 超时
            logger.error(
                "Handler for '%s' timed out after %.1fs (msg_id=%s)",
                msg.message_type,
                self._handler_timeout_seconds,
                msg.msg_id,
            )
            # 注意：超时后 handler 仍在后台运行（fire-and-forget）
        except Exception as e:
            # handler 抛异常（已隔离，不影响其他消息）
            logger.exception(
                "Handler for '%s' raised exception (msg_id=%s): %s",
                msg.message_type,
                msg.msg_id,
                e,
            )

    def _check_and_mark_processed(self, msg_id: str) -> bool:
        """检查消息是否已处理，并标记（I3 幂等性保证）。

        去重算法（TTL + FIFO 双级驱逐）：
        ┌─────────────────────────────────────────────────────┐
        │  1. 检查 msg_id 是否在 _dedup_window 中          │
        │     - 在 → 检查是否未过期（TTL 内）             │
        │       - 未过期 → 返回 True（重复消息）           │
        │       - 已过期 → 覆盖（更新时间戳）               │
        │     - 不在 → 继续                                │
        │  2. 容量检查（仅达到上限时才驱逐）               │
        │  3. 标记为已处理（写入/更新时间戳）              │
        │  4. 返回 False（新消息）                        │
        └─────────────────────────────────────────────────────┘

        性能优化：
        - 使用 OrderedDict：O(1) 查找 + O(1) 淘汰
        - 惰性驱逐：仅在达到容量上限时才触发 O(n) 扫描
        - move_to_end：保持插入顺序最新（FIFO 淘汰时用）

        Args:
            msg_id: 消息唯一 ID

        Returns:
            True  = 重复消息（已处理过，TTL 内）
            False = 新消息（首次处理）
        """
        now = datetime.now(timezone.utc)
        ttl = timedelta(seconds=self._dedup_ttl_seconds)

        # Step 1: 是否已存在且未过期？
        if msg_id in self._dedup_window:
            if (now - self._dedup_window[msg_id]) < ttl:
                return True  # 重复，TTL 内
            # 已过期，fall through 覆盖（更新时间戳）

        # Step 2: 容量检查（仅在达到上限时才驱逐）
        if len(self._dedup_window) >= self._dedup_max_size:
            self._evict_expired_dedup_entries(now)

        # Step 3: 标记为已处理
        self._dedup_window[msg_id] = now
        self._dedup_window.move_to_end(msg_id)  # 保持插入顺序最新
        return False  # 新消息

    def _evict_expired_dedup_entries(self, now: datetime) -> None:
        """驱逐过期或超容量的去重条目（两级驱逐策略）。

        驱逐策略：
        ┌─────────────────────────────────────────────────────┐
        │  策略 1: TTL 驱逐（惰性清理）                    │
        │    - 删除超过 TTL 的条目                         │
        │    - 避免无限增长                               │
        │                                                   │
        │  策略 2: FIFO 容量驱逐（强制清理）              │
        │    - 如果 TTL 驱逐后仍然超容量                   │
        │    - 淘汰最旧的条目（OrderedDict 首部）          │
        │    - O(1) 操作（popitem(last=False)）          │
        └─────────────────────────────────────────────────────┘

        v1.1 改进：日志区分 n_expired（按 TTL 清理）与 n_fifo（按容量强制驱逐），
        便于运维判断驱逐原因——n_fifo > 0 提示容量不足需要调高 dedup_max_size，
        n_expired 高仅说明流量正常老化，无需干预。

        性能特点：
        - OrderedDict 保证插入顺序 = 时间顺序
        - popitem(last=False) 是 O(1) 操作（淘汰最旧）
        - 至少驱逐 1 条（为新条目腾位）

        Args:
            now: 当前时间（UTC）
        """
        ttl = timedelta(seconds=self._dedup_ttl_seconds)

        # 策略 1: TTL 驱逐（惰性清理）
        expired_ids = [
            mid for mid, ts in self._dedup_window.items()
            if (now - ts) >= ttl
        ]
        for mid in expired_ids:
            del self._dedup_window[mid]
        n_expired = len(expired_ids)

        # 策略 2: FIFO 容量驱逐（为新条目预留空间）
        n_fifo = 0
        if len(self._dedup_window) >= self._dedup_max_size:
            # 至少驱逐 1 条（为新条目腾位）
            # OrderedDict 插入顺序即时间顺序，popitem(last=False) 是 O(1) 淘汰最旧
            n_fifo = max(1, len(self._dedup_window) - self._dedup_max_size + 1)
            for _ in range(n_fifo):
                self._dedup_window.popitem(last=False)

        # v1.1: 日志区分两类驱逐原因（便于运维判断容量是否需要调高）
        if n_expired > 0 or n_fifo > 0:
            logger.warning(
                "Dedup window evicted %d expired + %d FIFO entries "
                "(remaining size=%d, max=%d)",
                n_expired,
                n_fifo,
                len(self._dedup_window),
                self._dedup_max_size,
            )
