#!/usr/bin/env python3
"""aggregate.py – 聚合所有用例的判分结果，生成 benchmark.json（v2，严格版）。

用法：
    python aggregate.py <target-skill-dir> [--run-id <id>]

扫描 eval-runs/<run-id>/ 下所有 grading.json（兼容 v1 与 v2 格式），计算：
- 机械断言通过率（mech，按 sample 汇总）
- 语义断言加权分（sem，按 severity 加权：critical×3 / major×2 / minor×1）
- critical 失败一票否决清单（blocker）
- delta 的 bootstrap 95% 置信区间（对 case 重采样）
- 显著性判据 + 结论（verdict）
- token 开销对比（mean）

## benchmark.json 契约（v2）

    {
      "run": "run-1-baseline",
      "skill": "prd-design",
      "cases": 4,
      "sampling": {"n_samples": 3, "note": "采样数为各 variant 目录 sample-* 的最小值，无采样目录记 1"},
      "mech": {"with_skill": {"pass_rate": 1.0}, "without_skill": {"pass_rate": 1.0}, "delta": 0.0},
      "sem": {
        "with_skill": {"weighted_score": 0.98, "pass_rate": 0.94, "critical_fail": 0, "major_fail": 1, "minor_fail": 2},
        "without_skill": {"weighted_score": 0.5, ...},
        "delta": {"weighted": 0.48, "ci95": [0.12, 0.81], "significant": true, "effective": true}
      },
      "tokens": {"with_skill": {"mean": 7875}, "without_skill": {"mean": 900}, "delta": 6975},
      "critical_blockers": [{"case": "...", "variant": "with_skill", "assertion": "..."}],
      "invalid_sem_assertions": [...],
      "verdict": "有效 | 无效 | 证据不足 | 数据不完整 | BLOCKER"
    }

verdict 判定规则（依次短路）：
1. invalid_sem_assertions 非空      → "数据不完整（语义断言 rubric 不合法，需重判）"
2. with_skill 存在 critical 失败     → "BLOCKER（关键断言失败）"
3. CI 下界 > 0 且 delta ≥ 0.2       → "有效"
4. CI 含 0（不显著）                → "证据不足（95% CI 跨越 0）"
5. delta < 0.2                      → "无效（效应量太小）"

## 兼容性

- v1 grading.json（semantic_assertions 挂在 variant 顶层、断言带 pass bool、mech 为顶层 assertion_N）
  自动识别并转换：score = 1 if pass else 0，severity = "major"，evidence 取 detail。
- v2 grading.json（mech_assertions[sample] + semantic_assertions 带 severity/score/evidence）。
"""

import json
import random
import sys
from pathlib import Path

SEVERITY_WEIGHT = {"critical": 3, "major": 2, "minor": 1}
MIN_EFFECT = 0.2  # 最小效应量：delta 低于此值判"无效"
BOOTSTRAP_ITERS = 2000
BOOTSTRAP_SEED = 42
CI_QUANTILES = (0.025, 0.975)


def find_latest_run(skill_dir: Path) -> str | None:
    runs_dir = skill_dir / "eval-runs"
    if not runs_dir.exists():
        return None
    dirs = sorted([d.name for d in runs_dir.iterdir() if d.is_dir()], reverse=True)
    return dirs[0] if dirs else None


def load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_mech(grading: dict, variant: str) -> list[dict]:
    """提取机械断言结果列表 [{pass: bool|None}, ...]（v2 mech_assertions 或 v1 顶层 assertion_N）。"""
    v2 = grading.get("mech_assertions", {})
    if isinstance(v2, dict) and variant in v2:
        by_sample = v2[variant]
        if isinstance(by_sample, dict):
            results = []
            for sample, assertions in by_sample.items():
                if isinstance(assertions, dict):
                    for item in assertions.values():
                        if isinstance(item, dict) and "pass" in item:
                            results.append(item)
            return results

    # v1：variant 顶层 assertion_N
    top = grading.get(variant)
    if isinstance(top, dict):
        results = []
        for key, item in top.items():
            if key.startswith("assertion_") and isinstance(item, dict) and "pass" in item:
                results.append(item)
        return results
    return []


