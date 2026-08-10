#!/usr/bin/env python3
"""grade.py – 机械断言判分脚本（v2，严格版）。

用法：
    python grade.py <target-skill-dir> [--run-id <id>]

扫描 eval-runs/<run-id>/ 下所有用例目录，读取 evals.json 中的 assertions_mech
并逐项检查（支持多采样目录结构），将机械结果写入各用例的 grading.json；
同时校验语义断言的 rubric 合法性（severity/score/evidence），不合格的标记 invalid。

## grading.json 契约（v2）

机械断言结果（脚本写入，按 sample 组织）：
    "mech_assertions": {
        "sample-1": {"assertion_0": {"pass": true, "detail": "..."}},
        "sample-2": {"assertion_0": {"pass": false, "detail": "..."}}
    }
    无采样目录时使用单个 key "default"。

语义断言（裁判/主 agent 写入，脚本只校验不判分）：
    "semantic_assertions": [
        {
            "assertion": "产出物包含权限矩阵（≥2 角色）",
            "severity": "critical|major|minor",
            "score": 1,            # 0=fail, 0.5=partial, 1=pass
            "evidence": "引用 transcript/产出文件的具体原文",
            "evidence_ref": "outputs/PRD.md:45"
        }
    ]
    校验规则：severity 必须合法；score 必须 ∈ {0, 0.5, 1}；evidence 非空。
    违反任一条 → "valid": false + "issue"，由 aggregate 告警并强制重判。

## 机械断言类型（v2）

- `File exists: <path>`                     文件存在于 <variant>/outputs/ 下
- `Exit code N`                              读取 <variant>/exit_code.txt，值 == N
- `File contains: <path>: <pattern>`         文件内容包含子串 pattern
- `File not contains: <path>: <pattern>`     文件内容不含子串 pattern
- `File size <op> N: <path>`                 文件字节数满足 <op> ∈ {<, <=, >, >=}

## 目录结构（v2，采样）

<case>/<variant>/
    sample-1/{transcript.md, outputs/, timing.json}
    sample-2/{transcript.md, outputs/, timing.json}
    （无采样时退化为旧结构：transcript.md / outputs/ / timing.json）
"""

import json
import re
import sys
from pathlib import Path

VALID_SEVERITIES = {"critical", "major", "minor"}
VALID_SCORES = {0, 0.5, 1}
_SIZE_CMP = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


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


def list_samples(variant_dir: Path) -> list[str]:
    """返回采样目录名列表；无采样目录时返回 ["default"]。"""
    samples = sorted(
        d.name for d in variant_dir.iterdir() if d.is_dir() and d.name.startswith("sample-")
    )
    return samples if samples else ["default"]


def sample_dir(variant_dir: Path, sample: str) -> Path:
    if sample == "default":
        return variant_dir
    return variant_dir / sample


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"<read error: {e}>"


def check_assertion_mech(assertion: str, variant_dir: Path, sample: str) -> dict:
    """检查单条机械断言，返回 {"pass": bool|None, "detail": str}。"""
    a = assertion.strip()
    base = sample_dir(variant_dir, sample)

    # File exists: <path>  —— 相对于 outputs/
    if a.startswith("File exists:"):
        rel = a[len("File exists:"):].strip()
        full = base / "outputs" / rel
        return ({"pass": True, "detail": f"文件存在：{rel}"}
                if full.exists()
                else {"pass": False, "detail": f"文件缺失：{rel}"})

    # File contains: <path>: <pattern> / File not contains: ...
    for prefix, invert in (("File contains:", False), ("File not contains:", True)):
        if a.startswith(prefix):
            rest = a[len(prefix):].strip()
            if ":" not in rest:
                return {"pass": None, "detail": f"语法错误（缺 ':' 分隔）：{a}"}
            rel, pattern = rest.split(":", 1)
            rel, pattern = rel.strip(), pattern.strip()
            full = base / "outputs" / rel
            if not full.exists():
                return {"pass": False, "detail": f"文件缺失：{rel}（无法检查 {'包含' if not invert else '不包含'} '{pattern}'）"}
            found = pattern in read_text(full)
            ok = (not found) if invert else found
            verb = "不包含" if invert else "包含"
            return {"pass": ok, "detail": f"{rel} {verb} '{pattern}'"}

    # File size <op> N: <path>，<op> ∈ {<, <=, >, >=}
    m = re.match(r"^File size\s*([<>=]+)\s*(\d+):\s*(.+)$", a)
    if m:
        op, n, rel = m.group(1), int(m.group(2)), m.group(3).strip()
        cmp_fn = _SIZE_CMP.get(op)
        if cmp_fn is None:
            return {"pass": None, "detail": f"未知比较符：{op}（合法：< <= > >=）"}
        full = base / "outputs" / rel
        if not full.exists():
            return {"pass": False, "detail": f"文件缺失：{rel}（无法检查大小）"}
        size = full.stat().st_size
        ok = cmp_fn(size, n)
        return {"pass": ok, "detail": f"{rel} 大小 {size}B，条件 {op} {n}B"}

    # Exit code N
    if a.startswith("Exit code"):
        expected = a[len("Exit code"):].strip()
        code_path = base / "exit_code.txt"
        if not code_path.exists():
            return {"pass": False, "detail": "缺少 exit_code.txt，无法检查退出码"}
        actual = code_path.read_text().strip()
        return ({"pass": True, "detail": f"退出码 {actual} 符合预期 {expected}"}
                if actual == expected
                else {"pass": False, "detail": f"退出码 {actual}，预期 {expected}"})

    return {"pass": None, "detail": f"未知断言类型：{a}"}


