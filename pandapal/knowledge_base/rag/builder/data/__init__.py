"""builder/data — 数据准备：文档加载、分割、词法增强、实体关系抽取"""

__all__ = [
    "DocumentSplitter",
    "DocumentLoader",
    "LLMEntityRelationshipExtractor",
    "LexicalEnricher",
    "DataPreparer",
    "DataPreparationResult",
    "build_chunk_position_map",
    "build_chunk_position_map_batch",
    "build_rag_data",
    "load_prepared_rag_data",
]


from typing import Any


def __getattr__(name: str) -> Any:
    _module_map = {
        "DocumentSplitter": (".splitter", "DocumentSplitter"),
        "DocumentLoader": (".loader", "DocumentLoader"),
        "LLMEntityRelationshipExtractor": (
            ".entity_relationship_extractor_llm",
            "LLMEntityRelationshipExtractor",
        ),
        "LexicalEnricher": (".lexical_enricher", "LexicalEnricher"),
        "DataPreparer": (".data_preparer", "DataPreparer"),
        "DataPreparationResult": (".data_preparer", "DataPreparationResult"),
        "build_chunk_position_map": (".chunk_position_mapper", "build_chunk_position_map"),
        "build_chunk_position_map_batch": (".chunk_position_mapper", "build_chunk_position_map_batch"),
        "build_rag_data": (".build_rag_data", "build_rag_data"),
        "load_prepared_rag_data": (".build_rag_data", "load_prepared_rag_data"),
    }
    if name in _module_map:
        import importlib
        mod_name, attr_name = _module_map[name]
        mod = importlib.import_module(mod_name, __name__)
        value = getattr(mod, attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
