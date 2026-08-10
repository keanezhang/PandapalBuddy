#!/usr/bin/env python3
"""judge.py – 语义断言双盲裁判（脚本化，替代手工委派 subagent）。

用法：
    python judge.py <target-skill-dir> [--run-id <id>] [--sample all|sample-1,sample-2]
        [--credentials-file <path>] [--model-id <id>] [--provider <p>]
        [--seed 42] [--temperature 0.0] [--only <case-id>] [--force]

职责（对应 eval-runner SKILL.md 的 Step 4b，脚本化落地）：
1. 对每个 case：随机打乱 with/without 的 transcript → judge/A.md、B.md + mapping.json（双盲，
   A/B 与 with/without 的对应关系只存在于脚本内，裁判不可见）。
2. 把 A/B 内容（transcript + outputs 文本文件）内联进附录 A 裁判 prompt —— 裁判没有任何
   文件读取能力，双盲边界从"主 agent 自觉"变为"脚本物理隔离"。
3. 单次 LLM 调用（temperature=0 + seed，可复现），严格 JSON 解析。
4. 按 mapping 把裁判对 A/B 的判定映射回 with/without → semantic_assertions。
5. 与 grade.py 写入的 mech_assertions 合并，写回 grading.json（v2 契约）。

幂等：某 case 的 semantic_assertions（with + without 均非空）已存在 → 跳过，--force 重判。

## 与 grade.py / aggregate.py 的契约对齐

- grading.json v2 契约：semantic_assertions = {"with_skill": [...], "without_skill": [...]}，
  条目 {assertion, severity, score(0|0.5|1), evidence, evidence_ref}。
- judge 只写 semantic_assertions + judge 元信息；mech_assertions 由 grade.py 写。
  两者互不覆盖（各自读 existing 保留对方字段），顺序：grade.py → judge.py → aggregate.py。
- aggregate.py 的 extract_sem() 直接消费本脚本产物；rubric 合法性由 aggregate 二次校验。

## 双盲细节

- 默认对 case 下**全部 sample** 逐个双盲判分（--sample all，with/without 同一位置）；
  可用逗号分隔指定子集（如 --sample sample-1,sample-2）。无 sample-* 目录时
  fallback 到 variant 根目录的 transcript.md（记为 default）。
- 每个 sample 独立打乱 + 独立 seed（seed + sample 序号），互不干扰、可复现。
- judge/{sample}/A.md、B.md、mapping.json 写入 case_dir/judge/ 留审计痕迹；prompt 内联内容与之完全相同。
- grading.json 的 semantic_assertions 按 sample 组织：
  {"with_skill": {"sample-1": [...]}, "without_skill": {"sample-1": [...]}}。
- evidence_ref 规范化：裁判给出的 "A.md:12" 前缀在映射回 with/without 时替换为
  "with_skill/sample-1"（只补结构字段，不修改 score/evidence 原文）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import tomllib
from datetime import datetime
from pathlib import Path

# ── 项目根注入：向上找含 pandaren/ 的目录（脚本可能在任意 cwd 下被调用） ──
_PROJECT_ROOT = None
_here = Path(__file__).resolve().parent
for _p in (_here, *_here.parents):
    if (_p / "pandaren").is_dir() and (_p / "pandapal").is_dir():
        _PROJECT_ROOT = _p
        break
if _PROJECT_ROOT is None:
    print("❌ 找不到项目根（pandaren/ + pandapal/），请在 pandapal_buddy 仓库内运行本脚本")
    sys.exit(1)
sys.path.insert(0, str(_PROJECT_ROOT))

from pandaren.llm.client import OpenAICompatibleClient  # noqa: E402
from pandaren.llm.exceptions import LLMRequestError  # noqa: E402
from pandaren.llm.types import ModelSettings  # noqa: E402

VALID_SCORES = {0, 0.5, 1}
VALID_SEVERITIES = {"critical", "major", "minor"}
# outputs/ 下内联进裁判输入的最大单文件字符数（保护 token 预算）
_MAX_INLINE_FILE_CHARS = 8000
# 允许内联的文本类扩展名（其余扩展名只列文件名不读内容）
_TEXT_EXTS = {
    ".md", ".txt", ".json", ".csv", ".tsv", ".html", ".htm", ".xml", ".yaml", ".yml",
    ".toml", ".ini", ".log", ".py", ".ts", ".tsx", ".js", ".sql", ".css", ".svg",
}
_EMPTY_TRANSCRIPT = "（该样本无 transcript 文件）"


# ─────────────────────────── 凭据（与 run_isolated.py 同款逻辑） ───────────────────────────

def find_credentials_file() -> Path | None:
    appdata = Path.home() / "AppData" / "Roaming"
    candidates = []
    for app_dir in ("com.pandapal.desktop", "com.pandapal.app"):
        base = appdata / app_dir / "users"
        if base.is_dir():
            for user_dir in base.iterdir():
                f = user_dir / "credentials" / "llm_credentials.toml"
                if f.is_file():
                    candidates.append(f)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def load_credential(credentials_file: Path, model_id: str | None, provider: str | None) -> dict:
    if not credentials_file.is_file():
        print(f"❌ 凭据文件不存在：{credentials_file}")
        sys.exit(1)
    with open(credentials_file, "rb") as f:
        data = tomllib.load(f)
    creds = data.get("credentials", [])
    if not isinstance(creds, list) or not creds:
        print(f"❌ 凭据文件 {credentials_file} 中没有 [[credentials]] 条目")
        sys.exit(1)
    default_model = data.get("default_model_id")
    pick = None
    if model_id:
        pick = next((c for c in creds if c.get("model_id") == model_id), None)
        if pick is None:
            print(f"❌ 凭据文件中找不到 model_id={model_id!r}")
            sys.exit(1)
    else:
        pick = next((c for c in creds if c.get("model_id") == default_model), None) or creds[0]
    return {
        "provider": provider or pick.get("provider") or "openai",
        "model_id": pick.get("model_id") or "deepseek-v4-flash",
        "api_key": pick.get("api_key") or "",
    }


# ─────────────────────────── 数据加载 ───────────────────────────

def load_evals(skill_dir: Path) -> dict:
    evals_path = skill_dir / "evals" / "evals.json"
    if not evals_path.exists():
        print(f"❌ 找不到 {evals_path}")
        sys.exit(1)
    with open(evals_path, "r", encoding="utf-8") as f:
        return json.load(f)


def find_latest_run(skill_dir: Path) -> str | None:
    runs_dir = skill_dir / "eval-runs"
    if not runs_dir.exists():
        return None
    dirs = sorted([d.name for d in runs_dir.iterdir() if d.is_dir()], reverse=True)
    return dirs[0] if dirs else None


def load_grading(case_dir: Path) -> dict:
    path = case_dir / "grading.json"
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def sample_base_dir(case_dir: Path, variant: str, sample: str) -> Path:
    """返回 sample 的基目录；无 sample-* 目录时退化为 variant 根目录。"""
    variant_dir = case_dir / variant
    if sample != "default":
        sd = variant_dir / sample
        if sd.is_dir():
            return sd
    return variant_dir


# ─────────────────────────── 裁判输入打包（transcript + outputs 内联） ───────────────────────────

def collect_transcript_package(case_dir: Path, variant: str, sample: str) -> str:
    """组合裁判可见内容：transcript.md 全文 + 产出文件内容（截断保护）。

    transcript.md 单独内联；产出文件从 sample 基目录**递归**收集（含 outputs/、
    docs/ 等任何位置——agent 按 skill 约定写到哪都能被裁判看到），
    排除元数据文件（transcript.md/exit_code.txt/timing.json）。
    与 SKILL.md 附录 A 的输入（judge/A.md、B.md）对齐——内容**内联**进 prompt，
    裁判无需也不应有文件读取能力。
    """
    base = sample_base_dir(case_dir, variant, sample)
    parts: list[str] = []

    transcript_path = base / "transcript.md"
    transcript = (transcript_path.read_text(encoding="utf-8", errors="replace")
                  if transcript_path.exists() else _EMPTY_TRANSCRIPT)
    parts.append(f"=== transcript.md ===\n{transcript}")

    _META_FILES = {"transcript.md", "exit_code.txt", "timing.json"}
    artifact_files = [
        p for p in sorted(base.rglob("*"))
        if p.is_file() and p.name not in _META_FILES
    ]
    parts.append("\n=== 产出文件（sample 目录递归收集） ===")
    if not artifact_files:
        parts.append("（sample 目录下未找到产出文件）")
    for p in artifact_files:
        rel = p.relative_to(base).as_posix()
        if p.suffix.lower() in _TEXT_EXTS:
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                parts.append(f"\n--- {rel}（读取失败） ---")
                continue
            if len(content) > _MAX_INLINE_FILE_CHARS:
                content = content[:_MAX_INLINE_FILE_CHARS] + "\n…(截断)"
            parts.append(f"\n--- {rel} ---\n{content}")
        else:
            parts.append(f"\n--- {rel}（二进制/非文本，仅列名） ---")

    return "\n".join(parts)


# ─────────────────────────── 双盲 ───────────────────────────

def build_blind_pair(case_dir: Path, sample: str, rng: random.Random) -> tuple[str, str, dict]:
    """随机打乱 with/without → A/B。返回 (a_package, b_package, mapping)。

    mapping: {"A": {"variant": ..., "sample": ...}, "B": {...}, "seed": int}
    A/B 的 with/without 归属只存于 mapping（脚本私藏），不进入裁判 prompt。
    """
    entries = [
        {"variant": "with_skill", "sample": sample},
        {"variant": "without_skill", "sample": sample},
    ]
    rng.shuffle(entries)
    mapping = {
        "A": entries[0],
        "B": entries[1],
    }
    a_pkg = collect_transcript_package(case_dir, entries[0]["variant"], sample)
    b_pkg = collect_transcript_package(case_dir, entries[1]["variant"], sample)
    return a_pkg, b_pkg, mapping


# ─────────────────────────── 裁判 prompt（附录 A 模板） ───────────────────────────

def build_judge_prompt(case: dict, a_pkg: str, b_pkg: str) -> str:
    assertions_sem = case.get("assertions_sem", [])
    assertion_lines = "\n".join(
        f"{i + 1}. {a.get('assertion', '(空断言)')}（severity: {a.get('severity', 'major')}）"
        for i, a in enumerate(assertions_sem)
    )
    return f"""你是本次评测的独立裁判。你的任务是对两份 agent 输出（A.md 和 B.md）分别判断一组语义断言。
