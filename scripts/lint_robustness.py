#!/usr/bin/env python3
"""scripts/lint_robustness.py — 健壮性硬约束检查器

落实《健壮性与降级工程原则》§6 自查清单 + §7 CI/lint 规则里**标准 ruff 表达不了**
的那几条项目语义规则。ruff 只能识别语法形状，识别不了「这个 broad except 有没有留痕」
「这个 .get 的键是不是 ID 类」——这些要看语义，所以自建 AST 检查。

规则一览（编号 ROB0xx）:

  ERROR 级（blocking，进 CI 红灯）
    ROB001  broad except 且 body 只有 pass —— §7.1「except: pass 出现即违规，无条件改」
    ROB002  broad except 静默吞掉（return None / return 常量，且无 log、无 re-raise）
            —— §7.2「必须有 log，否则拦截」

  WARN 级（advisory，只报不拦，exit 0）
    ROB003  其它「静默 broad except」（无 log、无 re-raise，也不属 ROB001/002 形状）
            —— §7.3。注意：**会留痕（有 logger 调用）或会 re-raise 的 broad except 不报**，
            因为按 §3.1 那属于合规的「故障隔离点 / 非关键清理」写法。
    ROB004  ID 类字段零默认违规：`.get("session_id", 默认)` / `session_id or 默认`
            —— §7.4 + R1。ID 类（session_id/user_id/model_id/provider）缺失必须 fail-fast。
    ROB005  魔法数字（仅 --magic 时扫）：跨文件重复 ≥N 次的非平凡字面量 —— §7.5 一次性审计。

判 broad except：`except:`（裸）/ `except Exception` / `except BaseException`。
判 log 留痕：body 内出现 logger 方法调用（debug/info/warning/warn/error/exception/critical）。

用法:
    python scripts/lint_robustness.py                  # 扫默认目录，打印 ERROR + WARN
    python scripts/lint_robustness.py --errors-only    # 只报 ERROR（CI blocking 步用）
    python scripts/lint_robustness.py --format github   # GitHub Actions 注解格式（只注解 ERROR）
    python scripts/lint_robustness.py --magic           # 追加 ROB005 魔法数字审计
    python scripts/lint_robustness.py pandaren/agent    # 只扫指定路径

退出码: 存在任一 ERROR 级违规 → 1；仅 WARN / 干净 → 0。
"""

from __future__ import annotations

import argparse
import ast
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

# baseline（棘轮）默认落点：快照现存 ERROR，CI 只拦「净新增」。
DEFAULT_BASELINE = Path(__file__).resolve().parent / "robustness_baseline.json"

# ── 默认扫描根：与被检查的三个 Python 子项目对齐 ──────────────────────────
DEFAULT_ROOTS = ("pandaren", "pandapal", "pandapal_relay")

# ── 排除目录：复用 pyproject.toml [tool.ruff] extend-exclude 的同一份所有权边界 ──
#    （vendor 代码、程序生成状态、前端/固件工程），外加 tests —— 测试里的 except: pass
#    常是有意写法，§7 规则针对生产代码。
EXCLUDE_DIR_NAMES = frozenset({
    "venv", ".venv", "output", "claude-code",
    "pandapal_hardware_xiaozhi", ".pandapal", "__pycache__", ".git",
    "node_modules", "src-tauri", "tests",
})

# ID / 身份类字段键（R1）：缺失即报错，没有 default 这回事。
ID_FIELD_KEYS = frozenset({"session_id", "user_id", "model_id", "provider"})

# 视为「留痕」的 logger 方法名（§3.1：会留痕的 broad except 是合规的）。
LOG_METHOD_NAMES = frozenset({
    "debug", "info", "warning", "warn", "error", "exception", "critical", "log",
})

# 视为「留痕」的函数名（非 logger.xxx 形态）：统一降级通道 report_degradation 本身
# 就是 §5 钦定的留痕机制（双写 log+counter），在 broad except 里调用它 = 合规留痕。
TRACE_FUNC_NAMES = frozenset({"report_degradation"})

