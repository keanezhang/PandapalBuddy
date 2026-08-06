"""pandapal/hitl/approval_log.py — HITL 审批 Markdown 审计日志。

职责：
- 以追加方式将审批请求和决策结果写入人类可读的 Markdown 文件
- 持久化审批上下文，支持进程重启后回溯（Option X 持久化 HITL 的可见性层）
- 纯 I/O 层，零业务逻辑，永不向外抛异常（O3 保证）

文件格式（每条审批一个 section）：

  ## [2026-05-24 10:05:33 UTC] HITL 审批请求
  - **审批 ID**：`abc-123`
  - **Run ID**：`run-456`
  - **用户**：`user-001`
  - **工具**：`bash_execute`
  - **参数摘要**：`rm -rf /tmp/cache`
  - **来源渠道**：`wecom`
  - **Reply ID**：`reply-789`（用于续接前端流）

  ### [2026-05-24 10:06:01 UTC] 决策：approved
  - **决策方**：`ZhangGuoQian`

设计约束：
- 写操作有文件锁（asyncio.Lock），防止并发写入交错
- 路径由调用方注入，支持配置化
- 失败时仅 logger.warning，不影响主流程
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class ApprovalMarkdownLog:
    """HITL 审批 Markdown 审计日志写入器。

    用法：
        log = ApprovalMarkdownLog(path=Path("data/hitl_approvals.md"))
        await log.write_request(
            approval_id="...", run_id="...", user_id="...",
            tool_name="...", tool_args_summary="...",
            source_channel_id="...", reply_id="...",
        )
        await log.write_decision(
            approval_id="...", run_id="...", decision="approved",
            decision_user_id="...",
        )
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    # ──────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────

    async def write_request(
        self,
        *,
        approval_id: str,
        run_id: str,
        user_id: str,
        tool_name: str,
        tool_args_summary: str | None,
        source_channel_id: str,
        reply_id: str | None = None,
    ) -> None:
        """记录新的审批请求（O3：永不向外抛异常）。"""
        now = self._now_str()
        reply_note = f"`{reply_id}`（用于续接前端流）" if reply_id else "无"
        summary_display = (tool_args_summary or "").replace("\n", " ")[:300]

        lines = [
            f"\n## [{now}] HITL 审批请求\n",
            f"- **审批 ID**：`{approval_id}`\n",
            f"- **Run ID**：`{run_id}`\n",
            f"- **用户**：`{user_id}`\n",
            f"- **工具**：`{tool_name}`\n",
            f"- **参数摘要**：`{summary_display}`\n",
            f"- **来源渠道**：`{source_channel_id}`\n",
            f"- **Reply ID**：{reply_note}\n",
        ]
        await self._append("".join(lines))

    async def write_decision(
        self,
        *,
        approval_id: str,
        run_id: str,
        decision: str,
        decision_user_id: str | None = None,
    ) -> None:
        """记录审批决策结果（O3：永不向外抛异常）。"""
        now = self._now_str()
        by_line = f"  - **决策方**：`{decision_user_id}`\n" if decision_user_id else ""
        lines = [
            f"\n### [{now}] 决策：{decision}\n",
            f"  - **审批 ID**：`{approval_id}`\n",
            f"  - **Run ID**：`{run_id}`\n",
            by_line,
        ]
        await self._append("".join(lines))

    # ──────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────

    async def _append(self, text: str) -> None:
        """追加写入文件，失败时仅 warning 不抛异常。"""
        async with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(text)
            except Exception as e:
                logger.warning("ApprovalMarkdownLog: write failed: %s", e)

    @staticmethod
    def _now_str() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
