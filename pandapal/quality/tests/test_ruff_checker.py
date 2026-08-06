"""RuffChecker 集成测试 —— 跑**真实 ruff 子进程**，读**本仓真实 pyproject.toml**。

设计 §9e 把「与 CI 同尺」列为最高优先级验证项：门控不是在 CI 之外另立标准，
而是把**同一把尺子**从"PR 时"提前到"写完那一秒"。规则若有分歧，就会出现
「门控放行 → CI 红」或「门控拦 → CI 根本不管」，门控的价值当场归零。

这些用例故意不 mock 子进程 —— mock 掉就测不到「配置到底有没有被 ruff 读到」，
而那正是本设计最隐蔽的失效模式（ruff 找不到配置会**静默回落默认档**，不报错）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pandaren.tool.definition.tool_result import FeedbackSeverity

from pandapal.quality.checker import RuffChecker

pytestmark = pytest.mark.skipif(
    shutil.which("ruff") is None, reason="ruff 不在 PATH，跳过真实子进程测试"
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def in_repo_dir():
    """在**仓库内**建临时目录并保证清理。

    per-file-ignores 的 glob（`**/tests/**`）是相对项目根解析的 —— 仓库**外**的文件
    算不出能匹配的相对路径，规则不生效（实测：repo 外 tests/ 下的 F401 照报）。
    生产中 Agent 写的都是仓库内的文件，所以要验 per-file-ignores 就必须在仓库内建文件，
    拿 tmp_path 测等于断言一个永不发生的场景。
    （`ignore` 是全局规则，不涉路径匹配，用 tmp_path 即可。）
    """
    import shutil as _shutil
    d = _REPO_ROOT / "_gate_probe_tmp"
    _shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        _shutil.rmtree(d, ignore_errors=True)


def _write(tmp_path: Path, name: str, content: str) -> str:
    f = tmp_path / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    return str(f)


async def _check(path: str, cwd: str | None = None):
    return await RuffChecker().check(path, timeout=10.0, cwd=cwd or str(_REPO_ROOT))


# ─── 基本能力 ────────────────────────────────────────────────────────


async def test_clean_file_yields_empty_list_not_none() -> None:
    """干净文件 → `[]`（跑通了、没问题），**不是** None（降级）。

    两者语义天差地别：`[]` 会重置熔断计数，None 不会。
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(Path(tmp), "clean.py", "x = 1\n")
        out = await _check(path)

    assert out == []


async def test_detects_target_problems(tmp_path: Path) -> None:
    """PRD 点名的目标问题（F401 未用 import / F821 未定义引用）必须报出来。"""
    path = _write(tmp_path, "bad.py", "import os\n\n\ndef f():\n    return reuslt\n")

    out = await _check(path)

    codes = {d.code for d in out}
    assert "F401" in codes, "未用 import 没报出来"
    assert "F821" in codes, "未定义引用没报出来"


async def test_all_ruff_diagnostics_normalized_to_error(tmp_path: Path) -> None:
    """#16：ruff 诊断一律归一化为 ERROR。

    若有人把 F401 映射成 warning，配上默认 feedback_warnings=false，
    门控对最高频问题就静默归零。
    """
    path = _write(tmp_path, "bad.py", "import os\n\n\ndef f():\n    return reuslt\n")

    out = await _check(path)

    assert out
    assert all(d.severity is FeedbackSeverity.ERROR for d in out)


async def test_diagnostic_carries_location_and_message(tmp_path: Path) -> None:
    path = _write(tmp_path, "bad.py", "import os\n")

    out = await _check(path)

    d = next(x for x in out if x.code == "F401")
    assert d.line == 1
    assert d.column > 0
    assert "os" in d.message
    assert d.checker == "ruff"


async def test_syntax_error_is_reported(tmp_path: Path) -> None:
    """语法错误必须报 —— Agent 写出跑不起来的 Python 正是门控要拦的。"""
    path = _write(tmp_path, "syntax.py", "def f(:\n    pass\n")

    out = await _check(path)

    assert out, "语法错误没报出来"
    assert any("syntax" in d.code.lower() for d in out)


# ─── ★ 与 CI 同尺（最高优先级）★ ──────────────────────────────────────


async def test_inherits_project_ignore_rules(tmp_path: Path) -> None:
    """★ 取一个**同时含 F841（本仓已 ignore）与 F401（未 ignore）**的文件，
    断言门控**只**报 F401、**不**报 F841 → 证明确实继承了 pyproject.toml
    的 [tool.ruff.lint]，而不是跑 ruff 默认档。

    若门控不读配置，会反复要求 Agent 修项目**明确决定不管**的东西
    （F841 有 26 处待人工 triage，部分是「调用只为触发校验、返回值本就不用」
    的有意写法）→ 纯噪音 + 白烧 token + 抬高熔断率。
    """
    path = _write(
        tmp_path, "mixed.py",
        "import os\n\n\ndef f():\n    unused_var = 1\n    return 2\n",
    )

    out = await _check(path)
    codes = {d.code for d in out}

    assert "F401" in codes, "未 ignore 的规则应当报出"
    assert "F841" not in codes, (
        "F841 在本仓 pyproject.toml 里被 ignore 了，门控却报了它 —— "
        "说明没继承项目配置，正在跑 ruff 默认档，与 CI 不是同一把尺子"
    )


