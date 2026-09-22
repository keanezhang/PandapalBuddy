#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAG 对外门面：RAGEngine 为唯一入口。

- 本文件不 import agent.config。
- 创建：RAGEngine(**kwargs) 或 RAGEngine(config=existing_config)；可选 engine.initialize() 延迟初始化。
- 查询：engine.query(query, k=5, call_llm=True, ...) 返回 RAGQueryResult。
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from .rag_config import RAGConfig
from .rag_instance import RAGInstance, RAGQueryResult
from .exceptions import RAGConfigError, RAGQueryError

# 阶段 3 方案 A：由 initialize() 统一设置 agent.rag 命名空间 log level，子模块用 logging.getLogger(__name__)
RAG_LOGGER_NAMESPACE = __name__.rsplit(".", 1)[0]


class RAGEngine:
    """
    RAG 纯 SDK 门面（Facade）。

    调用方只跟 RAGEngine 打交道，不需要知道 RAGConfig 的存在::

        # 最简：全部用默认值
        engine = RAGEngine()

        # 覆盖部分参数
        engine = RAGEngine(config=RAGConfig(enable_graph=False, neo4j_password="xxx"))

        # 高级用法：传入已构造好的 config
        config = RAGConfig(db_path="/my/path")
        engine = RAGEngine(config=config)

        # 查询
        result = engine.query("你好", k=5, call_llm=True)
    """

    def __init__(self, config: Optional[RAGConfig] = None, **kwargs: Any) -> None:
        """
        创建 RAGEngine。

        Args:
            config: 已构造好的 RAGConfig 对象。
                    若不传，则使用默认配置创建 RAGConfig。
                    若同时传入 **kwargs，将把它们作为 RAGConfig 的构造参数。
            **kwargs: 传给 RAGConfig 的命名参数（与 config 互斥）。
        """
        if config is not None and kwargs:
            raise RAGConfigError("config 和 **kwargs 不能同时传入，请选择其一")
        self._config = config if config is not None else RAGConfig(**kwargs)
        self._instance: Optional[RAGInstance] = None
        self._closed: bool = False

    @property
    def config(self) -> RAGConfig:
        """访问内部配置对象（只读）。"""
        return self._config

    def initialize(self) -> None:
        """根据 config 构建组件并加载索引；未调用则首次 query() 时自动调用。同时设置 agent.rag 命名空间 log level。"""
        if self._closed:
            raise RAGQueryError("RAGEngine 已关闭，无法初始化。请创建新的 RAGEngine 实例。")
        if self._instance is not None:
            return
        # 阶段 3 方案 A：统一设置 agent.rag 下所有 logger 的 level，避免各子模块 import settings
        root_rag = logging.getLogger(RAG_LOGGER_NAMESPACE)
        root_rag.setLevel(self._config.log_level)
        if not root_rag.handlers:
            root_rag.addHandler(logging.NullHandler())
        self._instance = RAGInstance.from_config(
            self._config,
            knowledge_base_name="default",
            try_load_bm25_cache=True,
            try_connect_graph=True,
        )

    async def query(
        self,
        query: str,
        k: Optional[int] = None,
        call_llm: bool = False,
        **kwargs: Any,
    ) -> RAGQueryResult:
        """委托 RAGInstance.query；若未初始化则先 initialize()。"""
        if self._closed:
            raise RAGQueryError("RAGEngine 已关闭，无法执行查询")
        if self._instance is None:
            self.initialize()
        if self._instance is None:
            raise RAGQueryError("RAGEngine 初始化失败，无法执行查询")
        return await self._instance.query(query, k=k, call_llm=call_llm, **kwargs)

    async def query_stream(
        self,
        query: str,
        k: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        流式查询：检索 → 逐 token 生成答案。委托 RAGInstance.query_stream()。

        Yields:
            Dict[str, Any]: 流式事件（retrieval_done / llm_delta / llm_done / error）。
        """
        if self._closed:
            raise RAGQueryError("RAGEngine 已关闭，无法执行查询")
        if self._instance is None:
            self.initialize()
        if self._instance is None:
            raise RAGQueryError("RAGEngine 初始化失败，无法执行查询")
        async for event in self._instance.query_stream(query, k=k, **kwargs):
            yield event

    async def search(
        self,
        query: str,
        query_analysis: Optional[Dict[str, Any]] = None,
        k: Optional[int] = None,
        **kwargs: Any,
    ):
        """
        纯检索入口：委托 RAGInstance.search()，返回原文片段，不调用 LLM 生成答案。

        语义理解（意图/实体/关系/策略/关键词）由外部 LLM 通过 ``query_analysis`` 提供；
        分词由内部固定 LLM（segmenter）补全。
        """
        if self._closed:
            raise RAGQueryError("RAGEngine 已关闭，无法执行检索")
        if self._instance is None:
            self.initialize()
        if self._instance is None:
            raise RAGQueryError("RAGEngine 初始化失败，无法执行检索")
        return await self._instance.search(query, query_analysis=query_analysis, k=k, **kwargs)

    def get_parent_documents(self, parent_ids: List[str]) -> List[Dict[str, Any]]:
        """按 parent_id 获取完整父文档（原文 + 元数据）。委托 RAGInstance.get_parent_documents()。"""
        if self._instance is None:
            raise RAGQueryError("RAGEngine 未初始化，无法获取父文档")
        return self._instance.get_parent_documents(parent_ids)

    def __enter__(self) -> "RAGEngine":
        """Support context manager: ``with RAGEngine(...) as engine:``"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def close(self) -> None:
        """释放索引连接与资源。"""
        if self._instance is not None:
            self._instance.close()
            self._instance = None
        self._closed = True