def validate_semantic_assertion(item) -> dict:
    """校验单条语义断言 rubric 合法性，返回补全 valid/issue 后的副本。

    v1 兼容：无 severity 且无 score 的旧格式条目（{assertion, pass, detail}）
    原样保留、不强制校验（valid=True），由 aggregate.py 走 v1 转换路径。
    """
    out = dict(item) if isinstance(item, dict) else {"assertion": str(item)}
    out.setdefault("assertion", "")
    if "severity" not in out and "score" not in out:
        out["valid"] = True
        return out

    issues = []

    sev = out.get("severity")
    if sev not in VALID_SEVERITIES:
        issues.append(f"severity={sev!r}（合法值：critical/major/minor）")

    score = out.get("score")
    try:
        score_f = float(score)
        if score_f not in VALID_SCORES:
            issues.append(f"score={score!r}（合法值：0/0.5/1）")
    except (TypeError, ValueError):
        issues.append(f"score={score!r} 非数字（合法值：0/0.5/1）")

    ev = out.get("evidence", "")
    if not isinstance(ev, str) or not ev.strip():
        issues.append("evidence 为空（必须引用 transcript/产出文件原文）")

    out["valid"] = not issues
    if issues:
        out["issue"] = "; ".join(issues)
    return out


def load_existing_grading(case_dir: Path) -> dict:
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
        print("❌ 没有找到任何运行目录，请先执行 run_isolated.py")
        sys.exit(1)

    run_dir = skill_dir / "eval-runs" / run_id
    if not run_dir.is_dir():
        print(f"❌ 运行目录不存在：{run_dir}")
        sys.exit(1)

    evals_data = load_evals(skill_dir)
    cases = {c["id"]: c for c in evals_data.get("evals", [])}

    print(f"判分运行：{run_id}  （grade.py v2）")
    print(f"用例数：{len(cases)}")
    print()

    total_mech = {"with_skill": [0, 0], "without_skill": [0, 0]}  # [passed, total]
    invalid_sem = 0

    for case_id, case in cases.items():
        case_dir = run_dir / case_id
        if not case_dir.is_dir():
            print(f"  ⚠ 用例目录不存在：{case_id}")
            continue

        assertions = case.get("assertions_mech", [])
        existing = load_existing_grading(case_dir)
        grading = {
            "case_id": case_id,
            "prompt": case.get("prompt", ""),
            "expected_output": case.get("expected_output", ""),
            "mech_assertions": {},
            "semantic_assertions": {},
            "overall": existing.get("overall", ""),
        }

        print(f"📋 {case_id}")

        for variant in ("with_skill", "without_skill"):
            variant_dir = case_dir / variant
            if not variant_dir.is_dir():
                print(f"  ⚠ {variant} 目录不存在")
                continue
            samples = list_samples(variant_dir)
            mech_by_sample = {}

            for sample in samples:
                results = {}
                for i, assertion in enumerate(assertions):
                    key = f"assertion_{i}"
                    result = check_assertion_mech(assertion, variant_dir, sample)
                    results[key] = result
                    symbol = "✅" if result["pass"] is True else "❌" if result["pass"] is False else "❓"
                    print(f"  {variant}/{sample} {symbol} [{key}] {result['detail']}")
                    total_mech[variant][1] += 1
                    if result["pass"] is True:
                        total_mech[variant][0] += 1
                mech_by_sample[sample] = results

            grading["mech_assertions"][variant] = mech_by_sample

        # 语义断言：保留已有（裁判写入的），只做合法性校验
        # （v3 = {variant: {sample: [...]}} 逐 sample 校验并原样保留结构；v2/v1 = list）
        for variant in ("with_skill", "without_skill"):
            v2 = existing.get("semantic_assertions", {})
            existing_sem = v2.get(variant, []) if isinstance(v2, dict) else []
            # 兼容旧格式：语义断言直接挂在 variant 顶层
            if not existing_sem:
                existing_sem = existing.get("semantic_assertions", [])
            if isinstance(existing_sem, dict):
                validated = {}
                for sample, items in existing_sem.items():
                    sample_items = []
                    for item in (items or []):
                        if not isinstance(item, dict):
                            continue
                        v = validate_semantic_assertion(item)
                        if not v["valid"]:
                            invalid_sem += 1
                            print(f"  ⚠ {variant}/{sample} 语义断言校验失败：{v.get('issue')}")
                        sample_items.append(v)
                    validated[sample] = sample_items
            else:
                validated = []
                for item in existing_sem:
                    if not isinstance(item, dict):
                        continue
                    v = validate_semantic_assertion(item)
                    if not v["valid"]:
                        invalid_sem += 1
                        print(f"  ⚠ {variant} 语义断言校验失败：{v.get('issue')}")
                    validated.append(v)
            grading["semantic_assertions"][variant] = validated

        grading_path = case_dir / "grading.json"
        with open(grading_path, "w", encoding="utf-8") as f:
            json.dump(grading, f, indent=2, ensure_ascii=False)
        print("  💾 已写入 grading.json（v2 格式）")

    print()
    print("=" * 50)
    print("机械断言汇总（含全部 sample）")
    for variant in ("with_skill", "without_skill"):
        passed, total = total_mech[variant]
        if total:
            print(f"  {variant}: {passed}/{total}")
    if invalid_sem:
        print(f"  ⚠ {invalid_sem} 条语义断言 rubric 不合法，需按契约重判")


if __name__ == "__main__":
    main()