async def test_inherits_per_file_ignores(in_repo_dir: Path) -> None:
    """★ per-file-ignores：`**/tests/**` 下的 F401 不该触发。

    否则 Agent 每写一个测试文件都会被 F401 骚扰 —— 测试里 import 不直接引用的
    fixture 是常见写法，项目已在 pyproject.toml 明确豁免。

    必须用**仓库内**路径：见 in_repo_dir 的说明。
    """
    in_tests = _write(in_repo_dir / "tests", "test_x.py", "import os\n")
    outside = _write(in_repo_dir, "plain.py", "import os\n")

    out_tests = await _check(in_tests)
    out_plain = await _check(outside)

    assert {d.code for d in out_tests} == set(), (
        "tests/ 下的 F401 被报了 —— per-file-ignores 没生效"
    )
    assert "F401" in {d.code for d in out_plain}, "对照组：同一临时目录、tests/ 外应正常报 F401"


async def test_selected_rules_only(tmp_path: Path) -> None:
    """本仓 select = ["E4","E7","E9","F"]，未选中的规则档不该冒出来。

    例如 I(import 排序)/UP(语法升级)/B(bugbear) 都没开 —— 若报了它们，
    说明跑的是别的配置，Agent 会被要求做项目根本没要求的改动。
    """
    path = _write(tmp_path, "unsorted.py", "import sys\nimport os\n\nprint(os, sys)\n")

    out = await _check(path)
    codes = {d.code for d in out}

    assert not any(c.startswith(("I0", "UP", "B0")) for c in codes), (
        f"报出了未 select 的规则档: {codes}"
    )


# ─── 降级路径：绝不误导 ───────────────────────────────────────────────


async def test_missing_file_degrades_instead_of_reporting_io_error(tmp_path: Path) -> None:
    """★ 文件不存在 → 降级（None），**绝不**把 E902 当代码问题回灌。

    ruff 用普通诊断（exit 1）报告「文件读不了」，长这样：
        E902 | io-error | 系统找不到指定的文件。 (os error 2)
    若原样回灌，Agent 会收到"你的代码有个 E902 错误"并试图去修一个根本不存在的
    问题 —— 正是 E 原则明令禁止的「误导 Agent 空改」。这不是代码问题，
    是门控自己路径解析错了。
    """
    out = await _check(str(tmp_path / "does_not_exist.py"))

    assert out is None, "文件不存在时必须降级，而不是报 E902 诊断"


async def test_missing_ruff_binary_degrades() -> None:
    """ruff 未安装 → 降级 None + 留痕，绝不产出"代码有问题"。"""
    checker = RuffChecker(command=("definitely_not_a_real_binary_xyz",))

    out = await checker.check("whatever.py", timeout=5.0, cwd=str(_REPO_ROOT))

    assert out is None


async def test_timeout_degrades(tmp_path: Path) -> None:
    """超时 → 杀子进程 + 降级，不阻塞、不误导。"""
    path = _write(tmp_path, "a.py", "x = 1\n")
    checker = RuffChecker()

    out = await checker.check(path, timeout=0.0001, cwd=str(_REPO_ROOT))

    assert out is None


async def test_unparsable_output_degrades(tmp_path: Path) -> None:
    """ruff 输出不是 JSON（如坏配置导致 "ruff failed"）→ 降级。"""
    path = _write(tmp_path, "a.py", "x = 1\n")
    # 用 `ruff --version` 冒充：它成功退出但输出不是 JSON 数组
    checker = RuffChecker(command=("ruff", "--version"))

    out = await checker.check(path, timeout=5.0, cwd=str(_REPO_ROOT))

    assert out is None


# ─── 配置可达性（最隐蔽的失效模式）────────────────────────────────────


async def test_config_reachable_from_foreign_cwd(tmp_path: Path) -> None:
    """★ cwd 指向项目根时，即便被检查的文件在仓库外，也必须读到 pyproject.toml。

    ruff 找不到配置不会报错，会**静默回落默认档** —— 门控看起来在跑、
    结论却与 CI 不同。这是本设计最隐蔽的失效模式，只能靠断言规则行为来拦。
    """
    path = _write(tmp_path, "mixed.py", "def f():\n    unused_var = 1\n    return 2\n")

    with_config = await _check(path, cwd=str(_REPO_ROOT))

    assert with_config is not None
    assert "F841" not in {d.code for d in with_config}, (
        "以项目根为 cwd 却仍报 F841 —— pyproject.toml 没被读到"
    )
