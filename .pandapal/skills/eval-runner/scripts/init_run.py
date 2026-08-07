#!/usr/bin/env python3
"""init_run.py – 初始化一次 eval 运行的目录结构。

用法：
    python init_run.py <target-skill-dir> [--run-id run-1-baseline]

在 <target-skill-dir>/eval-runs/<run-id>/ 下为 evals.json 中的每个用例
创建 without_skill/ 和 with_skill/ 目录骨架。
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path


def load_evals(skill_dir: Path) -> dict:
    evals_path = skill_dir / "evals" / "evals.json"
    if not evals_path.exists():
        print(f"❌ 找不到 {evals_path}，请先创建 evals/evals.json")
        sys.exit(1)
    with open(evals_path, "r", encoding="utf-8") as f:
        return json.load(f)


def determine_run_id(skill_dir: Path, run_id: str | None) -> str:
    if run_id:
        return run_id
    runs_dir = skill_dir / "eval-runs"
    if not runs_dir.exists():
        return "run-1-baseline"
    existing = [d.name for d in runs_dir.iterdir() if d.is_dir()]
    if not existing:
        return "run-1-baseline"
    existing.sort()
    last = existing[-1]
    parts = last.split("-")
    try:
        num = int(parts[1])
        return f"run-{num + 1}-iter-1"
    except (IndexError, ValueError):
        return f"run-{len(existing) + 1}-baseline"


def main():
    if len(sys.argv) < 2:
        print("用法：python init_run.py <target-skill-dir> [--run-id <id>]")
        sys.exit(1)

    skill_dir = Path(sys.argv[1]).resolve()
    run_id = None
    if len(sys.argv) >= 4 and sys.argv[2] == "--run-id":
        run_id = sys.argv[3]

    if not skill_dir.is_dir():
        print(f"❌ 目录不存在：{skill_dir}")
        sys.exit(1)

    evals_data = load_evals(skill_dir)
    run_id = determine_run_id(skill_dir, run_id)
    run_dir = skill_dir / "eval-runs" / run_id

    cases = evals_data.get("evals", [])
    if not cases:
        print("⚠ evals.json 中没有用例")
        sys.exit(0)

    print(f"初始化运行：{run_id}")
    print(f"用例数：{len(cases)}")

    for case in cases:
        case_id = case["id"]
        case_dir = run_dir / case_id
        for sub in ("without_skill", "with_skill"):
            out_dir = case_dir / sub / "outputs"
            out_dir.mkdir(parents=True, exist_ok=True)
            print(f"  ✅ {case_id}/{sub}/")

    # 写入运行元信息
    meta = {
        "run_id": run_id,
        "skill_name": evals_data.get("skill_name", skill_dir.name),
        "created_at": datetime.now().isoformat(),
        "cases": [c["id"] for c in cases],
    }
    meta_path = run_dir / "meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    print(f"\n运行目录就绪：{run_dir}")
    print(f"元信息：{meta_path}")


if __name__ == "__main__":
    main()
