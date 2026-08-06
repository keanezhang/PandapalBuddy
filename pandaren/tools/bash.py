"""pandaren/tools/bash.py — bash 内置工具

执行 shell 命令并返回输出。高危操作，sensitivity=HIGH。
"""

import logging
import os
import platform as _platform
import signal
import subprocess
import tempfile

from pandaren.tool.types import ToolTier, SensitivityLevel
from pandaren.tool import ToolContext
from pandaren.tool.definition.tool_policy import ToolPolicy
from pandaren.tool.decorator import tool
from pandaren.utils.project_root import resolve_project_root

logger = logging.getLogger("pandaren.tools.bash")

_IS_WINDOWS = _platform.system() == "Windows"
_DEFAULT_TIMEOUT = 30
_MAX_TIMEOUT = 60

_OS_HINT = ""
if _IS_WINDOWS:
    _OS_HINT = """
⚠️ 当前环境是 Windows！以下 Linux 命令不可用：
  - find → 用 glob 工具或 dir /s
  - pwd → 用 echo %cd% 或 cd（不带参数）
  - ls → 用 dir 或 list_files 工具
  - cat → 用 type 或 read_file 工具
  - grep → 用 grep 工具（ripgrep）
  - rm → 用 del
  - cp → 用 copy
  - mv → 用 move
  - chmod → 不适用，Windows 无此命令

Windows 命令通过临时 .bat 脚本执行，天然支持多行：
  - 变量展开、if/else 块与标准 cmd 批处理一致
  - if/else 与后续命令请换行写，避免一行内 & 连接被 else 块吞掉
  - for 循环变量请用 %%i（批处理标准），而非交互式 cmd 的 %i
"""

BashLLMGuide = f"""执行 Shell 命令。此工具可执行任意命令，请谨慎使用。

重要规则：
- 仅当其他专用工具无法完成时使用 bash
- 搜索内容用 grep，搜索文件名用 glob，列目录用 list_files
- 永远不要用 bash 运行 rg/grep/find/ls——这些都有专用工具
- 使用绝对路径，避免依赖当前工作目录
- 每次调用执行一个明确的任务，不要在一次调用中做太多事
- 优先使用非破坏性命令，修改文件前确认
- 用 && 连接多个相关命令，用 ; 连接多个独立命令
{_OS_HINT}"""


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """超时后查杀整个进程树（含孙进程）。

    关键：subprocess 超时只杀直接子进程（cmd.exe / sh），命令内部再起的
    孙进程仍活着并持有管道写句柄 → 管道永不 EOF → communicate() 无限阻塞。
    必须把整棵树杀掉，管道才会关闭。Windows 用 taskkill /T /F，
    POSIX 配合 start_new_session=True 杀整个进程组。
    """
    try:
        if _IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                proc.kill()
    except Exception:
        logger.exception("查杀进程树失败: pid=%s", proc.pid)


def _resolve_cwd(cwd: str | None) -> str | None:
    """工作目录解析：显式 cwd > 工作区根目录 > 继承进程 CWD。"""
    if cwd:
        return cwd
    try:
        return str(resolve_project_root())
    except Exception:
        return None


