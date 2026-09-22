"""覆盖度与多样性指标"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------- numpy 延迟导入 ----------
_np = None  # type: Any


def _ensure_numpy() -> Any:
    global _np
    if _np is None:
        try:
            import numpy as np
            _np = np
        except ImportError:
            raise ImportError(
                "numpy is required for evaluation metrics. "
                "Install with: pip install numpy"
            )
    return _np


def calculate_coverage_metrics(search_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    计算覆盖度指标。

    Returns:
        包含覆盖度指标的字典：sources, unique_sources, source_diversity, chapter_coverage
    """
    sources = []
    chapters = []

    for doc in search_results:
        metadata = doc.get('parent_metadata', {})
        source = metadata.get('source', '未知来源')
        sources.append(source)

        chapter_title = metadata.get('chapter_title')
        if chapter_title:
            chapters.append(chapter_title)

    unique_sources = len(set(sources))
    source_diversity = unique_sources / len(search_results) if search_results else 0.0

    chapter_coverage: Optional[float] = None
    if chapters:
        unique_chapters = len(set(chapters))
        chapter_coverage = unique_chapters / len(chapters)

    return {
        'sources': sources,
        'unique_sources': unique_sources,
        'source_diversity': source_diversity,
        'chapter_coverage': chapter_coverage,
    }


def calculate_diversity_metrics(search_results: List[Dict[str, Any]]) -> float:
    """
    计算内容多样性指标（基于内容长度变异系数的简单代理指标）。

    Args:
        search_results: 搜索结果列表

    Returns:
        内容多样性分数
    """
    np = _ensure_numpy()

    content_lengths = []
    for doc in search_results:
        content = doc.get('parent_content', '') or doc.get('content', '')
        content_lengths.append(len(content))

    if not content_lengths:
        return 0.0

    length_std = np.std(content_lengths)
    length_mean = np.mean(content_lengths)

    if length_mean > 0:
        return float(length_std / length_mean)

    return 0.0
