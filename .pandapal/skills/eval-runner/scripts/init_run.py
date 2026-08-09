#!/usr/bin/env python3
"""init_run.py – 初始化一次 eval 运行的目录结构（v2，支持多采样）。

用法：
    python init_run.py <target-skill-dir> [--run-id run-1-baseline] [--samples 3]

在 <target-skill-dir>/eval-runs/<run-id>/ 下为 evals.json 中的每个用例
创建 sample-1..sample-N 的 without_skill/ 和 with_skill/ 目录骨架。

v2 变更：
- 每 case 每 variant 下创建 sample-<i>/{outputs/} 骨架（--samples 默认 3）
- judge/ 目录：由 Step 4 判分阶段创建，这里不预建（保持骨架干净）
"""

import json
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


def parse_args(argv: list[str]) -> tuple[str | None, int, str | None]:
    """解析 <skill-dir> [--run-id <id>] [--samples N]，返回 (skill_dir, samples, run_id)。"""
    if not argv:
        print("用法：python init_run.py <target-skill-dir> [--run-id <id>] [--samples N]")
        sys.exit(1)
    skill_dir = argv[0]
    samples = 3
    run_id = None
    i = 1
    while i < len(argv):
        if argv[i] == "--run-id" and i + 1 < len(argv):
            run_id = argv[i + 1]
            i += 2
        elif argv[i] == "--samples" and i + 1 < len(argv):
            try:
                samples = max(1, int(argv[i + 1]))
            except ValueError:
                print(f"⚠ --samples 参数无效：{argv[i + 1]}，使用默认 3")
            i += 2
        else:
            i += 1
    return skill_dir, samples, run_id


def main():
    skill_dir_str, samples, run_id = parse_args(sys.argv[1:])
    skill_dir = Path(skill_dir_str).resolve()

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

    print(f"初始化运行：{run_id}  （init_run.py v2，采样数 {samples}）")
    print(f"用例数：{len(cases)}")

    for case in cases:
        case_id = case["id"]
        case_dir = run_dir / case_id
        for sub in ("without_skill", "with_skill"):
            for i in range(1, samples + 1):
                out_dir = case_dir / sub / f"sample-{i}" / "outputs"
                out_dir.mkdir(parents=True, exist_ok=True)
                print(f"  ✅ {case_id}/{sub}/sample-{i}/")

    # 写入运行元信息
    meta = {
        "run_id": run_id,
        "skill_name": evals_data.get("skill_name", skill_dir.name),
        "created_at": datetime.now().isoformat(),
        "samples": samples,
        "cases": [c["id"] for c in cases],
    }
    meta_path = run_dir / "meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    print(f"\n运行目录就绪：{run_dir}")
    print(f"元信息：{meta_path}")


if __name__ == "__main__":
    main()
