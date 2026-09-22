"""重排序效果与 RRF 融合效果指标"""

import logging
from typing import Any, Dict, List, Optional

from .similarity import _distance_to_similarity

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


def calculate_rerank_metrics(search_results: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    """
    计算重排序效果指标。

    Returns:
        包含重排序指标的字典
    """
    np = _ensure_numpy()

    rerank_scores = []
    original_scores = []

    for doc in search_results:
        rerank_score = doc.get('rerank_score')
        if rerank_score is not None:
            rerank_scores.append(float(rerank_score))

        best_distance = doc.get('best_distance')
        if best_distance is not None:
            similarity = _distance_to_similarity(best_distance)
            if similarity is not None:
                original_scores.append(similarity)
        elif doc.get('relevance_score') is not None:
            original_scores.append(float(doc.get('relevance_score')))

    result: Dict[str, Optional[float]] = {
        'avg_rerank_score': None,
        'min_rerank_score': None,
        'max_rerank_score': None,
        'rerank_score_std': None,
        'rerank_improvement': None,
    }

    if rerank_scores:
        result['avg_rerank_score'] = float(np.mean(rerank_scores))
        result['min_rerank_score'] = float(np.min(rerank_scores))
        result['max_rerank_score'] = float(np.max(rerank_scores))
        result['rerank_score_std'] = float(np.std(rerank_scores))

        if original_scores and len(original_scores) == len(rerank_scores):
            avg_original = np.mean(original_scores)
            avg_rerank = np.mean(rerank_scores)
            if avg_original > 0:
                result['rerank_improvement'] = float((avg_rerank - avg_original) / avg_original)

    return result


def calculate_rrf_metrics(search_results: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    """
    计算 RRF 融合效果指标。

    Returns:
        包含 RRF 指标的字典：avg_rrf_score, rrf_score_std
    """
    np = _ensure_numpy()

    rrf_scores = []

    for doc in search_results:
        rrf_score = doc.get('rrf_score')
        if rrf_score is not None:
            rrf_scores.append(float(rrf_score))

    result: Dict[str, Optional[float]] = {
        'avg_rrf_score': None,
        'rrf_score_std': None,
    }

    if rrf_scores:
        result['avg_rrf_score'] = float(np.mean(rrf_scores))
        result['rrf_score_std'] = float(np.std(rrf_scores))

    return result
