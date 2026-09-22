"""evaluation — RAG 效果评估"""
from typing import List


def __getattr__(name: str):
    """延迟导入：仅在访问时才加载 evaluation 子模块，避免 import 即拉起 numpy。"""
    _lazy_imports = {
        "evaluate_rag": ".evaluation",
        "evaluate_rag_batch": ".evaluation",
        "RAGEvaluationResult": ".evaluation",
    }
    if name in _lazy_imports:
        import importlib
        module = importlib.import_module(_lazy_imports[name], __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__: List[str] = [
    "evaluate_rag",
    "evaluate_rag_batch",
    "RAGEvaluationResult",
]
