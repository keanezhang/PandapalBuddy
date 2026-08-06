"""pandaren/tools/tests/test_bash_issues.py — bash 工具问题全面测试

目的：
  1. 验证 bash 工具基本功能正常（PASS 基线）
  2. 复现并锁定已知缺陷（当前 FAIL/红，修复后应转绿）：
     - 【卡死·根因1】命令内部派生孙进程持有管道 → bash 超时杀不掉进程树，永久卡住
     - 【卡死·根因2】同步 subprocess 阻塞 asyncio 事件循环 → 流式/取消/step_timeout 兜底失效
     - 【编码】UTF-8 输出被按 GBK 解码 → 乱码
     - 【策略】sensitivity=LOW 应为 HIGH、sensitive_permission 缺失（PermissionGuard 旁路）
     - 【工作目录】命令在 PandaPal AppData 执行，不受控
     - 【timeout】无钳制（timeout<=0 立即超时 / 可传任意大值）

运行：
  python -m pytest pandaren/tools/tests/test_bash_issues.py -v

说明：
  涉及真实子进程的命令均在 pytest 进程内执行；嵌套子进程用例用线程 + join 超时
  保护，确保即使触发卡死 bug 也不会挂死整个测试进程。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from pandaren.identity.models import SensitivePermission
from pandaren.tool import ToolContext
from pandaren.tool.execution.executor import ToolExecutor
from pandaren.tool.types import SensitivityLevel
from pandaren.tools import bash

# ── 确保项目根在 sys.path（pytest 从任意 cwd 运行时都能 import pandaren）──
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ════════════════════════════════════════════════════════════
#  Fixtures & 辅助
# ════════════════════════════════════════════════════════════

@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(
        run_id="test-bash-issues",
        step_n=1,
        agent_id="test-agent",
        session_id="test-session",
    )


@pytest.fixture
def executor() -> ToolExecutor:
    return ToolExecutor()


def _write_temp_script(code: str) -> str:
    """写临时 python 脚本，返回路径。"""
    fd, path = tempfile.mkstemp(suffix=".py", prefix="bash_test_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(code)
    return path


def _cleanup_ping() -> None:
    """清理测试可能残留的 ping 进程（孙进程卡死用例）。"""
    try:
        subprocess.run(
            "taskkill /IM ping.exe /F",
            shell=True, capture_output=True, timeout=5,
        )
    except Exception:
        pass


# ════════════════════════════════════════════════════════════
#  一、基本功能基线（当前应全部 PASS）
# ════════════════════════════════════════════════════════════

def test_basic_echo(ctx: ToolContext) -> None:
    """正常命令应返回输出。"""
    out = bash.executor(ctx, command="echo hello-pandaren", timeout=5)
    assert "hello-pandaren" in out


def test_exit_code_reported(ctx: ToolContext) -> None:
    """非零退出码应体现在输出中。"""
    out = bash.executor(ctx, command='python -c "import sys; sys.exit(3)"', timeout=5)
    assert "[exit code: 3]" in out


def test_stderr_merged(ctx: ToolContext) -> None:
    """stderr 应合并进输出。"""
    out = bash.executor(ctx, command="python -c \"import sys; sys.stderr.write('ERR-MARK')\"", timeout=5)
    assert "ERR-MARK" in out


def test_timeout_no_nested_subprocess(ctx: ToolContext) -> None:
    """无孙进程时，超时应在预算内返回（不卡死）。"""
    t0 = time.monotonic()
    out = bash.executor(ctx, command='python -c "import time; time.sleep(5)"', timeout=2)
    elapsed = time.monotonic() - t0
    assert "命令超时" in out
    assert elapsed < 4.5, f"无孙进程场景也超时返回过慢：{elapsed:.1f}s"


def test_empty_command_not_hang(ctx: ToolContext) -> None:
    """空命令不应抛异常、不应卡死。"""
    out = bash.executor(ctx, command="", timeout=3)
    assert isinstance(out, str)


def test_policy_audit_required() -> None:
    """bash 调用必须留审计。"""
    assert bash.policy.audit_required is True


async def test_output_truncated_via_executor(ctx: ToolContext, executor: ToolExecutor) -> None:
    """大输出应被 executor 层按 max_output_bytes 截断（防止撑爆上下文）。"""
    script = _write_temp_script("print('x' * 100000)")
    try:
        result = await executor.execute(
            bash, {"command": f'python "{script}"', "timeout": 5}, ctx,
        )
        assert result.success
        assert result.truncated is True
        assert len(result.data.encode("utf-8")) <= bash.policy.max_output_bytes
    finally:
        os.unlink(script)


# ════════════════════════════════════════════════════════════
#  二、已知缺陷回归（当前应 FAIL/红，修复后转绿）
# ════════════════════════════════════════════════════════════

def test_nested_subprocess_returns_within_budget(ctx: ToolContext) -> None:
    """【卡死·根因1】命令内部派生孙进程（持有管道）时，bash 必须在 timeout 预算内返回。

    构造：python 脚本启动 ping（长命孙进程）后 1 秒正常退出。
    期望（修复后）：bash 超时后 taskkill /T /F 杀进程树 → 管道关闭 → 返回"命令超时"。
    现状（bug）：kill 只杀 cmd.exe，ping 残留持有管道 → communicate() 无限等 → 卡死。
    """
    script = _write_temp_script(
        "import subprocess, time\n"
        "subprocess.Popen('ping -n 60 127.0.0.1', shell=True)\n"
        "time.sleep(1)\n"
    )
    try:
        holder: dict = {}

        def _run() -> None:
            holder["out"] = bash.executor(ctx, command=f'python "{script}"', timeout=4)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(8)  # bash 超时预算 4s + 3s 收尾余量
        assert not t.is_alive(), (
            "BUG 复现：bash 被孙进程（ping）持有的管道卡死，8 秒未返回。"
            "修复：超时后 taskkill /PID <pid> /T /F 杀进程树"
        )
        assert "命令超时" in holder["out"]
    finally:
        _cleanup_ping()


def test_utf8_output_not_garbled(ctx: ToolContext) -> None:
    """【编码】中文输出不应乱码。

    修复后的现实：Windows cmd 默认代码页 936，内建命令与 python print 均输出
    GBK 字节，bash 按 GBK 解码 → 中文正常（此前按 UTF-8 解码 GBK 字节 → 乱码）。
    """
    script = _write_temp_script(
        "print('中文测试')\n"
    )
    try:
        out = bash.executor(ctx, command=f'python "{script}"', timeout=5)
        assert "中文测试" in out, f"中文输出被错误解码（疑似编码不匹配）：{out!r}"
    finally:
        os.unlink(script)


def test_multiline_command_all_lines_run(ctx: ToolContext) -> None:
    """【多行】多行命令应全部执行（不再只执行第一行）。"""
    out = bash.executor(ctx, command="echo line-A\necho line-B\necho line-C", timeout=5)
    assert "line-A" in out and "line-B" in out and "line-C" in out, (
        f"多行命令未全部执行：{out!r}"
    )


def test_undefined_var_expands_empty(ctx: ToolContext) -> None:
    """【变量】未定义变量 %var% 应展开为空（不再保留字面量）。"""
    if not sys.platform.startswith("win"):
        pytest.skip("cmd 批处理语义，仅 Windows")
    out = bash.executor(ctx, command="echo x-%no_such_var_zzz%-y", timeout=5)
    assert "%no_such_var_zzz%" not in out, f"未定义变量保留字面量：{out!r}"
    assert "x--y" in out, f"未定义变量未展开为空：{out!r}"


def test_multiline_var_roundtrip(ctx: ToolContext) -> None:
    """【多行+变量】set 后跨行读取应得到新值（cmd /c 直传时连第一行都被吞）。"""
    if not sys.platform.startswith("win"):
        pytest.skip("cmd 批处理语义，仅 Windows")
    out = bash.executor(ctx, command="set v=42\necho v-is-%v%", timeout=5)
    assert "v-is-42" in out, f"跨行变量读取失败：{out!r}"


def test_ifelse_next_line_not_swallowed(ctx: ToolContext) -> None:
    """【if/else】if/else 换行书写时，后续命令不应被 else 块吞掉。"""
    if not sys.platform.startswith("win"):
        pytest.skip("cmd 批处理语义，仅 Windows")
    out = bash.executor(
        ctx,
        command=(
            "if exist C:\\Windows\\System32\\cmd.exe (echo found) else (echo missing)\n"
            "if not exist C:\\no_such_dir_zzz (echo second-ran)"
        ),
        timeout=5,
    )
    assert "found" in out, f"if 分支未执行：{out!r}"
    assert "second-ran" in out, f"后续命令被 else 块吞掉：{out!r}"


async def test_event_loop_not_blocked(ctx: ToolContext, executor: ToolExecutor) -> None:
    """【卡死·根因2】bash 执行期间事件循环必须保持可调度。

    构造：后台 tick 协程每 0.05s 计数一次，同时执行 sleep 2s 的命令。
    期望（修复后）：run_in_executor 隔离 → tick 持续增长（约 30+ 次）。
    现状（bug）：同步 subprocess 冻结事件循环 → tick 几乎不增长。
    """
    tick_count = {"n": 0}

    async def _tick() -> None:
        while True:
            tick_count["n"] += 1
            await asyncio.sleep(0.05)

    t = asyncio.create_task(_tick())
    await asyncio.sleep(0.3)  # 先让 tick 预热
    before = tick_count["n"]

    await executor.execute(
        bash, {"command": 'python -c "import time; time.sleep(2)"', "timeout": 10}, ctx,
    )

    await asyncio.sleep(0.3)
    t.cancel()
    grown = tick_count["n"] - before
    assert grown >= 15, (
        f"事件循环被 bash 同步执行冻结：sleep 2s 期间 tick 仅增长 {grown} 次"
        f"（期望 >= 15）。修复：ToolExecutor 用 run_in_executor 隔离同步工具"
    )


async def test_step_timeout_can_interrupt(ctx: ToolContext, executor: ToolExecutor) -> None:
    """【卡死·根因2】step_timeout 兜底（asyncio.wait_for）应能打断 bash。

    期望（修复后）：wait_for(2s) 在 2 秒抛 TimeoutError（事件循环未被冻结）。
    现状（bug）：同步阻塞事件循环 → wait_for 超时无法调度 → 等 bash 跑完 4s 才返回，
    且不抛 TimeoutError（超时兜底彻底失效）。
    """
    t0 = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            executor.execute(
                bash,
                {"command": 'python -c "import time; time.sleep(4)"', "timeout": 60},
                ctx,
            ),
            timeout=2,
        )
    elapsed = time.monotonic() - t0
    assert elapsed < 6, f"step_timeout 兜底失效，等了 {elapsed:.1f}s（事件循环被 bash 冻结）"


@pytest.mark.xfail(
    strict=False,
    reason=(
        "策略决策待权限体系统一评估：sensitivity 当前保持 LOW、未启用 SYSTEM_CMD"
        "（改动会影响 PermissionGuard 全局流程，属安全决策，另行处理，此处不擅自改）"
    ),
)
def test_policy_sensitivity_high() -> None:
    """【策略】bash 能执行任意系统命令，sensitivity 应为 HIGH（当前 LOW）。

    现状：LOW → PermissionGuard 直接放行（identity/models.py:62），任意信任级别可执行任意 shell。
    修复：sensitivity=SensitivityLevel.HIGH。
    """
    assert bash.policy.sensitivity == SensitivityLevel.HIGH, (
        f"bash 敏感度应为 HIGH，当前={bash.policy.sensitivity}"
    )


@pytest.mark.xfail(
    strict=False,
    reason=(
        "策略决策待权限体系统一评估：sensitive_permission 当前未启用 SYSTEM_CMD"
        "（改动会影响 PermissionGuard 全局流程，属安全决策，另行处理，此处不擅自改）"
    ),
)
def test_policy_sensitive_permission_system_cmd() -> None:
    """【策略】应声明 SYSTEM_CMD 权限（当前被注释掉）。

    现状：None → 高敏感工具无权限声明，权限守卫形同虚设。
    修复：sensitive_permission=SensitivePermission.SYSTEM_CMD。
    """
    assert bash.policy.sensitive_permission == SensitivePermission.SYSTEM_CMD, (
        f"应声明 SYSTEM_CMD 权限，当前={bash.policy.sensitive_permission}"
    )


def test_working_dir_project_root(ctx: ToolContext) -> None:
    """【工作目录】bash 命令应在项目根目录执行（当前落在 PandaPal AppData）。

    现状：无 cwd 控制 → %cd% 是 AppData\\Local\\PandaPal，与 llm_guide 的
    "使用绝对路径" 矛盾，相对路径行为不可预期。
    修复：bash 支持 cwd 参数，默认项目根目录。
    """
    out = bash.executor(ctx, command="echo %cd%", timeout=5)
    cwd = out.strip()
    assert str(PROJECT_ROOT).lower() in cwd.lower(), (
        f"bash 工作目录不受控：当前={cwd!r}，期望={PROJECT_ROOT}"
    )


def test_timeout_zero_clamped(ctx: ToolContext) -> None:
    """【timeout】timeout<=0 应被钳制为默认值，而不是立即超时或抛错。

    现状：timeout=0 → subprocess.run 立即抛 TimeoutExpired → "命令超时（0秒）"。
    修复：timeout = min(max(timeout, 默认), 上限)。
    """
    out = bash.executor(ctx, command="echo ok", timeout=0)
    assert "命令超时" not in out, f"timeout=0 未钳制，bash 立即超时：{out!r}"
