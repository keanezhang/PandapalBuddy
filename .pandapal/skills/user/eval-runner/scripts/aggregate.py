#!/usr/bin/env python3
"""aggregate.py – 聚合所有用例的判分结果，生成 benchmark.json。

用法：
    python aggregate.py <target-skill-dir> [--run-id <id>]

扫描 eval-runs/<run-id>/ 下所有 grading.json，
计算通过率、token 开销和 delta，输出 benchmark.json。
"""

import json
import sys
from pathlib import Path


def find_latest_run(skill_dir: Path) -> str | None:
    runs_dir = skill_dir / "eval-runs"
    if not runs_dir.exists():
        return None
    dirs = sorted([d.name for d in runs_dir.iterdir() if d.is_dir()], reverse=True)
    return dirs[0] if dirs else None


def count_passes(results: dict) -> tuple[int, int]:
    """从 grading 的 variant 结果中统计通过/总数。"""
    passed = 0
    total = 0
    for key, value in results.items():
        if not key.startswith("assertion_"):
            continue
        total += 1
        if isinstance(value, dict) and value.get("pass") is True:
            passed += 1
    return passed, total


def load_timing(case_dir: Path, variant: str) -> dict:
    timing_path = case_dir / variant / "timing.json"
    if timing_path.exists():
        with open(timing_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def main():
    if len(sys.argv) < 2:
        print("用法：python aggregate.py <target-skill-dir> [--run-id <id>]")
        sys.exit(1)

    skill_dir = Path(sys.argv[1]).resolve()
    run_id = None
    if len(sys.argv) >= 4 and sys.argv[2] == "--run-id":
        run_id = sys.argv[3]
    if not run_id:
        run_id = find_latest_run(skill_dir)
    if not run_id:
        print("❌ 没有找到任何运行目录")
        sys.exit(1)

    run_dir = skill_dir / "eval-runs" / run_id
    if not run_dir.is_dir():
        print(f"❌ 运行目录不存在：{run_dir}")
        sys.exit(1)

    # 读取 meta
    meta_path = run_dir / "meta.json"
    skill_name = skill_dir.name
    if meta_path.exists():
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
            skill_name = meta.get("skill_name", skill_name)

    case_dirs = sorted([d for d in run_dir.iterdir() if d.is_dir()])

    # 累计统计
    mech_with_pass = mech_with_total = 0
    mech_without_pass = mech_without_total = 0
    sem_with_pass = sem_with_total = 0
    sem_without_pass = sem_without_total = 0
    tokens_with = []
    tokens_without = []

    for case_dir in case_dirs:
        grading_path = case_dir / "grading.json"
        if not grading_path.exists():
            continue

        with open(grading_path, "r", encoding="utf-8") as f:
            grading = json.load(f)

        # 机械断言
        mp, mt = count_passes(grading.get("with_skill", {}))
        mech_with_pass += mp
        mech_with_total += mt
        mp, mt = count_passes(grading.get("without_skill", {}))
        mech_without_pass += mp
        mech_without_total += mt

        # Token 信息
        tw = load_timing(case_dir, "with_skill")
        two = load_timing(case_dir, "without_skill")
        if tw:
            tokens_with.append(tw.get("tokens", 0))
        if two:
            tokens_without.append(two.get("tokens", 0))

    # 语义断言：从 grading.json 的字段名无法区分 mech/sem，
    # 这里简单地把所有 assertion_* 都计入 mech。
    # 如果需要区分，可在 grading.json 中增加 _type 字段。

    cases_count = len(case_dirs)

    benchmark = {
        "run": run_id,
        "skill": skill_name,
        "cases": cases_count,
        "with_skill": {
            "pass_rate_mech": round(mech_with_pass / mech_with_total, 3) if mech_with_total > 0 else None,
            "tokens_mean": round(sum(tokens_with) / len(tokens_with)) if tokens_with else None,
        },
        "without_skill": {
            "pass_rate_mech": round(mech_without_pass / mech_without_total, 3) if mech_without_total > 0 else None,
            "tokens_mean": round(sum(tokens_without) / len(tokens_without)) if tokens_without else None,
        },
        "delta": {
            "pass_rate_mech": None,
            "tokens": None,
        },
    }

    # 计算 delta
    wr = benchmark["with_skill"]["pass_rate_mech"]
    wor = benchmark["without_skill"]["pass_rate_mech"]
    if wr is not None and wor is not None:
        benchmark["delta"]["pass_rate_mech"] = round(wr - wor, 3)

    wt = benchmark["with_skill"]["tokens_mean"]
    wot = benchmark["without_skill"]["tokens_mean"]
    if wt is not None and wot is not None:
        benchmark["delta"]["tokens"] = wt - wot

    benchmark_path = run_dir / "benchmark.json"
    with open(benchmark_path, "w", encoding="utf-8") as f:
        json.dump(benchmark, f, indent=2, ensure_ascii=False)

    print(f"✅ benchmark.json 已生成：{benchmark_path}")
    print()
    print(json.dumps(benchmark, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