def _validate_sem(item: dict) -> dict:
    """内联 rubric 校验（与 grade.py 契约一致）：severity/score/evidence 合法性。
    aggregate 不依赖 grade.py 是否先跑过，自己校验一遍，不合格标 valid=False。"""
    out = dict(item)
    issues = []
    if out.get("severity") not in SEVERITY_WEIGHT:
        issues.append(f"severity={out.get('severity')!r}")
    try:
        if float(out.get("score", -1)) not in (0, 0.5, 1):
            issues.append(f"score={out.get('score')!r}")
    except (TypeError, ValueError):
        issues.append(f"score={out.get('score')!r}")
    if not isinstance(out.get("evidence"), str) or not out["evidence"].strip():
        issues.append("evidence 为空")
    out["valid"] = not issues
    if issues:
        out["issue"] = "; ".join(issues)
    return out


def extract_sem(grading: dict, variant: str) -> list[dict]:
    """提取语义断言列表并归一化为 v2 契约：
    {assertion, severity, score, evidence, valid, evidence_ref?}。"""
    # v2：grading["semantic_assertions"][variant]
    v2 = grading.get("semantic_assertions", {})
    raw = v2.get(variant, []) if isinstance(v2, dict) else []
    if not raw:
        # v1：variant 顶层 semantic_assertions
        top = grading.get(variant)
        if isinstance(top, dict):
            raw = top.get("semantic_assertions", []) or []

    normalized = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        # 已是 v2 契约
        if "score" in item and "severity" in item:
            normalized.append(_validate_sem(item))
            continue
        # v1 转换
        pass_val = item.get("pass")
        score = 1.0 if pass_val is True else 0.0 if pass_val is False else 0.0
        converted = {
            "assertion": item.get("assertion", ""),
            "severity": item.get("severity", "major"),
            "score": item.get("score", score),
            "evidence": item.get("evidence", item.get("detail", "")),
        }
        normalized.append(_validate_sem(converted))
    return normalized


def rate_mech(results: list[dict]) -> float | None:
    """机械断言通过率（pass=True 占比；pass=None 不计入分母）。"""
    total = [r for r in results if r["pass"] is not None]
    if not total:
        return None
    return sum(1 for r in total if r["pass"] is True) / len(total)


def sem_stats(sem: list[dict]) -> dict:
    """语义断言统计：加权分、通过率（score≥1 视为 pass）、按 severity 的失败计数。"""
    valid = [s for s in sem if s.get("valid", True)]
    weight_sum = 0.0
    score_sum = 0.0
    fail_by_sev = {"critical": 0, "major": 0, "minor": 0}
    for s in valid:
        sev = s.get("severity", "major")
        w = SEVERITY_WEIGHT.get(sev, 1)
        score = float(s.get("score", 0))
        weight_sum += w
        score_sum += score * w
        if score < 1:
            fail_by_sev[sev] = fail_by_sev.get(sev, 0) + 1

    return {
        "weighted_score": round(score_sum / weight_sum, 3) if weight_sum else None,
        "pass_rate": round(sum(1 for s in valid if float(s.get("score", 0)) >= 1) / len(valid), 3) if valid else None,
        "critical_fail": fail_by_sev["critical"],
        "major_fail": fail_by_sev["major"],
        "minor_fail": fail_by_sev["minor"],
        "total": len(valid),
    }


