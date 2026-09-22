"""
RAG 指令加载模块（instructions）。

.. note:: 此模块为 SDK **内部** API，供 QueryIntentClassifier、Reranker 等组件使用。
          外部用户通常不需要直接调用。
"""
from .instruction_loader import load_instruction  # noqa: F401

__all__: list[str] = []
