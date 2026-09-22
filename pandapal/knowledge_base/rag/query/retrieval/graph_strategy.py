# -*- coding: utf-8 -*-
"""
图谱策略层：按 strategy 执行图操作，仅返回 child_ids。
依赖存储层 GraphStore，不依赖 GraphQuery。
"""

from typing import Dict, Any, List, Set, Optional

from ...store.graph.graph_constants import DEFAULT_RELATIONSHIP_LIMIT, STRATEGY_TYPES
from ...store.graph.graph_store import GraphStore
from ...schema import SchemaKeys, RelationType

import logging

logger = logging.getLogger(__name__)

# 子文档 ID 格式标识（用于判断 source_chunk 是否为有效子文档 ID）
CHILD_CHUNK_ID_MARKER = "_child_"

# 每次查询最多处理的实体数
MAX_ENTITIES_PER_QUERY = 5

# 策略命名常量（对应 STRATEGY_TYPES 索引，提升可读性）
STRATEGY_ENTITY_ATTRIBUTE = STRATEGY_TYPES[0]
STRATEGY_DIRECT_RELATION = STRATEGY_TYPES[1]
STRATEGY_PATH_QUERY = STRATEGY_TYPES[2]
STRATEGY_GRAPH_STRUCTURE = STRATEGY_TYPES[3]
STRATEGY_COMMON_ENDPOINT = STRATEGY_TYPES[4]
STRATEGY_LIST_BY_RELATION = STRATEGY_TYPES[5]
STRATEGY_LIST_BY_ENTITY = STRATEGY_TYPES[6]
STRATEGY_EXPAND_ENTITY = STRATEGY_TYPES[7]
STRATEGY_NARRATIVE_EVENTS = STRATEGY_TYPES[8]

# 事件关系类型：叙事事件策略用（按实体查参与的核心事件、际遇、叙事焦点）
NARRATIVE_EVENTS_RELATION_TYPES = (
    RelationType.PARTICIPATES_IN.value,
    RelationType.NARRATIVE_FOCUS.value,
    RelationType.SELECTED_BY.value,
)


