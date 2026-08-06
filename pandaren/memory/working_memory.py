"""pandaren/memory/working_memory.py — 工作记忆（session 级 KV 存储）

WorkingMemory：**session 级**键值存储，跨 run 自然保留；
切换 session 时由 ``set_session_id`` 清空内存并从新 session 的持久化文件
恢复；不同 session 的持久化文件物理隔离，不会互相污染。

【语义历史变更】
旧版：run 级清空 + ``RECENT_FILE_READS_WM_KEY`` 例外。
新版：统一为 session 级——run 结束不再自动清；run 级临时状态本来就
不应进入 KV 存储。需要显式清场（HITL 重置 / 调试等）请通过
``Memory.clear_working()`` 或直接调本类的 ``clear()``。

原则：
  HC1 — max_entries 初始化后不可变
  HC2 — get() 返回深拷贝
  O3  — 超容量时抛出 MemoryLimitError（不静默吞没）
"""

from __future__ import annotations

import copy
import logging
from typing import Any, TYPE_CHECKING

from .constants import DEFAULT_WORKING_MEMORY_MAX_ENTRIES
from .protocols import WorkingMemoryAccessor

if TYPE_CHECKING:
    from .protocols import WorkingMemoryBackend

logger = logging.getLogger("pandaren.memory.working_memory")


class MemoryLimitError(Exception):
    """工作记忆超出容量限制时抛出。"""


