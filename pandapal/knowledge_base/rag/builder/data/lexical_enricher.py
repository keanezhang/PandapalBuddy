#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LexicalEnricher：基于 base 结果，用 LLM 做 lexical 增强。

职责划分：
- DataPreparer 负责加载/解析/分块 + base 结果产出；
- LexicalEnricher 负责在 base 上做 lexical 增强，并写入 metadata，产出 full 结果。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Set, Tuple, runtime_checkable

from .chunk_position_mapper import build_chunk_position_map_batch
from .data_preparer import DataPreparer, DataPreparationResult
import logging
logger = logging.getLogger(__name__)


@runtime_checkable
class LexicalExtractorProtocol(Protocol):
    """LLM 抽取器协议，约束 lexical_extractor 的接口。"""
    async def extract_entities_and_relationships(
        self,
        text: str,
        doc_id: Optional[str] = None,
        chunk_id: Optional[str] = None,
        book_title: Optional[str] = None,
        chapter_info: Optional[Dict[str, Any]] = None,
        child_chunks: Optional[List[Dict[str, Any]]] = None,
    ) -> Any: ...


class LexicalEnricher:
    """
    在父/章级别调用 LLM，为子 chunk 生成 keywords/segmented_words 等 lexical 信息。

    约定：
    - lexical_extractor 需实现 extract_entities_and_relationships(...)；
    - 若抽取结果无 pos_tags、keyword_pos 等字段，则不写入 metadata；
    - 抽取得到的 lexical 为 dict：child_chunk_id -> lexical_info。
    """

    def __init__(self, *, verbose: bool = True):
        self.verbose = verbose

    async def enrich(
        self,
        base: DataPreparationResult,
        *,
        lexical_extractor: LexicalExtractorProtocol,
        ensure_child_positions: bool = True,
    ) -> DataPreparationResult:
        """
        在 base 上做 lexical 增强，产出 full 结果并写入 child_chunks.metadata。

        内部会将 base 中的 parent_chunks/child_chunks 转为统一 Document 形式再处理。
        """
        if lexical_extractor is None:
            raise ValueError("lexical_extractor 不能为空")

        # 将 parent/child 的 documents 转为统一结构，避免引用混用。
        parent_chunks = [DataPreparer.document_from_dict(DataPreparer.document_to_dict(d)) for d in base.parent_chunks]
        child_chunks = [DataPreparer.document_from_dict(DataPreparer.document_to_dict(d)) for d in base.child_chunks]

        # child_chunks_map 优先用 base 的；若缺失则现场构建。
        child_chunks_map = base.child_chunks_map
        if ensure_child_positions and not child_chunks_map:
            child_chunks_map = build_chunk_position_map_batch(parent_chunks=parent_chunks, child_chunks=child_chunks)

        lexical_by_chunk_id, all_entities, all_relationships = await self._generate_lexical(
            parent_chunks=parent_chunks,
            child_chunks_map=child_chunks_map or {},
            lexical_extractor=lexical_extractor,
        )
        lexical_hit = self._write_lexical_to_child_metadata(
            child_chunks=child_chunks,
            lexical_by_chunk_id=lexical_by_chunk_id,
            verbose=self.verbose,
        )

        # meta 继承 base，并标记本次为 full 构建。
        meta = dict(base.meta or {})
        meta["build_lexical"] = True

        return DataPreparationResult(
            documents=base.documents,
            parent_chunks=parent_chunks,
            child_chunks=child_chunks,
            child_chunks_map=child_chunks_map or {},
            meta=meta,
            lexical_hit=lexical_hit,
            lexical_by_chunk_id=lexical_by_chunk_id,
            entities=all_entities,
            relationships=all_relationships,
        )

    async def enrich_or_load_full(
        self,
        *,
        preparer: DataPreparer,
        base: DataPreparationResult,
        full_cache_path: Optional[Path] = None,
        rebuild: bool = False,
        validate: bool = True,
        lexical_extractor: LexicalExtractorProtocol,
        sources: Optional[Set[str]] = None,
    ) -> DataPreparationResult:
        """
        优先加载 full 缓存；若无缓存或校验失败，则在 base 上构建 full。

        Args:
            rebuild: 若为 True，会先删除已有缓存再重建。
            sources: 若提供，则跳过全量缓存读写，仅对 ``base`` 中这些 source 的
                子块做 lexical 增强（增量建库场景：base 已是"只含变更文件"的子集）。
        """
        # 增量子集增强：base 已由调用方过滤为变更文件，直接增强并返回，不碰全量缓存。
        if sources is not None:
            return await self.enrich(
                base,
                lexical_extractor=lexical_extractor,
                ensure_child_positions=True,
            )

        cache_path = full_cache_path or preparer.default_cache_path(name="prepared_data_full_v1")

        # 若 rebuild=True，先删缓存。
        if rebuild and cache_path.exists():
            try:
                cache_path.unlink()
                if self.verbose:
                    logger.info("  已删除 full 缓存: %s", cache_path)
            except Exception as e:
                if self.verbose:
                    logger.warning(" 删除缓存失败: %s", e)

        if not rebuild and cache_path.exists():
            try:
                loaded = preparer.load(cache_path, validate=validate)
                has_keywords = any(
                    (getattr(c, "metadata", {}) or {}).get("keywords")
                    for c in loaded.child_chunks
                )
                if has_keywords:
                    return loaded
                raise ValueError("full 缓存缺少 keywords，视为无效缓存，将重建")
            except Exception as e:
                if self.verbose:
                    logger.warning(" full 缓存加载或校验失败，将重建: %s", e)

        full = await self.enrich(base, lexical_extractor=lexical_extractor, ensure_child_positions=True)

        # 尽量把 base 的 max_docs 写入 full，以便 base 无缓存时 load 一致。
        max_docs = (base.meta or {}).get("max_docs")
        try:
            preparer.save(
                full,
                cache_path=cache_path,
                build_child_positions=bool(full.child_chunks_map),
                build_lexical=True,
                max_docs=max_docs,
            )
        except Exception as e:
            if self.verbose:
                logger.warning(" full 缓存保存失败，仅返回内存结果: %s", e)

        return full

    async def _generate_lexical(
        self,
        *,
        parent_chunks: List[Any],
        child_chunks_map: Dict[str, List[Dict[str, Any]]],
        lexical_extractor: LexicalExtractorProtocol,
    ) -> Tuple[Dict[str, Any], List[Any], List[Any]]:
        """
        调用 lexical_extractor 为每个父块及其子块生成 lexical 信息。

        按 parent_chunks 顺序遍历，用 child_chunks_map 取子块，再调用抽取器。

        Returns:
            (lexical_by_chunk_id, all_entities, all_relationships)
        """
        lexical_by_chunk_id: Dict[str, Any] = {}
        all_entities: List[Any] = []
        all_relationships: List[Any] = []

        total_chapters = len(parent_chunks)
        processed_count = 0

        if self.verbose:
            logger.info("\n%s", '=' * 60)
            logger.info(" 开始处理共 %s 个父/章", total_chapters)
            logger.info("%s\n", '=' * 60)

        for idx, p in enumerate(parent_chunks, 1):
            p_meta = p.metadata if hasattr(p, "metadata") else {}
            parent_id = p_meta.get("parent_id")
            if not parent_id:
                if self.verbose:
                    logger.warning(" 第 %s/%s 个父块缺少 parent_id，跳过", idx, total_chapters)
                continue

            mapped_children = child_chunks_map.get(parent_id) or []
            if not mapped_children:
                if self.verbose:
                    logger.warning(" 第 %s/%s 个父块 %s 无映射子块，跳过增强", idx, total_chapters, parent_id)
                continue

            # 从 metadata 取章节/场景信息。
            chapter_index = p_meta.get("chapter_index")
            chapter_title = p_meta.get("chapter_title") or "未命名"
            scene_index = p_meta.get("scene_index")
            scene_title = p_meta.get("scene_title")
            parent_type = p_meta.get("parent_type", "章节")

            # 拼展示用标签。
            chapter_label = ""
            if chapter_index is not None:
                chapter_label = f"第{chapter_index}章"
            if chapter_title and chapter_title != "未命名":
                chapter_label += f"《{chapter_title}》"
            if scene_index is not None:
                chapter_label += f" - 场景{scene_index}"
            if scene_title:
                chapter_label += f"《{scene_title}》"
            if not chapter_label:
                chapter_label = f"父块 {parent_id}"

            # 日志：当前父块与子块数量。
            if self.verbose:
                position_count = sum(1 for c in mapped_children if c.get('start_pos') is not None and c.get('end_pos') is not None)
                logger.info("\n [%s/%s] 正在处理: %s", idx, total_chapters, chapter_label)
                logger.info("   父块ID: %s", parent_id)
                logger.info("   类型: %s", parent_type)
                logger.info("   子块数: %s (其中 %s 个带位置)", len(mapped_children), position_count)
                logger.info("   正文长: %s 字", len(getattr(p, 'page_content', '')))

            chapter_info = {
                "chapter_index": chapter_index,
                "chapter_title": chapter_title,
                "scene_index": scene_index,
                "scene_title": scene_title,
            }

            # 调用 LLM 做实体关系与 lexical 抽取。
            if self.verbose:
                logger.info("  正在调用 lexical 抽取...")

            try:
                extraction = await lexical_extractor.extract_entities_and_relationships(
                    text=getattr(p, "page_content", ""),
                    doc_id=p_meta.get("source"),
                    chunk_id=parent_id,
                    book_title=None,
                    chapter_info=chapter_info,
                    child_chunks=mapped_children,  # 带位置的子块，用于生成 lexical
                )

                # 将 extraction.lexical（parent 级）合并进 lexical_by_chunk_id。
                # extraction.lexical 为 Dict[str, ChunkLexicalInfo]，key 为 child_chunk_id（或子块 id）；
                # 此处假定 extraction.lexical 的 key 与 child_chunk_id 一致。
                lexical_count = 0
                if extraction and getattr(extraction, "lexical", None):
                    lexical_count = len(extraction.lexical)
                    lexical_by_chunk_id.update(extraction.lexical)

                # 统计实体与关系数量。
                entity_count = 0
                relationship_count = 0
                if extraction:
                    if hasattr(extraction, "entities") and extraction.entities:
                        entity_count = len(extraction.entities)
                        all_entities.extend(extraction.entities)
                    if hasattr(extraction, "relationships") and extraction.relationships:
                        relationship_count = len(extraction.relationships)
                        all_relationships.extend(extraction.relationships)

                processed_count += 1
                # 本父块统计。
                if self.verbose:
                    logger.info("  本块: lexical=%s, 实体=%s, 关系=%s", lexical_count, entity_count, relationship_count)
                    logger.info("   进度: %s/%s 章 (%s%%)", processed_count, total_chapters, processed_count*100//total_chapters if total_chapters > 0 else 0)

            except Exception as e:
                if self.verbose:
                    logger.error("  抽取异常: %s", e, exc_info=True)
                # 单块失败不影响后续父块。
                continue

        if self.verbose:
            logger.info("\n%s", '=' * 60)
            logger.info(" lexical 增强结束")
            logger.info("   父块数: %s", total_chapters)
            logger.info("   成功数: %s", processed_count)
            logger.info("   汇总: lexical=%s, entities=%s, relationships=%s", len(lexical_by_chunk_id), len(all_entities), len(all_relationships))
            logger.info("%s\n", '=' * 60)

        return lexical_by_chunk_id, all_entities, all_relationships

    @staticmethod
    def _write_lexical_to_child_metadata(
        *,
        child_chunks: List[Any],
        lexical_by_chunk_id: Dict[str, Any],
        verbose: bool = True,
    ) -> int:
        lexical_hit = 0
        missing_lexical_chunks = []

        for c in child_chunks:
            # 将 c.metadata 转为 dict，避免 None 导致 c_meta.get 报错。
            c_meta = getattr(c, "metadata", None) or {}
            if getattr(c, "metadata", None) is None:
                c.metadata = c_meta
            # child_chunk_id 即子块 ID，如 parent_chapter_1_child_0。
            cid = c_meta.get("child_chunk_id")
            if not cid:
                if verbose:
                    logger.warning(" chunk 缺少 child_chunk_id，无法写入 lexical，跳过")
                # 无 child_chunk_id 时仍写入空列表，保证结构一致。
                c_meta["segmented_words"] = []
                c_meta["keywords"] = []
                continue

            info = lexical_by_chunk_id.get(str(cid))
            if not info:
                # 该 chunk 无对应 lexical（抽取未返回或 key 不一致），写入空并记录。
                missing_lexical_chunks.append(str(cid))
                c_meta["segmented_words"] = []
                c_meta["keywords"] = []
                continue

            def _get(field_name: str, default):
                if isinstance(info, dict):
                    return info.get(field_name, default)
                return getattr(info, field_name, default)

            c_meta["segmented_words"] = _get("segmented_words", []) or []
            c_meta["keywords"] = _get("keywords", []) or []
            # 若需写入 pos_tags、keyword_pos 等可在此扩展。
            lexical_hit += 1

        # 对缺少 lexical 的 chunk 打日志。
        if missing_lexical_chunks and verbose:
            logger.warning(
                "共 %s 个 chunk 无 lexical 信息，示例: %s%s",
                len(missing_lexical_chunks), missing_lexical_chunks[:5],
                '...' if len(missing_lexical_chunks) > 5 else ''
            )

        return lexical_hit