# ROB005 魔法数字：这些平凡值不算魔法数字（§4「别矫枉过正」）。
TRIVIAL_NUMBERS = frozenset({0, 1, 2, -1, 100, 1000, 10, 60, 24, 1024})
MAGIC_MIN_REPEATS = 3  # 跨文件重复达到此次数才收编


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    col: int
    code: str
    severity: str  # "ERROR" | "WARN"
    message: str


# ── AST 分类小工具 ───────────────────────────────────────────────────────

def _is_broad_except(handler: ast.ExceptHandler) -> bool:
    """裸 except / except Exception / except BaseException 判为 broad。"""
    exc = handler.type
    if exc is None:
        return True
    names: list[str] = []
    targets = exc.elts if isinstance(exc, ast.Tuple) else [exc]
    for t in targets:
        if isinstance(t, ast.Name):
            names.append(t.id)
        elif isinstance(t, ast.Attribute):
            names.append(t.attr)
    return any(n in ("Exception", "BaseException") for n in names)


def _handler_logs(handler: ast.ExceptHandler) -> bool:
    """body 内是否出现留痕调用：logger.xxx(...) 或 report_degradation(...)（视为留痕）。"""
    for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in LOG_METHOD_NAMES:
                return True
            if isinstance(func, ast.Name) and func.id in TRACE_FUNC_NAMES:
                return True
    return False


def _handler_reraises(handler: ast.ExceptHandler) -> bool:
    """body 内是否有 raise（重新抛出）。"""
    for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if isinstance(node, ast.Raise):
            return True
    return False


def _body_is_only_pass(handler: ast.ExceptHandler) -> bool:
    return len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass)


def _body_swallow_returns(handler: ast.ExceptHandler) -> bool:
    """body 里存在 `return`（无值）或 `return <常量>` —— 静默吞掉后返回替身。"""
    for node in handler.body:
        if isinstance(node, ast.Return):
            if node.value is None or isinstance(node.value, ast.Constant):
                return True
    return False


def _refers_id_key(node: ast.expr) -> bool:
    """表达式是否「指向」某个 ID 类字段：名字 / 属性 / .get('id_key') 调用。"""
    if isinstance(node, ast.Name):
        return node.id in ID_FIELD_KEYS
    if isinstance(node, ast.Attribute):
        return node.attr in ID_FIELD_KEYS
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
        if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value in ID_FIELD_KEYS:
            return True
    return False


# ── 单文件检查 ───────────────────────────────────────────────────────────

class _Visitor(ast.NodeVisitor):
    def __init__(self, relpath: str) -> None:
        self.relpath = relpath
        self.findings: list[Finding] = []
        self.magic_literals: Counter = Counter()  # 值 -> 本文件出现次数

    def _add(self, node: ast.AST, code: str, severity: str, msg: str) -> None:
        self.findings.append(Finding(
            self.relpath, getattr(node, "lineno", 0), getattr(node, "col_offset", 0) + 1,
            code, severity, msg,
        ))

    def visit_ExceptHandler(self, handler: ast.ExceptHandler) -> None:
        if _is_broad_except(handler):
            # 合规出口：留痕 或 re-raise —— §3.1 允许的写法，不报。
            if not (_handler_logs(handler) or _handler_reraises(handler)):
                if _body_is_only_pass(handler):
                    self._add(handler, "ROB001", "ERROR",
                              "broad except 且 body 只有 pass —— 静默吞异常，无条件改（§7.1）")
                elif _body_swallow_returns(handler):
                    self._add(handler, "ROB002", "ERROR",
                              "broad except 静默吞掉后 return 替身值，且无 log —— 必须留痕或传播（§7.2）")
                else:
                    self._add(handler, "ROB003", "WARN",
                              "broad except 无 log、无 re-raise —— 疑似静默吞异常，"
                              "确认是否在故障隔离点白名单，否则改具体异常或补留痕（§7.3）")
        self.generic_visit(handler)

    def visit_Call(self, node: ast.Call) -> None:
        # ROB004a: X.get("session_id", 默认)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get" and len(node.args) >= 2:
            key = node.args[0]
            if isinstance(key, ast.Constant) and key.value in ID_FIELD_KEYS:
                self._add(node, "ROB004", "WARN",
                          f'.get("{key.value}", 默认) —— ID 类字段零默认，缺失应 fail-fast（§7.4 / R1）')
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        # ROB004b: session_id or 默认 / obj.model_id or 默认 / d.get("user_id") or 默认
        if isinstance(node.op, ast.Or) and node.values and _refers_id_key(node.values[0]):
            self._add(node, "ROB004", "WARN",
                      "ID 类字段 `... or 默认` —— 缺失应 fail-fast，不许兜底替代（§7.4 / R1）")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        # ROB005 素材收集：非平凡数字字面量
        v = node.value
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)) and v not in TRIVIAL_NUMBERS:
            self.magic_literals[v] += 1


