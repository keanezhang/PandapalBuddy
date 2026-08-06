"""pandaren/memory/reinject — 压缩后回注子包

公共导出：
  - PostCompactReinjector  （编排器）
  - RecentFilesSource      （内置 source: 最近文件）
  - ActiveSkillsSource     （内置 source: 激活技能）
  - PlanStateSource        （内置 source: plan 状态）

应用层用法：
    from pandaren.memory.reinject import (
        PostCompactReinjector,
        RecentFilesSource,
        ActiveSkillsSource,
        PlanStateSource,
    )

    builder.memory(
        post_compact_sources=[
            RecentFilesSource(max_files=5),
            ActiveSkillsSource(),
            PlanStateSource(),
        ],
    )
"""

from .coordinator import PostCompactReinjector
from .sources import (
    RecentFilesSource,
    ActiveSkillsSource,
    PlanStateSource,
)

__all__ = [
    "PostCompactReinjector",
    "RecentFilesSource",
    "ActiveSkillsSource",
    "PlanStateSource",
]
