"""pandaren/memory/long_term.py — 长期记忆路由层（瘦身版）

LongTermMemory：管理 RawLogBackend 的薄路由层。

**v1.4 重构**：
  去 summary 化后，本层只剩 RawLog 路由，原本的 SummaryBackend / recall /
  store_session_summary / trigger_extraction 全部移除。

  raw_log 的语义重新定位为**离线分析数据源**：
    - 应用层定时任务通过 RawLogBackend.load_all() 读取
    - 提炼 User Model（个人画像）/ Episodic Archive（历史事件索引）
    - SDK 运行时不再做"跨 session 召回"

职责：
  1. session_id 运行时传入
  2. append_raw_message / append_compact_boundary：写日志
  3. load_for_restore：从 RawLogBackend 加载历史用于 STM 恢复

原则：
  B3  — Memory 层不调用 LLM
  E4  — 后端失败 log warning，不崩溃
  HC2 — 外部返回深拷贝
"""

from __future__ import annotations

import copy
import logging

from .models import MessageDict, CompactBoundaryDict
from .protocols import RawLogBackend
from .constants import DEFAULT_RESTORE_TOKEN_BUDGET

logger = logging.getLogger("pandaren.memory.long_term")


class LongTermMemory:
    """长期记忆路由层，封装 RawLogBackend。

    Args:
        raw_log_backend:    原始日志后端（None = 关闭原始日志持久化）
    """

    def __init__(
        self,
        raw_log_backend: RawLogBackend | None,
    ) -> None:
        self._raw_log = raw_log_backend

    # ── RawLogBackend 路由 ──

    def append_raw_message(
        self, message: MessageDict, session_id: str,
        run_id: str = "", step: int | None = None,
    ) -> None:
        """追加一条消息到原始日志。E4 失败降级。"""
        if self._raw_log is None:
            return
        try:
            self._raw_log.append_raw_message(
                message=message, session_id=session_id, run_id=run_id, step=step,
            )
        except Exception as exc:
            logger.warning(
                "LongTermMemory.append_raw_message failed (session_id=%s): %s",
                session_id, exc,
            )

    def append_compact_boundary(
        self, boundary: CompactBoundaryDict, session_id: str,
    ) -> None:
        """追加压缩边界标记。E4 失败降级。"""
        if self._raw_log is None:
            return
        try:
            self._raw_log.append_compact_boundary(
                boundary=boundary, session_id=session_id,
            )
        except Exception as exc:
            logger.warning(
                "LongTermMemory.append_compact_boundary failed (session_id=%s): %s",
                session_id, exc,
            )

    def load_for_restore(
        self,
        session_id: str,
        token_budget: int = DEFAULT_RESTORE_TOKEN_BUDGET,
    ) -> list[MessageDict]:
        """从 RawLogBackend 加载历史消息（用于 session restore）。

        E4：失败时返回空列表。HC2：返回深拷贝。
        """
        if self._raw_log is None:
            return []
        try:
            result = self._raw_log.load_within_budget(
                session_id=session_id, token_budget=token_budget,
            )
            return [copy.deepcopy(m) for m in result]  # HC2
        except Exception as exc:
            logger.warning(
                "LongTermMemory.load_for_restore failed (session_id=%s): %s",
                session_id, exc,
            )
            return []

    # ── 状态查询 ──

    @property
    def has_raw_log(self) -> bool:
        return self._raw_log is not None

    @property
    def raw_log_backend(self) -> RawLogBackend | None:
        return self._raw_log
