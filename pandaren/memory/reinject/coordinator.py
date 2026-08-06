"""pandaren/memory/reinject/coordinator.py — PostCompactReinjector

压缩成功后的回注编排器。

【整体流程背景】
当 Memory 中的对话历史过长时，系统会进行"压缩"（compact），即把旧的对话内容
压缩成摘要，以节省 token。但压缩后会丢失一些上下文信息（如最近读过的文件、
当前激活的技能、正在执行的 plan 等）。"回注"（reinject）就是把这些关键信息
重新注入到压缩后的上下文中，让 AI 不会"失忆"。

职责：
  1. 按顺序调用每个 PostCompactSource.collect() 收集 ReinjectionAttachment
  2. 按总 token 预算累计；超出预算时**优先保留前面的 source**
     （应用层注入顺序即重要性顺序）
  3. 过滤掉空 / 无效 attachment
  4. 返回保留下来的 attachments 列表

不做的事：
  - 不修改 messages（由 Memory.get_messages() 在拼装时插入）
  - 不调 LLM（B3）
  - source 抛异常时只 log warning，继续下一个 source（E4）
"""

from __future__ import annotations

import logging

from ..constants import DEFAULT_POST_COMPACT_TOKEN_BUDGET
from ..models import PostCompactContext, ReinjectionAttachment
from ..protocols import PostCompactSource

logger = logging.getLogger("pandaren.memory.reinject")


class PostCompactReinjector:
    """压缩后回注编排器。

    它是整个回注流程的"总指挥"：不关心每个 source 具体收集什么内容，
    只负责按顺序调度、按预算截断、过滤无效数据。

    工作流程示意：
        source_1.collect() → [att1, att2]  →  累计 token，未超预算 → 保留
        source_2.collect() → [att3]         →  累计 token，未超预算 → 保留
        source_3.collect() → [att4, att5]   →  累计 token，超预算了！→ 停止，返回 [att1, att2, att3]

    Args:
        sources:        按重要性排序的 PostCompactSource 列表（排在前面的更重要）
        token_budget:   总 token 预算（所有 source 累计，防止回注内容过多）
    """

    def __init__(
        self,
        sources: list[PostCompactSource] | None = None,
        token_budget: int = DEFAULT_POST_COMPACT_TOKEN_BUDGET,
    ) -> None:
        self._sources: list[PostCompactSource] = list(sources or [])
        self._token_budget = token_budget

    @property
    def has_sources(self) -> bool:
        """是否有任何 source 配置（无 source 时 collect_all 总返回 []）。"""
        return bool(self._sources)

    def collect_all(
        self,
        ctx: PostCompactContext,
    ) -> list[ReinjectionAttachment]:
        """按顺序调每个 source.collect()，按 token_budget 累计截断。

        核心逻辑：
        1. 遍历所有 source（按注入顺序，前面的优先级更高）
        2. 每个 source 收集自己的 attachments 列表
        3. 逐个 attachment 检查：
           - 是否是合法的 dict
           - 是否有 content 和 estimated_tokens
           - 加上后是否会超出总 token 预算
        4. 预算用完时立即停止，返回已收集到的全部 attachments
        5. source 本身抛异常时跳过该 source，继续下一个
        """
        if not self._sources:
            return []

        all_attachments: list[ReinjectionAttachment] = []  # 最终要返回的附件列表
        used_tokens = 0  # 已使用的 token 数

        for source in self._sources:
            source_name = type(source).__name__
            try:
                # 调用 source 的 collect 方法，获取该 source 想要回注的内容
                attachments = source.collect(ctx)
            except Exception as exc:
                # source 出错只打日志，不影响其他 source（容错设计 E4）
                logger.warning(
                    "PostCompactReinjector: %s.collect() failed: %s, skipping",
                    source_name,
                    exc,
                )
                continue

            if not attachments:
                continue

            for att in attachments:
                # 过滤：必须是 dict 类型
                if not isinstance(att, dict):
                    logger.warning(
                        "PostCompactReinjector: %s returned non-dict attachment, skipping",
                        source_name,
                    )
                    continue

                est = int(att.get("estimated_tokens", 0))  # 该附件估算的 token 数
                content = att.get("content", "")  # 附件正文内容

                # 过滤：内容为空或 token 估算为 0 的无效附件
                if not content or est <= 0:
                    continue

                # 预算检查：如果加上这个附件会超出总预算，且已有至少一个附件，则停止收集
                # 注意：如果还没有任何附件（all_attachments 为空），即使超预算也保留这一个，
                #       避免返回完全空的结果
                if used_tokens + est > self._token_budget and all_attachments:
                    logger.info(
                        "PostCompactReinjector: budget exhausted at %s "
                        "(used=%d, budget=%d), stopping",
                        source_name,
                        used_tokens,
                        self._token_budget,
                    )
                    return all_attachments

                # 通过所有检查，加入结果列表
                all_attachments.append(att)
                used_tokens += est

        if all_attachments:
            logger.info(
                "PostCompactReinjector: collected %d attachment(s) from %d source(s), "
                "~%d tokens (budget %d)",
                len(all_attachments),
                len(self._sources),
                used_tokens,
                self._token_budget,
            )
        return all_attachments
