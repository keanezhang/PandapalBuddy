"""query/enrich — 查询富化（标准名/别名扩展）"""
from .query_enricher import enrich_query_analysis_once  # noqa: F401

__all__ = ["enrich_query_analysis_once"]
