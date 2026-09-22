# -*- coding: utf-8 -*-
"""
实体类型常量

PER/LOC/ORG/EVENT 四类实体对应的 Neo4j 节点标签。
实体标签由 builds.entity_relationship_extractor_llm._ENTITY_LABELS_SECTION 定义，
Neo4j 节点标签在 graph_store_neo4j 中使用。
与 relation_type.py 的关系类型不同，entity_type 定义实体分类。
"""

from typing import Final, Dict

# ==================== 节点标签（Neo4j 用） ====================

NODE_LABEL_PERSON: Final[str] = "Person"
NODE_LABEL_LOCATION: Final[str] = "Location"
NODE_LABEL_ORGANIZATION: Final[str] = "Organization"
NODE_LABEL_EVENT: Final[str] = "Event"

# ==================== 实体标签 → 节点标签 ====================

ENTITY_LABEL_TO_NODE_LABEL: Final[Dict[str, str]] = {
    "PER": NODE_LABEL_PERSON,
    "LOC": NODE_LABEL_LOCATION,
    "ORG": NODE_LABEL_ORGANIZATION,
    "EVENT": NODE_LABEL_EVENT,
}