def check_source(source: str, relpath: str) -> tuple[list[Finding], Counter]:
    try:
        tree = ast.parse(source, filename=relpath)
    except SyntaxError as e:
        # 解析失败本身就是问题，但不是本检查器的职责 —— 交给 ruff/编译。降级为一条 WARN。
        return [Finding(relpath, e.lineno or 0, (e.offset or 0), "ROB000", "WARN",
                        f"文件无法解析为 AST（{e.msg}）—— 本检查器跳过")], Counter()
    v = _Visitor(relpath)
    v.visit(tree)
    return v.findings, v.magic_literals


# ── 文件发现 ─────────────────────────────────────────────────────────────

def iter_python_files(roots: list[Path], repo_root: Path) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
            continue
        for path in root.rglob("*.py"):
            if any(part in EXCLUDE_DIR_NAMES for part in path.relative_to(repo_root).parts[:-1]):
                continue
            files.append(path)
    return files


# ── 输出 ─────────────────────────────────────────────────────────────────

def _print_plain(findings: list[Finding]) -> None:
    by_sev: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        by_sev[f.severity].append(f)
    for sev in ("ERROR", "WARN"):
        group = by_sev.get(sev, [])
        if not group:
            continue
        print(f"\n{'='*70}\n{sev}  ({len(group)} 处)\n{'='*70}")
        for f in sorted(group, key=lambda x: (x.file, x.line)):
            print(f"  {f.file}:{f.line}:{f.col}  [{f.code}] {f.message}")


def _print_github(findings: list[Finding]) -> None:
    # GitHub Actions 注解：只对 ERROR 出 error 注解（会让 checks 失败并标红行）。
    for f in findings:
        if f.severity != "ERROR":
            continue
        print(f"::error file={f.file},line={f.line},col={f.col}::[{f.code}] {f.message}")


def _print_magic(magic: Counter) -> None:
    hot = [(v, n) for v, n in magic.items() if n >= MAGIC_MIN_REPEATS]
    if not hot:
        print("\n[ROB005] 魔法数字审计：未发现跨文件重复 ≥%d 次的非平凡字面量。" % MAGIC_MIN_REPEATS)
        return
    print(f"\n[ROB005] 魔法数字审计（重复 ≥{MAGIC_MIN_REPEATS} 次，建议命名+就近归属，§4）：")
    for v, n in sorted(hot, key=lambda x: -x[1]):
        print(f"  值 {v!r} 出现 {n} 次")


# ── baseline（棘轮）───────────────────────────────────────────────────────
#    存量 ERROR 太多且多在 §3.2 白名单边界，无法一夜清零。策略：快照现存量为基线，
#    CI 只对「净新增」红灯。按 (file, code) 计数比较 —— 容忍行号漂移（重构挪动代码
#    不会误判为新增），只在某文件某规则的违规**数量超过基线**时拦截。
#    代价：同文件内「删一处又加一处」净数不变时不被拦 —— 棘轮的通用取舍，可接受。

