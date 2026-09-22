#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图谱数据构建

提供知识图谱的构建能力（实体/关系写入 Neo4j、断点续传、统计查询）。
CLI 入口见 scripts/rag/run_build_graph.py。
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ...store.graph.graph_store_neo4j import GraphStoreNeo4j
from ...store.graph.graph_builder import GraphBuilder
from ...store.graph.entity_type import (
    NODE_LABEL_PERSON,
    NODE_LABEL_LOCATION,
    NODE_LABEL_ORGANIZATION,
    NODE_LABEL_EVENT,
)
from ..data.build_rag_data import load_prepared_rag_data
from .graph_build_progress import (
    load_processed_parent_ids,
    save_progress,
    clear_progress as clear_graph_progress,
)
from ...utils import build_parent_id
from ...store.vector.vector_store import VectorStore
from ...schema import Entity, Relationship

logger = logging.getLogger(__name__)

# 图谱构建断点续传：每批包含的 parent（章节/场景）数量
GRAPH_BUILD_PARENTS_PER_BATCH = 1
# 构建过程保存与日志间隔（供后续按章节保存/进度日志使用）
DEFAULT_SAVE_INTERVAL: int = 2  # 每处理 N 个章节保存一次
DEFAULT_PROGRESS_LOG_INTERVAL: int = 10  # 每处理 N 个章节记录一次进度日志


