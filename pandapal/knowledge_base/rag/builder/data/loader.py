#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文档加载模块

负责从文件系统加载各种格式的文档，支持：
- PDF (.pdf)
- Word (.docx)
- Markdown (.md)
- 文本文件 (.txt)
"""

import logging

from pathlib import Path
from typing import List, Any, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.documents import Document  # type: ignore[import]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 延迟导入 langchain 相关依赖（零泄露原则：SDK 用户不使用 loader 时不会触发 ImportError）
# ---------------------------------------------------------------------------
_langchain_community_loaders = None
_DOCX_SUPPORT: Optional[bool] = None


def _ensure_langchain_loaders():
    """延迟导入 langchain_community.document_loaders，首次调用时触发。"""
    global _langchain_community_loaders, _DOCX_SUPPORT
    if _langchain_community_loaders is not None:
        return _langchain_community_loaders
    try:
        from langchain_community import document_loaders as _mod  # type: ignore[import]
        _langchain_community_loaders = _mod
    except ImportError:
        raise ImportError(
            "langchain-community is required for DocumentLoader. "
            "Install with: pip install langchain-community"
        )
    # 检测 docx 支持
    try:
        _ = _langchain_community_loaders.Docx2txtLoader
        _DOCX_SUPPORT = True
    except AttributeError:
        _DOCX_SUPPORT = False
    return _langchain_community_loaders


def _get_docx_support() -> bool:
    """检查是否支持 .docx 加载。"""
    global _DOCX_SUPPORT
    if _DOCX_SUPPORT is None:
        _ensure_langchain_loaders()
    return _DOCX_SUPPORT
_DEFAULT_MAX_FILE_SIZE_MB = 50


class DocumentLoader:
    """
    文档加载器
    
    负责从指定目录加载各种格式的文档文件。
    """

    def __init__(
        self,
        documents_dir: Path,
        project_root: Path,
        max_file_size_mb: Optional[float] = None,
        verbose: bool = True
    ):
        """
        初始化文档加载器
        
        Args:
            documents_dir: 文档目录路径
            project_root: 项目根目录路径
            max_file_size_mb: 最大文件大小（MB），如果为 None 则从 settings 读取
            verbose: 是否输出详细信息
        """
        self.documents_dir = documents_dir
        self.project_root = project_root
        self.max_file_size_mb = max_file_size_mb if max_file_size_mb is not None else _DEFAULT_MAX_FILE_SIZE_MB
        self.verbose = verbose

    def load_documents(self, only_sources: Optional[Set[str]] = None) -> "List[Document]":
        """
        加载文档
        
        支持的文件格式：
        - PDF (.pdf) - 使用 PyPDFLoader
        - Word (.docx) - 使用 Docx2txtLoader（需要安装 docx2txt）
        - Markdown (.md) - 使用 TextLoader
        - 文本文件 (.txt) - 使用 TextLoader
        
        Returns:
            文档列表
        """
        loaders = _ensure_langchain_loaders()
        documents = []

        # 文件大小限制（转换为字节）
        max_file_size = self.max_file_size_mb * 1024 * 1024

        # 遍历所有支持的文件类型
        file_patterns = ["*.pdf", "*.md", "*.txt"]

        # 如果支持 Word 文档，添加 .docx 模式
        if _get_docx_support():
            file_patterns.append("*.docx")
        else:
            # 检查是否有 .docx 文件，如果有则提示
            docx_files = list(self.documents_dir.rglob("*.docx"))
            if docx_files:
                logger.warning(
                    "检测到 %s 个 .docx 文件，但未安装 docx2txt 库，将跳过。"
                    "安装方法: pip install docx2txt",
                    len(docx_files),
                )

        for pattern in file_patterns:
            for file_path in self.documents_dir.rglob(pattern):
                # 增量建库：只加载命中的变更文件，避免全量解析
                if only_sources is not None:
                    try:
                        rel_source = str(file_path.relative_to(self.project_root))
                    except ValueError:
                        rel_source = file_path.name
                    if rel_source not in only_sources:
                        continue
                try:
                    # 检查文件大小
                    file_size = file_path.stat().st_size
                    if file_size > max_file_size:
                        file_size_mb = file_size / (1024 * 1024)
                        max_size_mb = self.max_file_size_mb
                        logger.warning(
                            "跳过大文件 %s (%s MB > %s MB限制)",
                            file_path.name, f"{file_size_mb:.1f}", f"{max_size_mb:.0f}",
                        )
                        logger.info(
                            "提示: 可以通过构造参数 max_file_size_mb 来调整文件大小限制"
                        )
                        continue

                    # 根据文件扩展名选择正确的加载器
                    suffix = file_path.suffix.lower()
                    if suffix == '.pdf':
                        loader = loaders.PyPDFLoader(str(file_path))
                    elif suffix == '.docx':
                        if not _get_docx_support():
                            continue
                        loader = loaders.Docx2txtLoader(str(file_path))
                    else:
                        # .md 和 .txt 使用 TextLoader
                        loader = loaders.TextLoader(str(file_path), encoding='utf-8')

                    docs = loader.load()
                    # 添加文件路径作为元数据
                    for doc in docs:
                        doc.metadata['source'] = str(file_path.relative_to(self.project_root))
                    documents.extend(docs)

                    if self.verbose:
                        logger.info("加载文档: %s (%s 页)", file_path.name, len(docs))

                except (IOError, ValueError) as e:
                    logger.warning("加载失败 %s: %s", file_path.name, e)

        if self.verbose:
            logger.info("文档加载完成，共加载 %s 个文档", len(documents))

        return documents
