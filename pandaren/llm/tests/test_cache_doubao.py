"""explicit_cache_probe.py — 豆包（火山方舟）prompt cache 手工试用脚本

目的
    快速验证火山方舟 doubao-seed-2-0-pro 的 prompt cache 实际行为：
      - 隐式（无 cache_control）：服务端自动做 prefix cache，命中字段 cached_tokens
      - 显式（Anthropic 风格 cache_control: {"type": "ephemeral"}）：
        ⚠️ **火山方舟 OpenAI 兼容 chat 端点不解析 cache_control**
            （SDK 侧 VOLCENGINE_CHAT.explicit_cache == "none" 已明确声明）
        本探针用来**实测印证**该字段被静默忽略，而不是期望它生效。
        真正的显式缓存走 Context API（/context/create），属于另一套协议，
        本脚本不覆盖（见 docs/学习总结/prefix_cache 对应小节）。

    纯 httpx 直发 payload——探针刻意不走 SDK 的 `client.call()`，因为它要测的
    是**协议层**行为（cache_control 被如何处理、原始 usage 字段），走 SDK 会被
    capabilities 告警 / 字段归一化遮蔽。

与 pandaren/llm SDK 的关系
    - 探针**仍然直发 httpx**，不依赖 SDK 运行时
    - 但从 SDK **取"权威元数据"**：base_url 默认值、typed extras、capabilities 声明
    - 目的：SDK 端改 URL / 默认 extras 时，探针启动期 assert 立刻炸，防止双头漂移
    - 启动时打印 `capabilities.explicit_cache`，形成
      "SDK 声明预期 vs 探针实测结果" 的相互验证链

当前覆盖
    [doubao]  火山方舟 doubao-seed-2-0-pro-260215 —— OpenAI 兼容 /chat/completions
              A 组：无 cache_control                → 测隐式自动缓存命中率
              B 组：挂合法 cache_control            → **预期等同于 A 组**
                    （因为字段被兼容层静默剥离/忽略）
              C 组（--probe-invalid-cc）：挂非法 type 值
                    → **预期返回 200**（而不是百炼那样的 400）
                    - 返回 200 ⇒ 印证 SDK 声明 explicit_cache="none" 正确
                    - 返回 400 ⇒ SDK 声明漂移，请更新 VOLCENGINE_CHAT 常量

怎么用
    cd pandaren/llm/tests
    python3 explicit_cache_probe.py                        # A/B 组
    python3 explicit_cache_probe.py --probe-invalid-cc     # 顺带跑 C 组诊断
    python3 explicit_cache_probe.py --rounds 3             # 自定义显式轮次
    python3 explicit_cache_probe.py --bust-cache           # 强制第 1 次 miss

判定口径
    关键字段：usage.prompt_tokens_details
      - cached_tokens                 → 本次命中的 token 数（豆包唯一支持的 cache 字段）
      - cache_creation_input_tokens   → **豆包没有这个字段**（协议里就没有，不是漏抓）
    期待：
      - A 组：首次 cached=0；后续可能命中也可能不命中（隐式缓存本身就不稳定）
      - B 组：数字和 A 组分布一致（因为 cache_control 被忽略，相当于没挂）
      - C 组：HTTP 200，usage 和 A/B 组一样（静默忽略的铁证）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# ═══════════════════════════════════════════════════════════════
# 路径 & env 加载（复用 main_llm_test.py 的约定）
# ═══════════════════════════════════════════════════════════════

_THIS_FILE = Path(__file__).resolve()
_REAL_DIR = _THIS_FILE.parent                      # <repo>/pandaren/llm/tests
_ASSISTANT_DIR = _REAL_DIR.parent                  # <repo>/pandaren/llm
_REPO_ROOT = _ASSISTANT_DIR.parent.parent          # <repo>

# 注入 repo root 到 sys.path，保证 `import pandaren` 在 tests 目录下
# 直接运行时也能找到。和 main_llm_test.py 的约定保持一致。
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key and key not in os.environ:
                os.environ[key] = value


# 候选：模块目录 / 仓库根 / cwd 下的 .env.development（取第一个存在的）
_env_candidates = [
    _ASSISTANT_DIR / ".env.development",
    _REPO_ROOT / ".env.development",
    Path.cwd() / ".env.development",
]
for _c in _env_candidates:
    if _c.exists():
        _load_env_file(_c)
        break


# ═══════════════════════════════════════════════════════════════
# 颜色 & 打印
# ═══════════════════════════════════════════════════════════════

class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"


def section(title: str) -> None:
    print("\n" + "═" * 80)
    print(f"  {C.BOLD}{title}{C.RESET}")
    print("═" * 80)


# ═══════════════════════════════════════════════════════════════
# 真实业务 prompt / user message（和 main_llm_test 对齐，保证字节一致）
# ═══════════════════════════════════════════════════════════════

def load_real_intent_prompt() -> str:
    path = _ASSISTANT_DIR / "res_plugin" / "prompts" / "01-intent_instruction.md"
    return path.read_text(encoding="utf-8")


def build_real_user_message() -> str:
    sensor_data = {
        "master_behavior": {
            "trigger_reason": "gesture",
            "gesture": {"type": "wave", "score": 0.95},
            "posture": {"type": "", "score": 0},
            "gaze": {"count": 3, "duration_ms": 1500},
            "touch_sensors": {"pet_back": False, "pet_head": False},
        },
        "master_emotion": "happy",
        "master_asr_text": "晚上好！汤圆",
        "pet_behavior_type": "passive",
        "master_profile": {
            "age": 25, "gender": "female",
            "personality": "温和内向", "hobby": "阅读、画画",
            "relationship_stage": "熟悉期",
        },
        "pet_personality_profile": {
            "stability": "情绪基本稳定，轻微刺激不会触发强烈反应，但持续负向刺激会有影响",
            "social": "几乎无法克制靠近主人的冲动，长时间无互动会明显感到无聊",
            "affinity": "对熟悉的主人友善，但不会主动贴近陌生人",
            "brave": "对新事物会犹豫一下，但好奇心往往能战胜本能的谨慎",
            "openness": "极易被新刺激吸引，主动探索，行为充满活力",
        },
        "pet_mood": {
            "valence": "neutral", "arousal": "medium",
            "summary": "心情平和，对接下来的互动有所期待", "moodScore": 60,
        },
        "pet_state": {"intimacy": 65, "fatigue": 70},
    }
    payload = json.dumps(sensor_data, ensure_ascii=False, indent=2, sort_keys=True)
    return f"[传感器数据]\n{payload}"


# ═══════════════════════════════════════════════════════════════
# 核心：直发 chat/completions 并提取 cache 相关字段
# ═══════════════════════════════════════════════════════════════

async def post_chat(
    base_url: str,
    api_key: str,
    payload: dict[str, Any],
    timeout: float = 60.0,
) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    start = time.perf_counter()
    async with httpx.AsyncClient(timeout=timeout) as c:
        resp = await c.post(url, json=payload, headers=headers)
    latency_ms = (time.perf_counter() - start) * 1000

    out: dict[str, Any] = {
        "status": resp.status_code,
        "latency_ms": latency_ms,
        "body": None,
        "error": None,
    }
    try:
        out["body"] = resp.json()
    except Exception as e:
        out["error"] = f"decode: {e} | raw={resp.text[:200]}"
        return out
    if resp.status_code >= 400:
        out["error"] = f"HTTP {resp.status_code}: {json.dumps(out['body'], ensure_ascii=False)[:300]}"
    return out


def extract_cache_metrics(body: dict[str, Any]) -> dict[str, Any]:
    """从响应 body 里抠 cache 相关的 token 指标（跨厂商兜底版）。

    覆盖：
      - OpenAI / 百炼：usage.prompt_tokens_details.cached_tokens
                       usage.prompt_tokens_details.cache_creation_input_tokens（显式）
      - Anthropic 原生：usage.cache_read_input_tokens / cache_creation_input_tokens

    Returns:
        dict: prompt / cached / created / completion / source
              source 标记真正匹配到的字段路径，便于排查字段差异
    """
    usage = (body or {}).get("usage") or {}
    ptd = usage.get("prompt_tokens_details") or {}

    # ── cached（命中）——按优先级尝试 ──
    cached = 0
    source = "none"
    if ptd.get("cached_tokens") is not None:
        cached = int(ptd["cached_tokens"] or 0)
        source = "ptd.cached_tokens"
    elif usage.get("cache_read_input_tokens") is not None:
        cached = int(usage["cache_read_input_tokens"] or 0)
        source = "usage.cache_read_input_tokens"  # Anthropic 原生

    # ── created（本次写入缓存，显式独有）──
    created = 0
    if ptd.get("cache_creation_input_tokens") is not None:
        created = int(ptd["cache_creation_input_tokens"] or 0)
    elif usage.get("cache_creation_input_tokens") is not None:
        created = int(usage["cache_creation_input_tokens"] or 0)

    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)

    # ── 嗅探「未被识别的 cache 相关字段」──
    # 万一百炼将来改字段名（比如加 cache_write_tokens / cached_prompt_tokens），
    # 这里扫 usage / usage.prompt_tokens_details / body 顶层，把所有 key 里含
    # 'cache' 的条目都收集起来，除了已被上面规则消费掉的。
    known_keys = {
        "cached_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    }
    unknown_cache_fields: dict[str, Any] = {}

    def _scan(container: dict[str, Any] | None, prefix: str) -> None:
        if not isinstance(container, dict):
            return
        for k, v in container.items():
            kl = str(k).lower()
            if ("cache" in kl or "cached" in kl) and k not in known_keys:
                unknown_cache_fields[f"{prefix}{k}"] = v

    _scan(usage, "usage.")
    _scan(ptd, "usage.prompt_tokens_details.")
    _scan(body, "")

    return {
        "prompt": prompt,
        "cached": cached,
        "created": created,
        "completion": completion,
        "source": source,
        "unknown_cache_fields": unknown_cache_fields,
    }


def fmt_row(label: str, m: dict[str, Any], lat_ms: float, baseline_lat: float | None) -> str:
    parts = [
        f"{label:<28}",
        f"prompt={m['prompt']:>5}",
        f"cached={m['cached']:>5}",
        f"created={m['created']:>5}",
        f"comp={m['completion']:>4}",
        f"lat={lat_ms:>6.0f}ms",
    ]
    if baseline_lat and baseline_lat > 0:
        delta = (lat_ms - baseline_lat) / baseline_lat * 100
        if delta <= -20:
            tag = f"{C.GREEN}Δ=↓{-delta:>4.1f}%{C.RESET}"
        elif delta >= 20:
            tag = f"{C.RED}Δ=↑{delta:>4.1f}%{C.RESET}"
        else:
            tag = f"{C.DIM}Δ=≈{abs(delta):>4.1f}%{C.RESET}"
        parts.append(tag)
    src = m.get("source") or "none"
    parts.append(f"{C.DIM}({src}){C.RESET}")
    row = "  " + "  ".join(parts)
    unknown = m.get("unknown_cache_fields") or {}
    if unknown:
        extra = json.dumps(unknown, ensure_ascii=False)
        row += f"\n      {C.YELLOW}↳ 未识别 cache 字段: {extra}{C.RESET}"
    return row


# ═══════════════════════════════════════════════════════════════
# Provider 元数据 —— 单一来源：pandaren/llm SDK
# ═══════════════════════════════════════════════════════════════
#
# 为什么要从 SDK 取而不是在这里重新硬编码？
# ────────────────────────────────────────────────────────────
# 探针自己是直发 httpx 的（协议层探针必须保持裸态），但 base_url / 默认
# thinking 开关这类"元数据"如果两头各抄一份，SDK 改了探针没改，问题会在
# 用户反馈"跑不通"的时候才暴露。
#
# 做法：
#   1) **base_url 硬编码保留在本文件**（探针面对的就是固定 URL）
#   2) **启动期构造 SDK client，assert 其 _base_url == 本文件常量**
#      → SDK 改默认 URL 的那一刻，探针 import 就会炸（而不是静默漂移）
#   3) **厂商专属字段（thinking 开关等）用 SDK 的 typed extras 生成**
#      → 字段名 / 嵌套结构由 SDK 单点维护

from pandaren.llm import (  # noqa: E402  — 必须在 sys.path 注入之后
    OpenAICompatibleClient,
    EndpointCapabilities,
    VOLCENGINE_CHAT,
)
from pandaren.llm.providers import VolcEngineExtra  # noqa: E402


# ── 火山方舟 doubao ─────────────────────────────────────────────
DOUBAO_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DOUBAO_MODEL = "doubao-seed-2-0-code-preview-260215"
# Doubao Seed 系列自带深度思考，默认会产生 reasoning_tokens 并拉高延迟。
# 关掉后 completion_tokens 里不会出现 reasoning 部分，缓存对延迟的影响才好观察。
DOUBAO_EXTRA: dict[str, Any] = VolcEngineExtra(thinking_mode="disabled").as_extra_body()


# ═══════════════════════════════════════════════════════════════
# 启动期 SDK 对齐校验 —— SDK 改 URL / 改 capabilities 时立刻炸
# ═══════════════════════════════════════════════════════════════

def _assert_sdk_alignment() -> None:
    """用 SDK 的工厂方法构造一次 client，核对本文件常量是否和 SDK 同步。

    任何一端漂移都会在 import 这个模块的时候就 AssertionError，避免
    "探针跑一半才发现 URL 不对"。
    """
    c = OpenAICompatibleClient.for_volcengine(api_key="probe", model_name=DOUBAO_MODEL)
    assert c._base_url == DOUBAO_BASE_URL, (
        f"火山方舟 base_url 漂移：SDK={c._base_url!r} 探针={DOUBAO_BASE_URL!r}"
    )
    assert c.capabilities is VOLCENGINE_CHAT
    # 预期 SDK 把 volcengine chat 声明为 "none"（不解析 cache_control）。
    # 如果将来火山兼容层改行为（支持了 cache_control），SDK 会先升级 capabilities，
    # 这里的 assert 会立刻炸，提醒探针同步翻转 B/C 组判定口径。
    assert VOLCENGINE_CHAT.explicit_cache == "none", (
        "SDK 声明火山方舟支持 cache_control？探针假设被推翻，请同步更新 B/C 组判定口径"
    )


_assert_sdk_alignment()


def _messages_implicit(system_text: str, user_text: str) -> list[dict[str, Any]]:
    """隐式缓存：纯字符串 content，不带任何 cache 标记。"""
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]


def _messages_explicit(system_text: str, user_text: str) -> list[dict[str, Any]]:
    """显式缓存：system 用 content 数组形式，结尾加 cache_control。

    Anthropic 协议约定（百炼原生支持）：
      - cache_control 只能挂在 content block 上，单请求最多 4 个
      - 回溯范围最近 20 个 content block
      - 最低 1024 token 才能命中
    """
    return [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},  # 显示缓存，断点位置
                },
            ],
        },
        {"role": "user", "content": user_text},
    ]


def _messages_invalid_cc(system_text: str, user_text: str) -> list[dict[str, Any]]:
    """诊断构造：content 数组 + **非法** cache_control 值。

    用来判断服务端对 cache_control 是严格校验还是静默忽略：
      - 严格校验（真的解析协议）→ HTTP 400
      - 静默忽略（不解析该字段）→ 200，请求照常返回
    Anthropic 协议只接受 {"type": "ephemeral"} 一种值，其他的都是非法。
    """
    return [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": {
                        "type": "this-value-is-intentionally-invalid-xyz",
                    },
                },
            ],
        },
        {"role": "user", "content": user_text},
    ]


async def run_cache_compare(
    *,
    label: str,
    base_url: str,
    model: str,
    api_key_env: str,
    caps: EndpointCapabilities,  # SDK 侧对该 provider 的能力声明（预期值）
    rounds: int,
    debug_first: bool,
    bust_cache: bool,
    extra: dict[str, Any] | None = None,
    probe_invalid_cc: bool = False,
    dump_raw: bool = False,
) -> None:
    """跑一轮完整的「隐式对照 + 显式」对比。"""
    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        print(f"{C.RED}缺少 {api_key_env}{C.RESET}")
        return

    # ── SDK 侧对本 provider 的能力声明（探针据此写预期） ──────────
    print(
        f"  {C.CYAN}SDK 声明 capabilities.explicit_cache="
        f"{caps.explicit_cache!r}{C.RESET}  "
        f"{C.DIM}(provider={caps.provider}, "
        f"endpoint={caps.endpoint}){C.RESET}"
    )
    if caps.explicit_cache == "cache_control":
        print(f"  {C.DIM}→ 预期 B 组命中（cache_control 被服务端解析），"
              f"C 组返回 400{C.RESET}")
    else:
        print(f"  {C.DIM}→ 预期 B 组 cache_control 被静默忽略（C 组返回 200）；"
              f"命中只能靠隐式自动缓存{C.RESET}")

    system_text = load_real_intent_prompt()
    user_text = build_real_user_message()
    if bust_cache:
        import uuid
        system_text += f"\n\n<!-- cache-bust: {uuid.uuid4().hex} -->"
        print(f"  {C.DIM}(已启用 --bust-cache：本次 prompt 附带随机 tag，"
              f"保证第 1 次一定 miss){C.RESET}")
    print(f"  {C.DIM}system_prompt 长度: {len(system_text)} 字符 "
          f"(~{len(system_text)//2} tokens 估算){C.RESET}")
    print(f"  {C.DIM}user_message 长度:  {len(user_text)} 字符{C.RESET}")
    if extra:
        print(f"  {C.DIM}厂商特定 payload 字段: "
              f"{json.dumps(extra, ensure_ascii=False)}{C.RESET}")

    common: dict[str, Any] = {
        "model": model,
        "temperature": 0,
        "max_tokens": 80,
    }
    if extra:
        common.update(extra)

    # ── 组 A：隐式缓存对照（连发 2 次） ──────────────────────────
    section(f"A. 隐式缓存对照（{label}，无 cache_control）")
    baseline_lat: float | None = None
    for i in range(1, 3):
        if i > 1:
            await asyncio.sleep(1.0)
        payload = {**common, "messages": _messages_implicit(system_text, user_text)}
        r = await post_chat(base_url, api_key, payload)
        if r["error"]:
            print(f"  第{i}次: {C.RED}{r['error'][:200]}{C.RESET}")
            continue
        m = extract_cache_metrics(r["body"])
        row = fmt_row(f"隐式·第{i}次", m, r["latency_ms"],
                      baseline_lat if i > 1 else None)
        print(row)
        if dump_raw:
            body_dump = json.dumps(r["body"], ensure_ascii=False, indent=2)
            print(f"    {C.DIM}↳ raw body (第{i}次):{C.RESET}\n{body_dump}")
        if i == 1:
            baseline_lat = r["latency_ms"]
            if debug_first and not dump_raw:
                usage_dump = json.dumps(r["body"].get("usage"), ensure_ascii=False)
                print(f"    {C.DIM}↳ usage: {usage_dump}{C.RESET}")

    # ── 组 B：显式缓存（连发 rounds 次） ─────────────────────────
    section(f"B. 显式缓存（{label}，cache_control: ephemeral × {rounds}）")
    print(f"  {C.DIM}期待：第 1 次 created≈prompt / cached=0，后续 cached≈prompt{C.RESET}")
    baseline_lat = None
    hit_count = 0
    first_already_hit = False
    explicit_400 = False
    for i in range(1, rounds + 1):
        if i > 1:
            await asyncio.sleep(1.0)
        payload = {**common, "messages": _messages_explicit(system_text, user_text)}
        r = await post_chat(base_url, api_key, payload)
        if r["error"]:
            if r.get("status") == 400:
                explicit_400 = True
            print(f"  第{i}次: {C.RED}{r['error'][:220]}{C.RESET}")
            continue
        m = extract_cache_metrics(r["body"])
        if m["cached"] > 0:
            hit_count += 1
            if i == 1:
                first_already_hit = True
        row = fmt_row(f"显式·第{i}次", m, r["latency_ms"],
                      baseline_lat if i > 1 else None)
        print(row)
        if dump_raw:
            body_dump = json.dumps(r["body"], ensure_ascii=False, indent=2)
            print(f"    {C.DIM}↳ raw body (第{i}次):{C.RESET}\n{body_dump}")
        if i == 1:
            baseline_lat = r["latency_ms"]
            if debug_first and not dump_raw:
                usage_dump = json.dumps(r["body"].get("usage"), ensure_ascii=False)
                print(f"    {C.DIM}↳ usage: {usage_dump}{C.RESET}")

    # ── 汇总判定 ───────────────────────────────────────────────
    if explicit_400:
        print(f"\n  {C.RED}✗ 显式 cache_control 被 {label} 拒绝（HTTP 400）{C.RESET}")
        print(f"  {C.DIM}说明该 provider 的 OpenAI 兼容层不解析 cache_control，"
              f"需要走原生 Context API / 或改用纯字符串 content{C.RESET}")
        if caps.explicit_cache == "cache_control":
            print(f"  {C.RED}⚠ 与 SDK 声明冲突：capabilities.explicit_cache="
                  f"'cache_control' 但服务端返回 400，请检查 SDK capabilities "
                  f"常量是否过期{C.RESET}")
        return

    # 判定语义随 SDK 声明翻转：
    #   caps="cache_control" → 命中才是"显式缓存起效"的证据（绿色 ✓）
    #   caps="none"          → 命中 ≠ 显式缓存起效，只能说明"隐式自动缓存"也命中了
    #                          （B 组跟 A 组数字应当同分布；cache_control 被静默忽略）
    is_explicit_expected = caps.explicit_cache == "cache_control"

    if is_explicit_expected:
        verdict_color = C.GREEN if hit_count >= 1 else C.YELLOW
    else:
        # 豆包这类 provider：命中多不代表 cache_control 生效，着色降级为信息色
        verdict_color = C.CYAN if hit_count >= 1 else C.DIM
    print(
        f"\n  {C.BOLD}显式组汇总{C.RESET}：共 {rounds} 次里 "
        f"{verdict_color}{hit_count}{C.RESET} 次命中 cached>0"
    )
    if first_already_hit:
        print(
            f"  {C.YELLOW}注：第 1 次就命中说明本次运行复用了上次脚本留下的"
            f"缓存（TTL 通常 5 分钟）。{C.RESET}\n"
            f"  {C.DIM}想看到「第 1 次 miss、后续 hit」的干净波形，"
            f"加 --bust-cache 或等缓存过期再跑。{C.RESET}"
        )

    if is_explicit_expected:
        if hit_count >= 1:
            print(f"  {C.GREEN}✓ B 组观察到命中{C.RESET} "
                  f"{C.DIM}（{label} 至少在某条路径上做了缓存）{C.RESET}")
            print(f"  {C.DIM}判定 cache_control 是否真被解析，建议加 "
                  f"--probe-invalid-cc 跑一组非法值诊断（C 组）。{C.RESET}")
        else:
            print(f"  {C.YELLOW}⚠ 显式未观察到命中，可能原因：{C.RESET}")
            print(f"    {C.DIM}- provider 不认 cache_control，静默忽略（降级为纯字符串）"
                  f"→ 字段是否等同隐式？对比 A 组数据{C.RESET}")
            print(f"    {C.DIM}- prefix 不足 1024 tokens（本 prompt 约 5k，理论足够）{C.RESET}")
            print(f"    {C.DIM}- 第 1 次 created_tokens 是否 ≈ prompt？是则"
                  f"写入成功，只是后续没命中，重跑即可{C.RESET}")
    else:
        # caps.explicit_cache == "none"：B 组数字**应当**和 A 组同分布
        print(f"  {C.DIM}SDK 声明该 provider 不解析 cache_control（explicit_cache='none'）。"
              f"{C.RESET}")
        print(f"  {C.DIM}正确预期：B 组的 cached/created 分布应与 A 组一致 —— "
              f"字段被兼容层静默剥离，留下的只是隐式自动缓存的痕迹。{C.RESET}")
        if any(m > 0 for m in [hit_count]):
            print(f"  {C.CYAN}ℹ B 组有命中，但归因于隐式自动缓存（和 A 组同机制），"
                  f"**不是** cache_control 起效。{C.RESET}")
        print(f"  {C.DIM}想从协议层印证"
              f"「cache_control 真的被忽略而不是部分生效」，"
              f"加 --probe-invalid-cc 跑 C 组。{C.RESET}")

    # ── 组 C：非法 cache_control 诊断（可选） ────────────────────
    if probe_invalid_cc:
        section(f"C. 非法 cache_control 诊断（{label}）")
        print(f"  {C.DIM}把 cache_control 的 type 改成协议里不存在的值，"
              f"用 HTTP 状态码判断服务端是否真的解析该字段：{C.RESET}")
        print(f"    {C.DIM}- 返回 400（字段被校验）  ⇒ 服务端真的在解析协议{C.RESET}")
        print(f"    {C.DIM}- 返回 200（照常响应）     ⇒ 服务端静默忽略，"
              f"B 组的命中全靠隐式缓存{C.RESET}")
        payload = {
            **common,
            "messages": _messages_invalid_cc(system_text, user_text),
        }
        r = await post_chat(base_url, api_key, payload)
        status = r.get("status")

        # measured: "cache_control" / "none" / None（不确定）
        measured: str | None = None

        if r["error"]:
            if status == 400:
                measured = "cache_control"
                print(f"  {C.GREEN}✓ 返回 400 —— {label} 的兼容层"
                      f"**严格校验** cache_control 值{C.RESET}")
                print(f"    {C.DIM}body: {r['error'][:240]}{C.RESET}")
                print(f"  {C.DIM}结论：cache_control 协议被真正解析，"
                      f"B 组的命中是显式缓存的功劳。{C.RESET}")
            else:
                print(f"  {C.YELLOW}HTTP {status}（非 400）：{r['error'][:200]}"
                      f"{C.RESET}")
                print(f"  {C.DIM}结论不明确，可能是配额/路由/其他服务端异常。"
                      f"{C.RESET}")
        else:
            m = extract_cache_metrics(r["body"])
            row = fmt_row("C·非法cc", m, r["latency_ms"], None)
            print(row)
            if dump_raw:
                body_dump = json.dumps(r["body"], ensure_ascii=False, indent=2)
                print(f"    {C.DIM}↳ raw body (C 组):{C.RESET}\n{body_dump}")
            measured = "none"
            print(f"  {C.YELLOW}✗ 返回 200 —— {label} 的兼容层"
                  f"**静默忽略** cache_control{C.RESET}")
            print(f"  {C.DIM}结论：cache_control 协议没被解析，"
                  f"B 组的 cached>0 实际上是 A 组那套"
                  f"「隐式自动缓存」的命中。{C.RESET}")

        # ── SDK capabilities 对齐校验 ──
        if measured is not None and caps.explicit_cache in ("cache_control", "none"):
            if measured == caps.explicit_cache:
                print(f"  {C.GREEN}◎ SDK capabilities 对齐：声明="
                      f"{caps.explicit_cache!r}，实测={measured!r}{C.RESET}")
            else:
                print(f"  {C.RED}⚠ SDK capabilities 漂移：声明="
                      f"{caps.explicit_cache!r}，实测={measured!r} —— "
                      f"请更新 pandaren/llm/capabilities.py 的对应常量{C.RESET}")


async def test_doubao(
    rounds: int,
    debug_first: bool,
    bust_cache: bool,
    probe_invalid_cc: bool = False,
    dump_raw: bool = False,
) -> None:
    section(f"▶ 火山方舟 {DOUBAO_MODEL}（OpenAI 兼容 chat；cache_control 预期被静默忽略，已关思考）")
    await run_cache_compare(
        label=DOUBAO_MODEL,
        base_url=DOUBAO_BASE_URL,
        model=DOUBAO_MODEL,
        api_key_env="VOLCENGINE_API_KEY",
        caps=VOLCENGINE_CHAT,
        rounds=rounds,
        debug_first=debug_first,
        bust_cache=bust_cache,
        extra=DOUBAO_EXTRA,
        probe_invalid_cc=probe_invalid_cc,
        dump_raw=dump_raw,
    )


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════

def main() -> None:
    ap = argparse.ArgumentParser(description="豆包（火山方舟）prompt cache 手工试用")
    ap.add_argument("--rounds", type=int, default=3,
                    help="显式组连发次数（默认 3）")
    ap.add_argument("--no-debug", action="store_true",
                    help="不打印每组第 1 次的原始 usage")
    ap.add_argument("--bust-cache", action="store_true",
                    help="给 system 追加随机 tag，强制本次第 1 次 miss、后续 hit"
                         "（用于看完整波形；不影响业务 prompt 结构）")
    ap.add_argument("--probe-invalid-cc", action="store_true",
                    help="额外跑 C 组诊断：用非法 cache_control 值请求，"
                         "按 HTTP 状态码判断服务端是严格校验还是静默忽略"
                         "（豆包预期返回 200，印证 SDK 声明 explicit_cache='none'）")
    ap.add_argument("--dump-raw", action="store_true",
                    help="打印每次请求响应的完整 JSON body（不只 usage），"
                         "用来发现非标 cache 字段")
    args = ap.parse_args()

    debug_first = not args.no_debug

    async def run() -> None:
        await test_doubao(
            rounds=args.rounds,
            debug_first=debug_first,
            bust_cache=args.bust_cache,
            probe_invalid_cc=args.probe_invalid_cc,
            dump_raw=args.dump_raw,
        )

    asyncio.run(run())


if __name__ == "__main__":
    main()
