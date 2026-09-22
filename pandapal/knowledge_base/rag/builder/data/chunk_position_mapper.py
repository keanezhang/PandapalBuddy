#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Chunk 位置映射。

根据父文档与子 chunk 的文本，在父文档中定位每个子 chunk 的起止位置，
供后续按 token 或字符范围裁剪等使用。

参考: agent/rag/build/data/chunk_position_mapper.py
"""

from typing import List, Dict, Any, Optional, Protocol, runtime_checkable
import logging
logger = logging.getLogger(__name__)


@runtime_checkable
class DocumentLike(Protocol):
    """文档对象协议，兼容 LangChain Document 及字典形式。"""
    page_content: str
    metadata: Dict[str, Any]


def _extract_document_content(doc: Any) -> str:
    """
    从 Document 或字典中提取正文内容的辅助函数。

    Args:
        doc: Document 或字典对象。

    Returns:
        文档正文字符串。
    """
    if hasattr(doc, 'page_content'):
        return doc.page_content
    elif isinstance(doc, dict):
        return doc.get('content', doc.get('page_content', ''))
    else:
        return str(doc)


def _extract_document_metadata(doc: Any) -> Dict[str, Any]:
    """
    从 Document 或字典中提取 metadata 的辅助函数。

    Args:
        doc: Document 或字典对象。

    Returns:
        metadata 字典。
    """
    if hasattr(doc, 'metadata'):
        return doc.metadata if doc.metadata else {}
    elif isinstance(doc, dict):
        return doc.get('metadata', {})
    else:
        return {}


def _extract_parent_id(parent_doc: Any) -> Optional[str]:
    """
    从父文档中提取 parent_id 的辅助函数。

    Args:
        parent_doc: 父文档对象。

    Returns:
        parent_id；若不存在则返回 None。
    """
    metadata = _extract_document_metadata(parent_doc)
    return metadata.get('parent_id')


def _extract_child_chunk_info(child_doc: Any) -> Optional[Dict[str, str]]:
    """
    从子文档中提取 chunk 信息的辅助函数。

    Args:
        child_doc: 子文档对象。

    Returns:
        包含 child_chunk_id 与 content 的字典；无法提取时返回 None。
    """
    content = _extract_document_content(child_doc)
    metadata = _extract_document_metadata(child_doc)

    child_chunk_id = metadata.get('child_chunk_id')
    if not child_chunk_id:
        return None

    return {
        'child_chunk_id': child_chunk_id,
        'content': content
    }


def build_chunk_position_map(
    parent_content: str,
    child_chunks: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    在父文档正文中为子 chunk 列表计算起止位置的核心函数。

    按 child_chunks 顺序在 parent_content 中依次查找每段子内容，
    使用“上次结束位置”约束，保证不重叠、顺序一致。

    Args:
        parent_content: 父文档正文。
        child_chunks: 子 chunk 信息列表，每项需包含：
            - child_chunk_id: 子块 ID，如 parent_chapter_1_child_0。
            - content: 子块正文。

    Returns:
        带位置信息的子 chunk 列表，每项包含：
            - child_chunk_id: 子块 ID。
            - start_pos: 在父文档中的起始位置（字符索引）。
            - end_pos: 在父文档中的结束位置（字符索引）。
            - content: 子块正文（用于兜底或 fallback）。
    """
    result = []
    last_end_pos = 0  # 上一个 chunk 的结束位置，用于顺序约束。

    for chunk in child_chunks:
        child_chunk_id = chunk.get('child_chunk_id')
        chunk_content = chunk.get('content', '')
        if not child_chunk_id or not chunk_content:
            continue

        start_pos = None
        end_pos = None

        # 从 last_end_pos 附近开始找，避免重复匹配到更早出现的内容。
        search_start = max(0, last_end_pos - 100)  # 允许少量回退（100 字符）。
        start_pos = parent_content.find(chunk_content, search_start)

        if start_pos >= 0:
            # 精确匹配到。
            end_pos = start_pos + len(chunk_content)
            last_end_pos = end_pos
        else:
            # 精确匹配失败时，尝试去掉首尾空白再匹配。
            logger.debug("顺序匹配失败，尝试模糊匹配: %s", chunk_content)
            chunk_content_trimmed = chunk_content.strip()
            if chunk_content_trimmed:
                start_pos = parent_content.find(chunk_content_trimmed, search_start)
                if start_pos >= 0:
                    # 使用 trim 后的内容。
                    end_pos = start_pos + len(chunk_content_trimmed)
                    last_end_pos = end_pos
                else:
                    # 仍失败则全文查找一次，作为 fallback。
                    start_pos = parent_content.find(chunk_content_trimmed)
                    if start_pos >= 0:
                        end_pos = start_pos + len(chunk_content_trimmed)
                        last_end_pos = end_pos

        # 未匹配到时 start_pos/end_pos 为 None。
        result.append({
            'child_chunk_id': child_chunk_id,
            'start_pos': start_pos,
            'end_pos': end_pos,
            'content': chunk_content  # 保留原文用于 fallback。
        })

    return result


def build_chunk_position_map_batch(
    parent_chunks: List[Any],
    child_chunks: List[Any]
) -> Dict[str, List[Dict[str, Any]]]:
    """
    批量对多组父/子 chunk 做位置映射的入口。

    根据 child_chunks 中每条记录的 parent_id 归组，
    再对每个 parent_id 调用 build_chunk_position_map，得到该父下的子块位置列表。

    Args:
        parent_chunks: 父文档列表（如 LangChain Document）。
        child_chunks: 子文档列表（如 LangChain Document），其 metadata 需含 parent_id。

    Returns:
        以 parent_chunk_id 为键的映射：{ parent_chunk_id: [child1, child2, ...] }，
        每个 child 项包含：
            - child_chunk_id: 子块 ID，如 parent_chapter_1_child_0。
            - start_pos: 在父文档中的起始位置。
            - end_pos: 在父文档中的结束位置。
            - content: 子块正文（用于兜底或 fallback）。
    """
    result = {}

    # 按 parent_id 建立父文档索引。
    parent_map = {}
    for parent_doc in parent_chunks:
        parent_id = _extract_parent_id(parent_doc)
        if parent_id:
            parent_map[parent_id] = parent_doc

    # 按 parent_id 对子 chunk 分组。
    child_chunks_map = {}
    for child_doc in child_chunks:
        metadata = _extract_document_metadata(child_doc)
        parent_id = metadata.get('parent_id')
        if not parent_id:
            continue

        if parent_id not in child_chunks_map:
            child_chunks_map[parent_id] = []
        child_chunks_map[parent_id].append(child_doc)

    # 对每个 parent_id 做位置映射。
    for parent_id, child_docs in child_chunks_map.items():
        if parent_id not in parent_map:
            # 父文档不在当前集合中，跳过。
            continue

        parent_doc = parent_map[parent_id]

        # 取父文档正文。
        parent_content = _extract_document_content(parent_doc)

        # 取子 chunk 信息列表。
        child_chunks_info = []
        for child_doc in child_docs:
            chunk_info = _extract_child_chunk_info(child_doc)
            if chunk_info:
                child_chunks_info.append(chunk_info)

        # 计算位置。
        position_mapped_chunks = build_chunk_position_map(parent_content, child_chunks_info)

        if position_mapped_chunks:
            result[parent_id] = position_mapped_chunks

    return result
