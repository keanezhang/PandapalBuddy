#!/usr/bin/env python3
"""scripts/measure_context_budget.py — 上下文预算真实测量台。

目的：把 config 里所有"按比例拍"的 token 预算，换成**实测值**。

全部测量用真实 BPE tokenizer（cl100k_base，与 run_local._build_token_estimator
注入的一致），不依赖任何代码里的常量或注释。

用法：
    .venv/bin/python scripts/measure_context_budget.py

输出：
    A. system_prompt 实测（coding / office，逐成分拆解）
    B. static_context 实测（工具目录 / 技能 / 子 Agent）
    C. tool_schema 实测（逐工具 + 合计 + 真实 tools=[...] payload）
    D. 一次请求的固定开销合计 vs 配置配额（最关键的一张表）
    E. RESERVED（LLMDropSummarizer 摘要）实测
    F. TOOL_RESULT_CAP 截断效应实测（含中文截断失效验证）
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="    [log] %(name)s: %(message)s")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# ── 真实 tokenizer ─────────────────────────────────────────────────────────
import tiktoken  # noqa: E402

_ENC = tiktoken.get_encoding("cl100k_base")


def tok(text: str) -> int:
    if not text:
        return 0
    return len(_ENC.encode(text))


def tok_json(obj: object) -> int:
    """按真实 HTTP body 的序列化方式计数（ensure_ascii=False）。"""
    return tok(json.dumps(obj, ensure_ascii=False, default=str))


def line(label: str, chars: int, tokens: int, extra: str = "") -> None:
    print(f"  {label:<44}{chars:>9,}{tokens:>9,}   {extra}")


HEAD = f"  {'成分':<44}{'字符':>9}{'真实token':>9}   备注"


def _fallback_payload(tool) -> dict:
    """SchemaBuilder 不可用时的兜底：按 OpenAI tools 格式直接拼。"""
    params = getattr(tool, "input_schema", None)
    if params is not None and not isinstance(params, (dict, list, str, int, float, bool, type(None))):
        if hasattr(params, "model_json_schema"):
            try:
                params = params.model_json_schema()
            except Exception:  # noqa: BLE001
                params = str(params)
        elif hasattr(params, "items"):
            params = {k: v for k, v in params.items()}
        else:
            params = str(params)
    return {"type": "function", "function": {
        "name": getattr(tool, "name", "?"),
        "description": getattr(tool, "description", "") or "",
        "parameters": params if isinstance(params, dict) else {"type": "object", "properties": {}},
    }}


assembler = None

# ── G12：--check 断言模式（治理进度红绿灯）─────────────────────────────────
# 用法：.venv/bin/python scripts/measure_context_budget.py --check
# 退出码 0 = 全部实占 ≤ 配额；1 = 有超支项（治理未完成）
CHECK: bool = "--check" in sys.argv
FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    """记录一条断言结果（--check 模式下汇总后决定退出码）。"""
    if not cond:
        FAILURES.append(msg)


# ── 治理后的目标配额（B1 落地：固定槽位用实测绝对值）──────────────────────
NEW_SYS_SLOT = 24_000     # 实测 coding 17,590 → 留 ~40% 余量
NEW_TOOL_SLOT = 8_000     # 实测 13 个工具 3,374 → 留 2.4x 余量（MCP 另需封顶）
NEW_RECALL_SLOT = 0       # 功能已废弃
NEW_RESERVED = 512        # LLMDropSummarizer 实测 max_tokens=512
NEW_REINJECT = 8_000      # 原 50,000 是危险默认值（阈值 65,000 时占 77%）


# ═══════════════════════════════════════════════════════════════════════════
print("=" * 118)
print("A. system_prompt 实测")
print("=" * 118)
print(HEAD)

env_block = ""
try:
    from pandapal.local.run_local import _get_environment_block
    env_block = _get_environment_block(REPO)
except Exception as exc:  # noqa: BLE001
    print(f"  [warn] env_block 取不到：{type(exc).__name__}: {exc}")

try:
    from pandapal.local import prompts
    from pandapal.local.prompt_fragments import DEFAULT_FRAGMENTS, PromptAssembler

    print("\n  ── 基础 prompt（base_prompts）──")
    for name, text in prompts.PROMPTS.items():
        line(f"PROMPTS['{name}']", len(text), tok(text))

    print("\n  ── 运行环境块 ──")
    line("env_block", len(env_block), tok(env_block))

    print("\n  ── 片段（按 mode 注入）──")
    assembler = PromptAssembler(
        base_prompts=prompts.PROMPTS,
        env_block=env_block,
        fragments=DEFAULT_FRAGMENTS,
        work_dir=REPO,
    )
    frag_text = getattr(assembler, "_fragment_text", {})
    for spec in DEFAULT_FRAGMENTS:
        body = (frag_text.get(spec.filename) or "").strip()
        flag = "命中" if body else "缺失"
        line(f"{spec.filename} [{flag}] modes={sorted(spec.modes)}",
             len(body), tok(body))

    print("\n  ── 完整 system_prompt（并集 = base + env + 该 mode 的片段）──")
    for mode in ("coding", "office"):
        sp = assembler.get(mode)
        line(f"system_prompt[{mode}] 合计", len(sp), tok(sp))
except Exception as exc:  # noqa: BLE001
    import traceback
    print(f"  [FAIL] {type(exc).__name__}: {exc}")
    traceback.print_exc(limit=3)


# ═══════════════════════════════════════════════════════════════════════════
print()
print("=" * 118)
print("B. static_context 实测（拼进 system message 末尾的三段 XML）")
print("=" * 118)
print(HEAD)

skill_text = agent_text = tool_catalog_text = ""
skill_summaries = []
try:
    from pandaren.engine.message_builder import MessageBuilder
    from pandaren.skill.loader import load_skills_from_dir
    from pandaren.skill.models import SkillSource
    from pandaren.skill.registry import SkillRegistry

    skills = load_skills_from_dir(
        REPO / "pandapal" / "resources" / "skills", source=SkillSource.PROJECT
    )
    reg = SkillRegistry()
    reg.register_skills(list(skills))
    skill_summaries = list(reg.build_skill_summaries())
    print(f"  [info] SkillRegistry: 注册 {reg.skill_count()} 个 skill，"
          f"build_skill_summaries() 返回 {len(skill_summaries)} 条（1% 预算裁剪后）")
    skill_text = MessageBuilder.build_static_context_str(
        skill_summaries=skill_summaries
    ) or ""
except Exception as exc:  # noqa: BLE001
    import traceback
    print(f"  [warn] SkillRegistry: {type(exc).__name__}: {exc}")
    traceback.print_exc(limit=2)

try:
    from pandaren.sub_agent.loader import load_agents_from_dir
    from pandaren.sub_agent.models import SubAgentSource

    bps = load_agents_from_dir(
        REPO / "pandapal" / "resources" / "agents", source=SubAgentSource.BUILTIN
    )
    print(f"  [info] SubAgent 蓝图: {len(bps)} 个（registry.register 需 materialize() 对象，"
          f"此处只量字段规模）")
    raw = 0
    for bp in bps:
        desc = getattr(bp, "description", "") or ""
        wtu = getattr(bp, "when_to_use", "") or ""
        raw += len(str(getattr(bp, "name", ""))) + len(str(desc)) + len(str(wtu))
        print(f"    {getattr(bp, 'agent_id', '?')}: name={len(str(getattr(bp,'name','')))} "
              f"desc={len(str(desc))} when_to_use={len(str(wtu))} 字符")
    agent_text = "x" * raw
except Exception as exc:  # noqa: BLE001
    import traceback
    print(f"  [warn] SubAgent: {type(exc).__name__}: {exc}")
    traceback.print_exc(limit=2)

line("build_skill_summaries()", len(skill_text), tok(skill_text))
line("build_agent_summaries()", len(agent_text), tok(agent_text))
total_static = tok(skill_text) + tok(agent_text) + tok(tool_catalog_text)
line("static_context 小计（不含工具目录）", len(skill_text) + len(agent_text), total_static)


# ═══════════════════════════════════════════════════════════════════════════
print()
print("=" * 118)
print("C. tool_schema 实测（真实 Tool 实例 → 真实 OpenAI tools=[...] payload）")
print("=" * 118)
print(HEAD)

tools_payload: list[dict] = []
tools = []
try:
    import importlib
    import pkgutil

    import pandapal.tools as _tp

    print("  ── 工具模块加载明细 ──")
    for mi in pkgutil.iter_modules(_tp.__path__):
        if not mi.name.endswith("_tools"):
            continue
        try:
            mod = importlib.import_module(f"pandapal.tools.{mi.name}")
            fns = [a for a in dir(mod) if a.startswith("get_") and a.endswith("_tools")]
            print(f"    {mi.name:<28} get_*_tools = {fns or '（无）'}")
        except Exception as exc:  # noqa: BLE001
            print(f"    {mi.name:<28} ❌ 导入失败 {type(exc).__name__}: {exc}")

    from pandapal.tools import get_all_tools

    tools = list(get_all_tools())
    print(f"\n  get_all_tools() → {len(tools)} 个（仅 _tools.py + get_*_tools 约定）")

    # Provider 类工具（依赖注入式，app.py 在 container.start_all() 之后注册）
    class _Dummy:
        def __getattr__(self, k):  # noqa: ANN401
            return lambda *a, **kw: None

    provider_specs = [
        ("pandapal.tools.agent_task_tools", "AgentTaskTools"),
        ("pandapal.tools.scheduler_tools", "SchedulerTools"),
        ("pandapal.tools.progress_tools", "ProgressTools"),
        ("pandapal.tools.app_data_tools", "AppDataTools"),
    ]
    import inspect

    for mod_name, cls_name in provider_specs:
        try:
            mod = importlib.import_module(mod_name)
            cls = getattr(mod, cls_name)
            params = [p for p in inspect.signature(cls.__init__).parameters if p != "self"]
            inst = cls(**{p: _Dummy() for p in params})
            extra = list(inst.get_tools())
            names = [getattr(t, "name", "?") for t in extra]
            print(f"    {cls_name:<18} {len(extra)} 个 → {names}")
            tools.extend(extra)
        except Exception as exc:  # noqa: BLE001
            print(f"    {cls_name:<18} ❌ {type(exc).__name__}: {exc}")

    print(f"\n  工具总数（真实全量）: {len(tools)}\n")
except Exception as exc:  # noqa: BLE001
    print(f"  [FAIL] get_all_tools: {type(exc).__name__}: {exc}")

if tools:
    try:
        from pandaren.tool.exposure.schema_builder import SchemaBuilder

        sb = SchemaBuilder.__new__(SchemaBuilder)   # 只借用 _to_schema，不需要 __init__
    except Exception:  # noqa: BLE001
        sb = None

    rows = []
    for t in tools:
        if sb is not None:
            try:
                sch = sb._to_schema(t)                     # noqa: SLF001
                payload = {"type": "function", "function": {
                    "name": sch.name, "description": sch.description,
                    "parameters": sch.parameters}}
            except Exception:  # noqa: BLE001
                payload = _fallback_payload(t)
        else:
            payload = _fallback_payload(t)
        tools_payload.append(payload)
        rows.append((payload["function"]["name"], tok_json(payload),
                     len(payload["function"]["description"] or "")))

    rows.sort(key=lambda r: -r[1])
    for name, tk, dl in rows:
        print(f"  {name:<44}{dl:>9,}{tk:>9,}   (字符 = description)")
    tools_total = sum(r[1] for r in rows)
    payload_total = tok_json(tools_payload)
    print(f"\n  单个 schema token 合计            {tools_total:>9,}")
    line("真实 tools=[...] payload（含 JSON 结构）",
         len(json.dumps(tools_payload, ensure_ascii=False)), payload_total)
    line("平均每个工具", 0, round(tools_total / max(1, len(rows))))


# ═══════════════════════════════════════════════════════════════════════════
print()
print("=" * 118)
print("D. ★ 「固定开销」vs「配额」—— 治理前 / 治理后对照")
print("=" * 118)

meas_sys_coding = tok(assembler.get("coding")) if assembler is not None else 0
meas_sys_office = tok(assembler.get("office")) if assembler is not None else 0
meas_static = total_static
meas_tools = tools_total if tools else 0

# 治理前：run_local.py 原配置（比例口径，未实测校准）
OLD = {"CW": 100_000, "sys": int(100_000 * 0.15), "tool": int(100_000 * 0.10),
       "recall": int(100_000 * 0.10), "conv": int(100_000 * 0.65)}
# 治理后：绝对槽位（B1 落地后）
NEW = {"CW": 100_000, "sys": NEW_SYS_SLOT, "tool": NEW_TOOL_SLOT,
       "recall": NEW_RECALL_SLOT,
       "conv": 100_000 - NEW_SYS_SLOT - NEW_TOOL_SLOT}

print(f"\n  {'项目':<26}{'治理前':>12}{'治理后':>12}{'实测占用':>12}   判定")
print("  " + "-" * 84)
_rows = [
    ("system_prompt slot", OLD["sys"], NEW["sys"], meas_sys_coding + meas_static),
    ("tool_schema slot", OLD["tool"], NEW["tool"], meas_tools),
    ("recall slot", OLD["recall"], NEW["recall"], 0),
    ("conversation slot", OLD["conv"], NEW["conv"], None),
    ("→ compact_threshold", OLD["conv"], NEW["conv"], None),
]
for _name, _old, _new, _actual in _rows:
    if _actual is None:
        print(f"  {_name:<26}{_old:>12,}{_new:>12,}{'—':>12}")
    else:
        _ok = "✅ 实占 ≤ 配额" if _actual <= _new else f"❌ 超 {_actual / max(1, _new):.2f}x"
        print(f"  {_name:<26}{_old:>12,}{_new:>12,}{_actual:>12,}   {_ok}")

fixed_coding = meas_sys_coding + meas_static + meas_tools
_old_idle = (OLD["recall"] + max(0, OLD["tool"] - meas_tools)
             - max(0, meas_sys_coding + meas_static - OLD["sys"]))
print(f"""
  固定开销合计 [coding]   = {fixed_coding:,} token
  固定开销合计 [office]   = {meas_sys_office + meas_static + meas_tools:,} token
  治理前被闲置的配额      = {_old_idle:,} token
  compact_threshold       = {OLD['conv']:,} → {NEW['conv']:,}（+{NEW['conv'] - OLD['conv']:,}）
