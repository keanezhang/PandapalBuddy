#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据准备模块（DataPreparer）

目标：把三种检索/建库流程里重复的“数据准备”步骤抽成公共组件：
1) 从目录加载原始文档（DocumentLoader）
2) 分割为父/子文档（DocumentSplitter）
3)（可选）构建 parent_id -> 子文档位置映射（build_chunk_position_map_batch）

注意：
- 这个类只负责“准备数据集（base）”与落盘，不负责任何 LLM 增强（lexical）。
- LLM 相关的 enrichment 请放到独立的 Enricher（例如 LexicalEnricher）中完成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import gzip
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from .chunk_position_mapper import build_chunk_position_map_batch
from .loader import DocumentLoader
from .splitter import DocumentSplitter

if TYPE_CHECKING:
    from langchain_core.documents import Document  # type: ignore[import]
    from ...schema import Entity, Relationship

logger = logging.getLogger(__name__)

# 默认构建参数（build 脚本传入 RAGConfig 对应字段时覆盖；此处不读 agent.config）
_DEFAULT_CHUNK_SIZE = 800
_DEFAULT_CHUNK_OVERLAP_RATIO = 0.08
_DEFAULT_CHUNK_OVERLAP_MIN = 20
_DEFAULT_ENABLE_CHAPTER_SPLIT = True
_DEFAULT_PARENT_MAX_TOKENS = 5000
_DEFAULT_MAX_FILE_SIZE_MB = 50


@dataclass
class DataPreparationResult:
    """数据准备结果"""

    documents: List[Any] = field(default_factory=list)
    parent_chunks: List[Any] = field(default_factory=list)
    child_chunks: List[Any] = field(default_factory=list)

    # {parent_chunk_id: [{"child_chunk_id":..., "start_pos":..., "end_pos":..., "content":...}, ...]}
    child_chunks_map: Optional[Dict[str, List[Dict[str, Any]]]] = None

    # 保存/加载时的元信息（manifest），用于诊断与复现
    meta: Dict[str, Any] = field(default_factory=dict)

    # lexical 写回命中统计
    lexical_hit: int = 0

    # 可选：保留 LLM 的原始 lexical（便于调试/复用）
    lexical_by_chunk_id: Dict[str, Any] = field(default_factory=dict)

    # 实体和关系（用于图谱构建）
    entities: List[Any] = field(default_factory=list)
    relationships: List[Any] = field(default_factory=list)

    # 单文件清单：{source: sha256(内容)}，source 与 chunk.metadata["source"] 同口径
    # 用于增量建库的单文件变更检测（跨构建持久化于缓存 payload）
    file_manifest: Dict[str, str] = field(default_factory=dict)


@dataclass
class ManifestDiff:
    """单文件清单差异。"""

    added: List[str] = field(default_factory=list)
    modified: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)


@dataclass
class DataDiff:
    """增量数据准备的变更结果（供向量/BM25 流水线消费）。

    - mode="full"：无法增量（缓存缺失/schema 或切分参数变更），已全量重建
    - mode="incremental"：本次只重建了增删改文件
    - changed_sources：新增∪修改（需要重新 embed + upsert 的 source 白名单）
    - deleted_sources：删除∪修改（需要按 source 清除旧向量的文件）
    """

    mode: str = "full"
    changed_sources: List[str] = field(default_factory=list)
    deleted_sources: List[str] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)

    @property
    def is_noop(self) -> bool:
        """无任何变更（增量模式下可直接跳过建库）。"""
        return self.mode == "incremental" and not self.changed_sources and not self.deleted_sources


