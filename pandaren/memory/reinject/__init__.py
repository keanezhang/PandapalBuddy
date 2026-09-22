"""pandaren/memory/reinject — 压缩后回注子包

公共导出：
  - PostCompactReinjector  （编排器）
  - RecentFilesSource      （内置 source: 最近读过的**文件清单**）
  - PlanStateSource        （内置 source: 当前 **plan 文件路径**）

⚠️ 回注走「**指针模式**」：只注入索引（清单 / 路径），不注入正文 ——
正文由 AI 用 ``read_file`` 按需重取，避免挤占压缩后保留的对话历史。
技能正文不回注（AI 可用 ``search_skills`` 重新加载，技能目录已在 static_context 常驻）。

应用层用法：
    from pandaren.memory.reinject import (
        PostCompactReinjector,
        RecentFilesSource,
        PlanStateSource,
    )

    builder.memory(
        post_compact_sources=[
            RecentFilesSource(max_files=5),
            PlanStateSource(),
        ],
    )
"""

from .coordinator import PostCompactReinjector
from .sources import (
    PlanStateSource,
    RecentFilesSource,
)

__all__ = [
    "PostCompactReinjector",
    "PlanStateSource",
    "RecentFilesSource",
]