你不允许知道 A/B 中哪份来自"使用了 skill"的运行——这正是双盲设计，请勿猜测或推测。

输入内容（已内联，无需读取任何文件）：
================ 输出 A（对应 judge/A.md） ================
{a_pkg}
================ 输出 B（对应 judge/B.md） ================
{b_pkg}

断言清单（来自 evals.json 的 assertions_sem，逐条复制，severity 保留）：

{assertion_lines}

判定要求：
- 对每条断言，分别对 A 和 B 给出 score：1=完全满足（有明确证据）；0.5=部分满足；0=不满足或相反。
- evidence 必须引用 A.md/B.md 或对应产出文件的具体原文（引用位置 + 原文），不允许空证据。
- assertion_index 为断言清单中的编号（1 到 {len(assertions_sem)}），不要用 0 开始的索引。
- 输出纯 JSON（不要多余文字），格式：
{{
  "A": [
    {{"assertion_index": 1, "score": 1, "evidence": "A.md: '原文引用'", "evidence_ref": "A.md:12"}}
  ],
  "B": [...]
}}
- 若某条断言在输出中完全找不到对应内容，score 记 0，evidence 写 "A.md/B.md 中未找到相关内容"。"""


# ─────────────────────────── JSON 解析与映射 ───────────────────────────

def parse_judge_json(text: str) -> dict:
    """从 LLM 输出中提取纯 JSON（容忍 ```json 围栏或前后说明文字）。"""
    t = text.strip()
    # 去掉 ```json ... ``` 围栏
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("json"):
            t = t[4:]
        t = t.strip()
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"LLM 输出中没有 JSON 对象：{text[:200]!r}")
    return json.loads(t[start:end + 1])


def normalize_score(raw) -> float:
    """把裁判 score 归一化为 0 / 0.5 / 1；非法值抛 ValueError。"""
    try:
        s = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"score={raw!r} 非数字")
    if s not in VALID_SCORES:
        raise ValueError(f"score={raw!r} 不在合法集合 {{0, 0.5, 1}}")
    return s


def map_verdict_to_variant(raw_items: list, label: str, mapping: dict, assertions_sem: list) -> list:
    """把裁判对 label（A/B）的判定映射回 with/without 条目。

    只做结构映射与字段补全（assertion 文本、severity、evidence_ref 前缀规范化），
    **不修改裁判的 score/evidence**。
    """
    variant = mapping[label]["variant"]
    sample = mapping[label]["sample"]
    prefix = f"{variant}/{sample}" if sample != "default" else variant

    judged: dict[int, dict] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("assertion_index", -1)) - 1  # 清单 1-based → 0-based
        except (TypeError, ValueError):
            idx = -1
        if not (0 <= idx < len(assertions_sem)):
            continue  # 越界索引丢弃，由补全逻辑兜底
        score = normalize_score(item.get("score"))
        judged[idx] = {
            "assertion": assertions_sem[idx].get("assertion", ""),
            "severity": assertions_sem[idx].get("severity", "major"),
            "score": score,
            "evidence": str(item.get("evidence", "")).strip(),
            "evidence_ref": normalize_evidence_ref(item.get("evidence_ref"), label, prefix),
        }

    # 补全裁判漏判的断言：score 0 + 显式说明（结构补全，非编造裁判判定）
    out = []
    for i, a in enumerate(assertions_sem):
        if i in judged:
            out.append(judged[i])
        else:
            out.append({
                "assertion": a.get("assertion", ""),
                "severity": a.get("severity", "major"),
                "score": 0,
                "evidence": f"{label}.md 中裁判未给出 assertion_index {i + 1} 的判定（双盲 JSON 缺失该条目）",
                "evidence_ref": prefix,
            })
    return out


def normalize_evidence_ref(ref, label: str, prefix: str):
    """规范化 evidence_ref：把裁判引用的 "A.md:12" 前缀替换为真实 variant/sample 路径。"""
    if not isinstance(ref, str) or not ref.strip():
        return f"{prefix}"
    r = ref.strip()
    if r.startswith(f"{label}.md"):
        r = r[len(f"{label}.md"):]
        if r.startswith(":"):
            r = r[1:]
        return f"{prefix}/{r}".rstrip("/") if r else prefix
    return r


# ─────────────────────────── LLM 调用 ───────────────────────────

async def call_judge_llm(client: OpenAICompatibleClient, prompt: str,
                         temperature: float, seed: int) -> str:
    """单次裁判 LLM 调用；json_object 不被 provider 支持时降级为普通调用（重试一次）。"""
    messages = [
        {"role": "system",
         "content": "你是评测流程中的独立裁判。你只输出要求的 JSON 对象，不输出任何多余文字。"},
        {"role": "user", "content": prompt},
    ]
    try:
        resp = await client.call(
            messages,
            settings=ModelSettings(
                temperature=temperature,
                seed=seed,
                response_format={"type": "json_object"},
            ),
        )
    except LLMRequestError:
        # 部分 provider 不支持 response_format，降级重试（保留 temperature/seed）
        resp = await client.call(
            messages,
            settings=ModelSettings(temperature=temperature, seed=seed),
        )
    content = resp.get("content") or ""
    # 推理模型怪癖（deepseek-v4-flash 实测）：response_format=json_object 模式下，
    # 完整 JSON 可能落在 reasoning_content 而 content 为空（finish_reason=stop）。
    # 此时以 reasoning_content 作为裁判输出，否则会被误判为"LLM 输出为空"。
    if not content.strip():
        rc = resp.get("reasoning_content") or ""
        if rc.strip():
            content = rc
    return content


async def call_judge_with_retry(client: OpenAICompatibleClient, prompt: str,
                                temperature: float, seed: int, max_tries: int = 3) -> dict:
    """裁判 LLM 调用 + JSON 解析，失败自动重试（seed 递增偏移，保证重试采样不同）。

    历史 bug：单次调用返回空内容时直接抛 ValueError，导致整 case 的语义评分全部丢失
    （semantic_assertions 为空 → sem n_cases=0 → verdict 恒为"证据不足"）。
    重试耗尽才抛最后一个异常，由上层故障隔离点记入 grading.json 的 judge.error。
    """
    last_err: Exception | None = None
    for attempt in range(max_tries):
        raw = await call_judge_llm(client, prompt, temperature, seed + attempt)
        if not raw or not raw.strip():
            last_err = ValueError("LLM 输出为空")
            print(f"  ⚠ 裁判输出为空（第 {attempt + 1}/{max_tries} 次），重试…")
            continue
        try:
            parsed = parse_judge_json(raw)
        except ValueError as e:
            last_err = e
            print(f"  ⚠ 裁判 JSON 解析失败（第 {attempt + 1}/{max_tries} 次）：{e}")
            continue
        if not isinstance(parsed, dict) or "A" not in parsed or "B" not in parsed:
            last_err = ValueError(f"裁判 JSON 缺少 A/B 键：{str(parsed)[:200]}")
            print(f"  ⚠ 裁判 JSON 缺 A/B 键（第 {attempt + 1}/{max_tries} 次），重试…")
            continue
        return parsed
    raise last_err or ValueError("裁判调用失败")


# ─────────────────────────── 单 case 判分 ───────────────────────────

async def judge_case(client: OpenAICompatibleClient, case_dir: Path, case: dict,
                     args: argparse.Namespace, samples: list[str]) -> dict:
    """判单个 case 的多个 sample（单一 event loop 内调用）。

    返回 {"with_skill": {sample: [...]}, "without_skill": {sample: [...]}}；
    每个 sample 独立双盲打乱 + 独立 seed（seed + sample 序号），互不干扰、可复现。
    """
    assertions_sem = case.get("assertions_sem", [])
    result = {"with_skill": {}, "without_skill": {}}
    judge_dir = case_dir / "judge"
    judge_dir.mkdir(parents=True, exist_ok=True)

    for idx, sample in enumerate(samples):
        # 确认 with/without 都有对应 transcript（无 sample-* 时 fallback default）
        have = {}
        for variant in ("with_skill", "without_skill"):
            base = sample_base_dir(case_dir, variant, sample)
            have[variant] = (base / "transcript.md").exists()
            if not have[variant]:
                # 有 sample-* 目录但缺 transcript（执行失败）→ 用空包继续，裁判会判 0
                print(f"  ⚠ {case['id']}/{variant}/{sample} 缺 transcript.md，按空输入判分")
        if not have["with_skill"] and not have["without_skill"]:
            raise FileNotFoundError(f"{case['id']}/{sample} 的 with/without 都没有 transcript.md，无法判分")

        sample_seed = args.seed + idx  # 每个 sample 独立 seed：可复现且互不干扰
        rng = random.Random(sample_seed)
        a_pkg, b_pkg, mapping = build_blind_pair(case_dir, sample, rng)
        mapping["seed"] = sample_seed

        prompt = build_judge_prompt(case, a_pkg, b_pkg)

        # 落盘审计痕迹：judge/{sample}/A.md、B.md、mapping.json（mapping 仅供人查证，不进裁判输入）
        sample_judge_dir = judge_dir / sample
        sample_judge_dir.mkdir(parents=True, exist_ok=True)
        (sample_judge_dir / "A.md").write_text(a_pkg, encoding="utf-8")
        (sample_judge_dir / "B.md").write_text(b_pkg, encoding="utf-8")
        (sample_judge_dir / "mapping.json").write_text(
            json.dumps(mapping, indent=2, ensure_ascii=False), encoding="utf-8")
        (sample_judge_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

        parsed = await call_judge_with_retry(client, prompt, args.temperature, sample_seed)

        with_items = map_verdict_to_variant(parsed.get("B", []), "B", mapping, assertions_sem) \
            if mapping["B"]["variant"] == "with_skill" \
            else map_verdict_to_variant(parsed.get("A", []), "A", mapping, assertions_sem)
        without_items = map_verdict_to_variant(parsed.get("B", []), "B", mapping, assertions_sem) \
            if mapping["B"]["variant"] == "without_skill" \
            else map_verdict_to_variant(parsed.get("A", []), "A", mapping, assertions_sem)
        result["with_skill"][sample] = with_items
        result["without_skill"][sample] = without_items

    return result


def resolve_samples(case_dir: Path, requested: str) -> list[str]:
    """解析 --sample 为要判的 sample 列表。

    "all"（默认）→ 遍历 with_skill 下全部 sample-* 目录（与 without_skill 对齐）；
    无 sample-* 目录（旧结构）→ ["default"]。逗号分隔 → 按给定顺序去重保留。
    显式指定的 sample 存在性不在此校验，缺 transcript 由 judge_case 兜底（按空输入判分）。
    """
    if requested.strip().lower() == "all":
        variant_dir = case_dir / "with_skill"
        if variant_dir.is_dir():
            samples = sorted(
                d.name for d in variant_dir.iterdir()
                if d.is_dir() and d.name.startswith("sample-")
            )
            if samples:
                return samples
        return ["default"]
    out = []
    for s in (x.strip() for x in requested.split(",")):
        if s and s not in out:
            out.append(s)
    return out or ["default"]


# ─────────────────────────── main ───────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="语义断言双盲裁判（脚本化）")
    parser.add_argument("target_skill_dir", help="目标 skill 目录，如 .pandapal/skills/prd-design")
    parser.add_argument("--run-id", default=None, help="运行目录名（默认取最新 run）")
    parser.add_argument("--sample", default="all",
                        help="判分 sample：all=遍历全部 sample-*（默认），或逗号分隔指定（如 sample-1,sample-2）")
    parser.add_argument("--credentials-file", default=None, help="显式指定 llm_credentials.toml 路径")
    parser.add_argument("--model-id", default=None, help="覆盖 model_id（须在凭据文件中存在）")
    parser.add_argument("--provider", default=None, help="覆盖 provider（openai/deepseek/dashscope/volcengine）")
    parser.add_argument("--seed", type=int, default=42, help="双盲打乱 + LLM 采样 seed，默认 42")
    parser.add_argument("--temperature", type=float, default=0.0, help="裁判 LLM 温度，默认 0.0")
    parser.add_argument("--only", default=None, help="只判指定 case-id（可逗号分隔多个）")
    parser.add_argument("--force", action="store_true", help="已判过的 case 也重判")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    skill_dir = Path(args.target_skill_dir).resolve()
    if not skill_dir.is_dir():
        print(f"❌ skill 目录不存在：{skill_dir}")
        sys.exit(1)

    run_id = args.run_id or find_latest_run(skill_dir)
    if not run_id:
        print("❌ 没有找到任何运行目录，请先执行 run_isolated.py")
        sys.exit(1)
    run_dir = skill_dir / "eval-runs" / run_id
    if not run_dir.is_dir():
        print(f"❌ 运行目录不存在：{run_dir}")
        sys.exit(1)

    evals_data = load_evals(skill_dir)
    cases = {c["id"]: c for c in evals_data.get("evals", [])}

    credentials_file = Path(args.credentials_file).resolve() if args.credentials_file else find_credentials_file()
    if credentials_file is None:
        print("❌ 未找到凭据文件。请用 --credentials-file 指定 llm_credentials.toml 路径")
        sys.exit(1)
    credential = load_credential(credentials_file, args.model_id, args.provider)
    if not credential["api_key"]:
        print("❌ 凭据中 api_key 为空")
        sys.exit(1)

    client = OpenAICompatibleClient.for_provider(
        provider=credential["provider"],
        api_key=credential["api_key"],
        model_name=credential["model_id"],
        timeout=180.0,
    )

    only_ids = {c.strip() for c in args.only.split(",") if c.strip()} if args.only else None
    if only_ids:
        missing = only_ids - set(cases)
        if missing:
            print(f"❌ --only 指定的 case 不存在：{sorted(missing)}")
            sys.exit(1)

    print(f"🧑⚖️ 双盲裁判：{run_id}  （judge.py）")
    print(f"   model={credential['provider']}/{credential['model_id']} | "
          f"sample={args.sample} | seed={args.seed} | temperature={args.temperature}")
    print()

    judged, skipped, failed = 0, 0, 0
    for case_id, case in cases.items():
        if only_ids and case_id not in only_ids:
            continue
        case_dir = run_dir / case_id
        if not case_dir.is_dir():
            print(f"  ⚠ 用例目录不存在：{case_id}")
            continue

        if not case.get("assertions_sem"):
            print(f"  ⚠ {case_id}：assertions_sem 为空，跳过语义判分")
            skipped += 1
            continue

        # 幂等：按 sample 粒度检查（v3 结构 {variant: {sample: [...]}} 均非空才算已判）
        existing = load_grading(case_dir)
        sem = existing.get("semantic_assertions", {})
        pending = []
        for sample in resolve_samples(case_dir, args.sample):
            already = (
                isinstance(sem.get("with_skill"), dict) and bool(sem["with_skill"].get(sample))
                and isinstance(sem.get("without_skill"), dict) and bool(sem["without_skill"].get(sample))
            )
            if already and not args.force:
                print(f"  ⏭ {case_id}/{sample}：语义判分已存在（--force 重判）")
                skipped += 1
                continue
            pending.append(sample)
        if not pending:
            continue

        print(f"▶ [{case_id}] 双盲判分中（samples={pending}）…")
        try:
            result = await judge_case(client, case_dir, case, args, pending)
        except Exception as e:  # 故障隔离点：单个 case 失败不中断整体
            failed += 1
            print(f"  ❌ 判分失败：{e!r}")
            existing["judge"] = existing.get("judge", {})
            existing["judge"]["error"] = {"case": case_id, "detail": repr(e),
                                          "at": datetime.now().isoformat(timespec="seconds")}
            grading_path = case_dir / "grading.json"
            with open(grading_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)
            continue

        # 合并写回：保留 grade.py 写入的 mech_assertions；semantic_assertions 按 sample 合并
        # （兼容旧 v2 结构 {variant: [...]}：视为 default 且被新判结果覆盖）
        grading = existing
        new_sem = {}
        old_sem = existing.get("semantic_assertions", {})
        for variant in ("with_skill", "without_skill"):
            v = old_sem.get(variant) if isinstance(old_sem, dict) else None
            new_sem[variant] = dict(v) if isinstance(v, dict) else {}
        for variant in ("with_skill", "without_skill"):
            for sample, items in result[variant].items():
                new_sem[variant][sample] = items
        grading["semantic_assertions"] = new_sem
        grading["judge"] = {
            "script": "judge.py",
            "model": f"{credential['provider']}/{credential['model_id']}",
            "sample": args.sample,
            "samples": pending,
            "seed": args.seed,
            "temperature": args.temperature,
            "mapping_dir": "judge/",
            "judged_at": datetime.now().isoformat(timespec="seconds"),
        }
        grading_path = case_dir / "grading.json"
        with open(grading_path, "w", encoding="utf-8") as f:
            json.dump(grading, f, indent=2, ensure_ascii=False)
        judged += 1
        for sample in pending:
            with_items = new_sem["with_skill"].get(sample, [])
            without_items = new_sem["without_skill"].get(sample, [])
            avg_with = sum(float(s["score"]) for s in with_items) / len(with_items) if with_items else 0
            avg_without = (sum(float(s["score"]) for s in without_items) / len(without_items)
                           if without_items else 0)
            print(f"  ✅ {case_id}/{sample} 已写 | sem 平均分 with={avg_with:.2f} / without={avg_without:.2f}")

    print("\n" + "=" * 60)
    print(f"双盲判分完成：{judged} 个 case 判分，{skipped} 跳过，{failed} 失败")
    if failed:
        print("⚠ 存在失败 case，可重试（相同 --seed 下 A/B 标签与判分可复现）")

    await client.aclose()


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