class WorkingMemory:
    """session 级键值存储，实现 WorkingMemoryAccessor 协议。

    跨 run 自然保留：同一 session 内的多个 run 共享 KV；切换 session 时
    由 ``set_session_id`` 自动清空内存并从新 session 的持久化文件恢复。

    Args:
        max_entries: 最大条目数（默认 DEFAULT_WORKING_MEMORY_MAX_ENTRIES=1000）。
        backend: 工作记忆持久化后端（None = 不持久化，纯内存）。
    """

    def __init__(
        self,
        max_entries: int = DEFAULT_WORKING_MEMORY_MAX_ENTRIES,
        backend: "WorkingMemoryBackend | None" = None,
    ) -> None:
        self._max_entries: int = max_entries  # HC1: 赋值后不再修改
        self._backend: WorkingMemoryBackend | None = backend
        self._current_session_id: str = ""
        self._store: dict[str, Any] = {}
        self._initialized = True  # HC1：构造完成，冻结 _max_entries

    def __setattr__(self, name: str, value: Any) -> None:
        if name != "_initialized" and getattr(self, "_initialized", False) and name == "_max_entries":
            raise AttributeError(
                f"WorkingMemory.{name} is frozen after initialization (HC1). "
                f"Cannot modify configuration field after construction."
            )
        object.__setattr__(self, name, value)

    # ── WorkingMemoryAccessor Protocol ──

    def get(self, key: str) -> Any | None:
        """读取工作记忆中的值（深拷贝，HC2）。key 不存在时返回 None。"""
        val = self._store.get(key)
        if val is None:
            return None
        return copy.deepcopy(val)  # HC2

    def set(self, key: str, value: Any) -> None:
        """写入工作记忆中的值。

        如果 key 已存在则覆盖（不计入新增容量检查）。
        如果 key 不存在且已达上限，抛出 MemoryLimitError（O3）。
        """
        if key not in self._store and len(self._store) >= self._max_entries:
            raise MemoryLimitError(
                f"WorkingMemory: max_entries={self._max_entries} reached. "
                f"Cannot set key={key!r}."
            )
        self._store[key] = copy.deepcopy(value)  # HC2: 输入防御，防止调用方事后修改
        self._persist_key(key, value)

    # ── 持久化辅助 ──

    def set_session_id(self, session_id: str) -> None:
        """设置当前 session_id（供 Memory Facade 在 init_from_restore 时调用）。

        切换 session 时：
          1. 先清空内存中的 KV store（仅清内存，不删除旧 session 的持久化文件）
          2. 更新 _current_session_id
          3. 从 backend 恢复新 session 的 KV 数据

        为什么不删除旧 session 的文件？
          → WM 文件是 session 级持久化的，切换走后不应删除，
            这样下次切回来时 _restore_from_backend 才能恢复数据。
        """
        if self._current_session_id == session_id:
            return
        # 仅清空内存，不触发持久化（旧 session 文件保留，新 session 尚未写入）
        self._store.clear()
        self._current_session_id = session_id
        self._restore_from_backend(session_id)

    def _persist_key(self, key: str, value: Any) -> None:
        """将单个 KV 条目持久化到 backend。"""
        if self._backend is None or not self._current_session_id:
            return
        try:
            self._backend.save(key, value, session_id=self._current_session_id)
        except Exception as exc:
            logger.warning("WorkingMemory._persist_key: backend.save failed: %s", exc)

    def _persist_all(self) -> None:
        """将当前所有 KV 条目持久化到 backend（覆盖写入）。"""
        if self._backend is None or not self._current_session_id:
            return
        try:
            self._backend.save_all(self._store, session_id=self._current_session_id)
        except Exception as exc:
            logger.warning("WorkingMemory._persist_all: backend.save_all failed: %s", exc)

    def _restore_from_backend(self, session_id: str) -> None:
        """从 backend 恢复指定 session 的 KV 数据。"""
        if self._backend is None or not session_id:
            return
        try:
            data = self._backend.load(session_id=session_id)
            if data:
                self._store.update(data)
                logger.debug(
                    "WorkingMemory._restore_from_backend: restored %d keys (session_id=%s)",
                    len(data), session_id,
                )
        except Exception as exc:
            logger.warning("WorkingMemory._restore_from_backend: backend.load failed: %s", exc)

    # ── 管理接口（Loop 使用）──

    def clear(self, *, except_keys: frozenset[str] | set[str] | None = None) -> None:
        """清空工作记忆（显式清场入口）。

        SDK 不再在 run 结束时自动调用——WorkingMemory 现在是 session 级语义，
        跨 run 自然保留。本方法保留为应用层的显式清场入口（HITL 重置 / 调试等）。

        Args:
            except_keys: 保留这些 key 不清空。默认为 None = 清空所有。
                         保留 ``except_keys`` 参数为高级用法（应用层选择性清场）。
        """
        if not except_keys:
            self._store.clear()
            self._persist_all_after_clear()
            return
        # 保留指定 key
        preserved = {k: self._store[k] for k in except_keys if k in self._store}
        self._store.clear()
        self._store.update(preserved)
        self._persist_all_after_clear()

    def _persist_all_after_clear(self) -> None:
        """clear() 后将剩余 KV 持久化（覆盖写入，确保已删除的 key 从文件中移除）。"""
        if self._backend is None or not self._current_session_id:
            return
        if self._store:
            # 还有保留的 key，覆盖写入
            self._persist_all()
        else:
            # 全部清空，删除文件
            try:
                self._backend.delete_session(session_id=self._current_session_id)
            except Exception as exc:
                logger.warning("WorkingMemory._persist_all_after_clear: backend.delete_session failed: %s", exc)

    def snapshot(self) -> dict[str, Any]:
        """返回当前状态的深拷贝（HC2），用于调试或 HITL 序列化。"""
        return copy.deepcopy(self._store)

    def restore(self, data: dict[str, Any]) -> None:
        """从字典恢复状态（HITL resume 时使用）。"""
        self._store = dict(data)
        self._persist_all()

    @property
    def size(self) -> int:
        """当前条目数。"""
        return len(self._store)

    @property
    def accessor(self) -> WorkingMemoryAccessor:
        """返回 WorkingMemoryAccessor 视图（即 self，因为实现了协议）。"""
        return self  # type: ignore[return-value]

    @property
    def backend(self) -> "WorkingMemoryBackend | None":
        """返回持久化后端引用（供 Memory Facade 读取）。"""
        return self._backend
