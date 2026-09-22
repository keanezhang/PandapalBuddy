#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BM25 数据构建

提供 BM25 索引的构建能力（从 VectorStore 子文档读取 segmented_words 构建索引）。
CLI 入口见 scripts/rag/run_build_bm25.py。
"""

import logging
import time
from pathlib import Path
from typing import Dict, Any, Optional
from ...store.bm25.bm25_store import BM25Store, BM25_AVAILABLE
from ...store.vector.vector_store import VectorStore

logger = logging.getLogger(__name__)


class BM25PipelineBuild:
    """BM25 索引构建类（仅负责构建与结果展示）"""

    def __init__(
        self,
        documents_dir: Path,
        db_path: Path,
        collection_name: str,
        project_root: Path,
        verbose: bool = True
    ):
        """
        初始化构建

        Args:
            documents_dir: 文档目录路径
            db_path: 向量数据库路径（BM25 从向量存储读取文档）
            collection_name: 集合名称
            project_root: 项目根目录
            verbose: 是否输出详细信息
        """
        self.project_root = project_root
        self.documents_dir = documents_dir
        self.db_path = db_path
        self.collection_name = collection_name
        self.verbose = verbose

        self.vector_store = VectorStore(
            db_path=self.db_path,
            collection_name=self.collection_name,
            project_root=self.project_root,
            verbose=verbose
        )
        self.bm25_store = BM25Store(
            vector_store=self.vector_store,
            verbose=verbose
        )

        if self.verbose:
            logger.info("=" * 60)
            logger.info("BM25 构建初始化完成")
            logger.info("   文档目录: %s", self.documents_dir)
            logger.info("   数据库路径: %s", self.db_path)
            logger.info("   集合名称: %s", self.collection_name)
            logger.info("=" * 60)

    def build_index(self, rebuild: bool = False) -> Dict[str, Any]:
        """
        构建 BM25 索引（从 VectorStore 子文档读取 segmented_words，可写缓存）

        Returns:
            构建结果（success, document_count, elapsed_time 等）
        """
        if self.verbose:
            logger.info("\n" + "=" * 60)
            logger.info("BM25 索引构建")
            logger.info("=" * 60)

        try:
            start_time = time.time()
            self.bm25_store.rebuild_or_load_index_from_cache(rebuild=rebuild)
            elapsed_time = time.time() - start_time

            is_built = self.bm25_store.is_index_built()
            cache_path = None
            cache_exists = False
            try:
                cache_path = self.bm25_store.get_cache_path()
                cache_exists = cache_path.exists()
            except Exception as e:
                logger.debug("获取 BM25 缓存路径失败: %s", e)

            doc_count = self.bm25_store.get_document_count()

            result = {
                "success": is_built,
                "is_built": is_built,
                "document_count": doc_count,
                "elapsed_time": elapsed_time,
                "token_cache_path": str(cache_path) if cache_path else None,
                "token_cache_exists": cache_exists
            }

            if self.verbose:
                if is_built:
                    logger.info("BM25 索引构建成功: 文档数 %s, 耗时 %.2f 秒", doc_count, elapsed_time)
                else:
                    children_count = self.vector_store.count("children")
                    if children_count == 0:
                        logger.error(
                            "向量存储子文档集合为空（%s），请先运行 build_vector_pipeline.py",
                            self.vector_store.children_collection_name
                        )
                    elif not BM25_AVAILABLE:
                        logger.error("BM25 不可用，请安装: pip install rank-bm25")
                    else:
                        logger.warning("BM25 索引构建失败（未知原因）")

            return result

        except Exception as e:
            logger.error(" BM25 索引构建失败: %s", e, exc_info=True)
            raise RuntimeError(f"BM25 索引构建失败: {e}") from e

    def run_build(self, rebuild: bool = False) -> Dict[str, Any]:
        """
        执行构建流程并返回结果（仅构建，无查询测试）

        Args:
            rebuild: 是否强制重建索引

        Returns:
            包含 index_build 的构建结果
        """
        if self.verbose:
            logger.info("\n" + "=" * 80)
            logger.info("BM25 构建流程")
            logger.info("=" * 80)

        if not self.vector_store.exists():
            raise ValueError(
                " 向量存储不存在，请先运行 build_vector_pipeline.py 构建向量存储"
            )
        children_count = self.vector_store.count("children")
        if children_count == 0:
            raise ValueError(
                f" 向量存储子文档集合为空（{self.vector_store.children_collection_name}），"
                "请先运行 build_vector_pipeline.py"
            )

        index_result = self.build_index(rebuild=rebuild)
        if not index_result.get("success"):
            if not BM25_AVAILABLE:
                raise ValueError(" BM25 不可用，请安装: pip install rank-bm25")
            raise ValueError(" BM25 索引构建失败")

        if self.verbose:
            logger.info("\n" + "=" * 80)
            logger.info("BM25 构建流程完成")
            logger.info("=" * 80)

        return {"index_build": index_result}