class DataPreparer:
    """公共数据准备类（面向向量/BM25/图谱的建库前置步骤）"""

    def __init__(
        self,
        *,
        documents_dir: Path,
        project_root: Path,
        verbose: bool = True,
        max_file_size_mb: Optional[float] = None,
        cache_dir: Optional[Path] = None,
        chunk_size: Optional[int] = None,
        chunk_overlap_ratio: Optional[float] = None,
        chunk_overlap_min: Optional[int] = None,
        enable_chapter_split: Optional[bool] = None,
        parent_max_tokens: Optional[int] = None,
    ):
        self.documents_dir = documents_dir
        self.project_root = project_root
        self.verbose = verbose
        self.max_file_size_mb = max_file_size_mb or _DEFAULT_MAX_FILE_SIZE_MB
        self.chunk_size = chunk_size if chunk_size is not None else _DEFAULT_CHUNK_SIZE
        self.chunk_overlap_ratio = chunk_overlap_ratio if chunk_overlap_ratio is not None else _DEFAULT_CHUNK_OVERLAP_RATIO
        self.chunk_overlap_min = chunk_overlap_min if chunk_overlap_min is not None else _DEFAULT_CHUNK_OVERLAP_MIN
        self.enable_chapter_split = enable_chapter_split if enable_chapter_split is not None else _DEFAULT_ENABLE_CHAPTER_SPLIT
        self.parent_max_tokens = parent_max_tokens if parent_max_tokens is not None else _DEFAULT_PARENT_MAX_TOKENS
        self.cache_dir = cache_dir or (self.project_root / ".rag_cache" / "prepared_data_for_database")

    def prepare_base(
        self,
        *,
        max_docs: Optional[int] = None,
        build_child_positions: bool = False,
    ) -> DataPreparationResult:
        """
        执行“base 数据集”准备流程（不包含任何 LLM enrichment）。

        Args:
            max_docs: 限制处理的原始文档数（用于调试）
            build_child_positions: 是否构建 child_chunks_map（包含 start_pos/end_pos）
        """
        # 1) 加载文档
        loader = DocumentLoader(
            documents_dir=self.documents_dir,
            project_root=self.project_root,
            max_file_size_mb=self.max_file_size_mb,
            verbose=self.verbose,
        )
        documents = loader.load_documents()
        if not documents:
            raise ValueError("没有找到可加载的文档")

        if max_docs:
            documents = documents[:max_docs]

        # 2) 分割父/子文档
        splitter = DocumentSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap_ratio=self.chunk_overlap_ratio,
            chunk_overlap_min=self.chunk_overlap_min,
            enable_chapter_split=self.enable_chapter_split,
            parent_max_tokens=self.parent_max_tokens,
            verbose=self.verbose,
        )
        child_chunks, parent_chunks = splitter.split_documents(documents)

        result = DataPreparationResult(
            documents=documents,
            parent_chunks=parent_chunks,
            child_chunks=child_chunks,
        )

        # 3) 构建 child_chunks_map（位置映射）
        if build_child_positions:
            result.child_chunks_map = build_chunk_position_map_batch(
                parent_chunks=parent_chunks,
                child_chunks=child_chunks,
            )

        return result

    # ============================
    # 缓存（一次准备，多次复用）
    # ============================

    def default_cache_path(self, *, name: str = "prepared_data_full_v1") -> Path:
        """
        默认缓存路径（gzip JSON）。

        说明：
        - name 里建议区分 profile（例如 full/graph）和 schema version
        - 缓存目录可通过 __init__(cache_dir=...) 覆盖
        """
        safe_dir = self.cache_dir
        # 用 documents_dir 名称做一个子目录，避免不同语料互相覆盖
        corpus_name = self.documents_dir.name or "documents"
        return safe_dir / corpus_name / f"{name}.json.gz"

    def prepare_or_load_base(
        self,
        *,
        cache_path: Optional[Path] = None,
        rebuild: bool = False,
        validate: bool = True,
        max_docs: Optional[int] = None,
        build_child_positions: bool = False,
    ) -> DataPreparationResult:
        """
        优先从缓存加载 base 数据集；缓存不存在/无效时才重新准备并保存。
        
        Args:
            rebuild: 如果为 True，删除旧缓存并重新构建
        """
        cache_path = cache_path or self.default_cache_path(name="prepared_data_base_v1")

        # 如果 rebuild=True，删除旧缓存
        if rebuild and cache_path.exists():
            try:
                cache_path.unlink()
                if self.verbose:
                    logger.info("已删除旧的 base 数据集缓存: %s", cache_path)
            except Exception as e:
                if self.verbose:
                    logger.warning("删除旧缓存失败: %s", e)

        if not rebuild and cache_path.exists():
            try:
                return self.load(cache_path, validate=validate)
            except Exception as e:
                if self.verbose:
                    logger.warning("prepared data 缓存加载失败，将重建: %s", e)

        prepared = self.prepare_base(
            max_docs=max_docs,
            build_child_positions=build_child_positions,
        )
        try:
            self.save(
                prepared,
                cache_path=cache_path,
                build_child_positions=build_child_positions,
                build_lexical=False,
                max_docs=max_docs,
            )
        except Exception as e:
            if self.verbose:
                logger.warning("prepared data 缓存保存失败（不影响本次流程）: %s", e)

        return prepared

    def save(
        self,
        prepared: DataPreparationResult,
        *,
        cache_path: Path,
        build_child_positions: bool,
        build_lexical: bool,
        max_docs: Optional[int],
    ) -> None:
        """
        将 prepared data 落盘保存（gzip JSON）。
        """
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        sources_fingerprint = self._compute_sources_fingerprint()
        settings_fingerprint = self._compute_settings_fingerprint()
        file_manifest = self.compute_file_manifest()

        payload = {
            "schema_version": 2,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "documents_dir": str(self.documents_dir.resolve()),
            "project_root": str(self.project_root.resolve()),
            "max_docs": max_docs,
            "build_child_positions": bool(build_child_positions),
            "build_lexical": bool(build_lexical),
            "sources_fingerprint": sources_fingerprint,
            "settings_fingerprint": settings_fingerprint,
            "file_manifest": file_manifest,
            "counts": {
                "documents": len(prepared.documents),
                "parent_chunks": len(prepared.parent_chunks),
                "child_chunks": len(prepared.child_chunks),
                "lexical_hit": int(prepared.lexical_hit),
                "entities": len(prepared.entities),
                "relationships": len(prepared.relationships),
            },
            "documents": [self._document_to_dict(d) for d in prepared.documents],
            "parent_chunks": [self._document_to_dict(d) for d in prepared.parent_chunks],
            "child_chunks": [self._document_to_dict(d) for d in prepared.child_chunks],
            "child_chunks_map": prepared.child_chunks_map or {},
            "lexical_by_chunk_id": {
                str(cid): self._lexical_info_to_dict(info)
                for cid, info in (prepared.lexical_by_chunk_id or {}).items()
            },
            "entities": [self._entity_to_dict(e) for e in prepared.entities],
            "relationships": [self._relationship_to_dict(r) for r in prepared.relationships],
        }

        with gzip.open(cache_path, "wt", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

        if self.verbose:
            logger.info("已保存 prepared data 缓存: %s", cache_path)

    def _read_payload(self, cache_path: Path) -> Dict[str, Any]:
        """读取缓存 payload（gzip JSON），不做校验。"""
        with gzip.open(cache_path, "rt", encoding="utf-8") as f:
            return json.load(f)

    def load(self, cache_path: Path, *, validate: bool = True) -> DataPreparationResult:
        """
        从缓存加载 prepared data（gzip JSON），并重建为 Document 对象列表。
        """
        payload = self._read_payload(cache_path)

        if validate:
            self._validate_payload(payload)

        return self.payload_to_result(payload)

    def payload_to_result(self, payload: Dict[str, Any]) -> DataPreparationResult:
        """把缓存 payload 反序列化为 DataPreparationResult（不做校验）。"""
        documents = [self._document_from_dict(d) for d in payload.get("documents", [])]
        parent_chunks = [self._document_from_dict(d) for d in payload.get("parent_chunks", [])]
        child_chunks = [self._document_from_dict(d) for d in payload.get("child_chunks", [])]

        result = DataPreparationResult(
            documents=documents,
            parent_chunks=parent_chunks,
            child_chunks=child_chunks,
            child_chunks_map=payload.get("child_chunks_map") or {},
            meta={
                "schema_version": payload.get("schema_version"),
                "created_at": payload.get("created_at"),
                "documents_dir": payload.get("documents_dir"),
                "project_root": payload.get("project_root"),
                "max_docs": payload.get("max_docs"),
                "build_child_positions": payload.get("build_child_positions"),
                "build_lexical": payload.get("build_lexical"),
                "sources_fingerprint": payload.get("sources_fingerprint"),
                "settings_fingerprint": payload.get("settings_fingerprint"),
            },
            lexical_hit=int(payload.get("counts", {}).get("lexical_hit", 0) or 0),
            lexical_by_chunk_id=payload.get("lexical_by_chunk_id") or {},
            entities=[self._entity_from_dict(e) for e in payload.get("entities", [])],
            relationships=[self._relationship_from_dict(r) for r in payload.get("relationships", [])],
            file_manifest=dict(payload.get("file_manifest") or {}),
        )

        if self.verbose:
            logger.info(
                "  documents=%s, parents=%s, children=%s",
                len(result.documents), len(result.parent_chunks), len(result.child_chunks),
            )

        return result

    # ----------------------------
    # 内部：校验与序列化工具
    # ----------------------------

    def _validate_payload(self, payload: Dict[str, Any]) -> None:
        schema_version = payload.get("schema_version")
        if schema_version != 2:
            raise ValueError(f"prepared data schema_version 必须是 2，当前为: {schema_version}")

        doc_dir = payload.get("documents_dir")
        if doc_dir and str(self.documents_dir.resolve()) != str(Path(doc_dir).resolve()):
            raise ValueError("prepared data documents_dir 与当前不一致")

        # 校验 settings 指纹（chunk_size 等关键参数变化会导致切分结果不同）
        expected_settings = self._compute_settings_fingerprint()
        if payload.get("settings_fingerprint") != expected_settings:
            raise ValueError("prepared data settings_fingerprint 不匹配（可能切分参数已变更）")

        # 校验语料指纹（文件新增/修改会导致数据不一致）
        expected_sources = self._compute_sources_fingerprint()
        if payload.get("sources_fingerprint") != expected_sources:
            raise ValueError("prepared data sources_fingerprint 不匹配（语料已变更）")

    def validate_settings_only(self, payload: Dict[str, Any]) -> None:
        """
        只校验 schema / 目录 / 切分参数（不校验语料指纹）。

        增量建库需要在"语料已变更"的前提下复用旧缓存（正是要靠它算出变了哪些文件），
        因此不能走 ``_validate_payload`` 的整语料指纹校验。
        """
        if payload.get("schema_version") != 2:
            raise ValueError(
                f"prepared data schema_version 必须是 2，当前为: {payload.get('schema_version')}"
            )
        doc_dir = payload.get("documents_dir")
        if doc_dir and str(self.documents_dir.resolve()) != str(Path(doc_dir).resolve()):
            raise ValueError("prepared data documents_dir 与当前不一致")
        if payload.get("settings_fingerprint") != self._compute_settings_fingerprint():
            raise ValueError("prepared data settings_fingerprint 不匹配（切分参数已变更）")

    def _compute_settings_fingerprint(self) -> str:
        raw = {
            "rag_chunk_size": self.chunk_size,
            "rag_chunk_overlap_ratio": self.chunk_overlap_ratio,
            "rag_chunk_overlap_min": self.chunk_overlap_min,
            "rag_enable_chapter_split": self.enable_chapter_split,
            "rag_parent_max_tokens": self.parent_max_tokens,
            "rag_max_file_size_mb": self.max_file_size_mb,
        }
        data = json.dumps(raw, sort_keys=True, ensure_ascii=True).encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    def _compute_sources_fingerprint(self) -> str:
        """
        计算语料目录指纹（基于相对路径 + 文件内容 hash + size）。
        使用内容 hash 而非 mtime_ns，确保跨平台一致性。
        """
        patterns = ["*.pdf", "*.md", "*.txt", "*.docx"]
        rows: List[str] = []
        base = self.documents_dir
        if not base.exists():
            raise ValueError(f"documents_dir 不存在: {base}")

        for pat in patterns:
            for p in base.rglob(pat):
                try:
                    st = p.stat()
                    content_hash = hashlib.sha256(p.read_bytes()).hexdigest()
                except OSError:
                    continue
                rel = str(p.relative_to(base))
                rows.append(f"{rel}|{content_hash}|{st.st_size}")

        rows.sort()
        data = "\n".join(rows).encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    def _source_key(self, path: Path) -> str:
        """
        把文件路径转成与 chunk.metadata["source"] 同口径的键。

        DocumentLoader 用的是 ``str(file_path.relative_to(project_root))``，
        因此此处优先以 project_root 为基准，保证 manifest 的 key 可直接用于
        按 source 删除/过滤向量。
        """
        for base in (self.project_root, self.documents_dir):
            try:
                return str(path.relative_to(base))
            except ValueError:
                continue
        return path.name

    def compute_file_manifest(self) -> Dict[str, str]:
        """
        计算单文件清单：``{source: sha256(文件内容)}``。

        与 ``_compute_sources_fingerprint``（整语料级）不同，本清单保留每个文件的
        独立哈希，供增量建库做"新增/修改/删除"的精确判定。
        """
        base = self.documents_dir
        if not base.exists():
            raise ValueError(f"documents_dir 不存在: {base}")

        manifest: Dict[str, str] = {}
        for pat in ("*.pdf", "*.md", "*.txt", "*.docx"):
            for p in base.rglob(pat):
                if not p.is_file():
                    continue
                try:
                    content_hash = hashlib.sha256(p.read_bytes()).hexdigest()
                except OSError:
                    continue
                manifest[self._source_key(p)] = content_hash
        return manifest

    @staticmethod
    def diff_manifest(old: Dict[str, str], new: Dict[str, str]) -> "ManifestDiff":
        """对比两份单文件清单，返回 added/modified/removed（均为 source 列表）。"""
        old = old or {}
        new = new or {}
        added = sorted(k for k in new if k not in old)
        removed = sorted(k for k in old if k not in new)
        modified = sorted(k for k in new if k in old and old[k] != new[k])
        return ManifestDiff(added=added, modified=modified, removed=removed)

    @staticmethod
    def document_to_dict(doc: Any) -> Dict[str, Any]:
        """将 Document 对象序列化为字典。"""
        if hasattr(doc, "page_content"):
            return {
                "page_content": getattr(doc, "page_content", ""),
                "metadata": getattr(doc, "metadata", {}) or {},
            }
        if isinstance(doc, dict):
            return {
                "page_content": doc.get("page_content", doc.get("content", "")) or "",
                "metadata": doc.get("metadata", {}) or {},
            }
        return {"page_content": str(doc), "metadata": {}}

    # 保留旧名称以兼容内部调用
    _document_to_dict = document_to_dict

    @staticmethod
    def document_from_dict(data: Dict[str, Any]) -> Any:
        """从字典反序列化为 Document 对象。"""
        from langchain_core.documents import Document  # type: ignore[import]

        return Document(
            page_content=data.get("page_content", "") or "",
            metadata=data.get("metadata", {}) or {},
        )

    # 保留旧名称以兼容内部调用
    _document_from_dict = document_from_dict

    @staticmethod
    def _lexical_info_to_dict(info: Any) -> Dict[str, Any]:
        if isinstance(info, dict):
            cid = info.get("child_chunk_id")
            return {
                "child_chunk_id": cid,
                "segmented_words": info.get("segmented_words", []) or [],
                "keywords": info.get("keywords", []) or [],
                # 注意：词性信息（pos_tags, keyword_pos）不再传递到向量数据库
            }
        cid = getattr(info, "child_chunk_id", None)
        return {
            "child_chunk_id": cid,
            "segmented_words": getattr(info, "segmented_words", []) or [],
            "keywords": getattr(info, "keywords", []) or [],
            # 注意：词性信息（pos_tags, keyword_pos）不再传递到向量数据库
        }

    @staticmethod
    def _entity_to_dict(entity: Any) -> Dict[str, Any]:
        """将Entity对象转换为字典"""
        if isinstance(entity, dict):
            return entity
        from ...schema import Entity
        if isinstance(entity, Entity):
            return {
                "name": entity.name,
                "aliases": entity.aliases or [],
                "label": entity.label,
                "event_type": getattr(entity, "event_type", None),
                "description": entity.description,
                "frequency": entity.frequency,
                "first_appearance": entity.first_appearance,
                "first_book_title": entity.first_book_title,
                "first_chapter_index": entity.first_chapter_index,
                "first_chapter_title": entity.first_chapter_title,
                "first_scene_index": entity.first_scene_index,
                "first_scene_title": entity.first_scene_title,
                "source_texts": entity.source_texts or [],
                "metadata": entity.metadata or {},
            }
        return {}

    @staticmethod
    def _entity_from_dict(data: Dict[str, Any]) -> Any:
        """从字典重建Entity对象"""
        from ...schema import Entity
        return Entity(
            name=data.get("name", ""),
            aliases=data.get("aliases", []) or [],
            label=data.get("label", "PER"),
            event_type=data.get("event_type"),
            description=data.get("description"),
            frequency=data.get("frequency", 1),
            first_appearance=data.get("first_appearance"),
            first_book_title=data.get("first_book_title"),
            first_chapter_index=data.get("first_chapter_index"),
            first_chapter_title=data.get("first_chapter_title"),
            first_scene_index=data.get("first_scene_index"),
            first_scene_title=data.get("first_scene_title"),
            source_texts=data.get("source_texts", []) or [],
            metadata=data.get("metadata", {}) or {},
        )

    @staticmethod
    def _relationship_to_dict(relationship: Any) -> Dict[str, Any]:
        """将Relationship对象转换为字典"""
        if isinstance(relationship, dict):
            return relationship
        from ...schema import Relationship, RelationType
        if isinstance(relationship, Relationship):
            return {
                "source": relationship.source,
                "target": relationship.target,
                "relation_type": relationship.relation_type.value if isinstance(relationship.relation_type, RelationType) else str(relationship.relation_type),
                "description": relationship.description,
                "source_text": relationship.source_text,
                "confidence": relationship.confidence,
                "source_doc": relationship.source_doc,
                "source_chunk": relationship.source_chunk,
                "book_title": relationship.book_title,
                "chapter_index": relationship.chapter_index,
                "chapter_title": relationship.chapter_title,
                "scene_index": relationship.scene_index,
                "scene_title": relationship.scene_title,
                "metadata": relationship.metadata or {},
            }
        return {}

    @staticmethod
    def _relationship_from_dict(data: Dict[str, Any]) -> Any:
        """从字典重建Relationship对象"""
        from ...schema import Relationship, RelationType
        relation_type_str = data.get("relation_type", "UNKNOWN")
        try:
            relation_type = RelationType(relation_type_str)
        except ValueError:
            relation_type = RelationType.UNKNOWN
        return Relationship(
            source=data.get("source", ""),
            target=data.get("target", ""),
            relation_type=relation_type,
            description=data.get("description"),
            source_text=data.get("source_text", ""),
            confidence=data.get("confidence", 0.8),
            source_doc=data.get("source_doc"),
            source_chunk=data.get("source_chunk"),
            book_title=data.get("book_title"),
            chapter_index=data.get("chapter_index"),
            chapter_title=data.get("chapter_title"),
            scene_index=data.get("scene_index"),
            scene_title=data.get("scene_title"),
            metadata=data.get("metadata", {}) or {},
        )