class BuildGraphPipeline:
    """图谱构建和查询流程类（db_path/collection_name 由调用方传入，不读 agent.config）"""

    def __init__(
        self,
        documents_dir: Path,
        project_root: Path,
        db_path: Path,
        collection_name: str,
        verbose: bool,
        *,
        neo4j_uri: str = "bolt://localhost:7687",
        neo4j_user: str = "neo4j",
        neo4j_password: Optional[str] = None,
        neo4j_database: str = "neo4j",
    ):
        self.project_root = project_root
        self.documents_dir = documents_dir
        self.db_path = db_path
        self.collection_name = collection_name
        self.verbose = verbose

        # 初始化图谱存储（延迟连接，调用 connect() 或首次操作时才连接）
        self.graph_store = GraphStoreNeo4j(
            uri=neo4j_uri,
            user=neo4j_user,
            password=neo4j_password,
            database=neo4j_database,
            verbose=verbose,
        )
        self._graph_initialized = False

        # 初始化图谱构建器
        self.graph_builder = GraphBuilder(
            graph_store=self.graph_store,
            verbose=verbose
        )

        # 初始化向量存储（用于根据 child_chunk_id 回查子文档内容）
        self.vector_store = self._init_vector_store()

        if self.verbose:
            self._log_title(" 图谱流程初始化完成")
            logger.info("   文档目录: %s", self.documents_dir)

    def _ensure_graph_initialized(self) -> None:
        """确保 Neo4j 连接已初始化（延迟初始化）。"""
        if not self._graph_initialized:
            self.graph_store.initialize()
            self._graph_initialized = True

    def _init_vector_store(self) -> Optional[VectorStore]:
        """初始化向量存储（失败时返回 None，不影响图谱构建/查询主流程）。"""
        try:
            vector_store = VectorStore(
                db_path=self.db_path,
                collection_name=self.collection_name,
                project_root=self.project_root,
                verbose=False,
            )
            vector_store.initialize()
            return vector_store
        except Exception as e:
            if self.verbose:
                logger.warning("无法初始化向量存储（用于获取子文档）: %s", e)
            return None

    def _log_title(self, title: str, width: int = 60) -> None:
        """统一输出标题分隔线，减少重复日志代码。"""
        if not self.verbose:
            return
        logger.info("\n" + "=" * width)
        logger.info(title)
        logger.info("=" * width)

    @staticmethod
    def _is_child_chunk_id(source_chunk: Any) -> bool:
        """
        判断关系的 source_chunk 是否已映射到子文档 child_chunk_id。
        
        格式：parent_id_child_N（例如：chapter_1_child_0）
        """
        if source_chunk is None:
            return False
        source_chunk_str = str(source_chunk)
        if not source_chunk_str or source_chunk_str == "N/A":
            return False
        # 使用更严格的判断：检查格式是否为 xxx_child_数字
        if "_child_" not in source_chunk_str:
            return False
        # 验证 _child_ 后面是否跟着数字
        parts = source_chunk_str.rsplit("_child_", 1)
        if len(parts) == 2:
            try:
                int(parts[1])  # 尝试解析为整数
                return True
            except ValueError:
                return False
        return False

    @staticmethod
    def _log_unknown_fields(
        data: Dict[str, Any],
        known_keys: Set[str],
        *,
        indent: str = "  ",
        title: str = "其他字段",
    ) -> None:
        unknown_keys = set(data.keys()) - known_keys
        if not unknown_keys:
            return
        logger.info("%s%s: %s", indent, title, unknown_keys)
        for key in unknown_keys:
            logger.info("%s  %s: %s", indent, key, data.get(key, 'N/A'))

    def _cypher_label_filter(self, var: str) -> str:
        """Cypher：统一的实体 label 过滤（PER/LOC/ORG/EVENT）。"""
        return (
            f"({var}:{NODE_LABEL_PERSON} OR {var}:{NODE_LABEL_LOCATION} OR {var}:{NODE_LABEL_ORGANIZATION} OR {var}:{NODE_LABEL_EVENT})"
        )

    def _cypher_where_entity_labels(self, source_var: str = "source", target_var: str = "target") -> str:
        return f"{self._cypher_label_filter(source_var)} AND {self._cypher_label_filter(target_var)}"

    def _cypher_relationship_match(
        self,
        where_extra: str = "1=1",
        *,
        source_var: str = "source",
        target_var: str = "target",
        rel_var: str = "r",
    ) -> str:
        return f"""
        MATCH ({source_var})-[{rel_var}]->({target_var})
        WHERE {self._cypher_where_entity_labels(source_var, target_var)}
        AND {where_extra}
        """

    def _log_relationship_source_chunk(
        self,
        source_chunk: Any,
        *,
        indent: str = "",
        show_child_documents: bool = True,
        child_preview_chars: int = 200,
        show_full_child: bool = False,
    ) -> None:
        """统一输出 source_chunk 映射状态 +（可选）子文档内容。"""
        source_chunk_str = str(source_chunk) if source_chunk is not None else "N/A"
        is_mapped = self._is_child_chunk_id(source_chunk)
        mapped_status = " (已映射到子文档)" if is_mapped else " (使用parent_id)"
        logger.info("%s来源文档块 (source_chunk): %s %s", indent, source_chunk_str, mapped_status)

        if not show_child_documents:
            return
        if not is_mapped:
            logger.info("\n%s 对应的子文档: 无（关系使用parent_id，未映射到具体子文档）", indent)
            return

        child_doc = self._get_child_document_by_chunk_id(source_chunk_str)
        if not child_doc:
            logger.warning("%s 无法获取子文档内容 (child_chunk_id: %s)", indent, source_chunk_str)
            return

        logger.info("\n%s 对应的子文档:", indent)
        # 【数据结构说明】
        # child_doc 使用 child_chunk_id 字段
        child_doc_id = child_doc.get('child_chunk_id')
        logger.info("%s   Child Doc ID: %s", indent, child_doc_id)
        logger.info("%s   父文档ID: %s", indent, child_doc['metadata'].get('parent_id', 'N/A'))
        logger.info(
            "%s   Chunk索引: %s/%s", indent,
            child_doc['metadata'].get('child_chunk_index', 'N/A'),
            child_doc['metadata'].get('total_child_chunks', 'N/A')
        )
        content = child_doc.get("content", "") or ""
        if show_full_child:
            logger.info("%s   子文档内容 (%s 字符):", indent, len(content))
            if len(content) > 500:
                logger.info("%s   %s...", indent, content[:500])
                logger.info("%s   ... (还有 %s 字符)", indent, len(content) - 500)
            else:
                logger.info("%s   %s", indent, content)
        else:
            preview = content[:child_preview_chars] + ("..." if len(content) > child_preview_chars else "")
            logger.info("%s   内容预览: %s", indent, preview)

    def _log_entity_detail(
        self,
        entity: Dict[str, Any],
        *,
        index: Optional[int] = None,
        total: Optional[int] = None,
        include_unknown_keys: bool = False,
        show_source_texts_preview: bool = True,
    ) -> None:
        title = "【实体】" if index is None else f"【实体 {index}{f'/{total}' if total else ''}】"
        logger.info("\n%s", title)
        logger.info("  名称 (name): %s", entity.get('name', 'N/A'))
        aliases = entity.get("aliases", [])
        logger.info("  别名 (aliases): %s", aliases if aliases is not None else [])
        logger.info("  标签 (label): %s", entity.get('label', 'N/A'))
        logger.info("  事件类型 (event_type): %s", entity.get('event_type', 'N/A'))
        logger.info("  描述 (description): %s", entity.get('description', 'N/A'))
        logger.info("  出现频率 (frequency): %s", entity.get('frequency', 0))
        logger.info("  首次出现 (first_appearance): %s", entity.get('first_appearance', 'N/A'))
        logger.info("  首次出现书名 (first_book_title): %s", entity.get('first_book_title', 'N/A'))
        logger.info("  首次出现章节索引 (first_chapter_index): %s", entity.get('first_chapter_index', 'N/A'))
        logger.info("  首次出现章节标题 (first_chapter_title): %s", entity.get('first_chapter_title', 'N/A'))
        logger.info("  首次出现场景索引 (first_scene_index): %s", entity.get('first_scene_index', 'N/A'))
        logger.info("  首次出现场景标题 (first_scene_title): %s", entity.get('first_scene_title', 'N/A'))

        # 子文档相关信息（如果图谱节点有存）
        # 【数据结构说明】
        # LLM 返回的字段名是 child_chunk_ids（EntityOutput.child_chunk_ids: List[str]）
        # 在图谱节点中存储的字段名也是 child_chunk_ids（值相同，都是 child_chunk_id 格式的字符串列表）
        # 注意：chunk.metadata 中的字段名是 child_chunk_id（子文档ID），图谱节点中存储的是 child_chunk_ids（列表）
        child_chunk_ids = entity.get("child_chunk_ids", [])
        if child_chunk_ids:
            logger.info("  子文档ID (child_chunk_ids): %s%s", child_chunk_ids[:5], ' ...' if len(child_chunk_ids) > 5 else '')
            logger.info("  子文档ID数量: %s", len(child_chunk_ids))
        else:
            logger.info("子文档ID (child_chunk_ids): []")

        content_preview_chunk_id = entity.get("content_preview_chunk_id", "N/A")
        content_preview_25 = entity.get("content_preview_25", "")
        logger.info("  子文档预览ID (content_preview_chunk_id): %s", content_preview_chunk_id)
        if content_preview_25:
            logger.info("  子文档预览 (content_preview_25): %s", content_preview_25)
        else:
            logger.info("子文档预览 (content_preview_25): N/A")
        if show_source_texts_preview:
            logger.info("  来源文本 (source_texts): %s... (显示前3个)", entity.get('source_texts', [])[:3])
        logger.info("  创建时间 (created_at): %s", entity.get('created_at', 'N/A'))
        logger.info("  更新时间 (updated_at): %s", entity.get('updated_at', 'N/A'))

        if include_unknown_keys:
            known_keys = {
                'name', 'aliases', 'label', 'event_type', 'description', 'frequency',
                'first_appearance', 'first_book_title', 'first_chapter_index',
                'first_chapter_title', 'first_scene_index', 'first_scene_title',
                'source_texts', 'created_at', 'updated_at',
                'child_chunk_ids', 'content_preview_chunk_id', 'content_preview_25'
            }
            self._log_unknown_fields(entity, known_keys, indent="  ")


    def _execute_relationship_query(self, query: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """
        执行关系查询的通用方法（优先走 Neo4j 原生查询）
        
        Args:
            query: Cypher查询语句
            params: 查询参数（可选）
            
        Returns:
            查询结果列表
        """
        try:
            if hasattr(self.graph_store, "execute_query"):
                return self.graph_store.execute_query(query, params or {})
            return []
        except Exception as e:
            if self.verbose:
                logger.warning(" 关系查询失败: %s", e)
            return []

    def _get_child_document_by_chunk_id(self, chunk_id: str) -> Optional[Dict[str, Any]]:
        """
        根据chunk_id获取子文档内容
        
        Args:
            chunk_id: 子文档chunk_id
            
        Returns:
            子文档信息字典，包含content和metadata，如果未找到返回None
        """
        if not self.vector_store or not chunk_id:
            return None
        
        if not isinstance(chunk_id, str):
            chunk_id = str(chunk_id)
        
        try:
            children_collection = self.vector_store.get_collection("children")
            if not children_collection:
                return None
            
            # 根据chunk_id获取子文档
            result = children_collection.get(
                ids=[chunk_id],
                include=["documents", "metadatas"]
            )
            
            if result and result.get('ids') and len(result['ids']) > 0:
                idx = 0
                if result['ids'][idx] == chunk_id:
                    documents = result.get('documents', [])
                    metadatas = result.get('metadatas', [])
                    return {
                        'child_chunk_id': chunk_id,
                        'content': documents[idx] if documents and len(documents) > idx else '',
                        'metadata': metadatas[idx] if metadatas and len(metadatas) > idx else {}
                    }
        except Exception as e:
            if self.verbose:
                logger.debug(" 获取子文档失败 (chunk_id=%s): %s", chunk_id, e)
        
        return None

    @staticmethod
    def _extract_parent_id_from_entity(entity: Entity) -> Optional[str]:
        """从实体对象中提取 parent_id（与 GraphBuilder 逻辑一致）"""
        chapter_index = entity.first_chapter_index
        scene_index = entity.first_scene_index
        if chapter_index is not None:
            return build_parent_id(chapter_index, scene_index)
        return None

    @staticmethod
    def _extract_parent_id_from_relationship(relationship: Relationship) -> Optional[str]:
        """从关系对象中提取 parent_id（与 GraphBuilder 逻辑一致）"""
        chapter_index = relationship.chapter_index
        scene_index = relationship.scene_index
        if chapter_index is not None:
            return build_parent_id(chapter_index, scene_index)
        return None

    def _get_batches_by_parent_id(
        self,
        entities: List[Entity],
        relationships: List[Relationship],
        parents_per_batch: int = GRAPH_BUILD_PARENTS_PER_BATCH,
    ) -> List[Tuple[List[str], List[Entity], List[Relationship]]]:
        """
        按 parent_id 把实体和关系拆成一批批，供断点续传时逐批写入图谱。

        返回：[(parent_ids_batch, entities_batch, relationships_batch), ...]
        """
        # 实体/关系按 parent_id 分组
        entities_by_pid: Dict[str, List[Entity]] = {}
        for e in entities:
            pid = self._extract_parent_id_from_entity(e)
            if pid:
                entities_by_pid.setdefault(pid, []).append(e)
        relationships_by_pid: Dict[str, List[Relationship]] = {}
        for r in relationships:
            pid = self._extract_parent_id_from_relationship(r)
            if pid:
                relationships_by_pid.setdefault(pid, []).append(r)

        # 所有出现过的 parent_id，按 (chapter_index, scene_index) 排序以保持稳定顺序
        def sort_key(pid: str) -> Tuple[int, int]:
            if "_scene_" in pid:
                # parent_chapter_1_scene_2 -> (1, 2)
                parts = pid.replace("parent_chapter_", "").split("_scene_")
                ch = int(parts[0]) if parts[0].isdigit() else 0
                sc = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                return (ch, sc)
            # parent_chapter_1 -> (1, 0)
            try:
                ch = int(pid.replace("parent_chapter_", "").strip())
                return (ch, 0)
            except ValueError:
                return (0, 0)

        all_pids = sorted(
            set(entities_by_pid.keys()) | set(relationships_by_pid.keys()),
            key=sort_key,
        )
        if not all_pids:
            return []

        batches: List[Tuple[List[str], List[Entity], List[Relationship]]] = []
        for i in range(0, len(all_pids), parents_per_batch):
            pids_batch = all_pids[i : i + parents_per_batch]
            entities_batch: List[Entity] = []
            relationships_batch: List[Relationship] = []
            for pid in pids_batch:
                entities_batch.extend(entities_by_pid.get(pid, []))
                relationships_batch.extend(relationships_by_pid.get(pid, []))
            batches.append((pids_batch, entities_batch, relationships_batch))
        return batches

    def build_graph(self, rebuild: bool = False, resume: bool = False) -> Dict[str, Any]:
        """
        构建图谱（支持按 parent 分批写入与断点续传）

        - 按 parent_id 分批：每批写入对应实体和关系到图谱，并写入进度文件。
        - 断点续传：resume=True 时加载已处理的 parent_id，只处理未完成批次。

        【数据流说明】
        1. LLM 返回数据结构（graph_entity_relationship_extractor_llm.py）：
           - EntityOutput.child_chunk_ids: List[str] - 实体所属的子文档ID列表
           - RelationshipOutput.child_chunk_ids: List[str] - 关系所属的子文档ID列表
           - ChunkLexicalOutput.child_chunk_id: str - 子文档ID（lexical 中）

        2. LexicalEnricher 处理（lexical_enricher.py）：
           - 将 LLM 返回的实体和关系数据写入 prepared_rag_data.entities 和 relationships
           - 实体和关系对象中包含 metadata['child_chunk_ids']（子文档 ID 列表）

        3. GraphBuilder 构建（graph_builder.py）：
           - 从实体和关系对象中读取 child_chunk_ids（存储在 metadata 中）
           - 将实体和关系存储到 Neo4j，child_chunk_ids 作为节点属性

        4. 查询时：
           - 从 Neo4j 节点读取 child_chunk_ids
           - 使用 child_chunk_id 从 VectorStore 获取子文档内容

        Args:
            rebuild: 是否重建图谱（清空图与进度后全量重写）
            resume: 是否断点续传（仅处理未完成批次，与 rebuild 同时指定时以 rebuild 为准）
        Returns:
            构建结果统计
        """
        title_suffix = " · 断点续传" if resume and not rebuild else ""
        self._log_title(f" 开始构建图谱{title_suffix}")
        self._ensure_graph_initialized()

        try:
            db_path = self.db_path
            collection_name = self.collection_name

            # 优先从缓存读取已准备数据；无缓存时再执行完整准备流程
            prepared_rag_data = load_prepared_rag_data(
                documents_dir=self.documents_dir,
                project_root=self.project_root,
                collection_name=collection_name,
                db_path=db_path,
                verbose=self.verbose,
                must_exist=False,
            )
            if prepared_rag_data is None:
                raise ValueError("准备数据缓存不存在，请先运行数据准备")
            if self.verbose:
                logger.info(" 已从缓存加载准备数据")
                logger.info("   文档数: %s, 父: %s, 子: %s", len(prepared_rag_data.documents), len(prepared_rag_data.parent_chunks), len(prepared_rag_data.child_chunks))

            # 获取准备数据中的文档、父文档、子文档、实体、关系
            documents = prepared_rag_data.documents
            parent_chunks = prepared_rag_data.parent_chunks
            child_chunks = prepared_rag_data.child_chunks
            entities = prepared_rag_data.entities or []
            relationships = prepared_rag_data.relationships or []

            if self.verbose:
                logger.info(" 准备构建图谱：%s 个实体，%s 个关系", len(entities), len(relationships))

            batches = self._get_batches_by_parent_id(entities, relationships)
            if not batches:
                entity_count = self.graph_store.get_entity_count()
                relationship_count = self.graph_store.get_relationship_count()
                stats = self.graph_store.get_entity_statistics()
                return {
                    "success": True,
                    "documents_loaded": len(documents),
                    "parent_chunks": len(parent_chunks),
                    "child_chunks": len(child_chunks),
                    "child_chunks_map_size": len(prepared_rag_data.child_chunks_map or {}),
                    "entities_extracted": len(entities),
                    "relationships_extracted": len(relationships),
                    "entity_count": entity_count,
                    "relationship_count": relationship_count,
                    "statistics": stats,
                    "build_result": True,
                }

            # 进度：与向量构建一致，用有序列表 + set
            if rebuild:
                clear_graph_progress(db_path, collection_name)
                processed_list: List[str] = []
                processed_set: Set[str] = set()
            elif resume:
                processed_list = load_processed_parent_ids(db_path, collection_name)
                processed_set = set(processed_list)
                if self.verbose and processed_list:
                    logger.info("  断点续传: 已处理 %s 个 parent，跳过对应批次", len(processed_list))
            else:
                clear_graph_progress(db_path, collection_name)
                processed_list = []
                processed_set = set()

            todo_batches = [
                b for b in batches
                if any(pid not in processed_set for pid in b[0])
            ]
            if self.verbose:
                logger.info("  批次概览: 共 %s 批，待处理 %s 批（每批最多 %s 个 parent）", len(batches), len(todo_batches), GRAPH_BUILD_PARENTS_PER_BATCH)

            first_batch = True
            for batch_idx, (parent_ids_batch, entities_batch, relationships_batch) in enumerate(todo_batches):
                batch_rebuild = rebuild and first_batch
                self.graph_builder.build_graph_batch(
                    entities_batch=entities_batch,
                    relationships_batch=relationships_batch,
                    rebuild=batch_rebuild,
                )
                for pid in parent_ids_batch:
                    if pid not in processed_set:
                        processed_set.add(pid)
                        processed_list.append(pid)
                save_progress(db_path, collection_name, processed_list)

                if self.verbose:
                    logger.info(
                        "  已完成批次 %s/%s: %s 个 parent, 实体 %s, 关系 %s",
                        batch_idx + 1, len(todo_batches), len(parent_ids_batch), len(entities_batch), len(relationships_batch)
                    )
                first_batch = False

            if self.verbose and not todo_batches:
                logger.info("（无待处理批次，已全部完成或已跳过）")

            entity_count = self.graph_store.get_entity_count()
            relationship_count = self.graph_store.get_relationship_count()
            stats = self.graph_store.get_entity_statistics()

            result = {
                "success": True,
                "documents_loaded": len(documents),
                "parent_chunks": len(parent_chunks),
                "child_chunks": len(child_chunks),
                "child_chunks_map_size": len(prepared_rag_data.child_chunks_map or {}),
                "entities_extracted": len(entities),
                "relationships_extracted": len(relationships),
                "entity_count": entity_count,
                "relationship_count": relationship_count,
                "statistics": stats,
                "build_result": True,
            }

            if self.verbose:
                self._log_title(" 图谱构建完成")
                logger.info("构建统计:")
                logger.info("   实体数量: %s", entity_count)
                logger.info("   关系数量: %s", relationship_count)
                logger.info("   平均每个实体的关系数: %.2f", stats.get('average_relationships_per_entity', 0))
                logger.info("   关系类型分布: %s", stats.get('relation_type_distribution', {}))

            return result

        except Exception as e:
            logger.error(" 图谱构建失败: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

    def get_statistics(self) -> Dict[str, Any]:
        """
        获取图谱统计信息
        
        Returns:
            统计信息
        """
        self._log_title(" 获取图谱统计信息")

        try:
            stats = self.graph_store.get_entity_statistics()

            if self.verbose:
                logger.info("图谱统计信息:")
                logger.info("   实体数量: %s", stats.get('entity_count', 0))
                logger.info("   关系数量: %s", stats.get('relationship_count', 0))
                logger.info("   平均每个实体的关系数: %.2f", stats.get('average_relationships_per_entity', 0))

                relation_dist = stats.get('relation_type_distribution', {})
                if relation_dist:
                    logger.info("   关系类型分布:")
                    for rel_type, count in sorted(relation_dist.items(), key=lambda x: x[1], reverse=True):
                        logger.info("     - %s: %s", rel_type, count)

            return {
                "success": True,
                "statistics": stats
            }

        except Exception as e:
            logger.error(" 获取统计信息失败: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

    

    def print_all_relationship(self, **kwargs) -> Dict[str, Any]:
        """委托到 GraphInspector（向后兼容）。"""
        return GraphInspector(self).print_all_relationship(**kwargs)

    def print_all_entities(self) -> Dict[str, Any]:
        """委托到 GraphInspector（向后兼容）。"""
        return GraphInspector(self).print_all_entities()

    def run_build_graph_pipeline(
        self,
        rebuild: bool = False,
        resume: bool = False,
    ) -> Dict[str, Any]:
        """
        运行完整图谱流程

        Args:
            rebuild: 是否重建图谱（清空图与进度后全量重写）
            resume: 是否断点续传（仅处理未完成批次；与 rebuild 同时指定时以 rebuild 为准）

        Returns:
            流程执行结果
        """
        self._log_title(" 开始运行完整图谱流程", width=80)

        results = {}

        # 1. 构建图谱（支持断点续传，进度文件在 build 文件中管理）
        build_result = self.build_graph(rebuild=rebuild, resume=resume)
        results["builds"] = build_result

        if not build_result.get("success"):
            logger.error("图谱构建失败，停止流程")
            return results

        # 2. 获取统计信息
        stats_result = self.get_statistics()
        results["statistics"] = stats_result

        self._log_title(" 完整图谱流程完成", width=80)

        return results


