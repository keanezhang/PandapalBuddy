"""pandapal/config/budget_repo.py — 预算账本持久化（BudgetLedger 的注入依赖）

BudgetLedger（见 budget/ledger.py）把「按 (user_id, provider) 累计的净费用 + 用户设定的额度」
交给本层持久化，以支持**跨会话累计、跨进程重启不丢**。

分层（数据访问层 · D1/D5）：
    - 只封装存储细节，用业务语言暴露方法（upsert_budget / bump_spent / load_all）。
    - 不做任何业务判断（换算、超额、状态判定全在 BudgetLedger）。
    - 本模块**不 import** budget.pricing（cost_of_call），避免与账本形成循环依赖。

为什么用 JSON 文件而非 SQLite Repo（对设计文档的务实偏离）：
    桌面默认以 **markdown 存储模式** 运行（.pandapal/pandapal_md/...），SQLite-only 的
    Repository 在该模式下不可达。预算账本是「每 user × 每 provider 一行」的极小数据集
    （量级 ≤ 几行），用一个 **mode-agnostic 的 JSON 文件** 持久化最简单、最健壮，且对
    本地单用户场景完全够用。生产多租户如需并发强一致，可另实现满足同一 Protocol 的
    SQLite/PG 版本注入，账本零改动（BL4 依赖注入）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger("pandapal.config.budget_repo")


@dataclass(frozen=True)
class BudgetRow:
    """持久化的一条预算记录（一个 (user_id, provider) 账户的权威量）。

    注意：**不持久化 limit_usd**——它是 `limit_native × 当前FX` 的读时派生值
    （决策 E：运营调汇率即时生效），落盘只存权威量。
    """
    user_id: str
    provider: str
    currency: str               # 展示/输入币种（默认 "USD"）
    limit_native: float | None  # 用户设定的额度（该币种下）；None = 仅有消费未设额度
    spent_usd: float            # 已累计净费用（USD 内部记账）


@runtime_checkable
class BudgetRepository(Protocol):
    """预算账本持久化契约（BudgetLedger 注入依赖）。"""

    async def load_all(self) -> list[BudgetRow]:
        """载入全部预算行（启动 seed 用）。"""
        ...

    async def upsert_budget(
        self, user_id: str, provider: str, currency: str, limit_native: float
    ) -> None:
        """写入/更新某 (user,provider) 的额度与币种（保留既有 spent_usd）。"""
        ...

    async def bump_spent(self, entries: list[tuple[str, str, float]]) -> None:
        """批量落盘已累计净费用。entries = [(user_id, provider, spent_usd), ...]。

        D3：批量一次写，避免每步一次 I/O 的 N+1。语义为「以传入 spent_usd 覆盖」
        （账本内存态为权威，落盘只是快照）。
        """
        ...


class InMemoryBudgetRepo:
    """内存实现（测试双 / 无持久化降级）。满足 BudgetRepository Protocol。"""

    def __init__(self, rows: list[BudgetRow] | None = None) -> None:
        # 键 (user_id, provider) → BudgetRow
        self._rows: dict[tuple[str, str], BudgetRow] = {
            (r.user_id, r.provider): r for r in (rows or [])
        }

    async def load_all(self) -> list[BudgetRow]:
        return list(self._rows.values())

    async def upsert_budget(
        self, user_id: str, provider: str, currency: str, limit_native: float
    ) -> None:
        key = (user_id, provider)
        prev = self._rows.get(key)
        spent = prev.spent_usd if prev else 0.0
        self._rows[key] = BudgetRow(user_id, provider, currency, limit_native, spent)

    async def bump_spent(self, entries: list[tuple[str, str, float]]) -> None:
        for user_id, provider, spent_usd in entries:
            key = (user_id, provider)
            prev = self._rows.get(key)
            if prev is not None:
                self._rows[key] = BudgetRow(
                    user_id, provider, prev.currency, prev.limit_native, spent_usd
                )
            else:
                # 无 limit 但有 spent（用户从未设额度却已消费）：limit_native=None
                self._rows[key] = BudgetRow(user_id, provider, "USD", None, spent_usd)


class JsonFileBudgetRepo:
    """JSON 文件实现（mode-agnostic 持久化）。满足 BudgetRepository Protocol。

    文件结构（`{path}`，如 `{data_dir}/users/{uid}/budgets.json`）：
        {"budgets": [{"user_id","provider","currency","limit_native","spent_usd"}, ...]}

    并发：本地单用户 · 单进程 · 单 event loop；文件 I/O 通过 asyncio.to_thread 卸载，
    不阻塞 loop。整文件读改写（数据量极小，可接受）。写路径经 `_write_lock` 串行化：
    upsert_budget（IPC 设额度）与 bump_spent（run 结束 flush）可能并发提交，各自的
    read-modify-write 若交错会**丢更新**（后写者整文件覆盖前写者，limit 或 spent 丢失）；
    锁保证「读→改→写」作为一个整体串行，杜绝丢更新（load_all 只读，无需上锁）。
    """

    def __init__(self, path: str) -> None:
        self._path = path
        # 串行化写路径（read-modify-write 整体互斥），防止并发 upsert/bump 丢更新。
        self._write_lock = asyncio.Lock()

    def _read_sync(self) -> dict[tuple[str, str], BudgetRow]:
        if not os.path.isfile(self._path):
            return {}
        try:
            with open(self._path, encoding="utf-8") as f:
                obj = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            # 读失败不臆造：返回空 → 上层 seed 降级为「无持久额度」（Fail-Safe）
            logger.warning("budget json read failed (%s): %s", self._path, exc)
            return {}
        out: dict[tuple[str, str], BudgetRow] = {}
        for b in obj.get("budgets", []):
            try:
                raw_limit = b.get("limit_native")
                row = BudgetRow(
                    user_id=str(b["user_id"]),
                    provider=str(b["provider"]),
                    currency=str(b.get("currency", "USD")),
                    limit_native=(None if raw_limit is None else float(raw_limit)),
                    spent_usd=float(b.get("spent_usd", 0.0)),
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("budget json bad row skipped: %s (%s)", b, exc)
                continue
            out[(row.user_id, row.provider)] = row
        return out

    def _write_sync(self, rows: dict[tuple[str, str], BudgetRow]) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        payload = {
            "budgets": [
                {
                    "user_id": r.user_id,
                    "provider": r.provider,
                    "currency": r.currency,
                    "limit_native": r.limit_native,
                    "spent_usd": r.spent_usd,
                }
                for r in rows.values()
            ]
        }
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)  # 原子替换，避免半写文件

    async def load_all(self) -> list[BudgetRow]:
        rows = await asyncio.to_thread(self._read_sync)
        return list(rows.values())

    async def upsert_budget(
        self, user_id: str, provider: str, currency: str, limit_native: float
    ) -> None:
        def _do() -> None:
            rows = self._read_sync()
            key = (user_id, provider)
            prev = rows.get(key)
            spent = prev.spent_usd if prev else 0.0
            rows[key] = BudgetRow(user_id, provider, currency, limit_native, spent)
            self._write_sync(rows)

        async with self._write_lock:
            await asyncio.to_thread(_do)

    async def bump_spent(self, entries: list[tuple[str, str, float]]) -> None:
        if not entries:
            return

        def _do() -> None:
            rows = self._read_sync()
            for user_id, provider, spent_usd in entries:
                key = (user_id, provider)
                prev = rows.get(key)
                if prev is not None:
                    rows[key] = BudgetRow(
                        user_id, provider, prev.currency, prev.limit_native, spent_usd
                    )
                else:
                    # 无 limit 但有 spent（用户从未设额度却已消费）：limit_native=None。
                    # ⚠️ 历史缺陷：此处曾写 0.0，与内存孪生（InMemoryBudgetRepo）不一致。
                    #    落盘 0.0 后重启，_limit_usd 读回 0.0（而非 None）→ 第一步就判
                    #    「预算耗尽 $0.0000」→ 从未设过预算的用户被永久锁死。
                    #    现有 round-trip 测试用的是内存孪生（分支写对了的那个），
                    #    因此长期未暴露——务必保持 JsonFileBudgetRepo 的独立测试覆盖。
                    rows[key] = BudgetRow(user_id, provider, "CNY", None, spent_usd)
            self._write_sync(rows)

        async with self._write_lock:
            await asyncio.to_thread(_do)
