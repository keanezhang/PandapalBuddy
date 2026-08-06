"""
Pandaren Agent SDK · Prefix Cache v1.0 真实 LLM 端到端验证

目标
----
在真实 LLM（DashScope Qwen / DeepSeek）+ 真实 res_plugin 资源
（tools / skills / sub-agents）下，跨多轮对话自动验证 Prefix Cache 不变量，
并读出 provider 返回的 `usage.prompt_tokens_details.cached_tokens`
作为真实命中率指标。

与 mock 测试（pandaren/llm/tests/test_prefix_cache_mock.py）的分工
  mock:  白盒单元，断言框架内部拼装函数的不变量（0 成本、CI 可跑）
  live:  黑盒端到端，断言真实发出去的 messages[0] 是否跨轮字节级一致，
         并收集真实命中率 —— 证明"LLM 侧真的按我们的设计吃到了缓存"

覆盖的不变量
  PC1  跨轮 messages[0].content（system）字节级一致
  PC3  存在动态内容时，最后一条 role=user 且以 <system-reminder> 开头
  PC6  即使触发 discovered，system 中 <available_tools> 清单仍稳定
  真实缓存命中: 读每轮 response.usage.prompt_tokens_details.cached_tokens

运行
----
  cd pandaren/llm/tests && python test_prefix_cache_live.py              # 交互模式（stream）
  cd pandaren/llm/tests && python test_prefix_cache_live.py --mode run   # 非流式

交互模式：
  - 每输入一条任务立即执行、立即打印该轮 PC 小结
  - 整场 REPL 共用同一个 session_id，可前后呼应测记忆召回
  - 直接回车 / :q / :quit / exit / Ctrl-C / Ctrl-D 退出，退出时打印总汇总

前置
----
  仓库根目录的 .env.development 里配好 DASHSCOPE_API_KEY
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator

# ─── Windows 控制台 UTF-8 ─────────────────────────────────────
if sys.platform == "win32" and "pytest" not in sys.modules:  # pytest 下交给 capture，避免拆台
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ─── sys.path 注入：repo_root ──────────────────────────────────
_THIS = Path(__file__).resolve()
_REPO_ROOT = _THIS.parent.parent.parent.parent  # tests/ → llm/ → pandaren/ → <repo>
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ─── 加载 .env（main_sdk_test 的同款解析器）──────────────────
def _load_env(env_path: Path) -> None:
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


_load_env(_REPO_ROOT / ".env.development")

# ═══ 直接复用 main_sdk_test 搭好的 build_agent / run_task_* ═══
#   这样 tools/skills/sub-agents 全部从 res_plugin 真实加载，
#   和你日常调试入口行为完全一致，零分叉。
try:
    import main_test as sdk_main  # noqa: E402
except ModuleNotFoundError:  # main_test.py 是旧调试工程的入口，未随迁入本仓
    import pytest

    pytest.skip("main_test 模块不在本仓库，跳过前缀缓存真实探测", allow_module_level=True)
from pandaren.llm.client import OpenAICompatibleClient  # noqa: E402
from pandaren.llm.types import LLMStreamChunk  # noqa: E402


# ════════════════════════════════════════════════════
#  探针数据结构
# ════════════════════════════════════════════════════

@dataclass
class TurnRecord:
    """单次 LLM 调用的观测记录。"""
    call_idx: int                           # 第几次 LLM 调用（跨 turn 累计）
    sys_hash: str                           # system.content 的 sha256[:12]
    sys_len: int                            # system.content 字节数
    msg_count: int                          # 本次 messages 总条数
    last_is_reminder_user: bool             # 最后一条是否 user + <system-reminder>
    available_tools_hash: str               # system 中 <available_tools>...</> 段的哈希（PC6）
    # channel: 区分本次 LLM 调用来自哪条业务链路。
    #   "main"   → Agent 主运行循环（我们真正要验的 Prefix Cache 载体）
    #   "policy" → Memory 侧 LLM policy（compression / session summary），
    #              它们用自己的短 system、messages 是压缩输入，
    #              与主链路 PC1/PC6 不相关，分开统计避免污染结论。
    channel: str = "main"
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    stream: bool = False


@dataclass
class ProbeCollector:
    """monkey-patch `OpenAICompatibleClient.call/stream_response` 的观测器。

    设计要点
      - 拦截点选在 client 层：拿到的是**真正发往 provider 的 messages**，
        包括 client 内部的任何再加工，比 hook MessageBuilder 更真实。
      - 对 agent / loop / memory 零侵入：不改任何业务代码。
      - 流式通过 include_usage 终止 chunk 读到 cached_tokens（main_sdk_test 已默认开启）。
        若某些场景 include_usage 未开，cached 记为 0，但 sys_hash 仍能判定 PC1。
    """
    turns: list[TurnRecord] = field(default_factory=list)
    _installed: bool = False

    def install(self, client: OpenAICompatibleClient) -> None:
        if self._installed:
            return
        self._installed = True

        orig_call = client.call
        orig_stream = client.stream_response
        collector = self

        async def patched_call(messages, tools=None, settings=None):
            rec = collector._capture_request(messages, stream=False)
            resp = await orig_call(messages, tools, settings)
            usage = resp.get("usage") or {}
            collector._capture_usage(rec, dict(usage))
            return resp

        async def patched_stream(
            messages, tools=None, settings=None,
        ) -> AsyncGenerator[LLMStreamChunk, None]:
            rec = collector._capture_request(messages, stream=True)
            async for chunk in orig_stream(messages, tools, settings):
                if chunk.usage:
                    collector._capture_usage(rec, dict(chunk.usage))
                yield chunk

        client.call = patched_call                # type: ignore[method-assign]
        client.stream_response = patched_stream   # type: ignore[method-assign]

    # ── 捕获 ────────────────────────────────────────

    def _capture_request(
        self,
        messages: list[dict[str, Any]],
        stream: bool,
    ) -> TurnRecord:
        sys_msg = next((m for m in messages if m.get("role") == "system"), None)
        sys_content = (sys_msg.get("content") if sys_msg else "") or ""
        last = messages[-1] if messages else {}

        # <available_tools>...</available_tools> 段哈希（PC6）
        avail_hash = _slice_hash(sys_content, "<available_tools>", "</available_tools>")

        # 渠道识别：
        #   Memory 侧 policy (LLMSummaryCompressionPolicy / LLMSessionSummaryPolicy)
        #   的 system 是自己临时拼的短摘要提示（没有 <available_tools>），
        #   messages 第二条是 "请摘要以下..." 或 "对话历史：..."。
        #   主链路 Agent 的 system 里一定含 <available_tools>。
        if avail_hash:
            channel = "main"
        else:
            channel = "policy"

        rec = TurnRecord(
            call_idx=len(self.turns) + 1,
            sys_hash=hashlib.sha256(sys_content.encode("utf-8")).hexdigest()[:12],
            sys_len=len(sys_content),
            msg_count=len(messages),
            last_is_reminder_user=(
                last.get("role") == "user"
                and str(last.get("content") or "").startswith("<system-reminder>")
            ),
            available_tools_hash=avail_hash,
            channel=channel,
            stream=stream,
        )
        self.turns.append(rec)
        return rec

    def _capture_usage(self, rec: TurnRecord, usage: dict[str, Any]) -> None:
        rec.prompt_tokens = int(usage.get("prompt_tokens") or 0)
        rec.completion_tokens = int(usage.get("completion_tokens") or 0)
        ptd = usage.get("prompt_tokens_details") or {}
        rec.cached_tokens = int(ptd.get("cached_tokens") or 0)

    # ── 打印 ────────────────────────────────────────

    def print_last(self) -> None:
        """打印最近一次 **主链路** LLM 调用的小结（policy 调用静默）。"""
        last_main = next(
            (t for t in reversed(self.turns) if t.channel == "main"), None
        )
        if last_main is None:
            return
        t = last_main
        # 在所有 main 调用里找上一条 main 比对 system 是否漂移
        prev_main = None
        for rec in reversed(self.turns):
            if rec is t:
                continue
            if rec.channel == "main":
                prev_main = rec
                break
        stable = "初次" if prev_main is None else (
            "✅" if t.sys_hash == prev_main.sys_hash else "❌漂移"
        )
        hit = (t.cached_tokens / t.prompt_tokens * 100) if t.prompt_tokens else 0.0
        reminder = "✅" if t.last_is_reminder_user else "—"
        print(
            f"\n[PC-{t.call_idx:02d} main] "
            f"msgs={t.msg_count} sys_len={t.sys_len} sha12={t.sys_hash} "
            f"prompt={t.prompt_tokens} cached={t.cached_tokens} ({hit:.1f}%) "
            f"sys={stable} reminder_tail={reminder}"
        )

    def print_summary(self) -> bool:
        """返回 True 表示所有强不变量通过。

        关键：PC1 / PC6 **只看主链路 (channel=main)** 的 LLM 调用，
        不把 Memory policy 的 summary 请求纳入判定。
        """
        print("\n" + "═" * 70)
        print("  📊 Prefix Cache 端到端验证汇总")
        print("═" * 70)

        if not self.turns:
            print("  ⚠️  无 LLM 调用记录（未发生真实请求）")
            return True

        main_turns = [t for t in self.turns if t.channel == "main"]
        policy_turns = [t for t in self.turns if t.channel == "policy"]

        print(
            f"  分流统计: main(主链路)={len(main_turns)}  "
            f"policy(memory summary)={len(policy_turns)}  总计={len(self.turns)}"
        )

        if not main_turns:
            print("  ⚠️  主链路未发生 LLM 调用，PC 指标不可用")
            return False

        # ── PC1: 主链路 system 字节级一致 ──
        sys_hashes = {t.sys_hash for t in main_turns}
        pc1 = len(sys_hashes) == 1
        print(
            f"  PC1 system 字节级一致:     "
            f"{'✅' if pc1 else '❌'}  unique={len(sys_hashes)} / calls={len(main_turns)}"
        )
        if not pc1:
            print("       → system 漂移的 sha12 集合: " + ", ".join(sorted(sys_hashes)))

        # ── PC6: 主链路 <available_tools> 清单对 discovered 免疫 ──
        avail_hashes = {t.available_tools_hash for t in main_turns if t.available_tools_hash}
        if avail_hashes:
            pc6 = len(avail_hashes) == 1
            print(
                f"  PC6 available_tools 清单稳定: "
                f"{'✅' if pc6 else '❌'}  unique={len(avail_hashes)} / "
                f"calls_with_block={sum(1 for t in main_turns if t.available_tools_hash)}"
            )
        else:
            pc6 = True  # 没有 available_tools 段不代表失败
            print("  PC6 available_tools 清单稳定: ⚠️  未在 system 中发现 <available_tools> 段")

        # ── PC3: 动态 reminder 尾插通道可用（至少一轮触发即可）──
        any_reminder = any(t.last_is_reminder_user for t in main_turns)
        print(
            f"  PC3 reminder 尾插通道:     "
            f"{'✅ 已触发' if any_reminder else '⚠️  未触发'}  "
            f"(非每轮必有，取决于 recall/skill 是否激活)"
        )

        # ── 真实缓存命中（分通道统计）──
        def _hit(turns: list[TurnRecord]) -> tuple[int, int, float]:
            p = sum(t.prompt_tokens for t in turns)
            c = sum(t.cached_tokens for t in turns)
            return p, c, (c / p * 100) if p else 0.0

        m_p, m_c, m_hit = _hit(main_turns)
        print(
            f"\n  真实缓存命中 [main]:   cached={m_c} / prompt={m_p}  ({m_hit:.2f}%)"
        )
        if policy_turns:
            p_p, p_c, p_hit = _hit(policy_turns)
            print(
                f"  真实缓存命中 [policy]: cached={p_c} / prompt={p_p}  ({p_hit:.2f}%)  "
                f"(参考值，不影响判定)"
            )

        # ── 每轮明细 ──
        print("\n  主链路每轮明细 (PC1/PC6 依据此表):")
        self._print_detail_rows(main_turns)
        if policy_turns:
            print("\n  Memory policy 每轮明细 (仅参考):")
            self._print_detail_rows(policy_turns)
        print("═" * 70)

        return pc1 and pc6

    def _print_detail_rows(self, turns: list[TurnRecord]) -> None:
        print(
            f"  {'call':>4} {'msgs':>5} {'sys_len':>8} {'sys_sha12':>12} "
            f"{'prompt':>8} {'cached':>8} {'hit%':>6} stable"
        )
        prev_hash: str | None = None
        for t in turns:
            stable = "—" if prev_hash is None else (
                "✅" if t.sys_hash == prev_hash else "❌"
            )
            hit = (t.cached_tokens / t.prompt_tokens * 100) if t.prompt_tokens else 0.0
            print(
                f"  {t.call_idx:>4} {t.msg_count:>5} {t.sys_len:>8} "
                f"{t.sys_hash:>12} {t.prompt_tokens:>8} {t.cached_tokens:>8} "
                f"{hit:>5.1f}% {stable:>6}"
            )
            prev_hash = t.sys_hash


def _slice_hash(text: str, start_tag: str, end_tag: str) -> str:
    """提取 text 中 start_tag...end_tag 片段（含标签）的 sha256[:12]。
    未找到返回空串。"""
    i = text.find(start_tag)
    if i < 0:
        return ""
    j = text.find(end_tag, i)
    if j < 0:
        return ""
    frag = text[i:j + len(end_tag)]
    return hashlib.sha256(frag.encode("utf-8")).hexdigest()[:12]


# ════════════════════════════════════════════════════
#  驱动
# ════════════════════════════════════════════════════

async def run_live_probe(mode: str) -> int:
    if not sdk_main.DASHSCOPE_API_KEY:
        print("❌ DASHSCOPE_API_KEY 未配置，请检查 仓库根目录的 .env.development")
        return 1

    print("═" * 70)
    print("  🐼 Prefix Cache · 真实 LLM 端到端验证（交互模式）")
    print(f"     model={sdk_main.MODEL_NAME}  mode={mode}")
    print("═" * 70)

    agent = sdk_main.build_agent()

    # ── 挂探针：从 agent._loop 里取到真正被用的 llm_client ──
    llm_client: OpenAICompatibleClient = agent._loop._llm_client  # type: ignore[attr-defined]
    probe = ProbeCollector()
    probe.install(llm_client)
    print("\n🔬 PrefixCache 探针已安装到 LLM client\n")

    session_id = str(uuid.uuid4())
    runner = sdk_main.run_task_stream if mode == "stream" else sdk_main.run_task_blocking

    # 交互 REPL：输入一条 → 立即执行 → 立即打 PC 小结
    await _run_interactive_loop(runner, agent, probe, session_id)

    sdk_main.md_metrics.flush()

    if not probe.turns:
        print("\n⚠️  未发生任何 LLM 调用，跳过汇总")
        return 0

    ok = probe.print_summary()
    print("═" * 70)
    print("  ✅ 全部不变量通过" if ok else "  ❌ 存在不变量违反，请查看上方明细")
    print("═" * 70)
    return 0 if ok else 2


async def _run_interactive_loop(
    runner,
    agent,
    probe: "ProbeCollector",
    session_id: str,
) -> None:
    """交互式 REPL：逐条读入任务 → 执行 → 打印该轮 PC 小结。

    退出条件：
      - 空行（直接回车）
      - :quit / :q / exit
      - Ctrl-C / Ctrl-D (EOF)
    """
    print("💬 交互模式已就绪")
    print("   · 输入任务后回车执行；输入空行 / :q / exit / Ctrl-C 退出")
    print("   · 同一 session_id 贯穿整个会话，可以前后呼应测记忆召回\n")

    idx = 0
    while True:
        idx += 1
        try:
            raw = await asyncio.to_thread(input, f"🧑 [#{idx}] > ")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        task = raw.strip()
        if not task or task.lower() in (":q", ":quit", "exit", "quit"):
            break

        try:
            await runner(
                agent, task, f"#{idx} interactive",
                session_id=session_id,
            )
        except KeyboardInterrupt:
            print("\n⏹  当前任务被中断，可继续输入下一条或退出")
            continue
        except Exception as e:  # noqa: BLE001
            print(f"\n⚠️  任务执行出错：{type(e).__name__}: {e}\n")
            continue

        probe.print_last()


# ════════════════════════════════════════════════════
#  CLI
# ════════════════════════════════════════════════════

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pandaren Prefix Cache 真实 LLM 端到端验证（交互模式）"
    )
    parser.add_argument(
        "--mode",
        choices=["stream", "run"],
        default="stream",
        help="LLM 调用模式（默认 stream；run = 非流式 blocking）",
    )
    args = parser.parse_args()

    try:
        rc = asyncio.run(run_live_probe(args.mode))
    except KeyboardInterrupt:
        print("\n👋 已中断")
        rc = 130
    return rc


if __name__ == "__main__":
    sys.exit(main())
