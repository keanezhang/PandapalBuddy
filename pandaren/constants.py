"""pandaren/constants.py — 原 SDK 全局共用常量（已清空）。

原内容去向（见 COMPACT_BUDGET_LAYERING_SPEC.md）：

  · ``CHARS_PER_TOKEN`` → 迁往 ``pandaren/memory/protocols.py``
    （``CharBasedTokenEstimator.CHARS_PER_TOKEN``，它是 estimator 的系数）；
  · ``DEFAULT_CONTEXT_WINDOW`` / ``DEFAULT_TOOL_SCHEMA_RATIO`` / ``DEFAULT_CONVERSATION_RATIO``
    → 删除：窗口比例归应用层（``pandapal/config/llm/context_budget.py``），
      预算字段归 ``pandaren/behavior/context_window_budget.py``。

本模块保留为空，仅供打包脚本的 hiddenimports 引用。
"""
