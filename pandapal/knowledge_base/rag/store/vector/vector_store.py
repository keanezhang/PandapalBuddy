#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
向量存储模块 - 父子文档索引版本

负责管理 ChromaDB 向量数据库的双集合存储：
- 子文档集合：用于向量相似度搜索
- 父文档集合：用于上下文扩展（可选，也可以只存储元数据）
"""

import logging
from typing import TYPE_CHECKING, List, Any, Optional, Dict, Literal
from pathlib import Path
import json

from ...rag_protocol import ChunkLike
from ...utils.text_utils import normalize_list_field

# numpy / chromadb 延迟导入，避免未安装时 import 即失败
_np: Any = None
_chromadb: Any = None


def _ensure_numpy() -> Any:
    """延迟导入 numpy，返回模块对象。"""
    global _np
    if _np is None:
        try:
            import numpy as np  # type: ignore[import]
            _np = np
        except ImportError:
            raise ImportError("numpy 未安装，请运行: pip install numpy")
    return _np


def _ensure_chromadb() -> Any:
    """延迟导入 chromadb，返回模块对象。"""
    global _chromadb
    if _chromadb is None:
        try:
            import chromadb  # type: ignore[import]
            _chromadb = chromadb
        except ImportError:
            raise ImportError("chromadb 未安装，请运行: pip install chromadb")
    return _chromadb


if TYPE_CHECKING:
    import numpy as np
    import chromadb
    import chromadb.api as _chroma_api  # noqa: F401


logger = logging.getLogger(__name__)

__all__ = ["VectorStore", "ChunkLike"]


class VectorStore:
    """
    向量存储管理器 - 父子文档索引版本
    
    管理两个集合：
    1. 子文档集合（children）：存储子文档，用于向量相似度搜索
    2. 父文档集合（parents）：存储父文档，用于上下文扩展
    """

    def __init__(
        self,
        db_path: Path,
        collection_name: str,
        project_root: Optional[Path] = None,
        verbose: bool = True,
        *,
        db_batch_size: int = 5000,
        default_query_results: int = 15,
    ):
        """
        初始化向量存储管理器。纯 SDK：db_batch_size、default_query_results 由参数传入。
        """
        self.db_path = db_path
        self.collection_name = collection_name
        self.project_root = project_root
        self.verbose = verbose
        self._db_batch_size = db_batch_size
        self._default_query_results = default_query_results

        self.children_collection_name = f"{collection_name}_children"
        self.parents_collection_name = f"{collection_name}_parents"

        self._chroma_client: Optional[Any] = None
        self._children_collection: Optional[Any] = None
        self._parents_collection: Optional[Any] = None

        if self.verbose:
            logger.info("VectorStore 初始化完成（父子文档索引模式）")
            logger.info("子文档集合: %s", self.children_collection_name)
            logger.info("父文档集合: %s", self.parents_collection_name)

    def initialize(self) -> None:
        """初始化 ChromaDB 客户端和集合"""
        chromadb = _ensure_chromadb()
        self.db_path.mkdir(parents=True, exist_ok=True)
        self._chroma_client = chromadb.PersistentClient(path=str(self.db_path))

        self._children_collection = self._chroma_client.get_or_create_collection(
            name=self.children_collection_name,
            metadata={"description": "RAG 子文档集合（用于向量检索）"},
        )

        self._parents_collection = self._chroma_client.get_or_create_collection(
            name=self.parents_collection_name,
            metadata={"description": "RAG 父文档集合（用于上下文扩展）"},
            embedding_function=None,
        )

    def _should_skip_due_to_dimension_mismatch(
        self,
        collection: Any,
        collection_name: str,
        current_dim: Optional[int],
        chunk_count: int,
    ) -> bool:
        """若集合已有数据且向量维度与本次不一致，记录告警并返回 True（调用方应跳过写入）。"""
        if current_dim is None:
            return False
        try:
            if collection.count() == 0:
                return False
            sample = collection.get(limit=1, include=["embeddings"])
            if not sample.get("embeddings") or len(sample["embeddings"]) == 0:
                return False
            existing_dim = len(sample["embeddings"][0])
            if existing_dim == current_dim:
                return False
            logger.warning(
                " 向量维度不匹配，跳过本次写入。 集合=%s, 已有维度=%s, 本次维度=%s, 文档数=%s",
                collection_name, existing_dim, current_dim, chunk_count,
            )
            if self.verbose:
                logger.info("为保持流程继续已跳过写入。若需使用新维度，请使用 rebuild 模式重建集合。")
            return True
        except Exception as e:
            if self.verbose:
                logger.debug("检查维度时出现异常: %s", e)
            return False

    def store(
        self,
        child_chunks: List[ChunkLike],
        parent_chunks: List[ChunkLike],
        child_texts: List[str],
        child_embeddings: "np.ndarray",
        parent_texts: Optional[List[str]] = None,
        parent_embeddings: Optional["np.ndarray"] = None,
        rebuild: bool = False
    ) -> None:
        """
        存储父子文档到向量数据库
        
        Args:
            child_chunks: 子文档对象列表，每个对象含 .metadata（包括 child_chunk_id、keywords 等）和 .page_content
            parent_chunks: 父文档对象列表，每个对象含 .metadata（包括 parent_chunk_id 等）和 .page_content
            child_texts: 子文档文本列表
            child_embeddings: 子文档向量嵌入数组（numpy ndarray）
            parent_texts: 父文档文本列表（可选，如果为None则不存储父文档向量）
            parent_embeddings: 父文档向量嵌入数组（可选）
            rebuild: 是否重新构建（删除旧数据）
        """
        if self._chroma_client is None:
            self.initialize()

        if self.verbose:
            logger.info("本批写入: 子 %s, 父 %s", len(child_chunks), len(parent_chunks))

        # 1. 存储子文档集合（允许仅存父文档时 child_chunks 为空）
        if child_chunks:
            if len(child_embeddings) > 0:
                self._store_children(child_chunks, child_texts, child_embeddings, rebuild)
            else:
                logger.warning(
                    "子文档 %s 条但向量为空，已跳过子文档写入（仅写父文档）", len(child_chunks)
                )

        # 2. 存储父文档集合
        self._store_parents(parent_chunks, parent_texts, parent_embeddings, rebuild)

        if self.verbose:
            total_c = self._children_collection.count()
            total_p = self._parents_collection.count()
            logger.info("本批已写入；当前合计: 子文档 %s, 父文档 %s", total_c, total_p)

    def _store_children(
        self,
        chunks: List[ChunkLike],
        texts: List[str],
        embeddings: "np.ndarray",
        rebuild: bool = False
    ) -> None:
        """存储子文档到子文档集合"""
        # 子文档必需字段 child_chunk_id 校验：缺失说明上游数据有问题，直接暴露而非写入坏数据
        missing_ids = [
            idx for idx, chunk in enumerate(chunks)
            if not chunk.metadata.get("child_chunk_id")
        ]
        if missing_ids:
            raise ValueError(
                f"子文档缺少必需字段 child_chunk_id，共 {len(missing_ids)} 条，"
                f"示例索引: {missing_ids[:5]}"
            )

        if self.verbose and rebuild:
            logger.info("└ 子文档集合: 重建中...")

        if len(embeddings) == 0:
            raise ValueError("子文档向量数组为空，无法获取维度信息")
        current_embedding_dim = embeddings.shape[1] if len(embeddings.shape) > 1 else len(embeddings[0])

        if rebuild:
            try:
                self._chroma_client.delete_collection(self.children_collection_name)
                if self.verbose:
                    logger.info("└ 已删除旧子文档集合: %s", self.children_collection_name)
            except Exception as e:
                if self.verbose:
                    logger.debug("删除集合时出现异常（可能集合不存在）: %s", e)
            self._children_collection = self._chroma_client.create_collection(
                name=self.children_collection_name,
                metadata={"description": "RAG 子文档集合（用于向量检索）"},
                embedding_function=None,
            )
            if self.verbose:
                logger.info("└ 已创建子文档集合 (维度 %s)", current_embedding_dim)
        else:
            if self._should_skip_due_to_dimension_mismatch(
                self._children_collection,
                self.children_collection_name,
                current_embedding_dim,
                len(chunks),
            ):
                return

        if chunks:
            batch_size = self._db_batch_size
            total_chunks = len(chunks)
            embeddings_list = embeddings.tolist()

            for i in range(0, total_chunks, batch_size):
                end_idx = min(i + batch_size, total_chunks)
                batch_chunks = chunks[i:end_idx]
                batch_texts = texts[i:end_idx]
                batch_embeddings = embeddings_list[i:end_idx]
                batch_ids = [chunk.metadata.get("child_chunk_id") for chunk in batch_chunks]

                # Chroma 仅支持标量 metadata，keywords/segmented_words 以 JSON 字符串存储
                batch_metadatas = []
                _verbose_once = self.verbose and i == 0
                for chunk_idx, chunk in enumerate(batch_chunks):
                    keywords_list = normalize_list_field(
                        chunk.metadata.get("keywords"),
                        verbose=(_verbose_once and chunk_idx == 0),
                        field_name="keywords",
                    )
                    segmented_words_list = normalize_list_field(
                        chunk.metadata.get("segmented_words"),
                        verbose=(_verbose_once and chunk_idx == 0),
                        field_name="segmented_words",
                    )

                    chunk_metadata = {}
                    for key, value in chunk.metadata.items():
                        if key in ("keywords", "segmented_words"):
                            continue
                        chunk_metadata[key] = value
                    chunk_metadata["keywords"] = json.dumps(keywords_list, ensure_ascii=False)
                    chunk_metadata["segmented_words"] = json.dumps(segmented_words_list, ensure_ascii=False)
                    chunk_metadata["keywords_count"] = len(keywords_list)

                    if _verbose_once and chunk_idx == 0:
                        logger.debug(
                            "子文档已带关键词: keywords=%s, segmented_words=%s",
                            len(keywords_list), len(segmented_words_list),
                        )
                    batch_metadatas.append(chunk_metadata)

                try:
                    self._children_collection.upsert(
                        embeddings=batch_embeddings,
                        documents=batch_texts,
                        metadatas=batch_metadatas,
                        ids=batch_ids,
                    )
                except ValueError as e:
                    error_msg = str(e).lower()
                    if "dimension" in error_msg or "embedding" in error_msg:
                        logger.warning(
                            " 子文档集合维度不匹配，删除并重建集合: %s", e,
                        )
                        self._chroma_client.delete_collection(self.children_collection_name)
                        self._children_collection = self._chroma_client.create_collection(
                            name=self.children_collection_name,
                            metadata={"description": "RAG 子文档集合（用于向量检索）"},
                            embedding_function=None,
                        )
                        self._children_collection.upsert(
                            embeddings=batch_embeddings,
                            documents=batch_texts,
                            metadatas=batch_metadatas,
                            ids=batch_ids,
                        )
                    else:
                        raise

            if self.verbose:
                logger.info("└ 子文档: %s 条已写入", total_chunks)

    def _store_parents(
        self,
        chunks: List[ChunkLike],
        texts: Optional[List[str]] = None,
        embeddings: Optional["np.ndarray"] = None,
        rebuild: bool = False
    ) -> None:
        """存储父文档到父文档集合"""
        if not chunks:
            return

        current_embedding_dim = None
        if embeddings is not None and len(embeddings) > 0:
            current_embedding_dim = embeddings.shape[1] if len(embeddings.shape) > 1 else len(embeddings[0])

        if rebuild:
            try:
                self._chroma_client.delete_collection(self.parents_collection_name)
                if self.verbose:
                    logger.info("└ 已删除旧父文档集合: %s", self.parents_collection_name)
            except Exception as e:
                if self.verbose:
                    logger.debug("删除集合时出现异常（可能集合不存在）: %s", e)
            self._parents_collection = self._chroma_client.create_collection(
                name=self.parents_collection_name,
                metadata={"description": "RAG 父文档集合（用于上下文扩展）"},
                embedding_function=None,
            )
            if self.verbose:
                dim_info = " (维度 %s)" % current_embedding_dim if current_embedding_dim else ""
                logger.info("└ 已创建父文档集合%s", dim_info)
        else:
            if self._should_skip_due_to_dimension_mismatch(
                self._parents_collection,
                self.parents_collection_name,
                current_embedding_dim,
                len(chunks),
            ):
                return

        if texts is None:
            texts = [chunk.page_content for chunk in chunks]

        parent_ids = [chunk.metadata["parent_id"] for chunk in chunks]
        parent_metadatas = [chunk.metadata.copy() for chunk in chunks]

        if embeddings is not None and len(embeddings) > 0:
            embeddings_list = embeddings.tolist()
            try:
                self._parents_collection.upsert(
                    embeddings=embeddings_list,
                    documents=texts,
                    metadatas=parent_metadatas,
                    ids=parent_ids
                )
            except ValueError as e:
                error_msg = str(e).lower()
                if "dimension" in error_msg or "embedding" in error_msg:
                    logger.warning(
                        " 父文档集合维度不匹配，删除并重建集合: %s", e,
                    )
                    self._chroma_client.delete_collection(self.parents_collection_name)
                    self._parents_collection = self._chroma_client.create_collection(
                        name=self.parents_collection_name,
                        metadata={"description": "RAG 父文档集合（用于上下文扩展）"},
                        embedding_function=None,
                    )
                    self._parents_collection.upsert(
                        embeddings=embeddings_list,
                        documents=texts,
                        metadatas=parent_metadatas,
                        ids=parent_ids
                    )
                else:
                    raise
            if self.verbose:
                logger.info("└ 父文档: %s 条已写入（带向量）", len(chunks))
        else:
            self._parents_collection.upsert(
                documents=texts,
                metadatas=parent_metadatas,
                ids=parent_ids
            )
            if self.verbose:
                logger.info("└ 父文档: %s 条已写入（仅元数据）", len(chunks))

    def query_children(
        self,
        query_embedding: "np.ndarray",
        n_results: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        在子文档集合中查询
        
        Args:
            query_embedding: 查询向量
            n_results: 返回结果数量（如果为 None，则使用配置的默认值）
            
        Returns:
            查询结果字典，包含 documents、metadatas、distances、ids
        """
        if self._children_collection is None:
            self.initialize()

        if n_results is None:
            n_results = self._default_query_results

        results = self._children_collection.query(
            query_embeddings=[query_embedding.tolist()],
            n_results=n_results
        )

        return results

    def get_parents_by_ids(self, parent_ids: List[str]) -> Dict[str, Any]:
        """
        根据父文档ID获取父文档
        
        Args:
            parent_ids: 父文档ID列表
            
        Returns:
            父文档字典，包含 documents、metadatas、ids
        """
        if self._parents_collection is None:
            self.initialize()

        results = self._parents_collection.get(
            ids=parent_ids,
            include=["documents", "metadatas"]
        )

        return results

    def get_collection(self, collection_type: Literal["children", "parents"] = "children") -> "chromadb.Collection":
        """
        获取集合对象
        
        Args:
            collection_type: 集合类型，"children" 或 "parents"

        Returns:
            ChromaDB Collection 对象
        """
        if collection_type == "children":
            if self._children_collection is None:
                self.initialize()
            return self._children_collection
        else:
            if self._parents_collection is None:
                self.initialize()
            return self._parents_collection

    def count(self, collection_type: Literal["children", "parents"] = "children") -> int:
        """
        获取集合中的文档数量
        
        Args:
            collection_type: 集合类型，"children" 或 "parents"
        """
        if collection_type == "children":
            if self._children_collection is None:
                self.initialize()
            return self._children_collection.count()
        else:
            if self._parents_collection is None:
                self.initialize()
            return self._parents_collection.count()

    def delete_by_sources(self, sources: List[str]) -> int:
        """
        按来源文件（metadata["source"]）精确删除 children/parents 两集合中的向量。

        用于增量建库：删除或修改文件时，先按 source 清掉其旧向量，避免残留。

        Args:
            sources: 来源文件路径列表（与 chunk.metadata["source"] 一致）

        Returns:
            删除的向量总数（children + parents），失败时不抛异常、返回 0 并记日志。
        """
        if not sources:
            return 0
        # ChromaDB 的 where $in 不接受空集合，也不接受重复项过多影响性能，故去重
        uniq = [s for s in dict.fromkeys(sources) if s]
        if not uniq:
            return 0

        deleted = 0
        for collection_type in ("children", "parents"):
            collection = self.get_collection(collection_type)  # type: ignore[arg-type]
            before = collection.count()
            try:
                collection.delete(where={"source": {"$in": uniq}})
            except Exception as exc:  # noqa: BLE001
                # 单条/集合删除失败不应中断整库构建，记录后继续
                logger.warning(
                    "按 source 删除 %s 集合失败（sources=%d 个）: %s",
                    collection_type,
                    len(uniq),
                    exc,
                )
                continue
            after = collection.count()
            deleted += max(before - after, 0)
            if self.verbose and before != after:
                logger.info(
                    "  [增量] %s 集合按 source 删除 %d 条向量",
                    collection_type,
                    before - after,
                )
        return deleted

    def count_by_source(self, source: str) -> int:
        """
        统计某一来源文件在两集合中的向量条数（诊断用）。

        Args:
            source: 来源文件路径

        Returns:
            children + parents 中该 source 的向量条数。
        """
        if not source:
            return 0
        total = 0
        for collection_type in ("children", "parents"):
            collection = self.get_collection(collection_type)  # type: ignore[arg-type]
            try:
                result = collection.get(where={"source": source}, include=[])
                ids = result.get("ids") or []
                total += len(ids)
            except Exception as exc:  # noqa: BLE001
                logger.warning("统计 source=%s 的 %s 向量失败: %s", source, collection_type, exc)
        return total

    def exists(self) -> bool:
        """检查集合是否存在"""
        try:
            if self._chroma_client is None:
                self.initialize()
            self._chroma_client.get_collection(self.children_collection_name)
            self._chroma_client.get_collection(self.parents_collection_name)
            return True
        except Exception:
            return False

    def close(self) -> None:
        """关闭 ChromaDB 连接并释放集合引用。"""
        self._children_collection = None
        self._parents_collection = None
        if self._chroma_client is not None:
            # ChromaDB PersistentClient 会自动处理持久化
            self._chroma_client = None
            if self.verbose:
                logger.info("已关闭VectorStore连接，释放资源")

    def __enter__(self) -> 'VectorStore':
        """上下文管理器入口"""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """上下文管理器退出，自动清理资源"""
        self.close()

    def __repr__(self) -> str:
        return (
            f"VectorStore(db_path={self.db_path!r}, "
            f"collection={self.collection_name!r})"
        )
