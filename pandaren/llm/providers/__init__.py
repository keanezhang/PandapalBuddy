"""pandaren/llm/providers/ — 各家 provider 的 typed extra 结构

命名约定
────────────────────────────────────────────────────────────
每个文件和类一律以**平台/API 厂商**命名，不用模型品牌名：

    文件                   类                     平台                常跑品牌
    ────────────────────────────────────────────────────────────────────────
    dashscope.py          DashScopeExtra         阿里云百炼           通义千问 Qwen
    volcengine.py         VolcEngineExtra        火山引擎方舟         豆包 Doubao
    (未来) moonshot.py    MoonshotExtra          Moonshot            Kimi
    (未来) anthropic.py   AnthropicExtra         Anthropic           Claude

作用
────────────────────────────────────────────────────────────
把各 provider 专属的非标字段从"裸 dict"升级为"typed dataclass"，
让使用者通过 IDE 补全 + docstring 就能看懂字段名 / 取值 / 语义，
而不必去翻每家文档。

这是 SDK "三件套"中的第 ② 件（填写助手）
────────────────────────────────────────────────────────────
    ① capabilities.py  → provider 说明书（决定"走哪条路"）
    ② providers/       → 本目录，typed extras（决定"这条路的参数长啥样"）
    ③ types.py         → ModelSettings（装起来送出门）

要先知道"该用哪个 Extra"？查 `pandaren/llm/capabilities.py`：
    - client.capabilities.reasoning_control == "enable_thinking"
        → 用 DashScopeExtra(enable_thinking=..., thinking_budget=...)
    - client.capabilities.reasoning_control == "thinking"
        → 用 VolcEngineExtra(thinking_mode=...)
    - client.capabilities.explicit_cache == "context_id"
        → 用 VolcEngineExtra(context_id=...)

设计原则（配合 pandaren/llm/capabilities.py 的 L1/L2/L3/L4 原则）
    - 这里承担 **L3 透传**：**不做映射、不抹平语义**
    - 每家一个 *Extra dataclass，字段名 1:1 对齐 provider 官方文档
    - 提供 `.as_extra_body()` 方法 → 把 dataclass 还原为 OpenAI 能吃的 dict
    - 使用方**永远**可以选择不用这些 typed 结构，直接塞 dict 到 extra_body

使用姿势
────────────────────────────────────────────────────────────
推荐（从顶层一行 import 拿到三件套）：
    from pandaren.llm import ModelSettings, VolcEngineExtra

    settings = ModelSettings(
        extra_body=VolcEngineExtra(thinking_mode="disabled").as_extra_body(),
    )

或者直接 `**` 展开（自己再塞几个字段）：
    settings = ModelSettings(
        extra_body={
            **VolcEngineExtra(thinking_mode="disabled").as_extra_body(),
            "some_future_field": "value",   # SDK 还没跟进的字段
        },
    )

裸 dict 写法永远保留（可以混用）：
    settings = ModelSettings(
        extra_body={"thinking": {"type": "disabled"}},
    )

可运行演示：
    python assistant/real/simple_llm_test.py --demo-extras

想加一家新 provider？
────────────────────────────────────────────────────────────
  1. 读 ../ADDING_A_PROVIDER.md（6 步 checklist）
  2. 复制 _template.py.example 到 <platform_name>.py 照着填（用平台名，不用品牌名）
"""

from .dashscope import DashScopeExtra
from .volcengine import VolcEngineExtra

__all__ = [
    "DashScopeExtra",
    "VolcEngineExtra",
]
