#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Neo4j图谱存储实现

使用Neo4j作为图数据库后端，利用Cypher约束保证数据完整性。
要求Neo4j 4.3+版本，使用新的约束语法。
"""

from collections import defaultdict
from typing import List, Dict, Any, Optional, Set, Tuple
import re

try:
    from neo4j import GraphDatabase  # type: ignore[import]

    NEO4J_AVAILABLE = True
except ImportError:
    NEO4J_AVAILABLE = False
    GraphDatabase = None

from .graph_store import GraphStore
from ...schema import Entity, Relationship
from .entity_type import (
    NODE_LABEL_PERSON,
    NODE_LABEL_LOCATION,
    NODE_LABEL_ORGANIZATION,
    NODE_LABEL_EVENT,
    ENTITY_LABEL_TO_NODE_LABEL
)
from .graph_constants import MAX_RESULTS_LIMIT
from ...utils import build_parent_id
import logging

logger = logging.getLogger(__name__)

# 本模块专用：每个实体最大来源文本数
MAX_SOURCE_TEXTS_PER_ENTITY: int = 10

# 路径查询（仅本模块）
MIN_PATH_DEPTH: int = 1
MAX_PATH_DEPTH: int = 10
DEFAULT_MAX_PATH_DEPTH: int = 3
DEFAULT_PATH_LIMIT: int = 10

# Neo4j 约束名（与 entity_type 节点标签一一对应）
CONSTRAINT_PERSON_NAME_UNIQUE: str = "person_name_unique"
CONSTRAINT_PERSON_NAME_EXISTS: str = "person_name_exists"
CONSTRAINT_LOCATION_NAME_UNIQUE: str = "location_name_unique"
CONSTRAINT_LOCATION_NAME_EXISTS: str = "location_name_exists"
CONSTRAINT_ORGANIZATION_NAME_UNIQUE: str = "organization_name_unique"
CONSTRAINT_ORGANIZATION_NAME_EXISTS: str = "organization_name_exists"
CONSTRAINT_EVENT_NAME_UNIQUE: str = "event_name_unique"
CONSTRAINT_EVENT_NAME_EXISTS: str = "event_name_exists"

# 公共 Cypher RETURN 子句（关系查询用），避免多处重复
_RELATIONSHIP_RETURN_FIELDS = """source.name AS source,
                    target.name AS target,
                    type(r) AS relation_type,
                    props['description'] AS description,
                    props['confidence'] AS confidence,
                    props['source_text'] AS source_text,
                    props['source_doc'] AS source_doc,
                    props['source_chunk'] AS source_chunk,
                    props['child_chunk_ids'] AS child_chunk_ids,
                    props['content_preview_chunk_id'] AS content_preview_chunk_id,
                    props['content_preview_25'] AS content_preview_25,
                    props['book_title'] AS book_title,
                    props['chapter_index'] AS chapter_index,
                    props['chapter_title'] AS chapter_title,
                    props['scene_index'] AS scene_index,
                    props['scene_title'] AS scene_title"""


class GraphStoreNeo4j(GraphStore):
    """
    Neo4j图谱存储实现
    
    使用Neo4j作为图数据库后端，利用Cypher约束保证数据完整性。
    要求Neo4j 4.3+版本。
    
    特性：
    - 使用MERGE确保节点和关系唯一性
    - 自动创建约束（节点唯一性、属性非空）
    - 利用Neo4j原生CRUD功能
    - 支持复杂Cypher查询
    """

    def __init__(
        self,
        uri: str,
        user: str,
        password: str,
        database: str,
        verbose: bool = True
    ):
        """
        初始化Neo4j图谱存储

        所有连接参数均由调用方通过 RAGConfig 传入，不提供硬编码默认值。

        Args:
            uri: Neo4j连接URI（如 bolt://localhost:7687）
            user: Neo4j用户名
            password: Neo4j密码
            database: Neo4j数据库名称
            verbose: 是否输出详细信息
        """
        if not NEO4J_AVAILABLE:
            raise ImportError("Neo4j未安装，请运行: pip install neo4j")

        self.uri = uri
        self.user = user
        self._password = password  # 不以明文属性存储，用下划线标记为内部
        self.database = database
        self.verbose = verbose

        # Neo4j驱动
        self.driver: Optional[Any] = None

        if not self._password:
            raise ValueError("Neo4j密码未设置，请配置 rag_neo4j_password")

        if self.verbose:
            logger.info("GraphStoreNeo4j 初始化完成")
            logger.debug("URI: %s", self.uri)
            logger.info("数据库: %s", self.database)

    def initialize(self) -> None:
        """初始化Neo4j连接并创建约束"""
        try:
            # 创建驱动
            self.driver = GraphDatabase.driver(
                self.uri,
                auth=(self.user, self._password)
            )

            # 验证连接
            with self.driver.session(database=self.database) as session:
                session.run("RETURN 1")

            # 创建约束
            self._create_constraints()

            if self.verbose:
                logger.info("Neo4j连接成功，约束已创建")

        except Exception as e:
            # 确保异常时关闭驱动，避免资源泄漏
            if self.driver:
                try:
                    self.driver.close()
                except Exception:
                    pass  # 忽略关闭时的异常
                self.driver = None
            
            if self.verbose:
                logger.error("Neo4j连接失败: %s", e)
            raise

    def _get_node_label(self, entity_label: str) -> str:
        """
        根据实体类型标签获取Neo4j节点标签
        
        Args:
            entity_label: 实体类型标签（PER/LOC/ORG/EVENT）
            
        Returns:
            Neo4j节点标签（Person/Location/Organization/Event）
            
        Raises:
            ValueError: 未知实体类型
        """
        label = entity_label.upper()
        node_label = ENTITY_LABEL_TO_NODE_LABEL.get(label)
        if node_label is None:
            raise ValueError(
                f"未知实体类型: {entity_label!r}，"
                f"支持的类型: {list(ENTITY_LABEL_TO_NODE_LABEL.keys())}"
            )
        return node_label
    
    def _get_entity_label(self, entity_name: str) -> Optional[str]:
        """
        查询实体的label（通过在所有节点类型中查找）
        
        Args:
            entity_name: 实体名称
            
        Returns:
            实体label（PER/LOC/ORG），如果未找到返回None
        """
        # 在所有节点类型中查找（含 Event）
        for node_label in [NODE_LABEL_PERSON, NODE_LABEL_LOCATION, NODE_LABEL_ORGANIZATION, NODE_LABEL_EVENT]:
            query = f"""
            MATCH (p:{node_label} {{name: $name}})
            RETURN p.label AS label
            LIMIT 1
            """
            result = self._execute_query(query, {'name': entity_name}, fetch_one=True)
            if result and result.get('label'):
                return result['label']
        return None
    
    def _node_label_where(self, var: str) -> str:
        """
        生成单变量的节点类型 WHERE 条件（PER/LOC/ORG/Event 四类），供 Cypher 复用。
        """
        return (
            f"({var}:{NODE_LABEL_PERSON} OR {var}:{NODE_LABEL_LOCATION} OR {var}:{NODE_LABEL_ORGANIZATION} OR {var}:{NODE_LABEL_EVENT})"
        )

    @staticmethod
    def _validate_relation_type(relation_type: str) -> str:
        """校验 relation_type 仅包含合法字符（防 Cypher 注入），返回大写标准化结果。"""
        rel_type = str(relation_type).strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", rel_type):
            raise ValueError(f"非法关系类型: {relation_type!r}")
        return rel_type

    def _create_constraints(self) -> None:
        """创建Neo4j约束（确保数据完整性，要求Neo4j 4.3+）"""
        if self.driver is None:
            return

        constraints = [
            # Person节点唯一性约束
            {
                'name': CONSTRAINT_PERSON_NAME_UNIQUE,
                'cypher': f"""
                CREATE CONSTRAINT {CONSTRAINT_PERSON_NAME_UNIQUE} IF NOT EXISTS
                FOR (p:{NODE_LABEL_PERSON}) REQUIRE p.name IS UNIQUE
                """,
                'required': True
            },
            {
                'name': CONSTRAINT_PERSON_NAME_EXISTS,
                'cypher': f"""
                CREATE CONSTRAINT {CONSTRAINT_PERSON_NAME_EXISTS} IF NOT EXISTS
                FOR (p:{NODE_LABEL_PERSON}) REQUIRE p.name IS NOT NULL
                """,
                'required': False
            },
            # Location节点唯一性约束
            {
                'name': CONSTRAINT_LOCATION_NAME_UNIQUE,
                'cypher': f"""
                CREATE CONSTRAINT {CONSTRAINT_LOCATION_NAME_UNIQUE} IF NOT EXISTS
                FOR (p:{NODE_LABEL_LOCATION}) REQUIRE p.name IS UNIQUE
                """,
                'required': True
            },
            {
                'name': CONSTRAINT_LOCATION_NAME_EXISTS,
                'cypher': f"""
                CREATE CONSTRAINT {CONSTRAINT_LOCATION_NAME_EXISTS} IF NOT EXISTS
                FOR (p:{NODE_LABEL_LOCATION}) REQUIRE p.name IS NOT NULL
                """,
                'required': False
            },
            # Organization节点唯一性约束
            {
                'name': CONSTRAINT_ORGANIZATION_NAME_UNIQUE,
                'cypher': f"""
                CREATE CONSTRAINT {CONSTRAINT_ORGANIZATION_NAME_UNIQUE} IF NOT EXISTS
                FOR (p:{NODE_LABEL_ORGANIZATION}) REQUIRE p.name IS UNIQUE
                """,
                'required': True
            },
            {
                'name': CONSTRAINT_ORGANIZATION_NAME_EXISTS,
                'cypher': f"""
                CREATE CONSTRAINT {CONSTRAINT_ORGANIZATION_NAME_EXISTS} IF NOT EXISTS
                FOR (p:{NODE_LABEL_ORGANIZATION}) REQUIRE p.name IS NOT NULL
                """,
                'required': False
            },
            # Event 节点唯一性约束（方案 A：事件为一类节点）
            {
                'name': CONSTRAINT_EVENT_NAME_UNIQUE,
                'cypher': f"""
                CREATE CONSTRAINT {CONSTRAINT_EVENT_NAME_UNIQUE} IF NOT EXISTS
                FOR (p:{NODE_LABEL_EVENT}) REQUIRE p.name IS UNIQUE
                """,
                'required': True
            },
            {
                'name': CONSTRAINT_EVENT_NAME_EXISTS,
                'cypher': f"""
                CREATE CONSTRAINT {CONSTRAINT_EVENT_NAME_EXISTS} IF NOT EXISTS
                FOR (p:{NODE_LABEL_EVENT}) REQUIRE p.name IS NOT NULL
                """,
                'required': False
            },
        ]

        with self.driver.session(database=self.database) as session:
            for constraint in constraints:
                try:
                    session.run(constraint['cypher'])
                    if self.verbose:
                        logger.debug("创建约束: %s", constraint['name'])
                except Exception as e:
                    if constraint.get('required', True):
                        # 必需约束失败时抛出异常
                        if self.verbose:
                            logger.error("创建约束失败 %s: %s", constraint['name'], e)
                        raise
                    else:
                        # 可选约束失败时只记录警告（如社区版不支持某些约束）
                        if self.verbose:
                            logger.warning("跳过可选约束 %s（可能不支持）: %s", constraint['name'], e)

            # 为“别名查询”创建索引（可选优化）
            # 说明：aliases 是列表属性，Neo4j 可以对其建立索引以加速 `$alias IN p.aliases` 查询
            alias_indexes = [
                {
                    "name": "idx_person_aliases",
                    "cypher": f"""
                    CREATE INDEX idx_person_aliases IF NOT EXISTS
                    FOR (p:{NODE_LABEL_PERSON}) ON (p.aliases)
                    """,
                },
                {
                    "name": "idx_location_aliases",
                    "cypher": f"""
                    CREATE INDEX idx_location_aliases IF NOT EXISTS
                    FOR (p:{NODE_LABEL_LOCATION}) ON (p.aliases)
                    """,
                },
                {
                    "name": "idx_organization_aliases",
                    "cypher": f"""
                    CREATE INDEX idx_organization_aliases IF NOT EXISTS
                    FOR (p:{NODE_LABEL_ORGANIZATION}) ON (p.aliases)
                    """,
                },
            ]

            for idx in alias_indexes:
                try:
                    session.run(idx["cypher"])
                    if self.verbose:
                        logger.debug("创建索引: %s", idx['name'])
                except Exception as e:
                    # 索引属于可选优化，不影响主流程
                    if self.verbose:
                        logger.warning("创建索引失败 %s（可忽略）: %s", idx['name'], e)

    def _execute_query(
        self,
        query: str,
        parameters: Optional[Dict[str, Any]] = None,
        fetch_one: bool = False
    ) -> Any:
        """
        执行Cypher查询
        
        Args:
            query: Cypher查询语句
            parameters: 查询参数
            fetch_one: 是否只获取一条记录
            
        Returns:
            查询结果
        """
        if self.driver is None:
            raise RuntimeError("Neo4j驱动未初始化，请先调用initialize()")

        parameters = parameters or {}

        with self.driver.session(database=self.database) as session:
            result = session.run(query, parameters)

            if fetch_one:
                record = result.single()
                return record.data() if record else None
            else:
                return [record.data() for record in result]

    def execute_query(
        self,
        query: str,
        parameters: Optional[Dict[str, Any]] = None,
        fetch_one: bool = False,
    ) -> Any:
        """执行Cypher查询（公开接口，委托到 _execute_query）。"""
        return self._execute_query(query, parameters, fetch_one)

    def create_entity(self, entity: Entity) -> str:
        """
        创建实体节点（使用MERGE确保唯一性）
        
        Args:
            entity: 实体对象
            
        Returns:
            实体名称
        """
        # 根据实体label确定节点标签
        entity_label = entity.label.upper() if entity.label else 'PER'
        node_label = self._get_node_label(entity_label)
        
        query = f"""
        MERGE (p:{node_label} {{name: $name}})
        ON CREATE SET
            p.created_at = datetime(),
            p.aliases = COALESCE($aliases, []),
            p.label = COALESCE($label, 'PER'),
            p.event_type = $event_type,
            p.description = $description,
            p.frequency = $frequency,
            p.first_appearance = $first_appearance,
            p.first_book_title = $first_book_title,
            p.first_chapter_index = $first_chapter_index,
            p.first_chapter_title = $first_chapter_title,
            p.first_scene_index = $first_scene_index,
            p.first_scene_title = $first_scene_title,
            p.source_texts = $source_texts,
            p.child_chunk_ids = COALESCE($child_chunk_ids, []),
            p.content_preview_chunk_id = $content_preview_chunk_id,
            p.content_preview_25 = COALESCE($content_preview_25, ''),
            p.updated_at = datetime()
        ON MATCH SET
            p.aliases = COALESCE($aliases, p.aliases),
            p.label = COALESCE($label, p.label),
            p.event_type = CASE WHEN $event_type IS NOT NULL AND $event_type <> '' THEN $event_type ELSE p.event_type END,
            p.description = COALESCE($description, p.description),
            p.frequency = COALESCE(p.frequency, 0) + $frequency,
            p.first_appearance = COALESCE($first_appearance, p.first_appearance),
            p.first_book_title = COALESCE($first_book_title, p.first_book_title),
            p.first_chapter_index = COALESCE($first_chapter_index, p.first_chapter_index),
            p.first_chapter_title = COALESCE($first_chapter_title, p.first_chapter_title),
            p.first_scene_index = COALESCE($first_scene_index, p.first_scene_index),
            p.first_scene_title = COALESCE($first_scene_title, p.first_scene_title),
            p.source_texts = CASE 
                WHEN $source_texts IS NOT NULL AND size($source_texts) > 0
                THEN COALESCE(p.source_texts, []) + $source_texts 
                ELSE COALESCE(p.source_texts, [])
            END,
            p.child_chunk_ids = CASE
                WHEN $child_chunk_ids IS NOT NULL AND size($child_chunk_ids) > 0
                THEN COALESCE(p.child_chunk_ids, []) + $child_chunk_ids
                ELSE COALESCE(p.child_chunk_ids, [])
            END,
            p.content_preview_chunk_id = CASE
                WHEN $content_preview_chunk_id IS NOT NULL AND $content_preview_chunk_id <> ''
                THEN $content_preview_chunk_id
                ELSE p.content_preview_chunk_id
            END,
            p.content_preview_25 = CASE
                WHEN $content_preview_25 IS NOT NULL AND $content_preview_25 <> ''
                THEN $content_preview_25
                ELSE p.content_preview_25
            END,
            p.updated_at = datetime()
        RETURN p.name AS name
        """

        child_chunk_ids = []
        content_preview_chunk_id = None
        content_preview_25 = ""
        if entity.metadata:
            candidate = entity.metadata.get("child_chunk_ids", [])
            if isinstance(candidate, list):
                child_chunk_ids = [str(x) for x in candidate if x]
            content_preview_chunk_id = entity.metadata.get("content_preview_chunk_id")
            if (not content_preview_chunk_id) and child_chunk_ids:
                content_preview_chunk_id = str(child_chunk_ids[0])
            content_preview_25 = entity.metadata.get("content_preview_25", "") or ""

        parameters = {
            'name': entity.name,
            'aliases': entity.aliases,
            'label': entity.label,
            'event_type': entity.event_type or None,
            'description': entity.description,
            'frequency': entity.frequency,
            'first_appearance': entity.first_appearance,
            'first_book_title': entity.first_book_title,
            'first_chapter_index': entity.first_chapter_index,
            'first_chapter_title': entity.first_chapter_title,
            'first_scene_index': entity.first_scene_index,
            'first_scene_title': entity.first_scene_title,
            'source_texts': entity.source_texts[:MAX_SOURCE_TEXTS_PER_ENTITY] if entity.source_texts else [],
            # 对齐大模型抽取字段：child_chunk_ids（实体可能属于多个子文档）
            'child_chunk_ids': child_chunk_ids,
            # 调试用：子文档内容预览（前25个字）
            'content_preview_chunk_id': str(content_preview_chunk_id) if content_preview_chunk_id else '',
            'content_preview_25': str(content_preview_25)[:25] if content_preview_25 else '',
        }

        # 合并metadata
        if entity.metadata:
            for key, value in entity.metadata.items():
                parameters[f'metadata_{key}'] = value

        result = self._execute_query(query, parameters, fetch_one=True)
        return result['name'] if result else entity.name

    def create_relationship(
        self,
        source: str,
        target: str,
        relationship: Relationship
    ) -> str:
        """
        创建关系边（使用MERGE确保唯一性）
        
        Args:
            source: 源实体名称
            target: 目标实体名称
            relationship: 关系对象
            
        Returns:
            关系ID（字符串格式）
        """
        # 关系类型作为关系标签
        rel_type = relationship.relation_type.value
        
        # 查询源实体和目标实体的label，以确定节点类型
        source_label = self._get_entity_label(source) or 'PER'
        target_label = self._get_entity_label(target) or 'PER'
        source_node_label = self._get_node_label(source_label)
        target_node_label = self._get_node_label(target_label)

        query = f"""
        MATCH (source:{source_node_label} {{name: $source}})
        MATCH (target:{target_node_label} {{name: $target}})
        MERGE (source)-[r:{rel_type}]->(target)
        ON CREATE SET
            r.created_at = datetime(),
            r.description = $description,
            r.confidence = $confidence,
            r.source_text = $source_text,
            r.source_doc = $source_doc,
            r.source_chunk = $source_chunk,
            r.child_chunk_ids = COALESCE($child_chunk_ids, []),
            r.content_preview_chunk_id = $content_preview_chunk_id,
            r.content_preview_25 = COALESCE($content_preview_25, ''),
            r.book_title = $book_title,
            r.chapter_index = $chapter_index,
            r.chapter_title = $chapter_title,
            r.scene_index = $scene_index,
            r.scene_title = $scene_title,
            r.updated_at = datetime()
        ON MATCH SET
            r.description = COALESCE($description, r.description),
            r.confidence = CASE 
                WHEN $confidence IS NOT NULL AND r.confidence IS NOT NULL
                THEN CASE 
                    WHEN $confidence > r.confidence THEN $confidence
                    ELSE r.confidence
                END
                WHEN $confidence IS NOT NULL THEN $confidence
                ELSE r.confidence
            END,
            r.source_text = COALESCE($source_text, r.source_text),
            r.source_doc = COALESCE($source_doc, r.source_doc),
            r.source_chunk = COALESCE($source_chunk, r.source_chunk),
            r.child_chunk_ids = CASE
                WHEN $child_chunk_ids IS NOT NULL AND size($child_chunk_ids) > 0
                THEN COALESCE(r.child_chunk_ids, []) + $child_chunk_ids
                ELSE COALESCE(r.child_chunk_ids, [])
            END,
            r.content_preview_chunk_id = CASE
                WHEN $content_preview_chunk_id IS NOT NULL AND $content_preview_chunk_id <> ''
                THEN $content_preview_chunk_id
                ELSE r.content_preview_chunk_id
            END,
            r.content_preview_25 = CASE
                WHEN $content_preview_25 IS NOT NULL AND $content_preview_25 <> ''
                THEN $content_preview_25
                ELSE r.content_preview_25
            END,
            r.book_title = COALESCE($book_title, r.book_title),
            r.chapter_index = COALESCE($chapter_index, r.chapter_index),
            r.chapter_title = COALESCE($chapter_title, r.chapter_title),
            r.scene_index = COALESCE($scene_index, r.scene_index),
            r.scene_title = COALESCE($scene_title, r.scene_title),
            r.updated_at = datetime()
        RETURN elementId(r) AS rel_id, type(r) AS rel_type
        """

        child_chunk_ids = []
        content_preview_chunk_id = None
        content_preview_25 = ""
        if relationship.metadata:
            candidate = relationship.metadata.get("child_chunk_ids", [])
            if isinstance(candidate, list):
                child_chunk_ids = [str(x) for x in candidate if x]
            content_preview_chunk_id = relationship.metadata.get("content_preview_chunk_id")
            content_preview_25 = relationship.metadata.get("content_preview_25", "") or ""

        parameters = {
            'source': source,
            'target': target,
            'description': relationship.description,
            'confidence': relationship.confidence,
            'source_text': relationship.source_text,
            'source_doc': relationship.source_doc,
            'source_chunk': relationship.source_chunk,
            # 对齐大模型抽取字段：child_chunk_ids（关系可能属于多个子文档）
            'child_chunk_ids': child_chunk_ids,
            # 调试用：子文档内容预览（前25个字）
            'content_preview_chunk_id': str(content_preview_chunk_id) if content_preview_chunk_id else '',
            'content_preview_25': str(content_preview_25)[:25] if content_preview_25 else '',
            'book_title': relationship.book_title,
            'chapter_index': relationship.chapter_index,
            'chapter_title': relationship.chapter_title,
            'scene_index': relationship.scene_index,
            'scene_title': relationship.scene_title
        }

        # 合并metadata
        if relationship.metadata:
            for key, value in relationship.metadata.items():
                parameters[f'metadata_{key}'] = value

        result = self._execute_query(query, parameters, fetch_one=True)
        if result:
            return f"{rel_type}_{result['rel_id']}"
        return f"{rel_type}_{source}_{target}"

    def get_entity(self, entity_name: str) -> Optional[Dict[str, Any]]:
        """
        获取实体信息（通过标准名称）
        
        Args:
            entity_name: 实体名称
            
        Returns:
            实体信息字典，如果不存在返回None
        """
        # 在所有节点类型中查找实体（含 Event）
        query = f"""
        MATCH (p)
        WHERE p.name = $name AND {self._node_label_where('p')}
        WITH p, properties(p) AS props
        RETURN p.name AS name,
                p.aliases AS aliases,
                p.label AS label,
                props['event_type'] AS event_type,
                props['description'] AS description,
                p.frequency AS frequency,
                props['first_appearance'] AS first_appearance,
                props['first_book_title'] AS first_book_title,
                props['first_chapter_index'] AS first_chapter_index,
                props['first_chapter_title'] AS first_chapter_title,
                props['first_scene_index'] AS first_scene_index,
                props['first_scene_title'] AS first_scene_title,
                p.source_texts AS source_texts,
                props['child_chunk_ids'] AS child_chunk_ids,
                props['content_preview_chunk_id'] AS content_preview_chunk_id,
                props['content_preview_25'] AS content_preview_25,
                p.created_at AS created_at,
                p.updated_at AS updated_at
        LIMIT 1
        """

        result = self._execute_query(query, {'name': entity_name}, fetch_one=True)
        return result

    def get_entity_by_alias(self, alias: str) -> Optional[Dict[str, Any]]:
        """
        通过别名获取实体信息
        
        Args:
            alias: 别名（可能是标准名称或别名）
            
        Returns:
            实体信息字典，如果不存在返回None
        """
        # 为了更好地命中索引（尤其是 aliases 索引），这里避免使用 (p:Person OR p:Location ...)
        # 改为按 label 分支 UNION 查询
        query = f"""
        CALL () {{
            MATCH (p:{NODE_LABEL_PERSON})
            WHERE p.name = $alias OR (p.aliases IS NOT NULL AND $alias IN p.aliases)
            RETURN p
            UNION
            MATCH (p:{NODE_LABEL_LOCATION})
            WHERE p.name = $alias OR (p.aliases IS NOT NULL AND $alias IN p.aliases)
            RETURN p
            UNION
            MATCH (p:{NODE_LABEL_ORGANIZATION})
            WHERE p.name = $alias OR (p.aliases IS NOT NULL AND $alias IN p.aliases)
            RETURN p
            UNION
            MATCH (p:{NODE_LABEL_EVENT})
            WHERE p.name = $alias OR (p.aliases IS NOT NULL AND $alias IN p.aliases)
            RETURN p
        }}
        WITH p, properties(p) AS props
        RETURN p.name AS name,
                p.aliases AS aliases,
                p.label AS label,
                props['event_type'] AS event_type,
                props['description'] AS description,
                p.frequency AS frequency,
                props['first_appearance'] AS first_appearance,
                props['first_book_title'] AS first_book_title,
                props['first_chapter_index'] AS first_chapter_index,
                props['first_chapter_title'] AS first_chapter_title,
                props['first_scene_index'] AS first_scene_index,
                props['first_scene_title'] AS first_scene_title,
                p.source_texts AS source_texts,
                props['child_chunk_ids'] AS child_chunk_ids,
                props['content_preview_chunk_id'] AS content_preview_chunk_id,
                props['content_preview_25'] AS content_preview_25,
                p.created_at AS created_at,
                p.updated_at AS updated_at
        LIMIT 1
        """

        result = self._execute_query(query, {'alias': alias}, fetch_one=True)
        return result

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
        if relation_type:
            relation_type = self._validate_relation_type(relation_type)
            # 查询特定类型的关系（支持所有节点类型，含 Event，便于事件关系 PARTICIPATES_IN / NARRATIVE_FOCUS 等）
            query = f"""
            MATCH (source)
            WHERE source.name = $entity AND {self._node_label_where('source')}
            MATCH (source)-[r:{relation_type}]->(target)
            WHERE {self._node_label_where('target')}
            WITH r, source, target, properties(r) AS props
            RETURN {_RELATIONSHIP_RETURN_FIELDS},
                    'outgoing' AS direction
            UNION
            MATCH (source)
            WHERE {self._node_label_where('source')}
            MATCH (source)-[r:{relation_type}]->(target)
            WHERE target.name = $entity AND {self._node_label_where('target')}
            WITH r, source, target, properties(r) AS props
            RETURN {_RELATIONSHIP_RETURN_FIELDS},
                    'incoming' AS direction
            """
        else:
            # 查询所有关系（支持所有节点类型，含 Event）
            query = f"""
            MATCH (source)
            WHERE source.name = $entity AND {self._node_label_where('source')}
            MATCH (source)-[r]->(target)
            WHERE {self._node_label_where('target')}
            WITH r, source, target, properties(r) AS props
            RETURN {_RELATIONSHIP_RETURN_FIELDS},
                    'outgoing' AS direction
            UNION
            MATCH (source)
            WHERE {self._node_label_where('source')}
            MATCH (source)-[r]->(target)
            WHERE target.name = $entity AND {self._node_label_where('target')}
            WITH r, source, target, properties(r) AS props
            RETURN {_RELATIONSHIP_RETURN_FIELDS},
                    'incoming' AS direction
            """

        return self._execute_query(query, {'entity': entity})

    def query_relationship_between(
        self,
        entity1: str,
        entity2: str,
        relation_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        查询两个实体之间的关系（高效Cypher实现）
        
        【性能优化】
        - 使用Cypher直接查询两个实体之间的关系，避免先查询所有关系再过滤
        - 支持双向查询（entity1->entity2 和 entity2->entity1）
        - 支持特定关系类型过滤
        
        Args:
            entity1: 实体1名称
            entity2: 实体2名称
            relation_type: 关系类型（可选，如果为None则返回所有关系）
            
        Returns:
            关系列表（去重后）
        """
        # 支持 Person/Location/Organization/Event，与 query_relationships 一致
        node_pair_where = f"{self._node_label_where('source')} AND {self._node_label_where('target')}"
        if relation_type:
            relation_type = self._validate_relation_type(relation_type)
            query = f"""
            // 查询 entity1 -> entity2 的关系（支持 PER/LOC/ORG）
            MATCH (source)-[r:{relation_type}]->(target)
            WHERE {node_pair_where} AND ((source.name = $entity1 AND target.name = $entity2)
                    OR (source.name = $entity2 AND target.name = $entity1))
            WITH r, source, target, properties(r) AS props
            RETURN {_RELATIONSHIP_RETURN_FIELDS},
                    'outgoing' AS direction
            """
        else:
            query = f"""
            // 查询 entity1 -> entity2 的关系（支持 PER/LOC/ORG）
            MATCH (source)-[r]->(target)
            WHERE {node_pair_where} AND ((source.name = $entity1 AND target.name = $entity2)
                    OR (source.name = $entity2 AND target.name = $entity1))
            WITH r, source, target, properties(r) AS props
            RETURN {_RELATIONSHIP_RETURN_FIELDS},
                    'outgoing' AS direction
            """
        
        results = self._execute_query(
            query,
            {'entity1': entity1, 'entity2': entity2}
        )
        
        # 去重：使用 (source, target, relation_type) 作为唯一标识符
        seen_relationships = set()
        unique_results = []
        for rel in results:
            rel_key = (
                rel.get('source', ''),
                rel.get('target', ''),
                rel.get('relation_type', '')
            )
            if rel_key not in seen_relationships:
                seen_relationships.add(rel_key)
                unique_results.append(rel)
        
        return unique_results

    def query_relationships_among_entities(
        self,
        entity_names: List[str],
        relation_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        多实体之间关系查询：一次返回给定实体集合中「任意两实体」之间的所有直接关系。

        适用场景：如「陈平安、刘羡阳、姚老头三人之间是什么关系？」一次查出
        陈平安-刘羡阳、陈平安-姚老头、刘羡阳-姚老头 的全部关系，无需多次调用 query_relationship_between。

        Args:
            entity_names: 实体名称列表（2 个或以上）
            relation_type: 关系类型（可选，None 表示所有类型）

        Returns:
            关系列表，每项与 query_relationship_between 返回结构一致（source, target, relation_type, description, source_chunk 等）
        """
        if not entity_names or len(entity_names) < 2:
            return []

        node_pair_where = f"{self._node_label_where('source')} AND {self._node_label_where('target')}"
        if relation_type:
            relation_type = self._validate_relation_type(relation_type)
            query = f"""
            MATCH (source)-[r:{relation_type}]->(target)
            WHERE {node_pair_where}
                AND source.name IN $entity_names
                AND target.name IN $entity_names
                AND source.name <> target.name
            WITH r, source, target, properties(r) AS props
            RETURN {_RELATIONSHIP_RETURN_FIELDS},
                    'outgoing' AS direction
            """
        else:
            query = f"""
            MATCH (source)-[r]->(target)
            WHERE {node_pair_where}
                AND source.name IN $entity_names
                AND target.name IN $entity_names
                AND source.name <> target.name
            WITH r, source, target, properties(r) AS props
            RETURN {_RELATIONSHIP_RETURN_FIELDS},
                    'outgoing' AS direction
            """
        results = self._execute_query(query, {'entity_names': entity_names})

        seen = set()
        unique_results = []
        for rel in results or []:
            rel_key = (
                rel.get('source', ''),
                rel.get('target', ''),
                rel.get('relation_type', '')
            )
            if rel_key not in seen:
                seen.add(rel_key)
                unique_results.append(rel)
        return unique_results

    def get_top_entities_by_centrality(self, k: int) -> List[Dict[str, Any]]:
        """
        获取中心度最高的实体（中心度=关系数）

        说明：
        - 使用 OPTIONAL MATCH 统计每个实体节点的关系数
        - 支持 Person/Location/Organization/Event 四类节点
        """
        k = int(k) if k is not None else 10
        if k <= 0:
            return []

        query = f"""
        MATCH (n)
        WHERE {self._node_label_where('n')}
        OPTIONAL MATCH (n)-[r]-()
        WITH n, count(r) AS centrality
        RETURN n.name AS entity, centrality, properties(n) AS entity_info
        ORDER BY centrality DESC, entity ASC
        LIMIT $k
        """
        results = self._execute_query(query, {"k": k}) or []
        out: List[Dict[str, Any]] = []
        for row in results:
            out.append(
                {
                    "entity": row.get("entity", ""),
                    "centrality": int(row.get("centrality") or 0),
                    "entity_info": row.get("entity_info") or {},
                }
            )
        return out

    def get_entity_statistics(self) -> Dict[str, Any]:
        """
        获取图谱统计信息（Neo4j 实现）
        """
        entity_count = self.get_entity_count()
        relationship_count = self.get_relationship_count()

        query = """
        MATCH ()-[r]->()
        RETURN type(r) AS relation_type, count(r) AS cnt
        ORDER BY cnt DESC
        """
        rows = self._execute_query(query) or []
        relation_type_dist = {
            r.get("relation_type", "UNKNOWN"): int(r.get("cnt") or 0) for r in rows
        }

        return {
            "entity_count": int(entity_count or 0),
            "relationship_count": int(relationship_count or 0),
            "relation_type_distribution": relation_type_dist,
            "average_relationships_per_entity": (
                (relationship_count / entity_count) if entity_count else 0
            ),
            "stats_note": None,
        }

    def get_entities_by_relation_type(self, relation_type: str) -> List[str]:
        """
        根据关系类型获取所有涉及的实体名称（去重后）

        说明：
        - 关系类型在 Cypher 中是模式的一部分，无法参数化，因此这里做严格校验
        - 仅允许大写字母/数字/下划线，并且必须以字母开头
        """
        if not relation_type:
            return []

        rel_type = self._validate_relation_type(relation_type)

        query = f"""
        MATCH (source)-[r:{rel_type}]->(target)
        WHERE {self._node_label_where('source')} AND {self._node_label_where('target')}
        RETURN DISTINCT source.name AS source, target.name AS target
        """

        results = self._execute_query(query) or []
        entities = set()
        for row in results:
            s = row.get("source")
            t = row.get("target")
            if s:
                entities.add(s)
            if t:
                entities.add(t)

        return sorted(entities)

    def get_related_entities(
        self,
        entity: str,
        max_depth: int = 2,
        relation_type: Optional[str] = None
    ) -> List[str]:
        """
        获取相关实体（高效Cypher实现）
        
        【性能优化】
        - 使用Cypher的 `[*1..n]` 语法一次性查询多跳关系，避免递归查询
        - 比递归实现更高效，减少数据库查询次数
        - 支持特定关系类型过滤
        
        Args:
            entity: 实体名称
            max_depth: 最大深度（1-10，超出范围会被限制）
            relation_type: 关系类型（可选，如果为None则返回所有关系）
            
        Returns:
            相关实体名称列表（去重后，不包含自己）
        """
        # 验证并限制 max_depth 范围
        max_depth = max(MIN_PATH_DEPTH, min(MAX_PATH_DEPTH, int(max_depth)))
        
        # 支持 Person/Location/Organization/Event，与 query_relationships 一致
        start_where = f"{self._node_label_where('start')} AND start.name = $entity"
        related_where = self._node_label_where('related')
        if relation_type:
            relation_type = self._validate_relation_type(relation_type)
            query = f"""
            MATCH (start)-[r:{relation_type}*1..{max_depth}]-(related)
            WHERE {start_where} AND {related_where}
            RETURN DISTINCT related.name AS entity_name
            """
        else:
            query = f"""
            MATCH (start)-[*1..{max_depth}]-(related)
            WHERE {start_where} AND {related_where}
            RETURN DISTINCT related.name AS entity_name
            """
        
        results = self._execute_query(query, {'entity': entity})
        
        # 提取实体名称并去重
        related_entities = set()
        for result in results:
            entity_name = result.get('entity_name')
            if entity_name and entity_name != entity:  # 排除自己
                related_entities.add(entity_name)
        
        return list(related_entities)

    def query_path(
        self,
        source: str,
        target: str,
        max_depth: int = DEFAULT_MAX_PATH_DEPTH
    ) -> List[List[str]]:
        """
        查询路径
        
        Args:
            source: 源实体名称
            target: 目标实体名称
            max_depth: 最大深度（1-10，超出范围会被限制）
            
        Returns:
            路径列表（每个路径是一个实体名称列表）
        """
        # 验证并限制 max_depth 范围，防止过深查询和潜在安全问题
        max_depth = max(MIN_PATH_DEPTH, min(MAX_PATH_DEPTH, int(max_depth)))

        # 支持 Person/Location/Organization/Event，与 query_relationships 一致
        query = f"""
        MATCH (source), (target)
        WHERE {self._node_label_where('source')} AND source.name = $source
        AND {self._node_label_where('target')} AND target.name = $target
        MATCH path = shortestPath((source)-[*1..{max_depth}]-(target))
        RETURN [node in nodes(path) | node.name] AS path
        LIMIT {DEFAULT_PATH_LIMIT}
        """

        results = self._execute_query(
            query,
            {'source': source, 'target': target}
        )

        paths = []
        for result in results:
            if result.get('path'):
                paths.append(result['path'])

        return paths

    def get_all_entities(self) -> List[Dict[str, Any]]:
        """
        获取所有实体（包括 PER/LOC/ORG/Event 所有类型）
        
        Returns:
            实体列表
        """
        query = f"""
        MATCH (p)
        WHERE {self._node_label_where('p')}
        WITH p, properties(p) AS props
        RETURN p.name AS name,
                p.aliases AS aliases,
                p.label AS label,
                props['event_type'] AS event_type,
                props['description'] AS description,
                p.frequency AS frequency,
                props['first_appearance'] AS first_appearance,
                props['first_book_title'] AS first_book_title,
                props['first_chapter_index'] AS first_chapter_index,
                props['first_chapter_title'] AS first_chapter_title,
                props['first_scene_index'] AS first_scene_index,
                props['first_scene_title'] AS first_scene_title,
                p.source_texts AS source_texts,
                props['child_chunk_ids'] AS child_chunk_ids,
                props['content_preview_chunk_id'] AS content_preview_chunk_id,
                props['content_preview_25'] AS content_preview_25,
                p.created_at AS created_at,
                p.updated_at AS updated_at
        ORDER BY p.frequency DESC
        """

        return self._execute_query(query)

    def get_entity_count(self) -> int:
        """
        获取实体数量（包括 PER/LOC/ORG/Event 所有类型）
        
        Returns:
            实体数量
        """
        query = f"""
        MATCH (p)
        WHERE {self._node_label_where('p')}
        RETURN count(p) AS count
        """
        result = self._execute_query(query, fetch_one=True)
        return result['count'] if result else 0

    def get_relationship_count(self) -> int:
        """
        获取关系数量
        
        Returns:
            关系数量
        """
        query = "MATCH ()-[r]->() RETURN count(r) AS count"
        result = self._execute_query(query, fetch_one=True)
        return result['count'] if result else 0

    def clear(self) -> None:
        """清空图谱"""
        query = "MATCH (n) DETACH DELETE n"
        self._execute_query(query)

        if self.verbose:
            logger.info("图谱已清空")

    def save(self) -> None:
        """保存图谱到磁盘（Neo4j自动持久化）"""
        # Neo4j自动持久化，无需手动保存
        if self.verbose:
            logger.debug("Neo4j数据自动持久化")

    def load(self) -> None:
        """从磁盘加载图谱（Neo4j自动加载）"""
        # Neo4j自动加载，无需手动加载
        if self.verbose:
            logger.debug("Neo4j数据自动加载")

    def query_parent_ids_by_entities(
        self,
        entity_names: List[str],
        relation_type: Optional[str] = None,
        max_results: int = MAX_RESULTS_LIMIT
    ) -> List[str]:
        """
        根据实体名称列表查询相关的 parent_id（用于双路召回）
        
        Args:
            entity_names: 实体名称列表
            relation_type: 关系类型（可选，如果为None则返回所有关系）
            max_results: 最大返回结果数
            
        Returns:
            parent_id 列表（去重后）
        """
        if not entity_names:
            return []

        if relation_type:
            relation_type = self._validate_relation_type(relation_type)
        rel_node_where = f"{self._node_label_where('source')} AND {self._node_label_where('target')}"
        if relation_type:
            query = f"""
            MATCH (source)-[r:{relation_type}]->(target)
            WHERE {rel_node_where} AND (source.name IN $entity_names OR target.name IN $entity_names)
            WITH DISTINCT properties(r) AS props
            WITH DISTINCT props['chapter_index'] AS chapter_index, 
                        props['scene_index'] AS scene_index,
                        props['book_title'] AS book_title
            WHERE chapter_index IS NOT NULL
            RETURN chapter_index, scene_index, book_title
            LIMIT $max_results
            """
        else:
            query = f"""
            MATCH (source)-[r]->(target)
            WHERE {rel_node_where} AND (source.name IN $entity_names OR target.name IN $entity_names)
            WITH DISTINCT properties(r) AS props
            WITH DISTINCT props['chapter_index'] AS chapter_index, 
                        props['scene_index'] AS scene_index,
                        props['book_title'] AS book_title
            WHERE chapter_index IS NOT NULL
            RETURN chapter_index, scene_index, book_title
            LIMIT $max_results
            """

        results = self._execute_query(
            query,
            {'entity_names': entity_names, 'max_results': max_results}
        )

        # 构建 parent_id 列表（使用统一的工具函数确保格式一致）
        parent_ids = []
        for result in results:
            chapter_index = result.get('chapter_index')
            scene_index = result.get('scene_index')

            if chapter_index is not None:
                # 使用统一的工具函数构建parent_id，确保格式与文档分割时一致
                parent_id = build_parent_id(chapter_index, scene_index)
                parent_ids.append(parent_id)

        # 去重并返回
        return list(set(parent_ids))

    def query_child_ids_by_entities(
        self,
        entity_names: List[str],
        relation_type: Optional[str] = None,
        max_results: int = MAX_RESULTS_LIMIT,
        verbose: bool = False,
    ) -> Tuple[List[str], List[tuple]]:
        """
        根据实体名称列表查询相关的子文档ID（child_chunk_id，用于双路召回）
        
        【说明（改版）】
        - 三路径召回：实体节点关系（路径1）、关系扫描（路径2）、实体提及 child_chunk_ids（路径3）
        - 支持别名查询：通过实体 aliases 匹配
        - 返回统一打分：Hop/Coverage/StrategyPrior/Evidence（策略先验在本方法恒为 0，留给策略检索层处理）
        
        Args:
            entity_names: 实体名称列表
            relation_type: 关系类型（可选，如果为None则返回所有关系）
            max_results: 最大返回结果数
            verbose: 为 True 时打印每条结果的子项得分（关系距离、路径贡献等）与总得分，便于核对排序
            
        Returns:
            (child_ids_list, score_list)：子文档ID列表（去重、按加权分降序）及对应得分列表，
            score_list 每项为 (cid, total, hop_score, entity_coverage_score, strategy_prior_score, evidence_score)。
        """
        if not entity_names:
            return ([], [])

        query_entity_count = len(entity_names)
        entity_names_set = set(entity_names)

        # 每个 chunk 覆盖的查询实体集合、命中的路径集合(1/2/3)、是否来自路径3(0跳)
        chunk_entities: Dict[str, Set[str]] = defaultdict(set)
        chunk_paths: Dict[str, Set[int]] = defaultdict(set)
        from_path3: Set[str] = set()

        node_where = self._node_label_where('e')
        other_where = self._node_label_where('other')
        source_target_where = f"{self._node_label_where('source')} AND {self._node_label_where('target')}"

        def build_entity_query(use_relation_type: bool) -> str:
            """构建实体节点查询语句（路径1），返回 source_chunk + entity_name 以便算 coverage"""
            rel_filter = f":{relation_type}" if use_relation_type and relation_type else ""
            return f"""
                // 实体作为 source（支持 Person/Location/Organization）
                MATCH (e)-[r{rel_filter}]->(other)
                WHERE {node_where} AND {other_where}
                AND (e.name IN $entity_names 
                        OR (e.aliases IS NOT NULL AND ANY(alias IN e.aliases WHERE alias IN $entity_names)))
                WITH DISTINCT properties(r)['source_chunk'] AS source_chunk, e.name AS entity_name
                WHERE source_chunk IS NOT NULL AND source_chunk CONTAINS '_child_'
                RETURN source_chunk, entity_name
                LIMIT $max_results
                UNION
                // 实体作为 target
                MATCH (other)-[r{rel_filter}]->(e)
                WHERE {node_where} AND {other_where}
                AND (e.name IN $entity_names 
                        OR (e.aliases IS NOT NULL AND ANY(alias IN e.aliases WHERE alias IN $entity_names)))
                WITH DISTINCT properties(r)['source_chunk'] AS source_chunk, e.name AS entity_name
                WHERE source_chunk IS NOT NULL AND source_chunk CONTAINS '_child_'
                RETURN source_chunk, entity_name
                LIMIT $max_results
                """

        def build_relationship_query(use_relation_type: bool) -> str:
            """构建关系查询语句（路径2），返回 source_chunk + 两端 name 以便算 coverage"""
            rel_filter = f":{relation_type}" if use_relation_type and relation_type else ""
            return f"""
                MATCH (source)-[r{rel_filter}]->(target)
                WHERE {source_target_where}
                AND (source.name IN $entity_names OR target.name IN $entity_names
                        OR (source.aliases IS NOT NULL AND ANY(alias IN source.aliases WHERE alias IN $entity_names))
                        OR (target.aliases IS NOT NULL AND ANY(alias IN target.aliases WHERE alias IN $entity_names)))
                WITH DISTINCT properties(r)['source_chunk'] AS source_chunk, source.name AS sname, target.name AS tname
                WHERE source_chunk IS NOT NULL AND source_chunk CONTAINS '_child_'
                RETURN source_chunk, sname, tname
                LIMIT $max_results
                """

        # 路径1：查询实体节点本身（1跳）
        try:
            query1 = build_entity_query(use_relation_type=relation_type is not None)
            results1 = self._execute_query(
                query1,
                {'entity_names': entity_names, 'max_results': max_results}
            )
            
            for result in results1:
                source_chunk = result.get('source_chunk')
                entity_name = result.get('entity_name')
                if source_chunk and '_child_' in str(source_chunk):
                    chunk_paths[str(source_chunk)].add(1)
                    if entity_name:
                        chunk_entities[str(source_chunk)].add(str(entity_name))
        except Exception as e:
            logger.debug("路径1查询失败: %s", e)

        # 路径2：查询关系（1跳）
        try:
            query2 = build_relationship_query(use_relation_type=relation_type is not None)
            results2 = self._execute_query(
                query2,
                {'entity_names': entity_names, 'max_results': max_results}
            )
            for result in results2:
                source_chunk = result.get('source_chunk')
                sname = result.get('sname')
                tname = result.get('tname')
                if source_chunk and '_child_' in str(source_chunk):
                    cid = str(source_chunk)
                    chunk_paths[cid].add(2)
                    if sname and str(sname) in entity_names_set:
                        chunk_entities[cid].add(str(sname))
                    if tname and str(tname) in entity_names_set:
                        chunk_entities[cid].add(str(tname))
        except Exception as e:
            logger.debug("路径2查询失败: %s", e)

        # 路径3：实体节点上的 child_chunk_ids（实体提及，0跳）
        try:
            query3 = f"""
            MATCH (e)
            WHERE {self._node_label_where('e')}
                AND (e.name IN $entity_names
                    OR (e.aliases IS NOT NULL AND ANY(alias IN e.aliases WHERE alias IN $entity_names)))
            RETURN e.name AS entity_name, e.child_chunk_ids AS child_chunk_ids
            """
            results3 = self._execute_query(
                query3,
                {'entity_names': entity_names}
            )
            for result in results3:
                entity_name = result.get('entity_name')
                ids_from_entity = result.get('child_chunk_ids')
                if ids_from_entity:
                    for cid in ids_from_entity:
                        if cid and '_child_' in str(cid):
                            cid_str = str(cid)
                            chunk_paths[cid_str].add(3)
                            from_path3.add(cid_str)
                            if entity_name:
                                chunk_entities[cid_str].add(str(entity_name))
        except Exception as e:
            logger.debug("路径3(实体child_chunk_ids)查询失败: %s", e)

        # 统一打分并排序：Hop/Coverage/StrategyPrior/Evidence（本方法 strategy_prior 恒为 0）
        from ...utils.graph_relevance_score import (
            compute_graph_relevance_score,
            relation_distance_to_score,
            entity_coverage_to_score,
            evidence_count_to_score,
        )
        max_entity_paths = 3  # 路径1/2/3
        scored: List[tuple] = []

        all_chunk_ids = set(chunk_paths.keys()) | from_path3
        for cid in all_chunk_ids:
            if not cid or '_child_' not in cid:
                continue
            hop_count = 0 if cid in from_path3 else 1
            hop_score = relation_distance_to_score(hop_count)
            covered = len(chunk_entities.get(cid, set()))
            coverage_score = entity_coverage_to_score(covered, query_entity_count)
            entity_path_count = len(chunk_paths.get(cid, set()))
            evidence_score = evidence_count_to_score(entity_path_count, max_entity_paths)
            strategy_prior_score = 0.0
            total_score = compute_graph_relevance_score(
                hop_score=hop_score,
                entity_coverage_score=coverage_score,
                strategy_prior_score=strategy_prior_score,
                evidence_score=evidence_score,
            )
            scored.append((cid, total_score, hop_score, coverage_score, strategy_prior_score, evidence_score))

        scored.sort(key=lambda x: -x[1])
        child_ids_list = [x[0] for x in scored[:max_results]]
        score_list = scored[:max_results]  # 与 child_ids_list 顺序一致
        
        return (child_ids_list, score_list)

    def get_entity_signals_for_child_ids(
        self,
        entity_names: List[str],
        child_ids: List[str],
        relation_type: Optional[str] = None,
        path_limit: int = 500,
    ) -> Dict[str, Dict[str, Any]]:
        """
        为给定 child_ids 收集「实体相关」信号（不含策略信号），供策略检索统一打分使用。

        Returns:
            {cid: {"hop_count": int, "covered_entity_count": int, "entity_path_count": int}}
        """
        if not entity_names or not child_ids:
            return {}
        id_set = set(str(x) for x in child_ids)

        # 复用 query_child_ids_by_entities 的三路径查询，但只保留与 child_ids 相关的数据
        query_entity_count = len(entity_names)
        entity_names_set = set(entity_names)

        chunk_entities: Dict[str, Set[str]] = defaultdict(set)
        chunk_paths: Dict[str, Set[int]] = defaultdict(set)
        from_path3: Set[str] = set()

        node_where = self._node_label_where('e')
        other_where = self._node_label_where('other')
        source_target_where = f"{self._node_label_where('source')} AND {self._node_label_where('target')}"

        rel_filter = f":{relation_type}" if relation_type else ""

        # 路径1（实体节点关系）
        try:
            query1 = f"""
                MATCH (e)-[r{rel_filter}]->(other)
                WHERE {node_where} AND {other_where}
                AND (e.name IN $entity_names
                        OR (e.aliases IS NOT NULL AND ANY(alias IN e.aliases WHERE alias IN $entity_names)))
                WITH DISTINCT properties(r)['source_chunk'] AS source_chunk, e.name AS entity_name
                WHERE source_chunk IS NOT NULL AND source_chunk CONTAINS '_child_'
                RETURN source_chunk, entity_name
                LIMIT $max_results
                UNION
                MATCH (other)-[r{rel_filter}]->(e)
                WHERE {node_where} AND {other_where}
                AND (e.name IN $entity_names
                        OR (e.aliases IS NOT NULL AND ANY(alias IN e.aliases WHERE alias IN $entity_names)))
                WITH DISTINCT properties(r)['source_chunk'] AS source_chunk, e.name AS entity_name
                WHERE source_chunk IS NOT NULL AND source_chunk CONTAINS '_child_'
                RETURN source_chunk, entity_name
                LIMIT $max_results
            """
            results1 = self._execute_query(query1, {"entity_names": entity_names, "max_results": path_limit})
            for r in results1:
                cid = str(r.get("source_chunk") or "")
                if cid and cid in id_set:
                    chunk_paths[cid].add(1)
                    en = r.get("entity_name")
                    if en:
                        chunk_entities[cid].add(str(en))
        except Exception as e:
            logger.debug("get_entity_signals 路径1查询失败: %s", e)

        # 路径2（关系扫描）
        try:
            query2 = f"""
                MATCH (source)-[r{rel_filter}]->(target)
                WHERE {source_target_where}
                AND (source.name IN $entity_names OR target.name IN $entity_names
                        OR (source.aliases IS NOT NULL AND ANY(alias IN source.aliases WHERE alias IN $entity_names))
                        OR (target.aliases IS NOT NULL AND ANY(alias IN target.aliases WHERE alias IN $entity_names)))
                WITH DISTINCT properties(r)['source_chunk'] AS source_chunk, source.name AS sname, target.name AS tname
                WHERE source_chunk IS NOT NULL AND source_chunk CONTAINS '_child_'
                RETURN source_chunk, sname, tname
                LIMIT $max_results
            """
            results2 = self._execute_query(query2, {"entity_names": entity_names, "max_results": path_limit})
            for r in results2:
                cid = str(r.get("source_chunk") or "")
                if cid and cid in id_set:
                    chunk_paths[cid].add(2)
                    sname = r.get("sname")
                    tname = r.get("tname")
                    if sname and str(sname) in entity_names_set:
                        chunk_entities[cid].add(str(sname))
                    if tname and str(tname) in entity_names_set:
                        chunk_entities[cid].add(str(tname))
        except Exception as e:
            logger.debug("get_entity_signals 路径2查询失败: %s", e)

        # 路径3（实体提及）
        try:
            query3 = f"""
                MATCH (e)
                WHERE {self._node_label_where('e')}
                AND (e.name IN $entity_names
                        OR (e.aliases IS NOT NULL AND ANY(alias IN e.aliases WHERE alias IN $entity_names)))
                RETURN e.name AS entity_name, e.child_chunk_ids AS child_chunk_ids
            """
            results3 = self._execute_query(query3, {"entity_names": entity_names})
            for r in results3:
                en = r.get("entity_name")
                ids_from_entity = r.get("child_chunk_ids")
                if not ids_from_entity:
                    continue
                for cid_raw in ids_from_entity:
                    cid = str(cid_raw)
                    if cid and cid in id_set:
                        chunk_paths[cid].add(3)
                        from_path3.add(cid)
                        if en:
                            chunk_entities[cid].add(str(en))
        except Exception as e:
            logger.debug("get_entity_signals 路径3查询失败: %s", e)

        out: Dict[str, Dict[str, Any]] = {}
        for cid in child_ids:
            cid_s = str(cid)
            hop_count = 0 if cid_s in from_path3 else (1 if cid_s in chunk_paths else 3)
            out[cid_s] = {
                "hop_count": hop_count,
                "covered_entity_count": len(chunk_entities.get(cid_s, set())),
                "entity_path_count": len(chunk_paths.get(cid_s, set())),
                "query_entity_count": query_entity_count,
            }
        
        return out

    def query_parent_ids_by_entity_relationships(
        self,
        entity1: str,
        entity2: Optional[str] = None,
        relation_type: Optional[str] = None,
        max_results: int = MAX_RESULTS_LIMIT
    ) -> List[str]:
        """
        根据实体关系查询相关的 parent_id（用于双路召回）
        
        Args:
            entity1: 实体1名称
            entity2: 实体2名称（可选，如果提供则查询两个实体之间的关系）
            relation_type: 关系类型（可选）
            max_results: 最大返回结果数
            
        Returns:
            parent_id 列表（去重后）
        """
        if relation_type:
            relation_type = self._validate_relation_type(relation_type)
        if entity2:
            pair_where = (
                f"{self._node_label_where('source')} AND {self._node_label_where('target')} "
                f"AND ((source.name = $entity1 AND target.name = $entity2) "
                f"     OR (source.name = $entity2 AND target.name = $entity1))"
            )
            if relation_type:
                query = f"""
                MATCH (source)-[r:{relation_type}]->(target)
                WHERE {pair_where}
                WITH DISTINCT properties(r)['chapter_index'] AS chapter_index, 
                            properties(r)['scene_index'] AS scene_index
                WHERE chapter_index IS NOT NULL
                RETURN chapter_index, scene_index
                LIMIT $max_results
                """
            else:
                query = f"""
                MATCH (source)-[r]->(target)
                WHERE {pair_where}
                WITH DISTINCT properties(r)['chapter_index'] AS chapter_index, 
                            properties(r)['scene_index'] AS scene_index
                WHERE chapter_index IS NOT NULL
                RETURN chapter_index, scene_index
                LIMIT $max_results
                """
            results = self._execute_query(
                query,
                {'entity1': entity1, 'entity2': entity2, 'max_results': max_results}
            )
        else:
            # 查询单个实体的所有关系
            return self.query_parent_ids_by_entities([entity1], relation_type, max_results)

        # 构建 parent_id 列表（使用统一的工具函数确保格式一致）
        parent_ids = []
        for result in results:
            chapter_index = result.get('chapter_index')
            scene_index = result.get('scene_index')

            if chapter_index is not None:
                # 使用统一的工具函数构建parent_id，确保格式与文档分割时一致
                parent_id = build_parent_id(chapter_index, scene_index)
                parent_ids.append(parent_id)

        # 去重并返回
        return list(set(parent_ids))

    def get_processed_parent_ids(
        self,
        book_title: Optional[str] = None
    ) -> Set[str]:
        """
        查询已处理的 parent_id 集合（用于断点续传）
        
        同时从实体节点和关系边上收集章节信息，以章节为粒度判断是否已处理。
        
        Args:
            book_title: 书名（可选，如果提供则只查询该书的章节）
            
        Returns:
            已处理的 parent_id 集合
        """
        if book_title:
            query = """
            MATCH (p)
            WHERE p.first_chapter_index IS NOT NULL 
                AND p.first_book_title = $book_title
            WITH DISTINCT p.first_chapter_index AS chapter_index, 
                        p.first_scene_index AS scene_index
            RETURN chapter_index, scene_index
            UNION
            MATCH ()-[r]->()
            WHERE r.chapter_index IS NOT NULL 
                AND r.book_title = $book_title
            WITH DISTINCT r.chapter_index AS chapter_index, 
                        r.scene_index AS scene_index
            RETURN chapter_index, scene_index
            """
            results = self._execute_query(query, {'book_title': book_title})
        else:
            query = """
            MATCH (p)
            WHERE p.first_chapter_index IS NOT NULL
            WITH DISTINCT p.first_chapter_index AS chapter_index, 
                        p.first_scene_index AS scene_index
            RETURN chapter_index, scene_index
            UNION
            MATCH ()-[r]->()
            WHERE r.chapter_index IS NOT NULL
            WITH DISTINCT r.chapter_index AS chapter_index, 
                        r.scene_index AS scene_index
            RETURN chapter_index, scene_index
            """
            results = self._execute_query(query)

        # 构建 parent_id 集合
        parent_ids = set()
        for result in results:
            chapter_index = result.get('chapter_index')
            scene_index = result.get('scene_index')

            if chapter_index is not None:
                parent_id = build_parent_id(chapter_index, scene_index)
                parent_ids.add(parent_id)

        return parent_ids

    def close(self) -> None:
        """关闭Neo4j连接"""
        if self.driver:
            try:
                self.driver.close()
                if self.verbose:
                    logger.info("Neo4j连接已关闭")
            except Exception as e:
                if self.verbose:
                    logger.warning("关闭Neo4j连接时出错: %s", e)
            finally:
                self.driver = None  # 确保驱动标记为已关闭，避免重复关闭

    def __repr__(self) -> str:
        """返回安全的字符串表示（隐藏 URI 中的敏感信息）。"""
        masked_uri = re.sub(r"://[^@]*@", "://***:***@", self.uri) if self.uri else ""
        return f"GraphStoreNeo4j(uri={masked_uri!r}, database={self.database!r})"

    def __enter__(self):
        """上下文管理器入口"""
        self.initialize()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        self.close()
