"""相似度计算指标"""

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


def _distance_to_similarity(distance: Optional[float]) -> Optional[float]:
    """
    将距离转换为相似度分数，统一到 [0, 1]，与重排序分数可比。

    Args:
        distance: 向量/融合距离，可为 None。支持 Chroma L2（≥0）、RRF 输出的 -rrf（<0）。

    Returns:
        相似度分数（严格 [0, 1]），若 distance 为 None 则返回 None。

    转换公式（保证输出 [0, 1]）：
    - distance < 0（如 -rrf）：similarity = 1 / (1 - distance)，越接近 0 越优，映射到 (0, 1]
    - 0 <= distance <= 1：similarity = 1 - distance
    - distance > 1：similarity = 1 / (1 + distance)，再与 0 取 max
    """
    if distance is None:
        return None
    if distance < 0:
        s = 1.0 / (1.0 - distance)
        return min(1.0, max(0.0, s))
    if distance <= 1.0:
        return 1.0 - distance
    s = 1.0 / (1.0 + distance)
    return min(1.0, max(0.0, s))


def calculate_similarity_metrics(search_results: List[Dict[str, Any]]) -> Dict[str, float]:
    """
    计算搜索结果中每个父文档与查询的语义相似度指标。

    相似度计算策略（统一来源，保证一致性）：
    1. 优先使用 best_distance 计算相似度（统一转换公式，保证值域一致）
    2. 如果 best_distance 不存在，使用 relevance_score（假设已经是正确的相似度值）

    Args:
        search_results: 搜索结果列表，每个结果是一个父文档字典。

    Returns:
        包含相似度统计指标的字典：avg_similarity, min_similarity, max_similarity, similarity_std
    """
    np = _ensure_numpy()

    similarities = []
    use_best_distance_count = 0
    use_relevance_score_count = 0

    for doc in search_results:
        best_distance = doc.get('best_distance')
        relevance_score = doc.get('relevance_score')

        if best_distance is not None:
            use_best_distance_count += 1
            similarity = _distance_to_similarity(best_distance)
            if similarity is not None:
                similarities.append(similarity)
        elif relevance_score is not None:
            use_relevance_score_count += 1
            similarity = max(0.0, min(1.0, float(relevance_score)))
            similarities.append(similarity)

    if not similarities:
        return {
            'avg_similarity': 0.0,
            'min_similarity': 0.0,
            'max_similarity': 0.0,
            'similarity_std': 0.0,
        }

    if use_best_distance_count > 0 or use_relevance_score_count > 0:
        logger.debug(
            "相似度计算统计: 使用 best_distance=%d 个文档, 使用 relevance_score=%d 个文档",
            use_best_distance_count, use_relevance_score_count,
        )

    return {
        'avg_similarity': float(np.mean(similarities)),
        'min_similarity': float(np.min(similarities)),
        'max_similarity': float(np.max(similarities)),
        'similarity_std': float(np.std(similarities)),
    }
