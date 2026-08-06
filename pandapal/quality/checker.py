"""pandapal/quality/checker.py — 检查器协议与 ruff 实现

检查器以 Protocol 注入（设计 §8.5 可测试性）：测试注入 FakeChecker，
加 mypy/pyright/bandit 只是追加一个实现，不改任何契约。

**None 与 [] 的区别是本模块最重要的语义，勿混淆**：
  - `[]`   = 检查跑通了，文件干净        → 门控据此**重置熔断计数**
  - `None` = 检查没跑通（缺失/超时/崩溃/输出无法解析）→ 降级，**不得动计数**
若把降级当干净，一次 ruff 崩溃就能让已熔断的文件"洗白"退出熔断。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Protocol, runtime_checkable

from pandaren.tool.definition.tool_result import FeedbackSeverity

from .models import Diagnostic

logger = logging.getLogger("pandapal.quality.checker")

__all__ = ["Checker", "RuffChecker"]


@runtime_checkable
class Checker(Protocol):
    """单个检查器（应用层内部协议，非 SDK 契约）。"""

    name: str

    async def check(
        self, file_path: str, *, timeout: float, cwd: str,
    ) -> list[Diagnostic] | None:
        """检查单个文件。

        Returns:
            诊断列表（`[]` = 干净）；`None` = 降级（本次结论不可用，调用方不得据此改状态）。

        约定：实现内部必须自兜底 —— 绝不抛异常，绝不阻塞事件循环。
        """
        ...


class RuffChecker:
    """ruff 检查器：子进程 + 超时 + JSON 解析 + 归一化。

    ── 命令行纪律（设计 5B-c，最高优先级）────────────────────────────
    「与 CI 一致」这句话必须拆成**规则集**与**文件集**两问来回答，否则必然改错。

    ① 规则集：与 CI 完全同源 —— **不加 --isolated / --select / --ignore**
    只给文件路径与输出格式，并以项目根为 cwd，让 ruff 自行发现 pyproject.toml，
    完整继承 [tool.ruff.lint] 的 select / ignore / per-file-ignores。

    本仓 lint.yml 的注释已写明踩过的坑：「CLI 参数会覆盖配置里的 ignore，
    导致本地与 CI 结果不一致」。门控遵守同一条纪律。

    否则会反复要求 Agent 修项目明确 ignore 掉的东西（F841 有 26 处待 triage、
    E701 是 Protocol 桩写法噪音 51 处），纯噪音 + 白烧 token + 抬高熔断率；
    tests/ 下的 F401 也会每写一个测试文件就骚扰一次。

    ② 文件集：与 CI **有意不同** —— **不加 --force-exclude**
    ruff 沿袭 flake8/black 的惯例：显式传路径时不套用 exclude 配置（除非
    --force-exclude）。门控恰恰是逐文件显式传路径的，故 [tool.ruff] 的
    extend-exclude 对它天然不生效 —— **这正是要的行为，不是漏配**。

    因为两者在回答不同的问题：
      CI   遍历整棵树，需要 exclude 来跳过「不是我们的代码」（venv 里 5251 个
           第三方 .py、pandapal_hardware_xiaozhi 下 vendor 的 ESP-IDF 工具链、
           .pandapal 的程序生成状态）。那个列表是所有权边界，不是质量豁免。
      门控 从不遍历树。它的文件集只有一个成员：Agent 刚写的那个文件，在
           write_file 那一刻就已确定，不需要 exclude 帮它筛。

    一句话：门控管「写的瞬间」，写到哪就在哪检查，不看目录脸色；CI 管「提交的
    树」，覆盖所有属于我们的代码。两句不矛盾，只是恰好都用 ruff 实现。

    ⚠️ 加上 --force-exclude 会让门控对 output/ 等目录**彻底静默**，且不报任何错 ——
    与 5B-c 的失效模式同级（不同尺且无察觉）。若日后有人为了「和 CI 对齐」想加它，
    请先读完本段：要对齐的是①，不是②。
    """

    name = "ruff"

    #: ruff 的 JSON **确实带 severity 字段**（0.15.21 实测），但本类不读它，
    #: 一律归一化为 ERROR。两条理由：
    #:   1. 项目 pyproject.toml 的 select/ignore/per-file-ignores 已经把「什么值得报」
    #:      判断完了。凡是在该配置下仍报出来的，就是项目认定必须修的 —— CI 也会为它挂红，
    #:      门控没有理由再降一档。
    #:   2. pyproject.toml 锁的是 `ruff>=0.15`（下限而非精确版本），severity 字段在
    #:      不同版本上未必稳定存在；不依赖它更抗版本漂移。
    #: 反例警示：若有人把 F401 映射成 warning，配上 feedback_warnings=false，
    #: 门控对 PRD 点名的目标问题就**静默归零** —— 花光全部复杂度却对最高频问题不发一言。
    _SEVERITY = FeedbackSeverity.ERROR

    #: ruff 用普通诊断（exit 1）报告「文件读不了」，而不是进程失败。
    #: 若把它当诊断回灌，Agent 会收到「E902 系统找不到指定的文件」并试图去"修"
    #: 一个根本不存在的代码问题 —— 正是 E 原则明令禁止的「误导 Agent 空改」。
    #: 故 io-error 视为**降级信号**：说明门控自己的路径解析错了，不是代码有问题。
    _IO_ERROR_CODES = frozenset({"E902"})

    def __init__(self, command: tuple[str, ...] = ("ruff",)) -> None:
        self._command = command

    async def check(
        self, file_path: str, *, timeout: float, cwd: str,
    ) -> list[Diagnostic] | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._command, "check", "--output-format", "json", file_path,
                cwd=cwd or None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.warning(
                "[quality_gate.degraded] ruff 未安装或不在 PATH，门控静默跳过 | cmd=%s",
                self._command,
            )
            return None
        except OSError as e:
            logger.warning("[quality_gate.degraded] ruff 启动失败 | error=%s", e)
            return None

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "[quality_gate.degraded] ruff 超时 %.1fs，杀掉子进程并降级 | file=%s",
                timeout, file_path,
            )
            await _reap(proc)
            return None
        except asyncio.CancelledError:
            await _reap(proc)                 # 取消优先级最高：先收拾子进程再让异常继续传播
            raise

        # exit 0 = 无诊断；1 = 有诊断；≥2 = ruff 自身出错（坏配置等），此时 stdout 非 JSON
        if proc.returncode not in (0, 1):
            logger.warning(
                "[quality_gate.degraded] ruff 异常退出 | rc=%s | stderr=%s",
                proc.returncode, (stderr or b"").decode("utf-8", "replace")[:200],
            )
            return None

        try:
            raw = json.loads((stdout or b"").decode("utf-8", "replace") or "[]")
        except (json.JSONDecodeError, UnicodeError) as e:
            logger.warning("[quality_gate.degraded] ruff 输出无法解析为 JSON | error=%s", e)
            return None

        if not isinstance(raw, list):
            logger.warning("[quality_gate.degraded] ruff 输出不是数组 | type=%s", type(raw).__name__)
            return None

        return self._normalize(raw, file_path)

    def _normalize(self, raw: list, file_path: str) -> list[Diagnostic] | None:
        diagnostics: list[Diagnostic] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            code = item.get("code") or item.get("name") or "?"

            if code in self._IO_ERROR_CODES:
                # ruff 把「文件读不了」也当诊断报（exit 1）。这不是代码问题，是门控
                # 自己路径解析错了 —— 整份结论作废，降级，绝不回灌。
                logger.warning(
                    "[quality_gate.degraded] ruff 报 IO 错误（多半是路径解析有误）"
                    " | file=%s | code=%s | msg=%s",
                    file_path, code, item.get("message", ""),
                )
                return None

            loc = item.get("location") or {}
            diagnostics.append(Diagnostic(
                file=item.get("filename") or file_path,
                line=int(loc.get("row") or 0),
                column=int(loc.get("column") or 0),
                code=str(code),
                message=str(item.get("message") or ""),
                severity=self._SEVERITY,
                checker=self.name,
            ))
        return diagnostics


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """杀掉子进程**并收尸**，绝不因收尸失败再抛异常（O3）。

    必须 await 到进程真正结束：光 kill() 不 wait，stdout/stderr 的管道 transport
    不会被关闭（Windows 上表现为 `ResourceWarning: unclosed transport`）。
    门控跑在 per-app 长驻的 Agent 里，每次超时漏一对管道 = 一条确定的 fd 泄漏。
    """
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        return                                # 已经自己退了，没什么可收的
    try:
        # kill 之后 communicate 会很快返回；再套一层超时，防收尸本身把调用方卡住
        await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except (asyncio.TimeoutError, ProcessLookupError, OSError):
        pass
    except asyncio.CancelledError:
        pass                                  # 收尸阶段被取消：已 kill，不再向上抛第二次
