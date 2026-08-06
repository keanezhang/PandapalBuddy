"""pandaren/llm/providers/dashscope.py — 阿里云百炼（DashScope）专属参数 typed 结构

平台：阿里云百炼（DashScope），base_url 关键词 dashscope.aliyuncs.com。
常跑模型品牌：通义千问 Qwen（qwen3-max / qwen3.6-plus 等）、百炼上的 Claude-compat 等。

对应官方文档：
  - 深度思考:        https://help.aliyun.com/zh/model-studio/deep-thinking
  - 上下文缓存:      https://help.aliyun.com/zh/model-studio/context-cache
  - 联网搜索:        https://help.aliyun.com/zh/model-studio/web-search

重要事实：
  - Qwen3 Python OpenAI SDK 调用时，enable_thinking / thinking_budget / enable_search
    **必须**通过 extra_body 传入（不是 OpenAI 标准参数）
  - 显式缓存通过在 message content 上挂 cache_control 实现（见 CacheControlHelper）
  - cache_control 不放在 ModelSettings 里——它是 **message 级** 参数而非请求级参数，
    需要在组装 messages 时就嵌进 content 块
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


# ═══════════════════════════════════════════════════════════════
# 类型别名
# ═══════════════════════════════════════════════════════════════

DashScopeSearchStrategy = Literal["standard", "pro"]
"""联网搜索策略。
- "standard": 标准搜索
- "pro":      专业搜索（覆盖更广但单价更高）
"""


# ═══════════════════════════════════════════════════════════════
# DashScope Extra 主结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class DashScopeExtra:
    """阿里云百炼（DashScope）OpenAI 兼容 /chat/completions 端点的专属参数（透传不映射）。

    字段全部对应百炼官方文档原名，SDK 不做重命名或语义转换。

    典型用法
    ────────────────────────────────────────────────────────────
    # 1) 关闭思考模式（Qwen3.6-Max/Plus/Flash 默认开启，关掉能省 tokens）
    DashScopeExtra(enable_thinking=False)

    # 2) 硬上限推理 token 数（超过 budget 强制收敛进入生成）
    DashScopeExtra(enable_thinking=True, thinking_budget=4096)

    # 3) 多轮对话保留思考历史（仅 qwen3.6-plus / kimi-k2.6 等支持）
    DashScopeExtra(enable_thinking=True, preserve_thinking=True)

    # 4) 开启联网搜索
    DashScopeExtra(enable_search=True, search_strategy="pro")

    # 5) 组合
    DashScopeExtra(
        enable_thinking=True,
        thinking_budget=8192,
        enable_search=True,
    )

    ⚠️ 显式缓存（cache_control）说明
    ────────────────────────────────────────────────────────────
    百炼的显式缓存字段 `cache_control` 不在 DashScopeExtra 里——它是挂在
    **message content 块**上的字段，而不是请求级参数：

        messages = [{
            "role": "system",
            "content": [{
                "type": "text",
                "text": "<长 prompt>",
                "cache_control": {"type": "ephemeral"},   # ← 这里
            }],
        }]

    如需帮助组装，可参考 `pandaren/llm/providers/cache_control.py`（暂未实现，
    目前建议业务层直接按上述结构手拼）。
    """

    # ─── 思考模式（Qwen3 混合思考 / QwQ / DeepSeek 等）─────────
    enable_thinking: bool | None = None
    """是否开启深度思考。对应 body: {"enable_thinking": <bool>}。

    - None (默认): 不传该字段，用模型自身默认值（各模型默认值不同，见 capabilities 文档）
    - True:        强制开启
    - False:       强制关闭

    ⚠️ "仅思考"类模型（QwQ、DeepSeek-R1、Qwen3-thinking 系列）**无法关闭**，
       传 False 会被服务端忽略或报错。
    """

    thinking_budget: int | None = None
    """思考过程的最大 Token 数。对应 body: {"thinking_budget": <int>}。

    超过该上限时模型立即停止思考并生成回复（硬截断）。
    - None (默认): 用模型最大思维链长度
    - 正整数:      上限（常用 512 约等 low、4096 约等 high）

    仅 Qwen3 思考模式 / GLM / Kimi 支持；DeepSeek / QwQ / MiniMax 不支持此字段。
    """

    preserve_thinking: bool | None = None
    """多轮对话中是否将历史 reasoning_content 拼接到模型输入。
    对应 body: {"preserve_thinking": <bool>}。

    ⚠️ 仅 qwen3.6-max-preview / qwen3.6-plus / kimi-k2.6 支持；
       开启后历史思考内容会计入输入 token 并计费。
    """

    # ─── 联网搜索 ─────────────────────────────────────────────
    enable_search: bool | None = None
    """是否开启联网搜索。对应 body: {"enable_search": <bool>}。"""

    search_strategy: DashScopeSearchStrategy | None = None
    """联网搜索策略。对应 body: {"search_options": {"search_strategy": "<value>"}}。"""

    # ─── 逃生舱口 ─────────────────────────────────────────────
    raw: dict[str, Any] = field(default_factory=dict)
    """原样塞入 body 顶层的逃生舱口。用于尚未 typed 化的新字段。"""

    def as_extra_body(self) -> dict[str, Any]:
        """把 typed 字段还原成 OpenAI 兼容的 payload 顶层 dict。

        输出示例：
            DashScopeExtra(enable_thinking=True, thinking_budget=4096).as_extra_body()
            → {"enable_thinking": True, "thinking_budget": 4096}

        None 值的字段不出现。`raw` 最后 merge。
        """
        body: dict[str, Any] = {}

        if self.enable_thinking is not None:
            body["enable_thinking"] = self.enable_thinking

        if self.thinking_budget is not None:
            body["thinking_budget"] = self.thinking_budget

        if self.preserve_thinking is not None:
            body["preserve_thinking"] = self.preserve_thinking

        if self.enable_search is not None:
            body["enable_search"] = self.enable_search

        if self.search_strategy is not None:
            body["search_options"] = {"search_strategy": self.search_strategy}

        if self.raw:
            body.update(self.raw)

        return body