def bootstrap_ci(case_deltas: list[float], iters: int = BOOTSTRAP_ITERS) -> tuple[list[float], bool]:
    """对 case 重采样计算 delta 的 95% CI 与显著性（CI 不含 0）。"""
    if len(case_deltas) < 2:
        return [None, None], False
    rng = random.Random(BOOTSTRAP_SEED)
    n = len(case_deltas)
    means = []
    for _ in range(iters):
        sample = [case_deltas[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int(iters * CI_QUANTILES[0])]
    hi = means[int(iters * CI_QUANTILES[1])]
    return [round(lo, 3), round(hi, 3)], (lo > 0 or hi < 0)


def load_timing(case_dir: Path, variant: str) -> list[dict]:
    """读取 variant 下所有 timing.json（含 sample 级）。"""
    variant_dir = case_dir / variant
    if not variant_dir.is_dir():
        return []
    timing_files = sorted(variant_dir.glob("timing.json"))
    timing_files += sorted(variant_dir.glob("sample-*/timing.json"))
    out = []
    for t in timing_files:
        try:
            out.append(load_json(t))
        except (OSError, json.JSONDecodeError):
            continue
    return out


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

    meta = {}
    meta_path = run_dir / "meta.json"
    if meta_path.exists():
        meta = load_json(meta_path)
    skill_name = meta.get("skill_name", skill_dir.name)

    case_dirs = sorted([d for d in run_dir.iterdir() if d.is_dir()])
    print(f"聚合运行：{run_id}  （aggregate.py v2）")
    print(f"用例目录：{len(case_dirs)}")
    print()

    mech_rates = {"with_skill": [], "without_skill": []}
    sem_scores = {"with_skill": [], "without_skill": []}
    tokens = {"with_skill": [], "without_skill": []}
    critical_blockers = []
    invalid_sem = []

    for case_dir in case_dirs:
        grading_path = case_dir / "grading.json"
        if not grading_path.exists():
            print(f"  ⚠ 跳过（无 grading.json）：{case_dir.name}")
            continue
        grading = load_json(grading_path)

        for variant in ("with_skill", "without_skill"):
            mech = extract_mech(grading, variant)
            r = rate_mech(mech)
            if r is not None:
                mech_rates[variant].append(r)
            sem = extract_sem(grading, variant)
            stats = sem_stats(sem)
            if stats["weighted_score"] is not None:
                sem_scores[variant].append(stats["weighted_score"])
            for s in sem:
                if not s.get("valid", True):
                    invalid_sem.append({
                        "case": case_dir.name, "variant": variant,
                        "assertion": s.get("assertion", ""), "issue": s.get("issue", ""),
                    })
            # critical blocker：with_skill 的 critical 断言失败
            if variant == "with_skill":
                for s in sem:
                    if s.get("valid", True) and s.get("severity") == "critical" and float(s.get("score", 0)) < 1:
                        critical_blockers.append({
                            "case": case_dir.name,
                            "assertion": s.get("assertion", ""),
                            "score": s.get("score"),
                        })
            timing = load_timing(case_dir, variant)
            for t in timing:
                tok = t.get("tokens")
                if isinstance(tok, (int, float)):
                    tokens[variant].append(float(tok))

        print(f"  📦 {case_dir.name}：mech W={mech_rates['with_skill'][-1] if mech_rates['with_skill'] else '-'} / WO={mech_rates['without_skill'][-1] if mech_rates['without_skill'] else '-'} | "
              f"sem W={sem_scores['with_skill'][-1] if sem_scores['with_skill'] else '-'} / WO={sem_scores['without_skill'][-1] if sem_scores['without_skill'] else '-'}")

    # ---- 汇总统计 ----
    def mean(xs):
        return sum(xs) / len(xs) if xs else None

    mw, mwo = mean(mech_rates["with_skill"]), mean(mech_rates["without_skill"])
    sw, swo = mean(sem_scores["with_skill"]), mean(sem_scores["without_skill"])

    # 逐 case 差值（bootstrap 的采样单元是 case，不是断言）
    case_deltas = []
    for case_dir in case_dirs:
        grading_path = case_dir / "grading.json"
        if not grading_path.exists():
            continue
        grading = load_json(grading_path)
        sw_c = sem_stats(extract_sem(grading, "with_skill"))["weighted_score"]
        swo_c = sem_stats(extract_sem(grading, "without_skill"))["weighted_score"]
        if sw_c is not None and swo_c is not None:
            case_deltas.append(sw_c - swo_c)

    delta_sem = round(sw - swo, 3) if (sw is not None and swo is not None) else None
    ci, significant = bootstrap_ci(case_deltas) if case_deltas else ([None, None], False)
    effective = bool(significant and delta_sem is not None and delta_sem >= MIN_EFFECT)

    # verdict
    if invalid_sem:
        verdict = "数据不完整（语义断言 rubric 不合法，需重判）"
    elif critical_blockers:
        verdict = "BLOCKER（关键断言失败，不可接受）"
    elif significant and delta_sem is not None and delta_sem >= MIN_EFFECT:
        verdict = "有效"
    elif significant and delta_sem is not None and delta_sem <= -MIN_EFFECT:
        verdict = "反效果（with_skill 显著更差）"
    elif delta_sem is not None and abs(delta_sem) < MIN_EFFECT:
        verdict = "无效（效应量太小）"
    else:
        verdict = "证据不足（95% CI 跨越 0）"

    # 采样数：各 case 各 variant 的 sample-* 数最小值（无 sample-* 目录视为 1）
    n_samples = None
    for case_dir in case_dirs:
        for variant in ("with_skill", "without_skill"):
            vdir = case_dir / variant
            if vdir.is_dir():
                n = len([d for d in vdir.iterdir() if d.is_dir() and d.name.startswith("sample-")])
                if n:
                    n_samples = n if n_samples is None else min(n_samples, n)
    if n_samples is None:
        n_samples = 1

    benchmark = {
        "run": run_id,
        "skill": skill_name,
        "cases": len(case_dirs),
        "sampling": {
            "n_samples": n_samples,
            "note": "采样数为各 variant 目录 sample-* 的最小值；无 sample-* 目录记 1",
        },
        "mech": {
            "with_skill": {"pass_rate": round(mw, 3) if mw is not None else None},
            "without_skill": {"pass_rate": round(mwo, 3) if mwo is not None else None},
            "delta": round(mw - mwo, 3) if (mw is not None and mwo is not None) else None,
        },
        "sem": {
            "with_skill": sem_stats_agg(sem_scores["with_skill"]),
            "without_skill": sem_stats_agg(sem_scores["without_skill"]),
            "delta": {
                "weighted": delta_sem,
                "ci95": ci,
                "significant": significant,
                "effective": effective,
                "min_effect": MIN_EFFECT,
            },
        },
        "tokens": {
            "with_skill": {"mean": round(mean(tokens["with_skill"])) if tokens["with_skill"] else None,
                           "n": len(tokens["with_skill"])},
            "without_skill": {"mean": round(mean(tokens["without_skill"])) if tokens["without_skill"] else None,
                              "n": len(tokens["without_skill"])},
            "delta": round(mean(tokens["with_skill"]) - mean(tokens["without_skill"])) if tokens["with_skill"] and tokens["without_skill"] else None,
        },
        "critical_blockers": critical_blockers,
        "invalid_sem_assertions": invalid_sem,
        "verdict": verdict,
    }

    benchmark_path = run_dir / "benchmark.json"
    with open(benchmark_path, "w", encoding="utf-8") as f:
        json.dump(benchmark, f, indent=2, ensure_ascii=False)

    print()
    print("✅ benchmark.json 已生成：", benchmark_path)
    print(json.dumps(benchmark, indent=2, ensure_ascii=False))


def sem_stats_agg(scores: list[float]) -> dict:
    """跨 case 汇总语义分：均值/最小/最大/用例数。"""
    return {
        "mean": round(sum(scores) / len(scores), 3) if scores else None,
        "min": round(min(scores), 3) if scores else None,
        "max": round(max(scores), 3) if scores else None,
        "n_cases": len(scores),
    }


if __name__ == "__main__":
    main()
