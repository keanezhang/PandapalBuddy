"""pandapal/local/boot_logger.py — 启动阶段终端输出格式化工具。

仅用于 run_local.py 的启动序列，不用于运行时日志。
运行时日志继续使用标准 logging（可接入日志聚合平台）。

设计原则：
- 零外部依赖：仅 ANSI escape codes
- TTY 自动检测：非 TTY 环境（CI / 日志重定向）自动降级为纯文本
- 单一职责：只负责启动序列的视觉呈现
"""

from __future__ import annotations

import sys

# ── 强制 stdout / stderr 使用 UTF-8 ──────────────────────────────────────────
# 当作为 Tauri sidecar 等子进程启动时，Windows 默认编码为 GBK，
# 无法输出 emoji / box-drawing 等字符。这里在模块加载时统一切换到 UTF-8。
def _ensure_utf8_stdio() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

_ensure_utf8_stdio()

# ── ANSI codes ────────────────────────────────────────────────────────────────
_R = "\033[0m"      # reset
_BOLD  = "\033[1m"
_DIM   = "\033[2m"
_CYAN  = "\033[36m"
_BCYAN = "\033[96m"  # bright cyan
_GREEN = "\033[32m"
_BGRN  = "\033[92m"  # bright green
_YELLOW= "\033[33m"
_RED   = "\033[31m"
_BLUE  = "\033[34m"
_BBLUE = "\033[94m"  # bright blue
_WHITE = "\033[97m"
_GRAY  = "\033[90m"

_WIDTH = 68  # banner width


class BootLogger:
    """启动阶段专用的结构化终端输出工具。

    用法示例::

        boot = BootLogger()
        boot.banner()

        boot.step(1, "环境配置")
        boot.ok("env loaded — .env.development")
        boot.kv("model", "qwen-plus")
        boot.kv("base_url", "https://dashscope.aliyuncs.com/...")

        boot.step(7, "Gateway")
        boot.warn("relay_url 未配置，以 offline 模式运行")

        boot.ready()
    """

    def __init__(self, *, use_color: bool | None = None) -> None:
        """
        Args:
            use_color: None（默认）= 自动检测 TTY；True = 强制开启；False = 强制关闭
        """
        if use_color is None:
            self._color = sys.stdout.isatty()
        else:
            self._color = use_color

    # ── 内部着色工具 ──────────────────────────────────────────────────────────

    def _c(self, code: str, text: str) -> str:
        """给文本包上 ANSI 颜色，非 TTY 环境返回原文本。"""
        return f"{code}{text}{_R}" if self._color else text

    # ── 公开 API ──────────────────────────────────────────────────────────────

    def banner(self) -> None:
        """启动时的顶部 Banner。"""
        top  = "╔" + "═" * (_WIDTH - 2) + "╗"
        mid  = "║  🐼  PandaPal Agent (Local) — Starting..."
        mid  = mid.ljust(_WIDTH - 1) + "║"
        bot  = "╚" + "═" * (_WIDTH - 2) + "╝"
        print()
        print(self._c(_BCYAN + _BOLD, top))
        print(self._c(_BCYAN + _BOLD, mid))
        print(self._c(_BCYAN + _BOLD, bot))
        print()

    def step(self, n: int | str, title: str) -> None:
        """打印步骤标题行，带步骤编号。

        示例输出::
            [03] Config Manager ─────────────────────────────
        """
        if isinstance(n, int):
            label = f"{n:02d}"
        else:
            label = str(n)
        badge = self._c(_BBLUE + _BOLD, f" [{label}] ")
        name  = self._c(_BCYAN + _BOLD, title)
        # 填充虚线至固定宽度（保持对齐）
        pad   = max(0, 46 - len(title))
        dots  = self._c(_GRAY, " " + "─" * pad)
        print(f"\n{badge} {name}{dots}")

    def ok(self, msg: str) -> None:
        """成功 / 已就绪。"""
        mark = self._c(_BGRN + _BOLD, "  ✓")
        print(f"{mark}  {msg}")

    def warn(self, msg: str) -> None:
        """警告 / 降级运行。"""
        mark = self._c(_YELLOW + _BOLD, "  ⚠")
        print(f"{mark}  {self._c(_YELLOW, msg)}")

    def error(self, msg: str) -> None:
        """错误 / 启动失败。"""
        mark = self._c(_RED + _BOLD, "  ✗")
        print(f"{mark}  {self._c(_RED, msg)}")

    def kv(self, key: str, value: str) -> None:
        """在当前步骤下显示一组键值对。

        示例::
               model  qwen-plus
        """
        k = self._c(_GRAY, f"     {key:>10} ")
        v = self._c(_WHITE, str(value))
        print(f"{k} {v}")

    def ready(self) -> None:
        """所有步骤完成后的 Ready Banner。"""
        top = "╔" + "═" * (_WIDTH - 2) + "╗"
        mid = "║  ✅  Agent Ready — waiting for messages..."
        mid = mid.ljust(_WIDTH - 1) + "║"
        bot = "╚" + "═" * (_WIDTH - 2) + "╝"
        print()
        print(self._c(_BGRN + _BOLD, top))
        print(self._c(_BGRN + _BOLD, mid))
        print(self._c(_BGRN + _BOLD, bot))
        print()

    def separator(self) -> None:
        """模块组分隔线 —— 在主要模块组之间打印全宽分隔线，视觉区隔不同子系统。"""
        line = "─" * _WIDTH
        print(f"\n{self._c(_GRAY, line)}")

    def section(self, title: str) -> None:
        """大分组标题 —— 用于将相近的步骤归拢到同一模块组下。

        示例输出::

            ═══════════════  Core Services  ═══════════════
        """
        line = "═" * _WIDTH
        inner = f"  {title}  "
        pad_total = _WIDTH - len(inner)
        left = pad_total // 2
        right = pad_total - left
        header = "═" * left + inner + "═" * right
        print(f"\n{self._c(_CYAN + _BOLD, header)}")

    def shutdown_done(self) -> None:
        """Shutdown 完成时的简短提示。"""
        print(self._c(_GRAY, "\n  [Shutdown] Done.\n"))
