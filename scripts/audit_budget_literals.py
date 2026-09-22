"""预算裸字面量审计（COMPACT_BUDGET_LAYERING_SPEC §6.1 / §10 验收项）。

规则：**同一个数字只允许在其唯一定义处出现**（R4：消费点禁止 inline 兜底）。

本脚本扫描预算链路文件，若在**消费方**出现"已在别处定义"的字面量即报错（退出码 1），
只有显式列出的定义处/护栏处放行。

用法::

    python scripts/audit_budget_literals.py
    python scripts/audit_budget_literals.py --verbose
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 字面量 → 它的唯一事实来源（出现此地即违规）
LITERALS: dict[str, str] = {
    "24_000": "app SYSTEM_PROMPT_CAP.absolute_cap",
    "19_203": "启动实测 system_prompt_tokens（不可硬编码）",
    "102_400": "SDK_FALLBACK_BUDGET.context_window",
    "32_000": "toml [default].max_output / DEFAULT_MAX_OUTPUT_TOKENS",
    "128_000": "toml [default].max_context",
    "772_297": "派生量 compact_threshold（不可硬编码）",
    "0.26": "app SYSTEM_PROMPT_CAP.cw_share",
    "0.10": "app TOOL_SCHEMA_CAP.cw_share",
    "0.15": "app ESTIMATOR_ERROR_RATIO / TOOL_RESULT_CAP_RATIO",
}

#: 定义处 / 跨层护栏处白名单：file(相对路径) → 允许出现的字面量
ALLOWED: dict[str, set[str]] = {
    # —— 唯一定义处 ——
    "pandaren/behavior/context_window_budget.py": {"24_000", "8_000", "102_400", "19_203", "32_000", "0.26", "0.10", "0.15", "128_000"},
    "pandaren/memory/constants.py": {"8_000", "0.15", "500", "1_000"},
    "pandapal/config/llm/context_budget.py": {"24_000", "8_000", "0.26", "0.10", "0.15", "32_000"},
    # —— 跨层护栏测试（必须写出对端字面量才能锁死关系）——
    "pandaren/tests/test_budget_layering.py": set(LITERALS),
    "pandapal/config/tests/test_compact_budget_resolver.py": set(LITERALS),
}

#: 扫描范围：预算链路的消费方 + 定义处
SCAN: list[str] = [
    "pandaren/behavior/context_window_budget.py",
    "pandaren/memory/constants.py",
    "pandaren/memory/memory.py",
    "pandaren/memory/compaction/windowed.py",
    "pandaren/memory/compaction/micro_compact.py",
    "pandaren/tool/exposure/budget.py",
    "pandaren/builder.py",
    "pandaren/engine/loop.py",
    "pandaren/engine/run_core.py",
    "pandapal/config/llm/context_budget.py",
]


def _pattern(literal: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w.]){re.escape(literal)}(?![\w])")


def _audit_file(rel: str) -> list[tuple[int, str, str]]:
    path = ROOT / rel
    if not path.exists():
        return []
    allowed = ALLOWED.get(rel, set())
    hits: list[tuple[int, str, str]] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        for literal, source in LITERALS.items():
            if literal in allowed:
                continue
            if _pattern(literal).search(raw):
                hits.append((lineno, literal, source))
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="打印所有被扫描文件")
    args = parser.parse_args()

    total = 0
    for rel in SCAN:
        hits = _audit_file(rel)
        if args.verbose:
            print(f"  scanned {rel} → {len(hits)} 处")
        for lineno, literal, source in hits:
            print(f"{rel}:{lineno}: 裸字面量 {literal}（唯一来源：{source}）", file=sys.stderr)
            total += 1

    if total:
        print(f"\n[FAIL] 发现 {total} 处裸字面量（消费点禁止 inline 兜底）", file=sys.stderr)
        return 1

    print("[OK] 预算链路无二手字面量（每个数字只有一处定义）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