""")

# ── G12 断言 ──
check(meas_sys_coding + meas_static <= NEW["sys"],
      f"system_prompt[coding] 实占 {meas_sys_coding + meas_static:,} "
      f"> 配额 {NEW['sys']:,}（原配额 {OLD['sys']:,} 时超 "
      f"{(meas_sys_coding + meas_static) / max(1, OLD['sys']):.2f}x）")
check(meas_tools <= NEW["tool"],
      f"tool_schema 实占 {meas_tools:,} > 配额 {NEW['tool']:,}")
check(NEW["recall"] == NEW_RECALL_SLOT, "recall 配额应为 0（功能已废弃）")
check(NEW["conv"] > 0, "conversation 配额应为正")


# ═══════════════════════════════════════════════════════════════════════════
print()
print("=" * 118)
print("E. RESERVED 实测（LLMDropSummarizer 摘要输出上限）")
print("=" * 118)
try:
    import inspect

    from pandapal.local.llm_policies import LLMDropSummarizer

    sig = inspect.signature(LLMDropSummarizer.__init__)
    print(f"  LLMDropSummarizer.__init__ 默认参数: "
          f"max_summary_tokens={sig.parameters['max_summary_tokens'].default}")
    print(f"  DEFAULT_SUMMARY_PROMPT 字符={len(LLMDropSummarizer.DEFAULT_SUMMARY_PROMPT)} "
          f"token={tok(LLMDropSummarizer.DEFAULT_SUMMARY_PROMPT)}")
    # 摘要实际发给 LLM 的 max_tokens 是多少？
    src = inspect.getsource(LLMDropSummarizer.summarize)
    for ln in src.splitlines():
        if "max_tokens" in ln or "ModelSettings" in ln:
            print(f"    {ln.strip()}")
except Exception as exc:  # noqa: BLE001
    print(f"  [FAIL] {type(exc).__name__}: {exc}")


# ═══════════════════════════════════════════════════════════════════════════
print()
print("=" * 118)
print("F. TOOL_RESULT_CAP 截断效应实测（真 tokenizer 判定 + 截断后真实 token）")
print("=" * 118)
try:
    from pandaren.memory.compaction.micro_compact import MicroCompactor
    from pandaren.memory.models import MessageDict   # noqa: F401

    class TikEstimator:
        """真实 tokenizer 的 TokenEstimator 实现（与 TiktokenEstimator 同口径）。"""

        def estimate(self, messages: list) -> int:
            total = 0
            for m in messages:
                c = m.get("content", "")
                if isinstance(c, str):
                    total += tok(c)
                elif isinstance(c, list):
                    total += sum(tok(str(p)) for p in c)
                tc = m.get("tool_calls")
                if tc:
                    total += tok(str(tc))
            return max(1, total)

    SAMPLE_CAP = 20_000      # 代码默认 DEFAULT_MICROCOMPACT_SINGLE_RESULT_MAX_TOKENS
    mc = MicroCompactor(single_result_max_tokens=SAMPLE_CAP, token_estimator=TikEstimator())

    cases = {
        "英文（4字符/token）": ("the quick brown fox jumps over the lazy dog. " * 8_000),
        "代码（~3.5字符/token）": ("def f(x):\n    return x + 1\n" * 10_000),
        "中文（1字≈1token）": ("这是一段中文内容用于测试截断行为。" * 4_000),
    }
    print(f"  单条上限 SAMPLE_CAP = {SAMPLE_CAP:,} token")
    print(f"\n  {'内容类型':<24}{'原始token':>11}{'截断后token':>13}{'是否达标':>10}")
    for label, text in cases.items():
        before = tok(text)
        out = mc.truncate_single_result_if_needed(text)
        after = tok(out) if isinstance(out, str) else tok(str(out))
        ok = "✅" if after <= SAMPLE_CAP else f"❌ 超 {after/SAMPLE_CAP:.1f}x"
        print(f"  {label:<24}{before:>11,}{after:>13,}{ok:>10}")
        # 治理后：截断必须收敛到 cap 以内（此前中文会「越截越大」）
        check(after <= SAMPLE_CAP,
              f"TOOL_RESULT_CAP 截断未收敛 [{label}]：{after:,} > {SAMPLE_CAP:,}")
except Exception as exc:  # noqa: BLE001
    import traceback
    print(f"  [FAIL] {type(exc).__name__}: {exc}")
    traceback.print_exc(limit=3)


# ═══════════════════════════════════════════════════════════════════════════
print()
print("=" * 118)
print("G. ToolBudget.enforce 裁剪行为实测（含 MCP 模拟）")
print("=" * 118)
try:
    from pandaren.tool.definition.tool_schema import ToolSchema
    from pandaren.tool.exposure.budget import ToolBudget

    base_schemas = []
    for _p in tools_payload:
        _f = _p["function"]
        base_schemas.append(ToolSchema(
            name=_f["name"], description=_f["description"], parameters=_f["parameters"],
        ))

    # 模拟 MCP：10 个"ALWAYS 档"工具（名字 mcp_ 前缀，按字母序落在 always 段内）
    mcp_schemas = [
        ToolSchema(
            name=f"mcp_server{i}__do_thing",
            description="这是某个 MCP 服务器暴露的工具描述。" * 6,
            parameters={"type": "object", "properties": {"x": {"type": "string"}}},
        )
        for i in range(10)
    ]

    budget = ToolBudget()
    print(f"  max_always_count（硬下限）= {budget.max_always_count}\n")
    for label, sch in [("仅应用 13 个", list(base_schemas)),
                       ("13 应用 + 10 MCP = 23", base_schemas + mcp_schemas)]:
        b = ToolBudget()
        out = b.enforce(list(sch), tool_schema_tokens=10_000)
        kept = [s.name for s in out]
        before = sum(b._estimate_tokens(s) for s in sch)          # noqa: SLF001
        after = sum(b._estimate_tokens(s) for s in out)           # noqa: SLF001
        n_app = sum(1 for n in kept if not n.startswith("mcp_"))
        n_mcp = sum(1 for n in kept if n.startswith("mcp_"))
        print(f"  [{label}]  预算=10,000")
        print(f"      输入 {len(sch):>3} 个 / {before:>6,} token  →  "
              f"保留 {len(out):>3} 个 / {after:>6,} token")
        print(f"      应用工具存活 {n_app}/{len(base_schemas)}   "
              f"MCP 存活 {n_mcp}/{len(mcp_schemas)}")
except Exception as exc:  # noqa: BLE001
    import traceback
    print(f"  [FAIL] {type(exc).__name__}: {exc}")
    traceback.print_exc(limit=3)


print()
print("=" * 118)
print("完成。所有数字均由 cl100k_base 真实编码器计数。")
print("=" * 118)

# ── G12：--check 汇总 ───────────────────────────────────────────────────────
if CHECK:
    print()
    print("=" * 118)
    if FAILURES:
        print(f"❌ --check 失败 {len(FAILURES)} 项（治理未完成）：")
        for _f in FAILURES:
            print(f"   · {_f}")
        print("=" * 118)
        sys.exit(1)
    print("✅ --check 全部通过：各项实占 ≤ 对应配额")
    print("=" * 118)
