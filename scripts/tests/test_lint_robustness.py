"""scripts/tests/test_lint_robustness.py — 健壮性检查器单测

只测纯函数 check_source / 基线过滤，不碰文件系统与子进程。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "lint_robustness.py"
_spec = importlib.util.spec_from_file_location("lint_robustness", _SCRIPT)
assert _spec and _spec.loader
lint = importlib.util.module_from_spec(_spec)
sys.modules["lint_robustness"] = lint
_spec.loader.exec_module(lint)


def _codes(src: str) -> list[str]:
    findings, _ = lint.check_source(src, "t.py")
    return [f.code for f in findings]


def _sev(src: str, code: str) -> str | None:
    findings, _ = lint.check_source(src, "t.py")
    for f in findings:
        if f.code == code:
            return f.severity
    return None


# ── ROB001 · broad except + pass ─────────────────────────────────────────

def test_rob001_broad_except_pass_is_error():
    assert _sev("try:\n    x()\nexcept Exception:\n    pass\n", "ROB001") == "ERROR"


def test_rob001_bare_except_pass_is_error():
    assert _sev("try:\n    x()\nexcept:\n    pass\n", "ROB001") == "ERROR"


def test_rob001_narrow_except_pass_not_flagged():
    # 具体异常的 pass 是有意抑制，非 §7.1 的 broad 红线
    assert "ROB001" not in _codes("try:\n    x()\nexcept ValueError:\n    pass\n")


def test_rob001_not_fired_when_handler_logs():
    # 留痕即合规（§3.1）——即便 body 之后 pass 也不报
    src = "try:\n    x()\nexcept Exception:\n    logger.warning('oops')\n"
    assert "ROB001" not in _codes(src)


# ── ROB002 · broad except 静默 return 替身 ───────────────────────────────

def test_rob002_swallow_return_none_no_log_is_error():
    src = "def f():\n try:\n  x()\n except Exception:\n  return None\n"
    assert _sev(src, "ROB002") == "ERROR"


def test_rob002_not_fired_when_logs():
    src = "def f():\n try:\n  x()\n except Exception:\n  logger.error('e')\n  return None\n"
    assert "ROB002" not in _codes(src)


def test_reraise_handler_not_flagged():
    # 先兜底再 re-raise：错误仍传播，不算静默吞
    src = "try:\n    x()\nexcept Exception:\n    cleanup()\n    raise\n"
    codes = _codes(src)
    assert "ROB001" not in codes and "ROB002" not in codes and "ROB003" not in codes


def test_report_degradation_counts_as_trace():
    # 统一降级通道 report_degradation(...) 是 §5 钦定留痕：broad except 里调用它 = 合规，不报 ROB001/002/003
    src_pass = (
        "try:\n    x()\nexcept Exception:\n"
        "    report_degradation('e', category='id', source='s')\n"
    )
    assert "ROB001" not in _codes(src_pass)
    src_ret = (
        "def f():\n try:\n  x()\n except Exception:\n"
        "  report_degradation('e', category='id', source='s')\n  return None\n"
    )
    codes = _codes(src_ret)
    assert "ROB002" not in codes and "ROB003" not in codes


# ── ROB003 · 其它静默 broad except → WARN ────────────────────────────────

def test_rob003_other_silent_swallow_is_warn():
    src = "for i in r:\n try:\n  x()\n except Exception:\n  continue\n"
    assert _sev(src, "ROB003") == "WARN"


# ── ROB004 · ID 类零默认 → WARN ──────────────────────────────────────────

def test_rob004_get_id_key_with_default():
    assert _sev('d.get("session_id", "")', "ROB004") == "WARN"


def test_rob004_id_or_default():
    assert _sev('sid = session_id or "unknown"', "ROB004") == "WARN"


def test_rob004_attr_id_or_default():
    assert _sev('x = obj.model_id or "gpt"', "ROB004") == "WARN"


def test_rob004_non_id_key_not_flagged():
    assert "ROB004" not in _codes('d.get("title", "无标题")')


def test_rob004_get_id_key_without_default_not_flagged():
    # 只读取、不给默认 → 合规
    assert "ROB004" not in _codes('d.get("session_id")')


# ── baseline 棘轮 ────────────────────────────────────────────────────────

def _err(file: str, code: str) -> "lint.Finding":
    return lint.Finding(file, 1, 1, code, "ERROR", "m")


def test_baseline_grandfathers_existing():
    findings = [_err("a.py", "ROB001")]
    baseline = {"a.py\tROB001": 1}
    assert lint.filter_new_vs_baseline(findings, baseline) == []


def test_baseline_catches_net_new_in_same_bucket():
    findings = [_err("a.py", "ROB001"), _err("a.py", "ROB001")]
    baseline = {"a.py\tROB001": 1}
    assert len(lint.filter_new_vs_baseline(findings, baseline)) == 2  # 桶超限→整桶回报


def test_baseline_catches_new_file():
    findings = [_err("new.py", "ROB001")]
    assert lint.filter_new_vs_baseline(findings, {}) == findings
