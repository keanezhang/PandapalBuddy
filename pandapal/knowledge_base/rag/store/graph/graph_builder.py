#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图谱构建器

统一管理知识图谱的构建，使用已提取的实体和关系构建图谱。

结果存储到Neo4j图谱数据库中。

【重要】
- 实体和关系必须在外部通过 LLM 提取（通常在 prepare_rag_data 阶段完成），然后传入本构建器
- 实体和关系对象中已包含所有必要信息（child_chunk_id、章节信息等）
- 不再需要传入父文档，所有信息都可以从实体和关系对象中获取
"""

from typing import List, Tuple, Optional

import logging

from ...schema import Entity, Relationship
from ...utils import build_parent_id
from .graph_store import GraphStore

logger = logging.getLogger(__name__)


class GraphBuilder:
    """
    图谱构建器
    
    负责:
    1. 使用已提取的实体和关系构建知识图谱
    2. 管理增量更新
    
    父文档必须经过 DocumentSplitter 分割,包含完整的章节/场景内容和元数据。
    实体和关系必须在外部通过 LLM 提取(通常在 prepare_rag_data 阶段完成),然后传入本构建器。
    """

    def __init__(
        self,
        graph_store: GraphStore,
        verbose: bool = True
    ):
        """
        初始化图谱构建器
        
        Args:
            graph_store: 图谱存储对象(通常是GraphStoreNeo4j)
            verbose: 是否输出详细信息
        """
        self.graph_store = graph_store
        self.verbose = verbose
        self._graph_store_initialized = False
        self._rebuilt = False

        if self.verbose:
            logger.info("GraphBuilder 初始化完成")

    def _ensure_initialized(self) -> None:
        """确保图谱存储已初始化（延迟初始化）"""
        if not self._graph_store_initialized:
            self.graph_store.initialize()
            self._graph_store_initialized = True

    def build_graph_batch(
        self,
        entities_batch: List[Entity],
        relationships_batch: List[Relationship],
        rebuild: bool = False,
    ) -> Tuple[int, int]:
        """
        按批写入实体和关系（供 build_graph_pipeline 断点续传时逐批调用）。

        Args:
            entities_batch: 本批实体列表
            relationships_batch: 本批关系列表
            rebuild: 是否在本批前清空图谱（仅首批且 rebuild 时为 True）

        Returns:
            (本批写入实体数, 本批写入关系数)
        """
        self._ensure_initialized()
        # rebuild 仅在首次生效（幂等）：避免断点续传逐批调用时反复清空已写入的数据
        if rebuild and not self._rebuilt:
            self.graph_store.clear()
            self._rebuilt = True
            if self.verbose:
                logger.info("已清空旧图谱数据，重新建立图谱数据")

        total_entities = 0
        total_relationships = 0
        for entity in entities_batch:
            try:
                self.graph_store.create_entity(entity)
                total_entities += 1
            except Exception:
                logger.warning("写入实体失败: %s", entity.name, exc_info=True)
        for relationship in relationships_batch:
            try:
                self.graph_store.create_relationship(
                    source=relationship.source,
                    target=relationship.target,
                    relationship=relationship,
                )
                total_relationships += 1
            except Exception:
                logger.warning(
                    "写入关系失败: %s -> %s",
                    relationship.source, relationship.target,
                    exc_info=True,
                )
        if entities_batch or relationships_batch:
            self.graph_store.save()
        return total_entities, total_relationships

    def _extract_parent_id(self, chapter_index: Optional[int], scene_index: Optional[int]) -> Optional[str]:
        """从章节/场景索引提取 parent_id（通用逻辑）。"""
        if chapter_index is not None:
            return build_parent_id(chapter_index, scene_index)
        return None

    def _extract_parent_id_from_entity(self, entity: Entity) -> Optional[str]:
        """从实体对象中提取 parent_id"""
        return self._extract_parent_id(entity.first_chapter_index, entity.first_scene_index)

    def _extract_parent_id_from_relationship(self, relationship: Relationship) -> Optional[str]:
        """从关系对象中提取 parent_id"""
        return self._extract_parent_id(relationship.chapter_index, relationship.scene_index)

    def update_graph(
        self,
        entities: List[Entity],
        relationships: List[Relationship]
    ) -> bool:
        """
        增量更新图谱
        
        Args:
            entities: 已提取的实体列表（必需）
            relationships: 已提取的关系列表（必需）
            
        Returns:
            是否成功
        """
        if self.verbose:
            logger.info("开始增量更新图谱...")

        self.build_graph_batch(
            entities_batch=entities,
            relationships_batch=relationships,
            rebuild=False,
        )
        return True

    def add_entity(self, entity: Entity) -> str:
        """
        添加实体
        
        Args:
            entity: 实体对象
            
        Returns:
            实体节点ID
        """
        self._ensure_initialized()
        entity_id = self.graph_store.create_entity(entity)
        self.graph_store.save()
        if self.verbose:
            logger.info("已添加实体: %s", entity.name)
        return entity_id

    def add_relationship(self, relationship: Relationship) -> str:
        """
        添加关系
        
        Args:
            relationship: 关系对象
            
        Returns:
            关系边ID
        """
        self._ensure_initialized()
        edge_id = self.graph_store.create_relationship(
            source=relationship.source,
            target=relationship.target,
            relationship=relationship
        )
        self.graph_store.save()
        if self.verbose:
            logger.info("已添加关系: %s -> %s", relationship.source, relationship.target)
        return edge_id
