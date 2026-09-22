#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多知识库管理：RAGEngineManager。

仅维护 name -> RAGEngine 映射，不读 settings，不负责创建 Engine。
调用方自行 RAGEngine(config) 后 manager.register("kb1", engine)。
"""

from __future__ import annotations

import logging
from threading import Lock
from typing import Dict, Iterator, List

from .rag_engine import RAGEngine
from .exceptions import RAGConfigError

logger = logging.getLogger(__name__)


class RAGEngineManager:
    """
    多知识库薄管理：仅维护 name -> RAGEngine 映射，不读 settings，不负责创建 Engine。
    调用方自行 RAGEngine(config) 后 manager.register("kb1", engine)。
    """
    __slots__ = ("_engines", "_lock")

    def __init__(self) -> None:
        self._engines: Dict[str, RAGEngine] = {}
        self._lock = Lock()

    def __contains__(self, name: str) -> bool:
        """支持 ``'kb1' in manager`` 语法。"""
        return name in self._engines

    def __len__(self) -> int:
        """返回已注册的知识库数量。"""
        return len(self._engines)

    def register(self, name: str, engine: RAGEngine) -> None:
        """Register a RAGEngine under the given name."""
        if not name or not isinstance(name, str):
            raise RAGConfigError("name must be a non-empty string")
        with self._lock:
            if name in self._engines:
                logger.warning("Overwriting existing engine %r; consider closing it first.", name)
            self._engines[name] = engine

    def get(self, name: str) -> RAGEngine:
        """Get a RAGEngine by name."""
        with self._lock:
            if name not in self._engines:
                raise KeyError(f"No registered engine: {name!r}")
            return self._engines[name]

    def unregister(self, name: str) -> None:
        """Unregister and close the engine with the given name."""
        with self._lock:
            if name in self._engines:
                try:
                    self._engines[name].close()
                except Exception:
                    logger.warning("Error closing engine %r", name, exc_info=True)
                del self._engines[name]

    def close_all(self) -> None:
        """Close all registered engines and clear the registry."""
        with self._lock:
            for name in list(self._engines):
                try:
                    self._engines[name].close()
                except Exception:
                    logger.warning("close engine %r failed during close_all", name, exc_info=True)
            self._engines.clear()

    def __enter__(self) -> "RAGEngineManager":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close_all()

    def __iter__(self) -> Iterator[str]:
        """Iterate over registered engine names."""
        return iter(self._engines)

    def list_names(self) -> List[str]:
        """返回已注册的知识库名称列表。"""
        return list(self._engines.keys())
