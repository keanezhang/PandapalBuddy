"""pandaren/constants.py — SDK 全局共用常量

跨多个模块引用的常量集中在此。
单模块专用常量放在各模块自己的 constants.py 中。

判断标准：被 2 个以上模块引用 → 放这里；只被本模块引用 → 放模块内。
"""

# ── Token 估算 ──
# 粗略 token 估算系数：1 token ≈ 4 字符（中英文混合平均值）
# 被 memory / agent / skill / behavior 共用
CHARS_PER_TOKEN: float = 4.0

# ── 上下文窗口 ──
# 模型输入上下文窗口大小（保守默认值，用于未传参时兜底）
# 被 behavior/context_window_budget 和 tool/tool_budget 共用
DEFAULT_CONTEXT_WINDOW: int = 128000

# 工具 schema 占上下文窗口的 token 比例上限
# 被 behavior/context_window_budget（slot 分配）和 tool/tool_budget（兜底裁剪）共用
DEFAULT_TOOL_SCHEMA_RATIO: float = 0.10

# 对话历史占上下文窗口的默认比例
# 被 behavior/context_window_budget 和 memory/constants（compact_threshold 推导）共用
DEFAULT_CONVERSATION_RATIO: float = 0.50
