#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图谱存储抽象接口

定义图谱存储的统一接口，使用Neo4j作为后端实现
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, Set

from ...schema import Entity, Relationship
from .graph_constants import DEFAULT_RELATIONSHIP_LIMIT


__all__ = ["GraphStore"]


class GraphStore(ABC):
    """
    图谱存储抽象基类
    
    定义图谱存储的统一接口，子类需要实现具体的存储逻辑
    """

    @abstractmethod
    def initialize(self) -> None:
        """初始化图数据库连接"""
        pass

    @abstractmethod
    def create_entity(self, entity: Entity) -> str:
        """
        创建实体节点
        
        Args:
            entity: 实体对象
            
        Returns:
            实体节点ID
        """
        pass

    @abstractmethod
    def create_relationship(
        self,
        source: str,
        target: str,
        relationship: Relationship
    ) -> str:
        """
        创建关系边
        
        Args:
            source: 源实体名称
            target: 目标实体名称
            relationship: 关系对象
            
        Returns:
            关系边ID
        """
        pass

    @abstractmethod
    def get_entity(self, entity_name: str) -> Optional[Dict[str, Any]]:
        """
        获取实体信息
        
        Args:
            entity_name: 实体名称
            
        Returns:
            实体信息字典，如果不存在返回None
        """
        pass

    @abstractmethod
    def query_relationships(
        self,
        entity: str,
        relation_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        查询关系
        
        Args:
            entity: 实体名称
            relation_type: 关系类型（可选，如果为None则返回所有关系）
            
        Returns:
            关系列表
        """
        pass

    @abstractmethod
    def query_path(
        self,
        source: str,
        target: str,
        max_depth: int = 3
    ) -> List[List[str]]:
        """
        查询路径
        
        Args:
            source: 源实体名称
            target: 目标实体名称
            max_depth: 最大深度
            
        Returns:
            路径列表（每个路径是一个实体名称列表）
        """
        pass

    @abstractmethod
    def get_entities_by_relation_type(self, relation_type: str) -> List[str]:
        """
        根据关系类型获取所有涉及的实体名称（用于社区聚合等场景）

        Args:
            relation_type: 关系类型（如 "MASTER"、"FRIEND" 等）

        Returns:
            实体名称列表（去重后）
        """
        pass

    @abstractmethod
    def get_top_entities_by_centrality(self, k: int) -> List[Dict[str, Any]]:
        """
        获取中心度最高的实体（中心度=关系数，按降序）

        Args:
            k: 返回前 k 个

        Returns:
            列表元素格式：
            {
              "entity": str,
              "centrality": int,
              "entity_info": Dict[str, Any]
            }
        """
        pass

    @abstractmethod
    def get_entity_statistics(self) -> Dict[str, Any]:
        """
        获取图谱统计信息

        Returns:
            {
              "entity_count": int,
              "relationship_count": int,
              "relation_type_distribution": Dict[str, int],
              "average_relationships_per_entity": float,
              "stats_note": Optional[str]
            }
        """
        pass

    @abstractmethod
    def get_all_entities(self) -> List[Dict[str, Any]]:
        """
        获取所有实体
        
        Returns:
            实体列表
        """
        pass

    @abstractmethod
    def get_entity_count(self) -> int:
        """
        获取实体数量
        
        Returns:
            实体数量
        """
        pass

    @abstractmethod
    def get_relationship_count(self) -> int:
        """
        获取关系数量
        
        Returns:
            关系数量
        """
        pass

    @abstractmethod
    def clear(self) -> None:
        """清空图谱"""
        pass

    @abstractmethod
    def close(self) -> None:
        """关闭图谱存储连接并释放资源。"""
        pass

    @abstractmethod
    def save(self) -> None:
        """保存图谱到磁盘（无持久化需求的后端可空实现）。"""
        ...

    @abstractmethod
    def load(self) -> None:
        """从磁盘加载图谱（无持久化需求的后端可空实现）。"""
        ...

    @abstractmethod
    def get_processed_parent_ids(
        self,
        book_title: Optional[str] = None
    ) -> Set[str]:
        """
        查询已处理的 parent_id 集合（用于断点续传）

        Args:
            book_title: 书名（可选，如果提供则只查询该书的章节）

        Returns:
            已处理的 parent_id 集合
        """
        pass

    def get_entity_by_alias(self, alias: str) -> Optional[Dict[str, Any]]:
        """
        通过别名获取实体信息。默认返回 None，子类可重写以支持别名查询。

        Args:
            alias: 别名（可能是标准名称或别名）

        Returns:
            实体信息字典，如果不存在返回 None
        """
        return None

    def get_entity_info(self, entity_name: str) -> Optional[Dict[str, Any]]:
        """
        获取实体信息（支持别名查询）。
        先按标准名查，若无则尝试 get_entity_by_alias，并附带关系统计。
        子类可重写以优化或支持别名。
        """
        entity_info = self.get_entity(entity_name)
        if not entity_info:
            entity_info = self.get_entity_by_alias(entity_name)
        if not entity_info:
            return None
        standard_name = entity_info.get("name", entity_name)
        relationships = self.query_relationships(standard_name)
        entity_info = dict(entity_info)
        entity_info["relationship_count"] = len(relationships)
        entity_info["relationships"] = relationships[:DEFAULT_RELATIONSHIP_LIMIT]
        return entity_info
