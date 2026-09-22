#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文档分割模块 - 父子文档索引版本

负责将文档分割成父子文档结构：
- 父文档（Parent Chunk）：章节或场景级别的完整叙事单元
- 子文档（Child Chunk）：在父文档内部进行细粒度分割，用于精确检索
"""

import logging
import re
from typing import List, Any, Dict, Optional, Tuple
from pathlib import Path

from ...utils import estimate_tokens

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 延迟导入 langchain 相关依赖
# ---------------------------------------------------------------------------
_RecursiveCharacterTextSplitter = None
_Document = None


def _ensure_text_splitter():
    """延迟导入 RecursiveCharacterTextSplitter。"""
    global _RecursiveCharacterTextSplitter
    if _RecursiveCharacterTextSplitter is not None:
        return _RecursiveCharacterTextSplitter
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter  # type: ignore[import]
        _RecursiveCharacterTextSplitter = RecursiveCharacterTextSplitter
    except ImportError:
        raise ImportError(
            "langchain-text-splitters is required for DocumentSplitter. "
            "Install with: pip install langchain-text-splitters"
        )
    return _RecursiveCharacterTextSplitter


def _ensure_document():
    """延迟导入 Document。"""
    global _Document
    if _Document is not None:
        return _Document
    try:
        from langchain_core.documents import Document  # type: ignore[import]
        _Document = Document
    except ImportError:
        raise ImportError(
            "langchain-core is required for DocumentSplitter. "
            "Install with: pip install langchain-core"
        )
    return _Document

# 默认值（build 脚本可传入 RAGConfig 对应字段，此处不读 agent.config）
_DEFAULT_CHUNK_SIZE = 800
_DEFAULT_CHUNK_OVERLAP_RATIO = 0.08
_DEFAULT_CHUNK_OVERLAP_MIN = 20
_DEFAULT_ENABLE_CHAPTER_SPLIT = True
_DEFAULT_PARENT_MAX_TOKENS = 5000


class DocumentSplitter:
    """
    文档分割器 - 父子文档索引版本
    
    实现三级分割策略：
    1. 章节分割（Chapter Split）
    2. 场景分割（Scene Split，按 `————` 分隔）
    3. 子文档分割（Child Split，递归分割父文档）
    """

    def __init__(
        self,
        chunk_size: Optional[int] = None,
        chunk_overlap_ratio: Optional[float] = None,
        chunk_overlap_min: Optional[int] = None,
        enable_chapter_split: Optional[bool] = None,
        parent_max_tokens: Optional[int] = None,
        scene_separator: str = "————",
        chapter_pattern: Optional[str] = None,
        verbose: bool = True
    ):
        """
        初始化文档分割器
        
        Args:
            chunk_size: 子文档块大小（字符数），默认: 800
            chunk_overlap_ratio: 子文档块重叠比例（0-1之间），默认: 0.08 (8%)
            chunk_overlap_min: 子文档块重叠最小值（字符数），默认: 20
            enable_chapter_split: 是否启用章节分割，默认: True
            parent_max_tokens: 父文档（章节/场景）最大 token 数，默认: 5000
            scene_separator: 场景分隔符，默认: "————"
            chapter_pattern: 自定义章节匹配正则表达式，默认使用中文「第X章」格式
            verbose: 是否输出详细信息
        """
        if chunk_size is not None and chunk_size <= 0:
            raise ValueError(f"chunk_size must be positive, got {chunk_size}")
        self.chunk_size = chunk_size if chunk_size is not None else _DEFAULT_CHUNK_SIZE
        if self.chunk_size <= 0:
            raise ValueError(f"chunk_size must be positive, got {self.chunk_size}")
        self.chunk_overlap_ratio = chunk_overlap_ratio if chunk_overlap_ratio is not None else _DEFAULT_CHUNK_OVERLAP_RATIO
        self.chunk_overlap_min = chunk_overlap_min if chunk_overlap_min is not None else _DEFAULT_CHUNK_OVERLAP_MIN
        # 计算实际的 overlap 值（字符数），应用最小值限制
        calculated_overlap = int(self.chunk_size * self.chunk_overlap_ratio)
        self.chunk_overlap = max(calculated_overlap, self.chunk_overlap_min)
        self.enable_chapter_split = enable_chapter_split if enable_chapter_split is not None else _DEFAULT_ENABLE_CHAPTER_SPLIT
        self.parent_max_tokens = parent_max_tokens if parent_max_tokens is not None else _DEFAULT_PARENT_MAX_TOKENS
        self.scene_separator = scene_separator
        self.verbose = verbose

        # 自定义章节模式
        if chapter_pattern is not None:
            self._CHAPTER_PATTERN = re.compile(chapter_pattern, re.MULTILINE)

        if self.verbose:
            logger.info("DocumentSplitter 初始化完成（父子文档索引模式）")
            logger.info("   子文档块大小: %s 字符", self.chunk_size)
            logger.info(
                "   子文档重叠比例: %.1f%% (最小值: %s 字符)",
                self.chunk_overlap_ratio * 100, self.chunk_overlap_min)
            logger.info("   实际重叠大小: %s 字符", self.chunk_overlap)
            logger.info("   父文档最大token: %s (约 %s 字符)", self.parent_max_tokens, int(self.parent_max_tokens / 1.5))
            logger.info("   场景分隔符: '%s'", self.scene_separator)

    def split_documents(self, documents: List[Any]) -> Tuple[List[Any], List[Any]]:
        """
        分割文档为父子文档结构
        
        Args:
            documents: 原始文档列表
            
        Returns:
            (child_chunks, parent_chunks) 元组：
            - child_chunks: 子文档列表（用于向量检索）
            - parent_chunks: 父文档列表（用于上下文扩展）
        """
        if self.verbose:
            logger.info(" 开始分割文档，共 %s 个文档", len(documents))

        all_child_chunks = []
        all_parent_chunks = []

        for doc_idx, doc in enumerate(documents):
            if self.verbose:
                logger.info("\n%s", '=' * 60)
                logger.info(" 处理文档 %s/%s: %s", doc_idx + 1, len(documents), doc.metadata.get('source', 'unknown'))
                logger.info("%s", '=' * 60)

            text = doc.page_content
            base_metadata = doc.metadata.copy()

            # 检测章节模式
            chapter_pattern = self._detect_chapter_pattern(text)

            if chapter_pattern is None:
                # 未检测到章节结构，将整个文档作为父文档
                if self.verbose:
                    logger.info("未检测到章节结构，将整个文档作为父文档")

                parent_id = f"parent_doc_{doc_idx}"
                parent_doc = self._create_parent_doc(
                    parent_id=parent_id,
                    parent_type="document",
                    content=text,
                    metadata={
                        **base_metadata,
                        "title": "未命名文档",
                        "index": doc_idx + 1
                    }
                )
                all_parent_chunks.append(parent_doc)

                # 创建子文档
                child_chunks = self._split_into_child_chunks(
                    parent_id=parent_id,
                    parent_doc=parent_doc,
                    content=text,
                    base_metadata=base_metadata
                )
                all_child_chunks.extend(child_chunks)
                continue

            # 按章节分割
            chapters = self._split_by_chapter(text, chapter_pattern)

            if self.verbose:
                logger.info(" 检测到 %s 个章节", len(chapters))

            # 处理每个章节
            for chapter_idx, chapter in enumerate(chapters):
                chapter_content = chapter["content"]
                chapter_title = chapter["title"]
                chapter_tokens = estimate_tokens(chapter_content)
                chapter_chars = len(chapter_content)

                if self.verbose:
                    logger.info("\n 章节 %s: %s", chapter_idx + 1, chapter_title)
                    logger.info("     长度: %s 字符, %s tokens", chapter_chars, chapter_tokens)

                # 判断是否需要按场景分割
                scenes = self._split_by_scene(chapter_content, chapter_title, chapter_idx + 1)

                if len(scenes) == 1:
                    # 只有一个场景，章节直接作为父文档
                    scene = scenes[0]
                    parent_id = f"parent_chapter_{chapter_idx + 1}"

                    parent_doc = self._create_parent_doc(
                        parent_id=parent_id,
                        parent_type="chapter",
                        content=scene["content"],
                        metadata={
                            **base_metadata,
                            "chapter_index": chapter_idx + 1,
                            "chapter_title": chapter_title,
                            "title": chapter_title,
                            "index": chapter_idx + 1
                        }
                    )
                    all_parent_chunks.append(parent_doc)

                    # 创建子文档
                    child_chunks = self._split_into_child_chunks(
                        parent_id=parent_id,
                        parent_doc=parent_doc,
                        content=scene["content"],
                        base_metadata=base_metadata,
                        chapter_info={"index": chapter_idx + 1, "title": chapter_title}
                    )
                    all_child_chunks.extend(child_chunks)
                else:
                    # 多个场景，每个场景作为独立的父文档
                    if self.verbose:
                        logger.info("     检测到 %s 个场景", len(scenes))

                    for scene_idx, scene in enumerate(scenes):
                        scene_content = scene["content"]
                        scene_title = scene.get("title", f"场景{scene_idx + 1}")
                        scene_tokens = estimate_tokens(scene_content)
                        scene_chars = len(scene_content)

                        parent_id = f"parent_chapter_{chapter_idx + 1}_scene_{scene_idx + 1}"

                        if self.verbose:
                            logger.info("  场景 %s: %s", scene_idx + 1, scene_title)
                            logger.info("       长度: %s 字符, %s tokens", scene_chars, scene_tokens)

                        parent_doc = self._create_parent_doc(
                            parent_id=parent_id,
                            parent_type="scene",
                            content=scene_content,
                            metadata={
                                **base_metadata,
                                "chapter_index": chapter_idx + 1,
                                "chapter_title": chapter_title,
                                "scene_index": scene_idx + 1,
                                "scene_title": scene_title,
                                "title": f"{chapter_title} - {scene_title}",
                                "index": scene_idx + 1
                            }
                        )
                        all_parent_chunks.append(parent_doc)

                        # 创建子文档
                        child_chunks = self._split_into_child_chunks(
                            parent_id=parent_id,
                            parent_doc=parent_doc,
                            content=scene_content,
                            base_metadata=base_metadata,
                            chapter_info={"index": chapter_idx + 1, "title": chapter_title},
                            scene_info={"index": scene_idx + 1, "title": scene_title}
                        )
                        all_child_chunks.extend(child_chunks)

        if self.verbose:
            logger.info("\n%s", '=' * 60)
            logger.info(" 文档分割完成")
            logger.info("   父文档数量: %s", len(all_parent_chunks))
            logger.info("   子文档数量: %s", len(all_child_chunks))
            logger.info("%s\n", '=' * 60)

        return all_child_chunks, all_parent_chunks

    # 预编译章节匹配模式（类属性，避免每次调用时重复编译）
    _CHAPTER_PATTERN = re.compile(
        r'第[零一二三四五六七八九十百千万\d]+章(?:\s+[^\n]*)?',
        re.MULTILINE,
    )

    def _detect_chapter_pattern(self, text: str, sample_size: int = 50000) -> Optional[re.Pattern]:
        """
        检测文本中的章节标记模式
        
        Args:
            text: 完整文本
            sample_size: 采样大小（字符数），默认 50000
            
        Returns:
            匹配的章节模式（re.Pattern），如果未找到则返回 None
        """
        sample_text = text[:sample_size]

        # 使用合并后的单一模式：同时匹配「第X章 副标题」和「第X章」
        all_matches = self._CHAPTER_PATTERN.findall(sample_text)

        # 只要找到至少1个匹配，就认为有章节结构
        if all_matches:
            if self.verbose:
                logger.info(" 检测到章节结构，共找到 %s 个章节标记", len(all_matches))
                for i, match in enumerate(all_matches[:5], 1):
                    logger.info("   匹配 %s: %s...", i, match[:50])

            return self._CHAPTER_PATTERN

        if self.verbose:
            logger.info("ℹ 未检测到章节结构（找到 0 个匹配）")
        return None

    def _split_by_chapter(self, text: str, chapter_pattern: re.Pattern) -> List[Dict[str, Any]]:
        """
        按章节分割文本
        
        Args:
            text: 完整文本
            chapter_pattern: 章节匹配模式
            
        Returns:
            章节列表，每个章节包含：
            - content: 章节内容
            - title: 章节标题
            - start_pos: 起始位置
            - end_pos: 结束位置
        """
        chapters = []
        matches = list(chapter_pattern.finditer(text))

        if not matches:
            return [{
                "content": text,
                "title": "未命名章节",
                "start_pos": 0,
                "end_pos": len(text)
            }]

        # 处理每个章节
        for i, match in enumerate(matches):
            start_pos = match.start()
            title_match = match.group().strip()

            # 尝试提取完整的章节标题（包括副标题）
            match_end = match.end()
            if match_end < len(text):
                # 查找标题行的结束位置
                remaining_text = text[match_end:]
                next_line_end = remaining_text.find('\n')
                if next_line_end > 0:
                    title_suffix = remaining_text[:next_line_end].strip()
                    if title_suffix and not title_suffix.startswith('—'):
                        # 合并标题
                        full_title = title_match + " " + title_suffix
                    else:
                        full_title = title_match
                else:
                    full_title = title_match
            else:
                full_title = title_match

            # 确定章节结束位置
            if i + 1 < len(matches):
                end_pos = matches[i + 1].start()
            else:
                end_pos = len(text)

            # 提取章节内容
            content = text[start_pos:end_pos].strip()

            if content:
                chapters.append({
                    "content": content,
                    "title": full_title,
                    "start_pos": start_pos,
                    "end_pos": end_pos
                })

        return chapters

    def _split_by_scene(self, text: str, chapter_title: str, chapter_index: int) -> List[Dict[str, Any]]:
        """
        按场景分割文本（按 `————` 分隔符）
        
        Args:
            text: 章节内容
            chapter_title: 章节标题
            chapter_index: 章节索引
            
        Returns:
            场景列表，每个场景包含：
            - content: 场景内容
            - title: 场景标题（通常是场景第一句话或地点）
            - start_pos: 起始位置
            - end_pos: 结束位置
        """
        # 查找所有场景分隔符（使用构造参数中的 scene_separator）
        escaped_sep = re.escape(self.scene_separator)
        scene_separator_pattern = re.compile(f'^{escaped_sep}', re.MULTILINE)
        separators = list(scene_separator_pattern.finditer(text))

        if not separators:
            # 没有场景分隔符，整个章节作为一个场景
            # 尝试提取场景标题（第一句话或第一行）
            first_line = text.split('\n')[0].strip()[:50]
            scene_title = first_line if first_line else "场景1"
            return [{
                "content": text,
                "title": scene_title,
                "start_pos": 0,
                "end_pos": len(text)
            }]

        scenes = []

        # 第一个场景：从章节开始到第一个分隔符
        first_separator = separators[0]
        first_scene_content = text[:first_separator.start()].strip()
        if first_scene_content:
            first_line = first_scene_content.split('\n')[0].strip()[:50]
            scene_title = first_line if first_line else f"场景1"
            scenes.append({
                "content": first_scene_content,
                "title": scene_title,
                "start_pos": 0,
                "end_pos": first_separator.start()
            })

        # 中间的场景：分隔符之间的内容
        for i in range(len(separators)):
            start_pos = separators[i].end()
            end_pos = separators[i + 1].start() if i + 1 < len(separators) else len(text)

            scene_content = text[start_pos:end_pos].strip()
            if scene_content:
                # 提取场景标题（第一句话或第一行）
                first_line = scene_content.split('\n')[0].strip()[:50]
                scene_title = first_line if first_line else f"场景{i + 2}"
                scenes.append({
                    "content": scene_content,
                    "title": scene_title,
                    "start_pos": start_pos,
                    "end_pos": end_pos
                })

        return scenes

    def _split_into_child_chunks(
        self,
        parent_id: str,
        parent_doc: Any,
        content: str,
        base_metadata: Dict[str, Any],
        chapter_info: Optional[Dict[str, Any]] = None,
        scene_info: Optional[Dict[str, Any]] = None
    ) -> List[Any]:
        """
        将父文档内容分割成子文档
        
        Args:
            parent_id: 父文档ID
            parent_doc: 父文档对象
            content: 父文档内容
            base_metadata: 基础元数据
            chapter_info: 章节信息
            scene_info: 场景信息
            
        Returns:
            子文档列表
        """
        content_tokens = estimate_tokens(content)
        content_chars = len(content)

        # 判断是否需要分割
        if content_tokens <= self.parent_max_tokens and content_chars <= self.chunk_size:
            # 内容足够小，直接作为单个子文档
            if self.verbose:
                logger.info("  内容足够小，直接作为单个子文档 (%s 字符, %s tokens)", content_chars, content_tokens)
            child_doc = self._create_child_doc(
                parent_id=parent_id,
                parent_doc=parent_doc,
                content=content,
                chunk_index=1,
                total_chunks=1,
                base_metadata=base_metadata,
                chapter_info=chapter_info,
                scene_info=scene_info
            )
            return [child_doc]

        # 需要递归分割，统一使用 chunk_size 和 chunk_overlap
        # 注意：如果父文档超过 parent_max_tokens，应该先按场景分割
        # 这里假设已经按场景分割过了，所以统一使用子文档参数
        split_chunk_size = self.chunk_size
        # 计算实际的 overlap：
        # 1. 使用相对值计算：chunk_size * ratio
        # 2. 应用最小值限制：max(计算值, min_value)
        # 3. 确保不超过 chunk_size：min(最终值, chunk_size - 1)
        calculated_overlap = int(split_chunk_size * self.chunk_overlap_ratio)
        split_chunk_overlap = min(max(calculated_overlap, self.chunk_overlap_min), split_chunk_size - 1)

        if self.verbose:
            logger.info(
                "  分割成子文档: chunk_size=%s, overlap=%s (重叠率: %.1f%%)",
                split_chunk_size, split_chunk_overlap, split_chunk_overlap / split_chunk_size * 100)

        # 使用递归分割器
        RecursiveCharacterTextSplitter = _ensure_text_splitter()
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=split_chunk_size,
            chunk_overlap=split_chunk_overlap,
            length_function=len,
            separators=[
                "\n\n",  # 段落分隔符
                "\n",  # 行分隔符
                "。",  # 中文句号
                "！",  # 中文感叹号
                "？",  # 中文问号
                ".",  # 英文句号
                "!",  # 英文感叹号
                "?",  # 英文问号
                "；",  # 中文分号
                ";",  # 英文分号
                "，",  # 中文逗号
                ",",  # 英文逗号
                " ",  # 空格
            ]
        )

        sub_chunks = splitter.split_text(content)

        if self.verbose:
            logger.info("  生成 %s 个子文档", len(sub_chunks))

        child_docs = []
        for idx, sub_chunk in enumerate(sub_chunks):
            child_doc = self._create_child_doc(
                parent_id=parent_id,
                parent_doc=parent_doc,
                content=sub_chunk,
                chunk_index=idx + 1,
                total_chunks=len(sub_chunks),
                base_metadata=base_metadata,
                chapter_info=chapter_info,
                scene_info=scene_info
            )
            child_docs.append(child_doc)

        return child_docs

    def _create_parent_doc(
        self,
        parent_id: str,
        parent_type: str,
        content: str,
        metadata: Dict[str, Any]
    ) -> Any:
        """
        创建父文档对象
        
        Args:
            parent_id: 父文档ID
            parent_type: 父文档类型（"chapter" 或 "scene" 或 "document"）
            content: 父文档内容
            metadata: 元数据
            
        Returns:
            父文档对象
        """
        parent_metadata = {
            **metadata,
            "chunk_type": "parent",
            "parent_id": parent_id,
            "parent_type": parent_type,
            "content_length": len(content),
            "content_tokens": estimate_tokens(content)
        }

        Document = _ensure_document()
        return Document(
            page_content=content,
            metadata=parent_metadata
        )

    def _create_child_doc(
        self,
        parent_id: str,
        parent_doc: Any,
        content: str,
        chunk_index: int,
        total_chunks: int,
        base_metadata: Dict[str, Any],
        chapter_info: Optional[Dict[str, Any]] = None,
        scene_info: Optional[Dict[str, Any]] = None
    ) -> Any:
        """
        创建子文档对象
        
        Args:
            parent_id: 父文档ID
            parent_doc: 父文档对象
            content: 子文档内容
            chunk_index: 子文档索引
            total_chunks: 总子文档数
            base_metadata: 基础元数据
            chapter_info: 章节信息
            scene_info: 场景信息
            
        Returns:
            子文档对象
        """
        child_metadata = {
            **base_metadata,
            "chunk_type": "child",
            "child_chunk_id": f"{parent_id}_child_{chunk_index - 1}",  # child_index从0开始，child_chunk_index从1开始（用于显示）
            "parent_id": parent_id,
            "parent_type": parent_doc.metadata.get("parent_type", "unknown"),
            "parent_title": parent_doc.metadata.get("title", "未知"),
            # 移除完整父文档内容存储，避免内存浪费（N个子文档会复制N次）
            # 如需访问父文档内容，可通过 parent_id 查询父文档集合
            "child_chunk_index": chunk_index,
            "total_child_chunks": total_chunks,
            "content_length": len(content),
            "content_tokens": estimate_tokens(content)
        }

        # 添加章节信息
        if chapter_info:
            child_metadata["chapter_index"] = chapter_info.get("index")
            child_metadata["chapter_title"] = chapter_info.get("title")

        # 添加场景信息
        if scene_info:
            child_metadata["scene_index"] = scene_info.get("index")
            child_metadata["scene_title"] = scene_info.get("title")

        Document = _ensure_document()
        return Document(
            page_content=content,
            metadata=child_metadata
        )