def _bucket_key(f: Finding) -> str:
    return f"{f.file}\t{f.code}"


def load_baseline(path: Path) -> dict[str, int]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_baseline(path: Path, error_findings: list[Finding]) -> int:
    buckets: Counter = Counter(_bucket_key(f) for f in error_findings)
    path.write_text(
        json.dumps(dict(sorted(buckets.items())), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return len(error_findings)


def filter_new_vs_baseline(
    error_findings: list[Finding], baseline: dict[str, int],
) -> list[Finding]:
    """返回超出基线的「净新增」ERROR：某 (file,code) 桶当前数 > 基线数时，
    该桶内 findings 全部回报（计数法无法精确定位是哪一条新增，over-report 更安全）。"""
    by_bucket: dict[str, list[Finding]] = defaultdict(list)
    for f in error_findings:
        by_bucket[_bucket_key(f)].append(f)
    new: list[Finding] = []
    for key, group in by_bucket.items():
        if len(group) > baseline.get(key, 0):
            new.extend(group)
    return new


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="健壮性硬约束检查器（ROB001-005）")
    parser.add_argument("paths", nargs="*", help="扫描路径，默认 pandaren/ pandapal/ pandapal_relay/")
    parser.add_argument("--errors-only", action="store_true", help="只报 ERROR 级（CI blocking 用）")
    parser.add_argument("--format", choices=("plain", "github"), default="plain")
    parser.add_argument("--magic", action="store_true", help="追加 ROB005 魔法数字审计")
    parser.add_argument("--baseline", nargs="?", const=str(DEFAULT_BASELINE), default=None,
                        help="棘轮模式：只拦相对基线的净新增 ERROR（默认基线 scripts/robustness_baseline.json）")
    parser.add_argument("--update-baseline", action="store_true",
                        help="用当前 ERROR 快照重写基线文件后退出（存量还债后运行以收紧棘轮）")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    if args.paths:
        roots = [Path(p) if Path(p).is_absolute() else repo_root / p for p in args.paths]
    else:
        roots = [repo_root / r for r in DEFAULT_ROOTS]
    roots = [r for r in roots if r.exists()]

    all_findings: list[Finding] = []
    magic_total: Counter = Counter()
    for path in iter_python_files(roots, repo_root):
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        rel = path.relative_to(repo_root).as_posix()
        findings, magic = check_source(source, rel)
        all_findings.extend(findings)
        magic_total.update(magic)

    error_findings = [f for f in all_findings if f.severity == "ERROR"]
    warn_findings = [f for f in all_findings if f.severity == "WARN"]

    # --update-baseline：把当前全部 ERROR 快照进基线，退出（不做拦截判断）。
    if args.update_baseline:
        path = Path(args.baseline) if args.baseline else DEFAULT_BASELINE
        n = save_baseline(path, error_findings)
        print(f"已写入基线 {path.name}：{n} 处 ERROR（{len({_bucket_key(f) for f in error_findings})} 个桶）")
        return 0

    # 棘轮：有基线时，ERROR 收敛为「净新增」；存量被 grandfather。
    if args.baseline:
        baseline = load_baseline(Path(args.baseline))
        blocking = filter_new_vs_baseline(error_findings, baseline)
    else:
        blocking = error_findings

    shown = blocking if args.errors_only else (blocking + warn_findings)

    if args.format == "github":
        _print_github(shown)
    else:
        _print_plain(shown)

    if args.magic and not args.errors_only:
        _print_magic(magic_total)

    if args.format == "plain":
        base_note = f"（基线内 grandfather {len(error_findings) - len(blocking)} 处）" if args.baseline else ""
        print(f"\n汇总：净新增 ERROR {len(blocking)} 处{base_note}"
              f"{'' if args.errors_only else f' | WARN {len(warn_findings)} 处'}")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
