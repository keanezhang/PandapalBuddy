#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据准备

提供统一的数据准备接口，供向量数据库、BM25数据库、图数据库共享使用。
完成 base 和 full 数据集的准备流程。
CLI 入口见 scripts/rag/run_build_data.py。
"""

from __future__ import annotations

import importlib.resources
import logging
from pathlib import Path
from typing import Optional

from ...rag_config import RAGConfig, RAGLLMProvider
from ... import instructions as _instructions_pkg  # 模块对象引用，零包名耦合
from ...instructions.instruction_loader import load_instruction
from .data_preparer import DataPreparer, DataPreparationResult, DataDiff
from .lexical_enricher import LexicalEnricher
from .entity_relationship_extractor_llm import LLMEntityRelationshipExtractor
from .loader import DocumentLoader
from .splitter import DocumentSplitter
from .chunk_position_mapper import build_chunk_position_map_batch

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# using_prompt 审计目录：运行时将最终拼合的 prompt 落盘，便于人工审查
# 注意：写操作不能写回 Python 包内部（pip install 后 site-packages 可能只读），
# 因此定位到 instructions 包的文件系统路径仅作为开发期便利，
# 生产部署建议写到 db_path 下。
# ---------------------------------------------------------------------------
_USING_PROMPT_SUBDIR = "using_prompt"

# 通用词法抽取指令（图关闭时替代小说实体关系联合抽取）
_LEXICAL_INSTRUCTION_FILENAME = "lexical_extractor_instruction.md"


def _get_instructions_dir() -> Path:
    """
    通过 importlib.resources 定位 instructions 包的文件系统路径。

    使用模块对象引用而非字符串，因此无论 SDK 包名叫 agent.rag 还是 rag_sdk
    都能正确定位。
    """
    ref = importlib.resources.files(_instructions_pkg)
    return Path(str(ref))


def _load_lexical_instruction() -> str:
    """加载通用词法抽取指令（分词 + 关键词，领域无关）。"""
    return load_instruction(_get_instructions_dir() / _LEXICAL_INSTRUCTION_FILENAME)


def _get_using_prompt_dir() -> Path:
    """获取 using_prompt 审计目录路径。"""
    return _get_instructions_dir() / _USING_PROMPT_SUBDIR


def _write_extractor_instruction_to_using_prompt(
    *,
    lexical_extractor: LLMEntityRelationshipExtractor,
    verbose: bool,
) -> None:
    """将建立数据用 instruction 写入 instructions/using_prompt，便于审计。"""
    try:
        using_prompt_dir = _get_using_prompt_dir()
        using_prompt_dir.mkdir(parents=True, exist_ok=True)
        using_path = using_prompt_dir / "entity_relationship_extractor_instruction.txt"
        using_path.write_text(lexical_extractor.get_instruction(), encoding="utf-8")
        if verbose:
            logger.info(" 已写出建立数据 instruction 到: %s", using_path)
    except Exception as e:
        if verbose:
            logger.warning(" 写出 using_prompt 建立数据 instruction 失败: %s", e)


def load_prepared_rag_data(
    *,
    documents_dir: Path,
    project_root: Path,
    collection_name: str,
    db_path: Path,
    verbose: bool = True,
    must_exist: bool = False,
) -> Optional[DataPreparationResult]:
    """
    仅从缓存读取已准备好的 RAG 数据，不执行任何准备/重建逻辑。
    供图谱、向量等 pipeline 在「只消费已有数据」时显式调用，避免依赖 prepare_rag_data(rebuild=False) 的隐式语义。

    Args:
        documents_dir: 文档目录路径（用于构造 DataPreparer，与缓存路径一致）
        project_root: 项目根目录
        collection_name: 集合名称（用于缓存路径命名）
        db_path: 数据库路径（缓存位于 db_path / "_prepared_rag_data"）
        verbose: 是否输出详细信息
        must_exist: 若为 True 且缓存不存在或无效则抛出异常；若为 False 则返回 None

    Returns:
        成功时返回 DataPreparationResult；缓存不存在或无效时返回 None（must_exist=False）或抛异常（must_exist=True）
    """
    preparer = DataPreparer(
        documents_dir=documents_dir,
        project_root=project_root,
        verbose=verbose,
        cache_dir=db_path / "_prepared_rag_data",
    )
    full_cache_path = preparer.default_cache_path(name=f"{collection_name}_prepared_data_full_v1")

    if not full_cache_path.exists():
        if must_exist:
            raise FileNotFoundError(f"RAG 准备数据缓存不存在，请先运行数据准备: {full_cache_path}")
        if verbose:
            logger.info("未发现已准备数据缓存，需先执行数据准备")
        return None

    try:
        loaded = preparer.load(full_cache_path, validate=True)
        has_keywords = any(
            (getattr(c, "metadata", {}) or {}).get("keywords")
            for c in loaded.child_chunks
        )
        if not has_keywords:
            if must_exist:
                raise ValueError("full 数据集缓存缺少 keywords，请重新生成数据准备缓存")
            if verbose:
                logger.warning("full 数据集缓存缺少 keywords，需重新生成")
            return None
        if verbose:
            logger.info(" 已从缓存加载准备数据: %s", full_cache_path)
            logger.info("   文档数: %s, 父: %s, 子: %s", len(loaded.documents), len(loaded.parent_chunks), len(loaded.child_chunks))
        return loaded
    except Exception as e:
        if must_exist:
            raise
        if verbose:
            logger.warning(" 加载准备数据缓存失败，将需要重新准备: %s", e)
        return None


async def build_rag_data(
    *,
    documents_dir: Path,
    project_root: Path,
    collection_name: str,
    db_path: Path,
    rebuild: bool = False,
    max_docs: Optional[int] = None,
    verbose: bool = True,
    build_config: Optional[RAGConfig] = None,
    llm_provider: Optional[RAGLLMProvider] = None,
) -> DataPreparationResult:
    """
    为数据库构建准备数据（base + full 数据集）
    
    该函数统一处理数据准备流程，供向量数据库、BM25数据库、图数据库共享使用：
    1) base 数据集：加载→分割→位置映射（可落盘复用）
    2) full 数据集：读取 base → 生成 lexical（keywords 等写回子文档 metadata）→ 落盘复用
    
    Args:
        documents_dir: 文档目录路径
        project_root: 项目根目录
        collection_name: 集合名称（用于缓存路径命名）
        db_path: 数据库路径（缓存将放在 db_path / "_prepared_data" 下）
        rebuild: 是否重建（忽略缓存）
        max_docs: 最大处理文档数（用于调试）
        verbose: 是否输出详细信息
        llm_provider: 符合 RAGLLMProvider 协议的 LLM，用于 full 数据集的实体/关键词抽取；由调用方创建并注入，无则且需生成 full 时会报错
        
    Returns:
        DataPreparationResult: 包含 documents, parent_chunks, child_chunks, child_chunks_map 等
    """
    if verbose:
        logger.info("")
        logger.info("─" * 56)
        logger.info("【数据准备】 base + full 数据集")
        logger.info("─" * 56)
    
    # 初始化 DataPreparer（用于缓存路径和加载/保存）；构建参数来自 build_config 或默认
    preparer = DataPreparer(
        documents_dir=documents_dir,
        project_root=project_root,
        verbose=verbose,
        cache_dir=db_path / "_prepared_rag_data",
        chunk_size=build_config.chunk_size if build_config else None,
        chunk_overlap_ratio=build_config.chunk_overlap_ratio if build_config else None,
        chunk_overlap_min=build_config.chunk_overlap_min if build_config else None,
        enable_chapter_split=build_config.enable_chapter_split if build_config else None,
        parent_max_tokens=build_config.parent_max_tokens if build_config else None,
    )
    base_cache_path = preparer.default_cache_path(name=f"{collection_name}_prepared_data_base_v1")
    full_cache_path = preparer.default_cache_path(name=f"{collection_name}_prepared_data_full_v1")
    
    # 优先尝试从缓存加载（与 load_prepared_rag_data 逻辑一致，避免重复实现时漂移）
    if not rebuild:
        loaded = load_prepared_rag_data(
            documents_dir=documents_dir,
            project_root=project_root,
            collection_name=collection_name,
            db_path=db_path,
            verbose=verbose,
            must_exist=False,
        )
        if loaded is not None:
            return loaded
    
    # full 缓存不存在或无效，继续处理流程
    # 1) base 数据集：加载→分割→位置映射（可落盘复用）
    # 注意：prepare_or_load_base() 内部会检查 base 缓存，如果存在会直接加载，不会重新分割
    base = preparer.prepare_or_load_base(
        cache_path=base_cache_path,
        rebuild=rebuild,
        validate=True,
        max_docs=max_docs,
        build_child_positions=True,
    )
    
    # 2) full 数据集：读取 base → 生成 lexical（keywords 等写回子文档 metadata）→ 落盘复用
    enricher = LexicalEnricher(verbose=verbose)
    
    # 只有在需要生成 full 数据集时才需要 LLM 抽取器（由调用方注入 llm_provider）
    if verbose:
        logger.info("需要生成/重新生成 full 数据集，初始化 LLM 抽取器...")
    if llm_provider is None:
        raise ValueError(
            "生成 full 数据集需要 LLM 抽取。"
            " 请以代码方式调用 build_rag_data(..., llm_provider=...) 并传入符合 RAGLLMProvider 的实例；"
            " 若在本仓库宿主环境中运行脚本，请确保已安装 agent 且 agent.config.settings 中已配置对应业务名。"
        )
    # 实体/关系抽取的唯一消费者是图谱；图关闭（且未启用三路召回）时无需抽取实体/关系，
    # 改用通用词法抽取（分词 + 关键词），避免被小说专用指令束缚在单一领域。
    need_entity_relationship = bool(
        build_config and (build_config.enable_graph or build_config.use_triple_retrieval)
    )
    if need_entity_relationship:
        lexical_extractor = LLMEntityRelationshipExtractor(llm_provider=llm_provider, verbose=verbose)
    else:
        lexical_extractor = LLMEntityRelationshipExtractor(
            llm_provider=llm_provider,
            verbose=verbose,
            instructions=_load_lexical_instruction(),
        )

    _write_extractor_instruction_to_using_prompt(
        lexical_extractor=lexical_extractor,
        verbose=verbose,
    )

    prepared_rag_data = await enricher.enrich_or_load_full(
        preparer=preparer,
        base=base,
        full_cache_path=full_cache_path,
        rebuild=rebuild,
        validate=True,
        lexical_extractor=lexical_extractor,
    )
    
    if verbose:
        logger.info("完成: 文档 %s, 父 %s, 子 %s, 实体 %s, 关系 %s, lexical命中 %s",
            len(prepared_rag_data.documents), len(prepared_rag_data.parent_chunks),
            len(prepared_rag_data.child_chunks), len(prepared_rag_data.entities),
            len(prepared_rag_data.relationships), prepared_rag_data.lexical_hit)

        # 调试输出：仅在数据量较小时打印详细内容
        if len(prepared_rag_data.child_chunks) <= 10:
            logger.debug("=" * 60 + "\n")
            logger.debug("keywords全部内容（调试模式，仅显示前10个）:")
            for chunk in prepared_rag_data.child_chunks[:10]:
                keywords = (chunk.metadata or {}).get('keywords', [])
                logger.debug("关键词: %s", keywords)
            logger.debug("=" * 60 + "\n")

            logger.debug("segmented_words全部内容（调试模式，仅显示前10个）:")
            for chunk in prepared_rag_data.child_chunks[:10]:
                segmented_words = (chunk.metadata or {}).get('segmented_words', [])
                logger.debug("分词: %s", segmented_words)
            logger.debug("=" * 60 + "\n")
    
    return prepared_rag_data


def _make_preparer(
    *,
    documents_dir: Path,
    project_root: Path,
    db_path: Path,
    verbose: bool,
    build_config: Optional[RAGConfig],
) -> DataPreparer:
    """构造 DataPreparer（增量/全量共用，避免参数漂移）。"""
    return DataPreparer(
        documents_dir=documents_dir,
        project_root=project_root,
        verbose=verbose,
        cache_dir=db_path / "_prepared_rag_data",
        chunk_size=build_config.chunk_size if build_config else None,
        chunk_overlap_ratio=build_config.chunk_overlap_ratio if build_config else None,
        chunk_overlap_min=build_config.chunk_overlap_min if build_config else None,
        enable_chapter_split=build_config.enable_chapter_split if build_config else None,
        parent_max_tokens=build_config.parent_max_tokens if build_config else None,
    )


def _build_lexical_extractor(
    *,
    llm_provider: RAGLLMProvider,
    build_config: Optional[RAGConfig],
    verbose: bool,
) -> LLMEntityRelationshipExtractor:
    """按"是否启用图谱/三路召回"选择抽取器（与 build_rag_data 保持一致）。"""
    need_entity_relationship = bool(
        build_config and (build_config.enable_graph or build_config.use_triple_retrieval)
    )
    if need_entity_relationship:
        return LLMEntityRelationshipExtractor(llm_provider=llm_provider, verbose=verbose)
    return LLMEntityRelationshipExtractor(
        llm_provider=llm_provider,
        verbose=verbose,
        instructions=_load_lexical_instruction(),
    )


def _doc_source(doc: Any) -> Optional[str]:
    """取文档/chunk 的 source 元数据。"""
    return (getattr(doc, "metadata", {}) or {}).get("source")


async def build_rag_data_incremental(
    *,
    documents_dir: Path,
    project_root: Path,
    collection_name: str,
    db_path: Path,
    verbose: bool = True,
    build_config: Optional[RAGConfig] = None,
    llm_provider: Optional[RAGLLMProvider] = None,
) -> tuple[DataPreparationResult, DataDiff]:
    """
    增量数据准备：只重建"新增/修改"的文件，复用其余文件的已有 full 缓存。

    与 ``build_rag_data`` 的区别：后者在任何语料变更下都会整库重建（慢）；
    本函数基于单文件清单（``file_manifest``）做精细 diff，只对变更文件做
    切分 + LLM 词法抽取。

    无法增量（无缓存/旧版本缓存/切分参数变更）时**自动回退全量**，
    返回的 ``DataDiff.mode == "full"``，调用方据此决定是否走全量向量重建。

    Returns:
        (DataPreparationResult, DataDiff)
    """
    preparer = _make_preparer(
        documents_dir=documents_dir,
        project_root=project_root,
        db_path=db_path,
        verbose=verbose,
        build_config=build_config,
    )
    full_cache_path = preparer.default_cache_path(
        name=f"{collection_name}_prepared_data_full_v1"
    )

    async def _fallback_full() -> tuple[DataPreparationResult, DataDiff]:
        result = await build_rag_data(
            documents_dir=documents_dir,
            project_root=project_root,
            collection_name=collection_name,
            db_path=db_path,
            rebuild=False,
            verbose=verbose,
            build_config=build_config,
            llm_provider=llm_provider,
        )
        manifest = dict(result.file_manifest or {})
        return result, DataDiff(
            mode="full",
            changed_sources=sorted(manifest.keys()),
            deleted_sources=[],
            counts={"documents": len(result.documents)},
        )

    if verbose:
        logger.info("")
        logger.info("─" * 56)
        logger.info("【数据准备·增量】仅重建变更文件")
        logger.info("─" * 56)

    # 1) 缓存与前置校验（只校验 schema/目录/切分参数，不校验语料指纹）
    if not full_cache_path.exists():
        if verbose:
            logger.info("未发现 full 缓存，回退全量构建")
        return await _fallback_full()

    try:
        payload = preparer._read_payload(full_cache_path)
        preparer.validate_settings_only(payload)
    except Exception as e:  # noqa: BLE001
        if verbose:
            logger.warning("增量前置校验失败，回退全量: %s", e)
        return await _fallback_full()

    old_manifest = dict(payload.get("file_manifest") or {})
    if not old_manifest:
        if verbose:
            logger.info("旧缓存缺少 file_manifest（旧版本），回退全量")
        return await _fallback_full()

    # 2) 单文件 diff
    new_manifest = preparer.compute_file_manifest()
    mdiff = DataPreparer.diff_manifest(old_manifest, new_manifest)

    if not (mdiff.added or mdiff.modified or mdiff.removed):
        if verbose:
            logger.info("未检测到文件变更，直接复用缓存")
        cached = preparer.payload_to_result(payload)
        return cached, DataDiff(
            mode="incremental",
            counts={"added": 0, "modified": 0, "removed": 0},
        )

    changed = sorted(set(mdiff.added) | set(mdiff.modified))
    deleted = sorted(set(mdiff.removed) | set(mdiff.modified))
    if verbose:
        logger.info(
            "变更检测：新增 %d，修改 %d，删除 %d（需重建 %d，需清理 %d）",
            len(mdiff.added), len(mdiff.modified), len(mdiff.removed),
            len(changed), len(deleted),
        )

    # 3) 复用缓存中"未变文件"的 chunks/documents，丢弃被删除/修改文件
    cached = preparer.payload_to_result(payload)
    drop = set(changed) | set(mdiff.removed)
    keep_parents = [d for d in cached.parent_chunks if _doc_source(d) not in drop]
    keep_children = [d for d in cached.child_chunks if _doc_source(d) not in drop]
    keep_documents = [d for d in cached.documents if _doc_source(d) not in drop]

    # 4) 只加载/切分/增强"变更文件"
    max_file_size_mb = getattr(build_config, "max_file_size_mb", None) if build_config else None
    loader = DocumentLoader(
        documents_dir=documents_dir,
        project_root=project_root,
        max_file_size_mb=max_file_size_mb,
        verbose=verbose,
    )
    changed_documents = loader.load_documents(only_sources=set(changed))

    new_parents: List[Any] = []
    new_children: List[Any] = []
    if changed_documents:
        splitter = DocumentSplitter(
            chunk_size=preparer.chunk_size,
            chunk_overlap_ratio=preparer.chunk_overlap_ratio,
            chunk_overlap_min=preparer.chunk_overlap_min,
            enable_chapter_split=preparer.enable_chapter_split,
            parent_max_tokens=preparer.parent_max_tokens,
            verbose=verbose,
        )
        new_children, new_parents = splitter.split_documents(changed_documents)

        if llm_provider is None:
            raise ValueError(
                "增量建库需要 LLM 抽取变更文件的关键词，但未提供 llm_provider。"
            )
        lexical_extractor = _build_lexical_extractor(
            llm_provider=llm_provider, build_config=build_config, verbose=verbose
        )
        _write_extractor_instruction_to_using_prompt(
            lexical_extractor=lexical_extractor, verbose=verbose
        )
        enricher = LexicalEnricher(verbose=verbose)
        subset = DataPreparationResult(
            documents=changed_documents,
            parent_chunks=new_parents,
            child_chunks=new_children,
        )
        # sources 非空 → 只对子集做 lexical，不读写全量缓存
        subset_full = await enricher.enrich_or_load_full(
            preparer=preparer,
            base=subset,
            lexical_extractor=lexical_extractor,
            sources=set(changed),
        )
        new_parents = subset_full.parent_chunks
        new_children = subset_full.child_chunks

    # 5) 合并并重建位置映射
    merged = DataPreparationResult(
        documents=keep_documents + changed_documents,
        parent_chunks=keep_parents + new_parents,
        child_chunks=keep_children + new_children,
        meta=dict(cached.meta or {}),
        lexical_hit=int(cached.lexical_hit),
    )
    if not merged.documents:
        raise ValueError("没有找到可加载的文档")
    merged.child_chunks_map = build_chunk_position_map_batch(
        parent_chunks=merged.parent_chunks,
        child_chunks=merged.child_chunks,
    )
    merged.meta["build_lexical"] = True

    # 6) 回写缓存（save 会重算并写入最新 file_manifest）
    try:
        preparer.save(
            merged,
            cache_path=full_cache_path,
            build_child_positions=bool(merged.child_chunks_map),
            build_lexical=True,
            max_docs=(cached.meta or {}).get("max_docs"),
        )
    except Exception as e:  # noqa: BLE001
        if verbose:
            logger.warning("增量缓存保存失败（不影响本次流程）: %s", e)

    if verbose:
        logger.info(
            "增量准备完成：文档 %s, 父 %s, 子 %s",
            len(merged.documents), len(merged.parent_chunks), len(merged.child_chunks),
        )

    return merged, DataDiff(
        mode="incremental",
        changed_sources=changed,
        deleted_sources=deleted,
        counts={
            "added": len(mdiff.added),
            "modified": len(mdiff.modified),
            "removed": len(mdiff.removed),
        },
    )

