#!/usr/bin/env python3
"""grade.py – 机械断言判分脚本。

用法：
    python grade.py <target-skill-dir> [--run-id <id>]

扫描 eval-runs/<run-id>/ 下所有用例目录，
读取 evals.json 中的 assertions_mech 并逐项检查，
将结果写入各用例的 grading.json（只写 mech 部分，sem 部分留空）。
"""

import json
import os
import sys
from pathlib import Path


def find_latest_run(skill_dir: Path) -> str | None:
    runs_dir = skill_dir / "eval-runs"
    if not runs_dir.exists():
        return None
    dirs = sorted([d.name for d in runs_dir.iterdir() if d.is_dir()], reverse=True)
    return dirs[0] if dirs else None


def load_evals(skill_dir: Path) -> dict:
    evals_path = skill_dir / "evals" / "evals.json"
    if not evals_path.exists():
        print(f"❌ 找不到 {evals_path}")
        sys.exit(1)
    with open(evals_path, "r", encoding="utf-8") as f:
        return json.load(f)


def check_assertion_mech(assertion: str, case_dir: Path, variant: str) -> dict:
    """检查单条机械断言，返回 {"pass": bool, "detail": str}。"""
    a = assertion.strip()

    # File exists: <path>
    if a.startswith("File exists:"):
        file_path = a[len("File exists:"):].strip()
        full_path = case_dir / variant / "outputs" / file_path
        if full_path.exists():
            return {"pass": True, "detail": f"文件存在：{file_path}"}
        else:
            return {"pass": False, "detail": f"文件缺失：{file_path}"}

    # Exit code N
    if a.startswith("Exit code"):
        # 需要 exit_code.txt 文件（由 subagent 写入）
        code_path = case_dir / variant / "exit_code.txt"
        expected = a[len("Exit code"):].strip()
        if code_path.exists():
            actual = code_path.read_text().strip()
            if actual == expected:
                return {"pass": True, "detail": f"退出码 {actual} 符合预期 {expected}"}
            else:
                return {"pass": False, "detail": f"退出码 {actual}，预期 {expected}"}
        else:
            return {"pass": False, "detail": f"缺少 exit_code.txt，无法检查退出码"}

    # 未知断言类型
    return {"pass": None, "detail": f"未知断言类型：{a}"}


def load_existing_grading(case_dir: Path) -> dict:
    """加载已有的 grading.json（可能包含语义断言结果）。"""
    grading_path = case_dir / "grading.json"
    if grading_path.exists():
        with open(grading_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def main():
    if len(sys.argv) < 2:
        print("用法：python grade.py <target-skill-dir> [--run-id <id>]")
        sys.exit(1)

    skill_dir = Path(sys.argv[1]).resolve()
    run_id = None
    if len(sys.argv) >= 4 and sys.argv[2] == "--run-id":
        run_id = sys.argv[3]
    if not run_id:
        run_id = find_latest_run(skill_dir)
    if not run_id:
        print("❌ 没有找到任何运行目录，请先执行 init_run.py")
        sys.exit(1)

    run_dir = skill_dir / "eval-runs" / run_id
    if not run_dir.is_dir():
        print(f"❌ 运行目录不存在：{run_dir}")
        sys.exit(1)

    evals_data = load_evals(skill_dir)
    cases = {c["id"]: c for c in evals_data.get("evals", [])}

    print(f"判分运行：{run_id}")
    print(f"用例数：{len(cases)}")
    print()

    total_mech = 0
    passed_mech_with = 0
    passed_mech_without = 0

    for case_id, case in cases.items():
        case_dir = run_dir / case_id
        if not case_dir.is_dir():
            print(f"  ⚠ 用例目录不存在：{case_id}")
            continue

        assertions = case.get("assertions_mech", [])
        if not assertions:
            continue

        # 加载已有 grading（保留语义断言）
        existing = load_existing_grading(case_dir)
        grading = {
            "case_id": case_id,
            "prompt": case.get("prompt", ""),
            "expected_output": case.get("expected_output", ""),
            "with_skill": {},
            "without_skill": {},
            "overall": existing.get("overall", ""),
        }

        print(f"📋 {case_id}")

        for variant in ("with_skill", "without_skill"):
            results = {}
            for i, assertion in enumerate(assertions):
                key = f"assertion_{i}"
                result = check_assertion_mech(assertion, case_dir, variant)
                results[key] = result
                symbol = "✅" if result["pass"] else "❌" if result["pass"] is False else "❓"
                print(f"  {variant} {symbol} [{key}] {result['detail']}")
                total_mech += 1
                if result["pass"]:
                    if variant == "with_skill":
                        passed_mech_with += 1
                    else:
                        passed_mech_without += 1

            # 合并：保留已有语义断言，覆盖机械断言
            grading[variant] = {**(existing.get(variant, {})), **results}

        grading_path = case_dir / "grading.json"
        with open(grading_path, "w", encoding="utf-8") as f:
            json.dump(grading, f, indent=2, ensure_ascii=False)
        print(f"  💾 已写入 grading.json")

    print()
    print("=" * 50)
    print("机械断言汇总")
    print(f"  with_skill:    {passed_mech_with}/{total_mech // 2}" if total_mech > 0 else "  with_skill:    N/A")
    print(f"  without_skill: {passed_mech_without}/{total_mech // 2}" if total_mech > 0 else "  without_skill: N/A")


if __name__ == "__main__":
    main()