class GraphStrategyExecutor:
    """
    根据 understanding(intent + entities + relation_type + strategy)执行图谱查询,仅返回 child_ids。
    """

    def __init__(self, graph_store: GraphStore, verbose: bool = True):
        """
        初始化策略执行器
        
        Args:
            graph_store: 图谱存储实例
            verbose: 是否打印详细日志
        """
        self.graph_store = graph_store
        self.verbose = verbose

    def execute(
        self,
        understanding: Dict[str, Any],
        max_results: int = 50,
    ) -> List[str]:
        """
        执行图谱策略查询
        
        Args:
            understanding: 查询理解结果,需含 intent, entities, relation_type, strategy, keywords
            max_results: 最大结果数
            
        Returns:
            子文档 ID 列表(child_ids)
        """
        if not understanding:
            return []
        
        K = SchemaKeys
        strategy = understanding.get(K.QA_STRATEGY) or STRATEGY_ENTITY_ATTRIBUTE
        entities = understanding.get(K.QA_ENTITIES) or []
        expanded_entities = understanding.get(K.QA_EXPANDED_ENTITIES)
        relation_type = understanding.get(K.QA_RELATION_TYPE)

        try:
            if strategy == STRATEGY_ENTITY_ATTRIBUTE:
                use_entities = expanded_entities if expanded_entities else entities
                return self._exec_entity_attribute(use_entities, max_results)
            if strategy == STRATEGY_DIRECT_RELATION:
                return self._exec_direct_relation(entities, relation_type, max_results)
            if strategy == STRATEGY_PATH_QUERY:
                return self._exec_path_query(entities, max_results)
            if strategy in (STRATEGY_GRAPH_STRUCTURE, STRATEGY_LIST_BY_RELATION):
                return self._exec_graph_structure(relation_type, max_results)
            if strategy == STRATEGY_COMMON_ENDPOINT:
                return self._exec_common_relation_endpoint(entities, relation_type, max_results)
            if strategy == STRATEGY_LIST_BY_ENTITY:
                return self._exec_list_by_entity(entities, relation_type, max_results)
            if strategy == STRATEGY_EXPAND_ENTITY:
                use_entities = expanded_entities if expanded_entities else entities
                return self._exec_expand_entity(use_entities, max_results)
            if strategy == STRATEGY_NARRATIVE_EVENTS:
                use_entities = expanded_entities if expanded_entities else entities
                return self._exec_narrative_events(use_entities, max_results)
        except Exception as e:
            if self.verbose:
                logger.warning(" 策略 %s 执行失败: %s", strategy, e)
            return self._exec_entity_attribute(entities, max_results)

        return self._exec_entity_attribute(entities, max_results)

    def _collect_chunk_ids(
        self,
        rels: List[Dict],
        seen: Set[str],
        max_total: int,
    ) -> List[str]:
        out = []
        for r in rels:
            cid = r.get(SchemaKeys.REL_SOURCE_CHUNK)
            if cid and CHILD_CHUNK_ID_MARKER in str(cid) and cid not in seen:
                out.append(cid)
                seen.add(cid)
                if len(out) >= max_total:
                    break
        return out

    def _exec_entity_attribute(self, entities: List[str], max_results: int) -> List[str]:
        if not entities:
            return []
        if hasattr(self.graph_store, "query_child_ids_by_entities"):
            try:
                result = self.graph_store.query_child_ids_by_entities(
                    entity_names=entities,
                    relation_type=None,
                    max_results=max_results,
                    verbose=self.verbose,
                )
                ids = result[0] if isinstance(result, tuple) else result
                return ids[:max_results]
            except Exception as e:
                if self.verbose:
                    logger.debug("query_child_ids_by_entities 快路径异常，回退逐实体查询: %s", e)
        seen = set()
        out = []
        for e in entities[:MAX_ENTITIES_PER_QUERY]:
            rels = self.graph_store.query_relationships(e)[:DEFAULT_RELATIONSHIP_LIMIT]
            out.extend(self._collect_chunk_ids(rels, seen, max_results - len(out)))
            if len(out) >= max_results:
                break
        return out[:max_results]

    def _exec_direct_relation(
        self,
        entities: List[str],
        relation_type: Optional[str],
        max_results: int,
    ) -> List[str]:
        seen = set()
        out = []

        if len(entities) == 1 and relation_type:
            rels = self.graph_store.query_relationships(entities[0], relation_type=relation_type)
            out.extend(self._collect_chunk_ids(rels, seen, max_results))
            return out[:max_results]

        if len(entities) >= 2:
            for i in range(len(entities)):
                for j in range(i + 1, len(entities)):
                    rels = self.graph_store.query_relationship_between(entities[i], entities[j])
                    out.extend(self._collect_chunk_ids(rels, seen, max_results))
                    if len(out) >= max_results:
                        return out[:max_results]
            if not out:
                for e in entities[:3]:
                    rels = self.graph_store.query_relationships(e, relation_type=relation_type)[:DEFAULT_RELATIONSHIP_LIMIT]
                    out.extend(self._collect_chunk_ids(rels, seen, max_results))
                    if len(out) >= max_results:
                        break
        elif entities:
            rels = self.graph_store.query_relationships(entities[0], relation_type=relation_type)[:DEFAULT_RELATIONSHIP_LIMIT]
            out.extend(self._collect_chunk_ids(rels, seen, max_results))

        return out[:max_results]

    def _exec_path_query(self, entities: List[str], max_results: int) -> List[str]:
        if len(entities) < 2:
            return self._exec_entity_attribute(entities, max_results)
        seen = set()
        out = []
        try:
            paths = self.graph_store.query_path(entities[0], entities[1], max_depth=3)
            path = paths[0] if paths else []
            for node in path[:10]:
                rels = self.graph_store.query_relationships(node)[:DEFAULT_RELATIONSHIP_LIMIT]
                out.extend(self._collect_chunk_ids(rels, seen, max_results - len(out)))
                if len(out) >= max_results:
                    break
        except Exception as e:
            if self.verbose:
                logger.debug("path_query 路径查询异常，降级到 direct_relation: %s", e)
        if not out:
            return self._exec_direct_relation(entities, None, max_results)
        return out[:max_results]

    def _exec_graph_structure(self, relation_type: Optional[str], max_results: int) -> List[str]:
        """按关系类型取成员及其关系，收集 source_chunk（策略内联，不再依赖 get_community_summary）。"""
        if not relation_type:
            if self.verbose:
                logger.warning(" graph_structure/list_by_relation_type 策略需要 relation_type，但未提供")
            return []
        seen = set()
        out = []
        try:
            members = self.graph_store.get_entities_by_relation_type(relation_type)[:50]
            for member_name in members[:20]:
                rels = self.graph_store.query_relationships(member_name, relation_type=relation_type)
                out.extend(self._collect_chunk_ids(rels[:DEFAULT_RELATIONSHIP_LIMIT], seen, max_results - len(out)))
                if len(out) >= max_results:
                    return out[:max_results]
        except Exception as e:
            logger.warning("graph_structure 策略执行失败: %s", e)
        return out[:max_results]

    def _exec_common_relation_endpoint(
        self,
        entities: List[str],
        relation_type: Optional[str],
        max_results: int,
    ) -> List[str]:
        if len(entities) < 2 or not relation_type:
            return self._exec_direct_relation(entities, relation_type, max_results)
        seen = set()
        others_per_entity: List[Set[str]] = []
        for e in entities:
            rels = self.graph_store.query_relationships(e, relation_type=relation_type)
            others = set()
            for r in rels:
                src, tgt = r.get(SchemaKeys.REL_SOURCE), r.get(SchemaKeys.REL_TARGET)
                if src == e and tgt:
                    others.add(tgt)
                elif tgt == e and src:
                    others.add(src)
            others_per_entity.append(others)
        common = others_per_entity[0]
        for s in others_per_entity[1:]:
            common = common & s
        if not common:
            return self._exec_direct_relation(entities, relation_type, max_results)
        entity_set = set(entities)
        out = []
        for e in entities:
            rels = self.graph_store.query_relationships(e, relation_type=relation_type)
            for r in rels:
                src, tgt = r.get(SchemaKeys.REL_SOURCE), r.get(SchemaKeys.REL_TARGET)
                if (src in common and tgt in entity_set) or (tgt in common and src in entity_set):
                    out.extend(self._collect_chunk_ids([r], seen, max_results - len(out)))
                    if len(out) >= max_results:
                        return out[:max_results]
        return out[:max_results]

    def _exec_list_by_entity(
        self,
        entities: List[str],
        relation_type: Optional[str],
        max_results: int,
    ) -> List[str]:
        if not entities:
            return []
        seen = set()
        out = []
        try:
            related = self.graph_store.get_related_entities(entities[0], max_depth=2)[:15]
            for other in [entities[0]] + related:
                rels = self.graph_store.query_relationships(other, relation_type=relation_type)[:DEFAULT_RELATIONSHIP_LIMIT]
                out.extend(self._collect_chunk_ids(rels, seen, max_results - len(out)))
                if len(out) >= max_results:
                    break
        except Exception as e:
            logger.warning("list_by_entity 策略执行失败: %s", e)
            out = self._exec_entity_attribute(entities, max_results)
        return out[:max_results]

    def _exec_expand_entity(self, entities: List[str], max_results: int) -> List[str]:
        """以实体为中心取多跳邻居相关 chunk（适合情节型/总结型背景检索）。"""
        if not entities:
            return []
        if not hasattr(self.graph_store, "get_related_entities"):
            return self._exec_entity_attribute(entities, max_results)
        seen: Set[str] = set()
        out: List[str] = []
        expand_depth = 2
        for e in entities[:MAX_ENTITIES_PER_QUERY]:
            try:
                related = self.graph_store.get_related_entities(e, max_depth=expand_depth)
                for other in [e] + related[:15]:
                    rels = self.graph_store.query_relationships(other)[:DEFAULT_RELATIONSHIP_LIMIT]
                    out.extend(self._collect_chunk_ids(rels, seen, max_results - len(out)))
                    if len(out) >= max_results:
                        return out[:max_results]
            except Exception as e_err:
                logger.debug("expand_entity 查询实体 %s 失败: %s", e, e_err)
                continue
        return out[:max_results]

    def _exec_narrative_events(self, entities: List[str], max_results: int) -> List[str]:
        """
        叙事事件:按实体查 PARTICIPATES_IN/NARRATIVE_FOCUS/SELECTED_BY 相关事件,
        收集 source_chunk 供情节/总结/角色定位推理。
        
        Args:
            entities: 实体列表
            max_results: 最大结果数
            
        Returns:
            子文档 ID 列表
        """
        if not entities:
            return []
        
        seen: Set[str] = set()
        out: List[str] = []
        
        for e in entities[:MAX_ENTITIES_PER_QUERY]:
            for rel_type in NARRATIVE_EVENTS_RELATION_TYPES:
                try:
                    rels = self.graph_store.query_relationships(e, relation_type=rel_type)
                    out.extend(self._collect_chunk_ids(rels[:DEFAULT_RELATIONSHIP_LIMIT], seen, max_results - len(out)))
                    if len(out) >= max_results:
                        return out[:max_results]
                except Exception as e_err:
                    logger.debug("narrative_events 查询 %s/%s 失败: %s", e, rel_type, e_err)
                    continue
        
        return out[:max_results]
