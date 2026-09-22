"""store/graph — 知识图谱存储

核心类型 ``Entity`` / ``Relationship`` 请从 ``agent.rag.schema`` 导入（单一入口）。
"""
from .relation_type import RelationType  # noqa: F401
from .graph_store import GraphStore  # noqa: F401

try:
    from .graph_store_neo4j import GraphStoreNeo4j  # noqa: F401
except ImportError:
    GraphStoreNeo4j = None  # type: ignore[assignment,misc]

try:
    from .graph_builder import GraphBuilder  # noqa: F401
except ImportError:
    GraphBuilder = None  # type: ignore[assignment,misc]

__all__ = [
    "RelationType",
    "GraphStore",
    "GraphStoreNeo4j",
    "GraphBuilder",
]
