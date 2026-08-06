"""pandaren/llm/providers/volcengine.py — 火山引擎方舟（VolcEngine Ark）专属参数 typed 结构

平台：火山引擎方舟（VolcEngine Ark），base_url 关键词 ark.cn-beijing.volces.com。
常跑模型品牌：豆包 Doubao（doubao-seed-2-* / doubao-1.5-thinking-* 等）。

对应官方文档：
  - 对话 Chat API:       https://www.volcengine.com/docs/82379/1494384
  - 深度思考:            https://www.volcengine.com/docs/82379/1449737
  - 上下文缓存 API:      https://www.volcengine.com/docs/82379/1528789（创建）
                        https://www.volcengine.com/docs/82379/1529329（对话）

重要事实（已由 explicit_cache_probe.py 实测确认，见 docs 2.6 节）：
  - 火山方舟 OpenAI 兼容 /chat/completions **不支持** cache_control（C 组非法值返回 200）
  - 火山方舟 chat 的"缓存"只有隐式自动 prefix cache，字段只有 cached_tokens（命中读取）
  - 显式缓存必须走 Context API：POST /context/create 换 context_id 后在 chat 请求中带上
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


# ═══════════════════════════════════════════════════════════════
# 类型别名
# ═══════════════════════════════════════════════════════════════

VolcEngineThinkingMode = Literal["disabled", "enabled", "auto"]
"""火山方舟 Doubao Seed 系列 thinking.type 的合法取值。

- "disabled": 强制关闭深度思考
- "enabled":  强制开启深度思考
- "auto":     由模型自行判断（默认）
"""


VolcEngineReasoningEffort = Literal["low", "medium", "high"]
"""火山方舟 thinking 系列（doubao-seed-thinking / doubao-1.5-thinking-pro 等）的顶层
reasoning_effort 参数取值。与 OpenAI 同名参数**语义一致**但**字段扁平化**
（OpenAI 是 reasoning.effort 嵌套，火山方舟是顶层 reasoning_effort）。"""


# ═══════════════════════════════════════════════════════════════
# VolcEngine Extra 主结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class VolcEngineExtra:
    """火山引擎方舟（VolcEngine Ark）OpenAI 兼容 /chat/completions 端点的专属参数（透传不映射）。

    字段全部对应火山方舟官方文档原名，SDK 不做任何重命名或语义转换。

    典型用法
    ────────────────────────────────────────────────────────────
    # 1) 关闭深度思考（Seed 系列常用，thinking 系列不认 thinking 字段）
    VolcEngineExtra(thinking_mode="disabled")

    # 2) thinking 系列调推理强度
    VolcEngineExtra(reasoning_effort="high")

    # 3) Context API：带上 /context/create 返回的 context_id
    VolcEngineExtra(context_id="ctx-2026xxx")

    # 4) 混合使用（实际业务中很少同时用 thinking + context_id）
    VolcEngineExtra(
        thinking_mode="disabled",
        context_id="ctx-2026xxx",
    )

    ⚠️ 注意
    ────────────────────────────────────────────────────────────
    - thinking_mode 只对 **Doubao-Seed** 系列有效；其他系列会被服务端忽略
    - reasoning_effort 只对 **doubao-*-thinking** 系列有效
    - context_id 只有在 model 是接入点 ID（ep-xxx）时才可能命中显式缓存
    - 本结构**不包含** cache_control —— 火山方舟 chat 不解析该字段，
      想用显式缓存请走 Context API（填 context_id）
    """

    thinking_mode: VolcEngineThinkingMode | None = None
    """深度思考开关（Seed 系列）。对应 body: {"thinking": {"type": "<mode>"}}。

    - None (默认): 不传该字段，由服务端/模型决定
    - "disabled":  关闭深度思考（省 tokens + 降延迟）
    - "enabled":   强制开启
    - "auto":      模型自判
    """

    reasoning_effort: VolcEngineReasoningEffort | None = None
    """推理强度（thinking 系列）。对应 body: {"reasoning_effort": "<level>"}。

    仅 doubao-*-thinking 系列模型识别。注意这是**顶层扁平字段**，
    不要和 OpenAI 的 reasoning.effort 嵌套对象混淆。
    """

    context_id: str | None = None
    """上下文缓存 ID（显式缓存入口）。对应 body: {"context_id": "ctx-..."}。

    获取流程：
      1. POST https://ark.cn-beijing.volces.com/api/v3/context/create
         body = {"model": "ep-xxx", "messages": [...], "mode": "common_prefix"}
      2. 响应 usage.prompt_tokens 就是本次写入的 token 数（显式写入账单）
      3. 把响应里的 id 填到这里，后续 /chat 请求带上即可命中

    ⚠️ model 字段必须是接入点 ID（ep-xxx），否则无法关联到缓存。
    """

    raw: dict[str, Any] = field(default_factory=dict)
    """逃生舱口（逃生舱口的逃生舱口）。

    如果火山方舟新加了一个字段，而本 dataclass 还没来得及跟进，
    可以先塞进这里——`as_extra_body()` 会把 raw 原样 merge 到输出 dict 里。
    字段 typed 化后应移出 raw。
    """

    def as_extra_body(self) -> dict[str, Any]:
        """把 typed 字段还原成 OpenAI 兼容的 payload 顶层 dict。

        输出结构示例：
            VolcEngineExtra(thinking_mode="disabled", context_id="ctx-xxx").as_extra_body()
            → {
                "thinking": {"type": "disabled"},
                "context_id": "ctx-xxx",
              }

        None 值的字段不会出现在输出中（遵循 ModelSettings 的"None = 不传"约定）。
        `raw` 中的键最后 merge 进去，使用者可以用 raw 覆盖 typed 字段的值。
        """
        body: dict[str, Any] = {}

        if self.thinking_mode is not None:
            body["thinking"] = {"type": self.thinking_mode}

        if self.reasoning_effort is not None:
            body["reasoning_effort"] = self.reasoning_effort

        if self.context_id is not None:
            body["context_id"] = self.context_id

        # raw 兜底覆盖（逃生舱口）
        if self.raw:
            body.update(self.raw)

        return body
