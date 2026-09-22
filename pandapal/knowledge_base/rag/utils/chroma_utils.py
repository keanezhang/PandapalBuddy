#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Chroma 相关工具：结果格式规范化等。"""

from typing import List, Any, Dict, NamedTuple

__all__ = ["ChromaGetResult", "flatten_chroma_get_result"]


class ChromaGetResult(NamedTuple):
    """Chroma get() 结果的扁平化表示。"""
    ids: List[str]
    documents: List[str]
    metadatas: List[Dict[str, Any]]


def flatten_chroma_get_result(
    result: Dict[str, Any],
) -> ChromaGetResult:
    """将 Chroma get(ids=...) 的返回统一为 (ids, documents, metadatas) 扁平列表，兼容单层或嵌套结构。"""
    ids_raw = result.get("ids") or []
    docs_raw = result.get("documents") or []
    metas_raw = result.get("metadatas") or []
    ids_flat = ids_raw[0] if ids_raw and isinstance(ids_raw[0], list) else ids_raw
    docs_flat = docs_raw[0] if docs_raw and isinstance(docs_raw[0], list) else docs_raw
    metas_flat = metas_raw[0] if metas_raw and isinstance(metas_raw[0], list) else metas_raw
    return ChromaGetResult(ids_flat, docs_flat, metas_flat)
