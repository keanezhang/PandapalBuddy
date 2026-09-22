#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BM25 分词检索存储模块

负责管理 BM25 索引，提供基于分词的检索。
- 索引构建：仅使用子文档 metadata 中的 segmented_words（CDM_SEGMENTED_WORDS）构建 token 语料，不使用 keywords。
- 检索：仅使用 query_analysis 中的 segmented_words（QA_SEGMENTED_WORDS）作为 query token 序列，不使用 keywords。
BM25 与向量检索独立；索引从 VectorStore 的 children 集合读取 segmented_words 构建。
"""

from __future__ import annotations

import os
from typing import List, Dict, Any, Optional, Tuple, Protocol, runtime_checkable, TYPE_CHECKING
from pathlib import Path
import gzip
import hashlib
import hmac
import pickle
from datetime import datetime, timezone
import sys

from ...schema import SchemaKeys
import logging

from ...utils.text_utils import generate_chunk_title, normalize_list_field

if TYPE_CHECKING:
    from rank_bm25 import BM25Okapi as _BM25OkapiType

logger = logging.getLogger(__name__)

# 检查BM25是否可用
try:
    from rank_bm25 import BM25Okapi

    BM25_AVAILABLE = True
except ImportError:
    BM25_AVAILABLE = False
    BM25Okapi = None

# HMAC 签名默认密钥：优先读环境变量，否则用 SDK 内置常量
_DEFAULT_CACHE_HMAC_KEY = os.getenv("BM25_CACHE_HMAC_KEY", "").encode() or b"bm25_store_cache_integrity_v1"

# BM25 检索最大返回数量
MAX_BM25_RESULTS = 1000


@runtime_checkable
class BM25VectorStoreProvider(Protocol):
    """BM25Store 所依赖的向量存储协议，解耦对 VectorStore 的硬依赖。"""
    db_path: Any
    collection_name: str
    children_collection_name: str

    def get_collection(self, collection_type: str) -> Any: ...
    def count(self, collection_type: str) -> int: ...


class BM25Store:
    """
    BM25分词搜索存储管理器
    
    从VectorStore读取文档分词结果，构建BM25索引，提供分词搜索功能。
    BM25搜索基于词频和逆文档频率，不涉及向量计算。
    """

    def __init__(
        self,
        vector_store: BM25VectorStoreProvider,
        verbose: bool = True,
        *,
        default_query_results: int = 15,
        cache_hmac_key: Optional[bytes] = None,
    ):
        """
        初始化 BM25 存储管理器。

        Args:
            vector_store: 符合 BM25VectorStoreProvider 协议的向量存储实例。
            verbose: 是否输出详细日志。
            default_query_results: 默认返回结果数量。
            cache_hmac_key: HMAC 签名密钥（用于缓存完整性校验）。
                不传则使用 SDK 内置默认密钥。
        """
        self.vector_store = vector_store
        self.verbose = verbose
        self._default_query_results = default_query_results
        self._cache_hmac_key = cache_hmac_key or _DEFAULT_CACHE_HMAC_KEY

        # BM25索引（内存对象）
        self._bm25_index: Optional["_BM25OkapiType"] = None
        self._bm25_tokenized_corpus: List[List[str]] = []  # 分词后的文档列表
        self._bm25_doc_to_chunk_id: Dict[int, str] = {}  # BM25文档索引到chunk_id的映射

        # 记录当前索引使用的分词器信息（用于缓存校验）
        self._tokenizer_name: Optional[str] = None  # "llm_segmented_words_v1"
        self._tokenizer_version: Optional[str] = None  # 预留（当前固定为 None）

        # 初始化时检查一次，后续方法只读此属性，避免重复判断
        self._bm25_available: bool = BM25_AVAILABLE

        if self.verbose:
            logger.info("BM25Store 初始化完成")
            if self._bm25_available:
                logger.info("BM25算法: 可用")
            else:
                logger.warning("BM25算法: 不可用（rank-bm25未安装）")

    def build_index(self, save_cache: bool = True, cache_path: Optional[Path] = None) -> None:
        """
        构建 BM25 索引（从 VectorStore 子文档 metadata 读取 segmented_words 构建）。

        仅使用 metadata[CDM_SEGMENTED_WORDS] 作为文档 token 序列，不使用 keywords。
        """
        if not self._bm25_available:
            if self.verbose:
                logger.warning("BM25不可用，跳过索引构建")
            return

        # 确保VectorStore已初始化并获取集合
        children_collection = self.vector_store.get_collection("children")

        try:
            # 从VectorStore获取所有子文档（只读取文本，不读取向量）
            # 注意：ids 会自动返回，不需要在 include 中指定
            all_docs = children_collection.get(
                include=["documents", "metadatas"]
            )

            if not all_docs or not all_docs.get('documents'):
                if self.verbose:
                    logger.warning("没有子文档，无法构建BM25索引")
                return

            documents = all_docs["documents"]
            ids = all_docs.get("ids", [])
            metadatas = all_docs.get("metadatas", []) or []

            # 新方案：BM25 使用索引阶段写入的 LLM 分词结果作为 token
            self._tokenizer_name = "llm_segmented_words_v1"
            self._tokenizer_version = None

            # token 处理（从 metadata['segmented_words'] 读取）
            tokenized_corpus = []
            doc_to_chunk_id = {}

            for idx, doc in enumerate(documents):
                chunk_id = ids[idx] if idx < len(ids) else f"child_{idx}"
                metadata = metadatas[idx] if idx < len(metadatas) else {}
                raw_segmented_words = metadata.get(SchemaKeys.CDM_SEGMENTED_WORDS) if isinstance(metadata, dict) else None

                # 使用统一的列表标准化工具函数
                tokens = normalize_list_field(raw_segmented_words, verbose=False, field_name="segmented_words")

                if tokens:  # 只添加非空文档
                    tokenized_corpus.append(tokens)
                    doc_to_chunk_id[len(tokenized_corpus) - 1] = chunk_id

            if not tokenized_corpus:
                if self.verbose:
                    logger.warning("分词后没有有效文档，无法构建BM25索引")
                return

            # 构建BM25索引
            self._bm25_index = BM25Okapi(tokenized_corpus)
            self._bm25_tokenized_corpus = tokenized_corpus
            self._bm25_doc_to_chunk_id = doc_to_chunk_id

            if self.verbose:
                logger.info("BM25索引构建完成: %s 个文档", len(tokenized_corpus))

            # 方案B：默认在构建成功后落盘保存分词语料缓存，便于后续复用
            if save_cache:
                saved = self.save_token_cache(cache_path=cache_path)
                if not saved and self.verbose:
                    logger.warning("BM25索引构建成功，但缓存保存失败")

        except Exception as e:
            if self.verbose:
                logger.error("BM25索引构建失败: %s: %s", type(e).__name__, e)
            self._bm25_index = None
            self._bm25_tokenized_corpus = []
            self._bm25_doc_to_chunk_id = {}
            self._tokenizer_name = None
            self._tokenizer_version = None
            raise  # 重新抛出异常，让调用者知道构建失败

    def _default_cache_path(self) -> Path:
        """
        获取默认BM25分词语料缓存路径（方案B：只缓存分词结果与映射）。
        
        缓存默认放在 ChromaDB 的持久化目录下，按 collection_name 区分。
        """
        db_path = getattr(self.vector_store, "db_path", None)
        collection_name = getattr(self.vector_store, "collection_name", "rag")
        if isinstance(db_path, Path):
            base_dir = db_path
        else:
            # 兜底：放到当前工作目录（通常不会走到这里）
            base_dir = Path(".")
        return base_dir / f"{collection_name}_bm25_tokens_v1.pkl.gz"

    def _get_tokenizer_info(self) -> Tuple[str, Optional[str]]:
        """返回当前环境的分词器信息（用于缓存校验）"""
        return "llm_segmented_words_v1", None

    def save_token_cache(self, cache_path: Optional[Path] = None) -> bool:
        """
        保存BM25分词语料缓存（方案B）。
        
        仅持久化：
        - tokenized_corpus
        - doc_to_chunk_id 映射
        - 校验元信息（collection_name、children_count、分词器/版本等）
        """
        if not self._bm25_tokenized_corpus or not self._bm25_doc_to_chunk_id:
            if self.verbose:
                logger.warning("BM25分词语料为空，跳过缓存保存")
            return False

        if cache_path is None:
            cache_path = self._default_cache_path()

        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)

            # 尽量用轻量校验：children_count（不读取全文）
            try:
                children_count = self.vector_store.count("children")
            except Exception:
                children_count = None

            tokenizer_name, tokenizer_version = self._get_tokenizer_info()

            payload = {
                "schema_version": 1,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "python_version": sys.version,
                "collection_name": getattr(self.vector_store, "collection_name", None),
                "children_collection_name": getattr(self.vector_store, "children_collection_name", None),
                "children_count": children_count,
                # BM25Store 本身不做停用词过滤，这里显式记录为 none，便于未来扩展
                "stop_words": "none",
                "tokenizer": tokenizer_name,
                "tokenizer_version": tokenizer_version,
                "tokenized_corpus_len": len(self._bm25_tokenized_corpus),
                "doc_to_chunk_id_len": len(self._bm25_doc_to_chunk_id),
                "tokenized_corpus": self._bm25_tokenized_corpus,
                "doc_to_chunk_id": self._bm25_doc_to_chunk_id,
            }

            with gzip.open(cache_path, "wb") as f:
                raw_data = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
                # 写入 HMAC 签名 + 数据
                signature = hmac.new(self._cache_hmac_key, raw_data, hashlib.sha256).digest()
                f.write(signature)
                f.write(raw_data)

            if self.verbose:
                logger.info("已保存BM25分词缓存: %s（%s 条）", cache_path, len(self._bm25_tokenized_corpus))
            return True
        except (IOError, OSError) as e:
            if self.verbose:
                logger.warning("保存BM25分词缓存失败（IO错误）: %s", e)
            return False
        except Exception as e:
            if self.verbose:
                logger.error("保存BM25分词缓存失败（未预期错误）: %s: %s", type(e).__name__, e)
            raise

    def load_token_cache(self, cache_path: Optional[Path] = None, validate: bool = True) -> bool:
        """
        加载BM25分词语料缓存（方案B），并重建BM25Okapi索引。
        
        validate=True 时会校验：
        - collection_name
        - children_count
        - 分词器/版本（jieba/char）
        """
        if not self._bm25_available:
            return False

        if cache_path is None:
            cache_path = self._default_cache_path()

        if not cache_path.exists():
            return False

        try:
            with gzip.open(cache_path, "rb") as f:
                raw_data = f.read()

            # HMAC 签名校验（前32字节为 SHA-256 签名）
            if len(raw_data) <= 32:
                if self.verbose:
                    logger.warning("BM25缓存文件过小，可能已损坏")
                return False
            signature = raw_data[:32]
            data = raw_data[32:]
            expected_signature = hmac.new(self._cache_hmac_key, data, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected_signature):
                if self.verbose:
                    logger.warning("BM25缓存文件签名校验失败，可能被篡改，忽略缓存")
                return False

            payload = pickle.loads(data)

            if not isinstance(payload, dict):
                return False

            if payload.get("schema_version") != 1:
                return False

            if validate:
                expected_collection = getattr(self.vector_store, "collection_name", None)
                if expected_collection and payload.get("collection_name") != expected_collection:
                    if self.verbose:
                        logger.warning("BM25缓存collection_name不匹配，忽略缓存")
                    return False

                # children_count 校验：尽量轻量（不读取全文）
                cached_count = payload.get("children_count")
                if cached_count is not None:
                    try:
                        current_count = self.vector_store.count("children")
                    except Exception:
                        current_count = None
                    if current_count is not None and current_count != cached_count:
                        if self.verbose:
                            logger.warning("BM25缓存children_count不匹配，忽略缓存")
                        return False

                cached_tokenizer = payload.get("tokenizer")
                cached_tokenizer_version = payload.get("tokenizer_version")
                current_tokenizer, current_tokenizer_version = self._get_tokenizer_info()
                if cached_tokenizer and cached_tokenizer != current_tokenizer:
                    if self.verbose:
                        logger.warning("BM25缓存分词器不匹配，忽略缓存")
                    return False
                # 版本不一致时也认为不匹配（避免分词边界变化导致结果漂移）
                # 新方案下 tokenizer_version 固定为 None；该分支主要用于拒绝旧缓存
                if cached_tokenizer_version != current_tokenizer_version:
                    if self.verbose:
                        logger.warning("BM25缓存tokenizer_version不匹配，忽略缓存")
                    return False

            tokenized_corpus = payload.get("tokenized_corpus")
            doc_to_chunk_id = payload.get("doc_to_chunk_id")
            if not isinstance(tokenized_corpus, list) or not isinstance(doc_to_chunk_id, dict):
                return False
            
            # 校验语料不为空
            if not tokenized_corpus:
                if self.verbose:
                    logger.warning("BM25缓存为空，忽略缓存")
                return False

            # 重建BM25索引（方案B：不持久化BM25Okapi对象本身）
            self._bm25_index = BM25Okapi(tokenized_corpus)
            self._bm25_tokenized_corpus = tokenized_corpus
            # 确保 key 是 int
            self._bm25_doc_to_chunk_id = {int(k): v for k, v in doc_to_chunk_id.items()}

            self._tokenizer_name = payload.get("tokenizer")
            self._tokenizer_version = payload.get("tokenizer_version")

            if self.verbose:
                logger.info("已加载BM25分词缓存并重建索引: %s（%s 条）", cache_path, len(tokenized_corpus))
            return True
        except (IOError, OSError) as e:
            if self.verbose:
                logger.warning("加载BM25分词缓存失败（IO错误）: %s", e)
            return False
        except Exception as e:
            if self.verbose:
                logger.error("加载BM25分词缓存失败（未预期错误）: %s: %s", type(e).__name__, e)
            raise

    def rebuild_or_load_index_from_cache(self, cache_path: Optional[Path] = None, rebuild: bool = False) -> None:
        """
        确保BM25索引可用：
        - 优先从分词缓存加载（方案B）
        - 失败则从Chroma全文构建
        - 构建成功后写回缓存
        """
        if not self._bm25_available:
            return

        # 如果重建
        if rebuild:
            # build_index 默认会保存缓存（方案B），build_index 内部已输出日志，这里不需要重复输出
            self.build_index(save_cache=True, cache_path=cache_path)
            return

        # 如果索引已构建，直接返回，一般这种情况是内存构建的
        if self._bm25_index is not None:
            if self.verbose:
                logger.info("BM25索引已存在，直接使用: %s 个文档", len(self._bm25_tokenized_corpus))
            return

        # 加载磁盘中的缓存，如果缓存存在，则直接返回，一般这种情况是磁盘构建的
        loaded = self.load_token_cache(cache_path=cache_path, validate=True)
        if loaded:
            return

        # 内存中、磁盘中都不存在，则重新构建
        # build_index 默认会保存缓存（方案B），build_index 内部已输出日志，这里不需要重复输出
        self.build_index(save_cache=True, cache_path=cache_path)

    def query_children(
        self,
        query_analysis: Dict[str, Any],
        n_results: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        使用 BM25 在子文档集合中检索。

        仅使用 query_analysis[QA_SEGMENTED_WORDS] 作为 query token 序列，不使用 keywords。

        Args:
            query_analysis: 查询分析结果（必需，含 segmented_words）
            n_results: 返回结果数量（None 时用配置默认值）

        Returns:
            与 VectorStore.query_children 格式一致：documents、metadatas、distances、ids
        """
        if not self._bm25_available:
            if self.verbose:
                logger.warning("BM25不可用，返回空结果")
            return self._empty_result()

        # 如果索引不存在，直接报错
        if self._bm25_index is None:
            # 直接报错
            raise ValueError(
                "BM25索引不存在：请先调用 build_index() 或 rebuild_or_load_index_from_cache()（方案B：优先加载缓存，失败再构建）")

        if n_results is None:
            n_results = self._default_query_results
        
        # 限制最大返回数量，避免内存问题
        if n_results > MAX_BM25_RESULTS:
            if self.verbose:
                logger.warning("请求的结果数量(%s)超过最大限制(%s)，已限制为%s", n_results, MAX_BM25_RESULTS, MAX_BM25_RESULTS)
            n_results = MAX_BM25_RESULTS

        # BM25 仅使用 query_analysis[QA_SEGMENTED_WORDS] 作为检索 token
        query_tokens = query_analysis.get(SchemaKeys.QA_SEGMENTED_WORDS, [])
        if not isinstance(query_tokens, list):
            query_tokens = []
        if self.verbose and query_tokens:
            logger.info("BM25 本次检索使用的 token 序列(segmented_words): %s", query_tokens)

        if not query_tokens:
            if self.verbose:
                logger.warning("查询文本分词后为空，返回空结果")
            return self._empty_result()

        # BM25搜索
        scores = self._bm25_index.get_scores(query_tokens)

        # 获取Top N结果
        top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:n_results]

        # 从VectorStore获取对应的文档（只读取需要的chunk_id，避免读取所有文档）
        children_collection = self.vector_store.get_collection("children")

        # 收集需要的chunk_id
        needed_chunk_ids = []
        bm25_idx_to_chunk_id = {}
        for bm25_idx in top_indices:
            chunk_id = self._bm25_doc_to_chunk_id.get(bm25_idx)
            if chunk_id:
                needed_chunk_ids.append(chunk_id)
                bm25_idx_to_chunk_id[bm25_idx] = chunk_id

        if not needed_chunk_ids:
            return self._empty_result()

        # 只获取需要的文档（按chunk_id获取，避免读取所有文档）
        # 注意：ids 会自动返回，不需要在 include 中指定
        all_docs = children_collection.get(
            ids=needed_chunk_ids,
            include=["documents", "metadatas"]
        )

        if not all_docs or not all_docs.get('documents'):
            return self._empty_result()

        documents = all_docs['documents']
        metadatas = all_docs.get('metadatas', [])
        ids = all_docs.get('ids', [])

        # 构建chunk_id到索引的映射（只针对返回的文档）
        chunk_id_to_idx = {chunk_id: idx for idx, chunk_id in enumerate(ids)}

        # 收集结果（保持Top N的顺序）
        result_documents = []
        result_metadatas = []
        result_distances = []
        result_ids = []

        for bm25_idx in top_indices:
            chunk_id = bm25_idx_to_chunk_id.get(bm25_idx)
            if chunk_id and chunk_id in chunk_id_to_idx:
                doc_idx = chunk_id_to_idx[chunk_id]
                # 加强边界检查，避免索引越界
                if doc_idx >= len(documents):
                    continue
                document = documents[doc_idx]
                metadata = metadatas[doc_idx] if doc_idx < len(metadatas) else {}

                # 使用公共工具函数生成标题
                generated_title = generate_chunk_title(metadata, document, max_preview=15)

                # 更新 metadata 中的 title
                metadata = metadata.copy()  # 避免修改原始 metadata
                metadata['title'] = generated_title

                result_documents.append(document)
                result_metadatas.append(metadata)
                # BM25分数转换为距离（分数越高，距离越小）
                # 使用 1 / (1 + score) 作为距离，确保分数越高距离越小
                bm25_score = scores[bm25_idx]
                distance = 1.0 / (1.0 + max(bm25_score, 0.0))
                result_distances.append(distance)
                result_ids.append(chunk_id)

        return {
            'documents': [result_documents],
            'metadatas': [result_metadatas],
            'distances': [result_distances],
            'ids': [result_ids]
        }

    @staticmethod
    def _empty_result() -> Dict[str, Any]:
        """返回空的 BM25 检索结果（与 VectorStore.query_children 格式一致）。"""
        return {
            'documents': [[]],
            'metadatas': [[]],
            'distances': [[]],
            'ids': [[]]
        }

    def is_index_built(self) -> bool:
        """
        检查BM25索引是否已构建
        
        Returns:
            如果索引已构建返回True，否则返回False
        """
        return self._bm25_index is not None

    def get_cache_path(self) -> Path:
        """获取默认BM25分词语料缓存路径（公开接口）。"""
        return self._default_cache_path()

    def get_document_count(self) -> int:
        """获取已索引的文档数量（公开接口）。"""
        return len(self._bm25_tokenized_corpus)

    def clear_index(self) -> None:
        """
        清理BM25索引，释放内存
        
        用于显式释放索引占用的内存资源，适用于：
        - 切换不同的数据集
        - 内存不足时的清理操作
        - 程序退出前的资源释放
        """
        self._bm25_index = None
        self._bm25_tokenized_corpus.clear()
        self._bm25_doc_to_chunk_id.clear()
        self._tokenizer_name = None
        self._tokenizer_version = None
        
        if self.verbose:
            logger.info("已清理BM25索引，释放内存")

    def __enter__(self) -> 'BM25Store':
        """上下文管理器入口"""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """上下文管理器退出，自动清理资源"""
        self.clear_index()

