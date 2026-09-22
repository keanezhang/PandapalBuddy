"""builder/graph — 图谱索引构建流水线"""

__all__ = [
    "BuildGraphPipeline",
    "GraphInspector",
    "load_processed_parent_ids",
    "save_progress",
    "clear_progress",
    "progress_file_path",
]


from typing import Any


def __getattr__(name: str) -> Any:
    _module_map = {
        "BuildGraphPipeline": (".build_graph_pipeline", "BuildGraphPipeline"),
        "GraphInspector": (".graph_inspector", "GraphInspector"),
        "load_processed_parent_ids": (".graph_build_progress", "load_processed_parent_ids"),
        "save_progress": (".graph_build_progress", "save_progress"),
        "clear_progress": (".graph_build_progress", "clear_progress"),
        "progress_file_path": (".graph_build_progress", "progress_file_path"),
    }
    if name in _module_map:
        import importlib
        mod_name, attr_name = _module_map[name]
        mod = importlib.import_module(mod_name, __name__)
        value = getattr(mod, attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
