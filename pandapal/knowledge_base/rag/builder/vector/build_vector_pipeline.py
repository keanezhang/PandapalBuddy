#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
向量化数据构建

提供向量存储的构建能力（子/父文档向量化、分批写入、断点续传、相似度分布统计）。
CLI 入口见 scripts/rag/run_build_vector.py。
"""

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Set

from ...embedding.embedding import (
    EmbeddingEncoder,
    CloudEmbeddingConfig,
    LocalEmbeddingConfig
)
from ...store.vector.vector_store import VectorStore
from ..data.build_rag_data import load_prepared_rag_data
from ..data.data_preparer import DataPreparationResult
from .vector_build_progress import (
    load_processed_parent_ids,
    save_progress,
    clear_progress,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# numpy 延迟导入（与 SDK 其他模块保持一致的延迟导入策略）
# ---------------------------------------------------------------------------
_np = None


def _ensure_numpy():
    """延迟导入 numpy。"""
    global _np
    if _np is not None:
        return _np
    try:
        import numpy as _numpy
        _np = _numpy
    except ImportError:
        raise ImportError(
            "numpy is required for BuildVectorPipeline. "
            "Install with: pip install numpy"
        )
    return _np

# 向量构建断点续传：每批包含的父文档数量，便于分批写入和分批记录进度
VECTOR_BUILD_PARENTS_PER_BATCH = 2

# 日志分隔符：主模块用双线，子步骤用单线，便于终端输出分块阅读
_LOG_SEP_MAIN = "═" * 72
_LOG_SEP_SUB = "─" * 56

# 相似度分布计算时最大文档对数限制
_MAX_SIMILARITY_PAIRS = 50


@dataclass
class BuildVectorPipelineConfig:
    """向量化流程构建配置"""
    documents_dir: Path
    db_path: Path
    collection_name: str
    project_root: Path
    use_cloud_embedding: bool
    cloud_config: Optional[CloudEmbeddingConfig] = None
    local_config: Optional[LocalEmbeddingConfig] = None
    verbose: bool = True

    def __post_init__(self):
        """验证配置一致性"""
        if self.use_cloud_embedding:
            if self.cloud_config is None:
                raise ValueError("使用云端向量化时，cloud_config 必填")
        else:
            if self.local_config is None:
                raise ValueError("使用本地向量化时，local_config 必填")


class BuildVectorPipeline:
    """向量化流程构建类"""

    def __init__(self, config: BuildVectorPipelineConfig):
        """
        初始化构建
        
        Args:
            config: 构建配置对象
        """
        self.config = config
        self.project_root = config.project_root
        self.documents_dir = config.documents_dir
        self.db_path = config.db_path
        self.collection_name = config.collection_name
        self.verbose = config.verbose
        self.use_cloud_embedding = config.use_cloud_embedding

        # 初始化向量化提供者
        if config.use_cloud_embedding:
            cloud_config = config.cloud_config
            self.cloud_api_type = cloud_config.api_type
            self.cloud_model = cloud_config.model
            self.cloud_dimension = cloud_config.dimension
            self.embedding_model_name = None

            # 直接使用配置对象（已经是 CloudEmbeddingConfig 类型）
            self.embedding_provider = EmbeddingEncoder(
                cloud_config=cloud_config,
                verbose=config.verbose
            )
        else:
            local_config = config.local_config
            self.cloud_api_type = None
            self.cloud_model = None
            self.cloud_dimension = None
            self.embedding_model_name = local_config.model_name

            # 直接使用配置对象（已经是 LocalEmbeddingConfig 类型）
            self.embedding_provider = EmbeddingEncoder(
                local_config=local_config,
                verbose=config.verbose
            )

        # 初始化向量存储
        self.vector_store = VectorStore(
            db_path=self.db_path,
            collection_name=self.collection_name,
            project_root=self.project_root,
            verbose=self.verbose
        )

        if self.verbose:
            logger.info("")
            logger.info(_LOG_SEP_MAIN)
            logger.info("【1】初始化 · 向量化流程构建")
            logger.info(_LOG_SEP_MAIN)
            logger.info("  文档目录:   %s", self.documents_dir)
            logger.info("  数据库路径: %s", self.db_path)
            logger.info("  集合名称:   %s", self.collection_name)
            logger.info("  向量化方式: %s", '云端' if self.use_cloud_embedding else '本地')
            if self.use_cloud_embedding:
                logger.info("  云端模型:   %s (API: %s)", self.cloud_model, self.cloud_api_type)
            elif self.embedding_model_name:
                logger.info("  本地模型:   %s", self.embedding_model_name)
            logger.info("")

    def _get_batches_by_parent(
        self,
        parent_chunks: List[Any],
        child_chunks: List[Any],
        parents_per_batch: int = VECTOR_BUILD_PARENTS_PER_BATCH,
    ) -> List[Tuple[List[Any], List[Any]]]:
        """
        按「父文档」把数据拆成一批批，供断点续传时逐批 embed + 写入。

        输入：
          - parent_chunks：父文档列表（如按章节/场景切出的整块）
          - child_chunks：子文档列表（扁平），每个子文档的 metadata["parent_id"] 指向所属父文档
          - parents_per_batch：每批包含的父文档数量（由常量 VECTOR_BUILD_PARENTS_PER_BATCH 控制）

        输出：
          - 列表 [(父文档列表_batch1, 子文档列表_batch1), (父文档列表_batch2, 子文档列表_batch2), ...]
          - 每批最多 parents_per_batch 个父文档，顺序与 parent_chunks 一致，便于按 parent_id 分批记录进度

        实现思路：
          1）先把扁平的 child_chunks 按 parent_id 分组 → child_by_parent[parent_id] = [子文档列表]
          2）按 parent_chunks 顺序，每 parents_per_batch 个父文档组成一批，收集这批父文档及其所有子文档
        """
        # 第一步：子文档按 parent_id 分组（扁平列表 → 按父文档聚合成字典）
        child_by_parent: Dict[str, List[Any]] = {}
        for c in child_chunks:
            pid = (getattr(c, "metadata", None) or {}).get("parent_id")
            if pid:
                child_by_parent.setdefault(pid, []).append(c)

        # 第二步：按父文档顺序，每 parents_per_batch 个父文档作为一批
        valid_parents = [
            p for p in parent_chunks
            if (getattr(p, "metadata", None) or {}).get("parent_id")
        ]
        batches: List[Tuple[List[Any], List[Any]]] = []
        for i in range(0, len(valid_parents), parents_per_batch):
            parent_batch = valid_parents[i : i + parents_per_batch]
            child_batch: List[Any] = []
            for p in parent_batch:
                pid = (getattr(p, "metadata", None) or {}).get("parent_id")
                if pid:
                    child_batch.extend(child_by_parent.get(pid, []))
            batches.append((parent_batch, child_batch))
        return batches

    def get_embedding_config(self) -> Dict[str, Any]:
        """
        获取向量化配置

        Returns:
            配置信息
        """
        if self.verbose:
            logger.info("\n" + _LOG_SEP_SUB)
            logger.info("获取向量化配置")
            logger.info(_LOG_SEP_SUB)

        try:
            config = self.embedding_provider.get_embedding_config()

            if self.verbose:
                logger.info("向量化配置:")
                for key, value in config.items():
                    logger.info("   %s: %s", key, value)

            return {
                "success": True,
                "config": config
            }

        except Exception as e:
            logger.error(" 获取配置失败: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

    def run_encode_texts(self, texts: List[str]) -> Dict[str, Any]:
        """
        文本向量化（批量编码并返回统计）

        Args:
            texts: 文本列表

        Returns:
            向量化结果（含向量数量、维度、耗时、范数统计等）
        """
        if self.verbose:
            logger.info("\n" + _LOG_SEP_SUB)
            logger.info("  文本向量化 (%s 个文本)", len(texts))
            logger.info(_LOG_SEP_SUB)

        try:
            np = _ensure_numpy()
            start_time = time.time()

            embeddings = self.embedding_provider.encode_texts(texts)

            elapsed_time = time.time() - start_time

            # 检查向量维度
            if isinstance(embeddings, np.ndarray):
                embedding_dim = embeddings.shape[1] if len(embeddings.shape) > 1 else len(embeddings[0])
                embedding_count = embeddings.shape[0] if len(embeddings.shape) > 1 else 1
            else:
                embedding_dim = len(embeddings[0]) if embeddings else 0
                embedding_count = len(embeddings)

            # 计算统计信息
            if isinstance(embeddings, np.ndarray) and len(embeddings) > 0:
                norms = [np.linalg.norm(emb) for emb in embeddings]
                mean_norm = np.mean(norms)
                std_norm = np.std(norms)
                min_norm = np.min(norms)
                max_norm = np.max(norms)
            else:
                mean_norm = std_norm = min_norm = max_norm = 0.0

            result = {
                "success": True,
                "embedding_count": embedding_count,
                "embedding_dim": embedding_dim,
                "elapsed_time": elapsed_time,
                "speed": embedding_count / elapsed_time if elapsed_time > 0 else 0,
                "statistics": {
                    "mean_norm": float(mean_norm),
                    "std_norm": float(std_norm),
                    "min_norm": float(min_norm),
                    "max_norm": float(max_norm)
                }
            }

            if self.verbose:
                logger.info(" 向量化完成:")
                logger.info("   向量数量: %s", embedding_count)
                logger.info("   向量维度: %s", embedding_dim)
                logger.info("   耗时: %.2f 秒", elapsed_time)
                logger.info("   速度: %.2f 文本/秒", result['speed'])
                logger.info("   向量范数统计:")
                logger.info("     平均值: %.4f", mean_norm)
                logger.info("     标准差: %.4f", std_norm)
                logger.info("     最小值: %.4f", min_norm)
                logger.info("     最大值: %.4f", max_norm)

            return result

        except Exception as e:
            logger.error(" 向量化失败: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

    def build_vector_store(
        self,
        prepared_rag_data: DataPreparationResult,
        rebuild: bool = False,
        resume: bool = False,
        incremental: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        向量存储构建（支持按 parent 分批写入与断点续传）

        - 按 parent 分批：每批 embed 该 parent 及其子文档后 store，并写入进度文件。
        - 断点续传：resume=True 时加载已处理的 parent_id，只处理未完成批次；下次中断后可再次带 --resume 继续。
        - 增量：incremental={"delete_sources":[...], "only_sources":[...]} 时，只对
          only_sources 命中的 parent/child 重新 embed + upsert，并先按 source 删除
          delete_sources 的旧向量；incremental=None 维持全量行为。

        【数据流说明】见方法内注释；lexical 校验在首批含子文档时执行。
        """
        if self.verbose:
            logger.info("")
            logger.info(_LOG_SEP_MAIN)
            logger.info("【3】向量存储构建" + (" · 断点续传" if resume else "") + (" · 增量" if incremental else ""))
            logger.info(_LOG_SEP_MAIN)

        try:
            # ── 增量模式：走独立分支（先算 embedding → 删旧块 → 写新块）──
            if incremental:
                return self._build_vector_store_incremental(
                    prepared_rag_data=prepared_rag_data,
                    incremental=incremental,
                )

            np = _ensure_numpy()
            documents = prepared_rag_data.documents
            child_chunks = prepared_rag_data.child_chunks
            parent_chunks = prepared_rag_data.parent_chunks

            batches = self._get_batches_by_parent(parent_chunks, child_chunks)
            if not batches:
                if self.verbose:
                    logger.warning("没有可用的 parent 批次，跳过构建")
                self.vector_store.initialize()
                return {
                    "success": True,
                    "documents_loaded": len(documents),
                    "parent_chunks": len(parent_chunks),
                    "child_chunks": len(child_chunks),
                    "children_stored": self.vector_store.count("children"),
                    "parents_stored": self.vector_store.count("parents"),
                    "embedding_dim": 0,
                }

            # 进度：用有序列表保存，便于与 parent_chunks 顺序一致；用 set 做快速判重
            if rebuild:
                clear_progress(self.db_path, self.collection_name)
                processed_list: List[str] = []
                processed_set: Set[str] = set()
            elif resume:
                processed_list = load_processed_parent_ids(self.db_path, self.collection_name)
                processed_set = set(processed_list)
                if self.verbose and processed_list:
                    logger.info("  断点续传: 已处理 %s 个 parent，跳过对应批次", len(processed_list))
            else:
                clear_progress(self.db_path, self.collection_name)
                processed_list = []
                processed_set = set()

            # 只处理「至少有一个父文档未完成」的批次
            def _batch_parent_ids(b: Tuple[List[Any], List[Any]]) -> List[str]:
                return [(getattr(p, "metadata", None) or {}).get("parent_id") for p in b[0] if (getattr(p, "metadata", None) or {}).get("parent_id")]

            todo_batches = [
                b for b in batches
                if any(pid not in processed_set for pid in _batch_parent_ids(b))
            ]
            if self.verbose:
                logger.info(_LOG_SEP_SUB)
                logger.info("  开始处理批次概览: 共 %s 批，待处理 %s 批（每批最多 %s 个父文档）", len(batches), len(todo_batches), VECTOR_BUILD_PARENTS_PER_BATCH)
                logger.info(_LOG_SEP_SUB)

            self.vector_store.initialize()

            first_batch = True
            last_embedding_dim = 0
            for batch_idx, (parent_chunks_batch, child_chunks_batch) in enumerate(todo_batches):
                batch_pids = _batch_parent_ids((parent_chunks_batch, child_chunks_batch))
                if not batch_pids:
                    continue

                # 首批含子文档时做 lexical 校验
                if first_batch and child_chunks_batch and self.verbose:
                    sample = child_chunks_batch[0]
                    if hasattr(sample, "metadata") and sample.metadata:
                        has_kw = "keywords" in sample.metadata and sample.metadata.get("keywords") is not None
                        has_seg = "segmented_words" in sample.metadata and sample.metadata.get("segmented_words") is not None
                        if not has_kw or not has_seg:
                            raise ValueError(
                                "子文档缺少必需的 lexical 数据（keywords/segmented_words），请确保已通过 LexicalEnricher 处理。"
                            )
                        logger.info("lexical 校验: 子文档 keywords / segmented_words 通过")

                batch_child_texts = [c.page_content for c in child_chunks_batch]
                batch_parent_texts = [p.page_content for p in parent_chunks_batch]
                batch_child_embeddings = (
                    self.embedding_provider.encode_texts(batch_child_texts)
                    if child_chunks_batch
                    else np.array([]).reshape(0, 0)
                )
                batch_parent_embeddings = self.embedding_provider.encode_texts(batch_parent_texts)

                if isinstance(batch_parent_embeddings, np.ndarray) and batch_parent_embeddings.size > 0:
                    last_embedding_dim = batch_parent_embeddings.shape[1]
                elif isinstance(batch_child_embeddings, np.ndarray) and batch_child_embeddings.size > 0:
                    last_embedding_dim = batch_child_embeddings.shape[1]

                batch_rebuild = rebuild and first_batch
                self.vector_store.store(
                    child_chunks=child_chunks_batch,
                    parent_chunks=parent_chunks_batch,
                    child_texts=batch_child_texts,
                    child_embeddings=batch_child_embeddings,
                    parent_texts=batch_parent_texts,
                    parent_embeddings=batch_parent_embeddings,
                    rebuild=batch_rebuild,
                )

                for pid in batch_pids:
                    if pid not in processed_set:
                        processed_set.add(pid)
                        processed_list.append(pid)
                save_progress(self.db_path, self.collection_name, processed_list)

                if self.verbose:
                    logger.info(
                        "  已完成批次 %s/%s: 父 %s, 子 %s",
                        batch_idx + 1, len(todo_batches), len(parent_chunks_batch), len(child_chunks_batch)
                    )
                first_batch = False
            else:
                if not todo_batches and self.verbose:
                    logger.info("（无待处理批次，已全部完成或已跳过）")

            children_count = self.vector_store.count("children")
            parents_count = self.vector_store.count("parents")
            result = {
                "success": True,
                "documents_loaded": len(documents),
                "parent_chunks": len(parent_chunks),
                "child_chunks": len(child_chunks),
                "children_stored": children_count,
                "parents_stored": parents_count,
                "embedding_dim": last_embedding_dim,
            }

            if self.verbose:
                logger.info("")
                logger.info("  【3】结果: 子文档 %s，父文档 %s，向量维度 %s", children_count, parents_count, result['embedding_dim'])
                logger.info("")

            return result

        except Exception as e:
            logger.error(" 向量存储构建失败: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

    @staticmethod
    def _doc_source(doc: Any) -> Optional[str]:
        return (getattr(doc, "metadata", None) or {}).get("source")

    def _build_vector_store_incremental(
        self,
        *,
        prepared_rag_data: DataPreparationResult,
        incremental: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        增量写入：只重建 only_sources 命中的文件，并先按 source 删除 delete_sources 的旧向量。

        顺序（规避"旧块已删、新块未写"空窗）：先算好全部 embedding → 再删旧块 → 最后 upsert 新块。
        embedding 失败时抛异常，不执行删除，旧数据保持可用。
        """
        np = _ensure_numpy()
        only_sources = incremental.get("only_sources")
        delete_sources = sorted(set(incremental.get("delete_sources") or []))
        only_set = set(only_sources) if only_sources is not None else None

        parent_chunks = prepared_rag_data.parent_chunks
        child_chunks = prepared_rag_data.child_chunks
        documents = prepared_rag_data.documents
        if only_set is not None:
            parent_chunks = [p for p in parent_chunks if self._doc_source(p) in only_set]
            child_chunks = [c for c in child_chunks if self._doc_source(c) in only_set]
            documents = [d for d in documents if self._doc_source(d) in only_set]

        if self.verbose:
            logger.info(
                "  增量模式：待重建 父 %s / 子 %s；待删除 source %s 个",
                len(parent_chunks), len(child_chunks), len(delete_sources),
            )

        batches = self._get_batches_by_parent(parent_chunks, child_chunks)

        # 阶段一：先算好所有 embedding（失败则不删旧数据）
        prepared_batches: List[Tuple[List[Any], List[Any], List[str], List[str], Any, Any]] = []
        last_embedding_dim = 0
        for parent_chunks_batch, child_chunks_batch in batches:
            child_texts = [c.page_content for c in child_chunks_batch]
            parent_texts = [p.page_content for p in parent_chunks_batch]
            child_embeddings = (
                self.embedding_provider.encode_texts(child_texts)
                if child_chunks_batch
                else np.array([]).reshape(0, 0)
            )
            parent_embeddings = self.embedding_provider.encode_texts(parent_texts)
            if isinstance(parent_embeddings, np.ndarray) and parent_embeddings.size > 0:
                last_embedding_dim = parent_embeddings.shape[1]
            elif isinstance(child_embeddings, np.ndarray) and child_embeddings.size > 0:
                last_embedding_dim = child_embeddings.shape[1]
            prepared_batches.append(
                (parent_chunks_batch, child_chunks_batch, child_texts, parent_texts,
                 child_embeddings, parent_embeddings)
            )

        self.vector_store.initialize()

        # 阶段二：删除变更/删除文件的旧向量
        deleted = 0
        if delete_sources and hasattr(self.vector_store, "delete_by_sources"):
            deleted = self.vector_store.delete_by_sources(delete_sources)

        # 阶段三：upsert 变更文件的新向量（不 rebuild，其余向量原样保留）
        for (parent_chunks_batch, child_chunks_batch, child_texts, parent_texts,
             child_embeddings, parent_embeddings) in prepared_batches:
            self.vector_store.store(
                child_chunks=child_chunks_batch,
                parent_chunks=parent_chunks_batch,
                child_texts=child_texts,
                child_embeddings=child_embeddings,
                parent_texts=parent_texts,
                parent_embeddings=parent_embeddings,
                rebuild=False,
            )

        children_count = self.vector_store.count("children")
        parents_count = self.vector_store.count("parents")
        if self.verbose:
            logger.info(
                "  增量写入完成：重建父 %s / 子 %s，删除旧向量 %s，库内子 %s / 父 %s",
                len(parent_chunks), len(child_chunks), deleted, children_count, parents_count,
            )

        return {
            "success": True,
            "incremental": True,
            "deleted_vectors": deleted,
            "documents_loaded": len(documents),
            "parent_chunks": len(parent_chunks),
            "child_chunks": len(child_chunks),
            "children_stored": children_count,
            "parents_stored": parents_count,
            "embedding_dim": last_embedding_dim,
        }

    def get_similarity_distribution(self, sample_size: int = 100) -> Dict[str, Any]:
        """
        相似度分布统计（在子文档集合上采样计算两两余弦相似度）

        Args:
            sample_size: 采样数量

        Returns:
            分布统计（均值、标准差、中位数、最小/最大）
        """
        if self.verbose:
            logger.info("")
            logger.info(_LOG_SEP_SUB)
            logger.info("  【4】相似度分布统计（采样 %s）", sample_size)
            logger.info(_LOG_SEP_SUB)

        try:
            np = _ensure_numpy()
            # 获取一些文档进行统计
            children_collection = self.vector_store.get_collection("children")
            all_docs = children_collection.get(limit=sample_size, include=["documents", "embeddings"])

            if not all_docs or not all_docs.get('documents'):
                return {"success": False, "error": "没有可用的文档"}

            documents = all_docs['documents']
            embeddings = all_docs.get('embeddings', [])

            # 检查embeddings是否为空（兼容列表和numpy数组）
            if len(embeddings) == 0:
                return {"success": False, "error": "没有可用的向量"}

            # 计算文档之间的相似度
            similarities = []
            for i in range(min(_MAX_SIMILARITY_PAIRS, len(embeddings))):  # 限制计算量
                for j in range(i + 1, min(_MAX_SIMILARITY_PAIRS, len(embeddings))):
                    emb1 = np.array(embeddings[i])
                    emb2 = np.array(embeddings[j])
                    # 计算余弦相似度
                    similarity = np.dot(emb1, emb2) / (np.linalg.norm(emb1) * np.linalg.norm(emb2))
                    similarities.append(similarity)

            if similarities:
                result = {
                    "success": True,
                    "sample_size": len(similarities),
                    "statistics": {
                        "mean_similarity": float(np.mean(similarities)),
                        "std_similarity": float(np.std(similarities)),
                        "min_similarity": float(np.min(similarities)),
                        "max_similarity": float(np.max(similarities)),
                        "median_similarity": float(np.median(similarities))
                    }
                }

                if self.verbose:
                    s = result["statistics"]
                    logger.info(
                        "  样本数 %s | 平均 %.4f 标准差 %.4f | 中位数 %.4f | 最小 %.4f 最大 %.4f",
                        len(similarities), s['mean_similarity'], s['std_similarity'],
                        s['median_similarity'], s['min_similarity'], s['max_similarity']
                    )
                    logger.info("")

                return result
            else:
                return {"success": False, "error": "无法计算相似度"}

        except Exception as e:
            logger.error(" 相似度分布统计失败: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

    def _diagnose_collections(self) -> None:
        """
        诊断数据库中的所有集合，检查是否有不同维度的向量
        """
        try:
            import chromadb
            chroma_client = chromadb.PersistentClient(path=str(self.db_path))
            all_collections = chroma_client.list_collections()

            if self.verbose:
                if not all_collections:
                    logger.info("（无集合）")
                    return

                for collection in all_collections:
                    try:
                        count = collection.count()
                        if count > 0:
                            sample = collection.get(limit=1, include=["embeddings"])
                            embeddings = sample.get("embeddings") if sample else None
                            if embeddings is not None and len(embeddings) > 0:
                                first_embedding = embeddings[0]
                                dim = len(first_embedding) if hasattr(first_embedding, "__len__") else "—"
                                logger.info("  · %s: 文档数=%s, 向量维度=%s", collection.name, count, dim)
                            else:
                                logger.info("  · %s: 文档数=%s, 向量维度=—", collection.name, count)
                        else:
                            logger.info("  · %s: 文档数=0", collection.name)
                    except Exception as e:
                        logger.warning("  检查集合 %s 时出错: %s", collection.name, e)
                logger.info("")
        except Exception as e:
            if self.verbose:
                logger.warning(" 诊断集合时出错: %s", e)

    def run_build_vector_pipeline(
        self,
        prepared_rag_data: DataPreparationResult,
        rebuild: bool = False,
        resume: bool = False,
        incremental: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        运行完整构建流程

        Args:
            prepared_rag_data: 已准备好的数据（包含 documents, parent_chunks, child_chunks 等）
            rebuild: 是否重建（清空库与进度后全量重写）
            resume: 是否断点续传（仅处理未完成批次，与 rebuild 互斥时以 rebuild 为准）
            incremental: 增量模式配置 {"delete_sources":[...], "only_sources":[...]}；
                非空时只重建变更文件并先删旧向量（与 rebuild/resume 互斥，优先级最高）

        Returns:
            包含 builds 和 similarity_distribution 的结果字典
        """
        if self.verbose:
            logger.info("")
            logger.info(_LOG_SEP_MAIN)
            logger.info("【2】流程开始 · " + ("增量向量化构建" if incremental else "完整向量化构建"))
            logger.info(_LOG_SEP_MAIN)

        results = {}

        # 诊断数据库集合（构建前状态）
        if self.verbose:
            logger.info("")
            logger.info(_LOG_SEP_SUB)
            logger.info("诊断 · 数据库集合（构建前）")
            logger.info(_LOG_SEP_SUB)
            self._diagnose_collections()

        # 2. 向量存储构建（支持断点续传 / 增量）
        build_result = self.build_vector_store(
            prepared_rag_data=prepared_rag_data,
            rebuild=rebuild,
            resume=resume,
            incremental=incremental,
        )
        results["builds"] = build_result

        if not build_result.get("success"):
            logger.error("向量存储构建失败，停止构建")
            return results

        # 相似度分布统计
        similarity_result = self.get_similarity_distribution(sample_size=50)
        results["similarity_distribution"] = similarity_result

        if self.verbose:
            logger.info("")
            logger.info(_LOG_SEP_MAIN)
            logger.info("【5】流程结束 · 完整构建已完成")
            logger.info(_LOG_SEP_MAIN)
            logger.info("")

        return results