def _write_batch_script(command: str) -> tuple[str, str]:
    """把命令写入临时 .bat 文件，返回 (文件路径, 解码编码)。

    为什么用 .bat 而不是 `cmd /c <command>` 直传：
      - cmd /c 的引号解析怪癖导致多行命令只执行第一行、未定义变量 %var%
        保留字面量（不回退为空）——.bat 文件逐行解析，行为与标准批处理一致；
      - 编码策略：优先 GBK（cmd 默认代码页 936，内建命令输出 GBK 字节，
        解码也要用 GBK 才对）；命令含无法 GBK 编码的字符（如 emoji）时
        降级 UTF-8 + chcp 65001。
    """
    body = command.replace("\n", "\r\n")
    try:
        body.encode("gbk")
        encoding, prefix = "gbk", "@echo off\r\n"
    except UnicodeEncodeError:
        encoding, prefix = "utf-8", "@echo off\r\nchcp 65001 >nul\r\n"
    script = prefix + body + "\r\n"

    fd, path = tempfile.mkstemp(suffix=".bat", prefix="pandaren_bash_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(script.encode(encoding))
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path, encoding


def _run_windows(command: str, timeout: int, work_dir: str | None) -> tuple[int, str, str, bool]:
    """Windows 分支：写临时 .bat 再执行（修复 cmd /c 直传的多行/变量展开怪癖）。"""
    path, encoding = _write_batch_script(command)
    bat_arg = f'"{path}"' if " " in path else path
    try:
        proc = subprocess.Popen(
            ["cmd.exe", "/d", "/c", bat_arg],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding=encoding,
            errors="replace",
            cwd=work_dir,
            creationflags=(
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            ),
        )
    except Exception as e:
        try:
            os.unlink(path)
        except OSError:
            pass
        return 0, "", f"命令启动失败：{e}", False

    try:
        try:
            out, err = proc.communicate(timeout=timeout)
            return proc.returncode or 0, out or "", err or "", False
        except subprocess.TimeoutExpired as exc:
            _kill_process_tree(proc)
            try:
                # 兜底 2s：直接子进程若已先退出（taskkill 无目标），
                # 孤儿孙进程仍持有管道 → 本次 communicate 会再次超时，
                # 由 except 返回部分输出，保证总耗时 ≤ timeout + 2s
                out, err = proc.communicate(timeout=2)
                return proc.returncode or 0, out or "", err or "", True
            except Exception:
                out = exc.output if isinstance(exc.output, str) else ""
                err = exc.stderr if isinstance(exc.stderr, str) else ""
                return 1, out or "", err or "", True
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _run_posix(command: str, timeout: int, work_dir: str | None) -> tuple[int, str, str, bool]:
    """POSIX 分支：shell 直传（多行/变量天然正常，UTF-8 解码）。"""
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=work_dir,
            start_new_session=True,
        )
    except Exception as e:
        return 0, "", f"命令启动失败：{e}", False

    try:
        try:
            out, err = proc.communicate(timeout=timeout)
            return proc.returncode or 0, out or "", err or "", False
        except subprocess.TimeoutExpired as exc:
            _kill_process_tree(proc)
            try:
                # 兜底 2s：直接子进程若已先退出（taskkill 无目标），
                # 孤儿孙进程仍持有管道 → 本次 communicate 会再次超时，
                # 由 except 返回部分输出，保证总耗时 ≤ timeout + 2s
                out, err = proc.communicate(timeout=2)
                return proc.returncode or 0, out or "", err or "", True
            except Exception:
                out = exc.output if isinstance(exc.output, str) else ""
                err = exc.stderr if isinstance(exc.stderr, str) else ""
                return 1, out or "", err or "", True
    finally:
        pass  # POSIX 无临时文件


@tool.function(
    tier=ToolTier.ALWAYS,
    name="bash",
    description="执行 shell 命令并返回输出",
    when_to_use=(
        "仅当需要执行系统命令（非文件搜索/非内容搜索/非目录浏览）时调用。"
        "搜索文件请用 glob，搜索内容请用 grep，列目录请用 list_files"
    ),
    policy=ToolPolicy(
        sensitivity=SensitivityLevel.LOW,
        # sensitive_permission=SensitivePermission.SYSTEM_CMD,
        is_reversible=True,
        audit_required=True,
        is_idempotent=False,
        max_calls_per_turn=20,
        max_output_bytes=50000,
    ),
    llm_guide=BashLLMGuide,
    progress_label='执行命令「{command}」',
)
def bash(ctx: ToolContext, command: str, timeout: int = _DEFAULT_TIMEOUT, cwd: str | None = None) -> str:
    """执行 shell 命令并返回标准输出和标准错误。

    Args:
        command: 要执行的 shell 命令（Windows 下支持多行，会写入临时 .bat 执行）。
        timeout: 超时时间（秒），默认 30 秒，最大 60 秒。
        cwd: 工作目录，默认为当前工作区根目录。

    Returns:
        命令的标准输出和标准错误合并文本；若超时或执行失败则返回错误信息。
    """
    # timeout 钳制：LLM 可传任意值，限制在 [1, 60]，
    # 避免一次卡死冻结过久
    try:
        timeout = min(max(int(timeout), 1), _MAX_TIMEOUT)
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT

    work_dir = _resolve_cwd(cwd)

    if _IS_WINDOWS:
        code, out, err, timed_out = _run_windows(command, timeout, work_dir)
    else:
        code, out, err, timed_out = _run_posix(command, timeout, work_dir)

    if timed_out:
        logger.warning("bash 命令超时（%ds），已查杀进程树: %s", timeout, command)
        partial = ((out or "") + (err or "")).strip()
        result = f"命令超时（{timeout}秒）：{command}"
        if partial:
            result += f"\n\n超时前已收集的部分输出：\n{partial}"
        return result

    output = ""
    if out:
        output += out
    if err:
        output += f"\n[stderr]\n{err}"
    if code:
        output += f"\n[exit code: {code}]"
    return output.strip() or "(无输出)"
