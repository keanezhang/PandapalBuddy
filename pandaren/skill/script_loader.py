"""pandaren/skill/script_loader.py — Skill 脚本安全加载器。

职责：
- 从文件系统安全加载 Skill 脚本 Python 模块
- 路径遍历防护（resolve + startswith 校验）
- 模块缓存（避免重复加载）
- 入口函数自动检测

安全约束：
- 仅允许 .py 后缀
- 脚本 resolve 路径必须在 base_path 内（防路径遍历）
- 使用 importlib 标准机制（非 exec/eval）
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import logging
import threading
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from .exceptions import SkillScriptError

logger = logging.getLogger("pandaren.skill.script_loader")

# 模块缓存 + 锁（线程安全）
_loaded_modules: dict[str, ModuleType] = {}
_load_lock = threading.Lock()


def load_skill_script(base_path: str | None, script_relative: str) -> ModuleType:
    """安全加载 Skill 脚本模块。

    Args:
        base_path: Skill 所在目录的绝对路径（SKILL.md 的父目录）。
        script_relative: 脚本文件相对路径（相对于 base_path）。

    Returns:
        加载后的 Python ModuleType。

    Raises:
        SkillScriptError: 路径不安全、文件不存在、加载失败等。
    """
    if not base_path:
        raise SkillScriptError("base_path 为空，无法定位脚本文件")

    if not script_relative:
        raise SkillScriptError("script 路径为空")

    base = Path(base_path).resolve()
    script_path = (base / script_relative).resolve()

    # 安全检查：防路径遍历
    if not str(script_path).startswith(str(base)):
        raise SkillScriptError(
            f"路径遍历拒绝：脚本 '{script_relative}' 解析到 base_path 之外 "
            f"(base={base}, resolved={script_path})"
        )

    # 文件类型限制
    if script_path.suffix != ".py":
        raise SkillScriptError(
            f"仅允许 .py 文件: '{script_path.name}'"
        )

    # 文件存在性
    if not script_path.is_file():
        raise SkillScriptError(
            f"脚本文件不存在: {script_path}"
        )

    # 缓存 key
    cache_key = str(script_path)

    with _load_lock:
        if cache_key in _loaded_modules:
            return _loaded_modules[cache_key]

        # 命名隔离：避免模块名冲突
        path_hash = hashlib.md5(str(script_path).encode()).hexdigest()[:8]
        module_name = f"_pandaren_skill_{path_hash}_{script_path.stem}"

        try:
            spec = importlib.util.spec_from_file_location(
                module_name, str(script_path)
            )
            if spec is None or spec.loader is None:
                raise SkillScriptError(
                    f"无法创建模块 spec: {script_path}"
                )

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)  # type: ignore[union-attr]

            _loaded_modules[cache_key] = module
            logger.debug("Skill 脚本已加载: %s → %s", script_path, module_name)
            return module

        except SkillScriptError:
            raise
        except Exception as e:
            raise SkillScriptError(
                f"脚本加载失败: {script_path} → {e}"
            ) from e


def resolve_entry_function(
    module: ModuleType,
    entry_name: str | None = None,
) -> Callable[..., Any]:
    """从模块中解析入口函数。

    优先级：
    1. entry_name 显式指定 → 精确查找
    2. 未指定 → 扫描模块中所有 public async def
    3. 唯一候选 → 使用
    4. 多个候选 → 报错，要求显式指定

    Args:
        module: 已加载的 Python 模块。
        entry_name: 入口函数名（可选）。

    Returns:
        入口函数引用。

    Raises:
        SkillScriptError: 函数未找到或存在歧义。
    """
    # 显式指定
    if entry_name:
        if not hasattr(module, entry_name):
            raise SkillScriptError(
                f"模块 '{module.__name__}' 中未找到函数 '{entry_name}'"
            )
        func = getattr(module, entry_name)
        if not callable(func):
            raise SkillScriptError(
                f"'{entry_name}' 不是可调用对象 (type={type(func).__name__})"
            )
        return func

    # 自动检测：扫描 public async/sync functions
    candidates: list[tuple[str, Callable]] = []
    for name, obj in inspect.getmembers(module, inspect.isfunction):
        # 跳过私有函数
        if name.startswith("_"):
            continue
        # 跳过从其他模块导入的函数
        if getattr(obj, "__module__", None) != module.__name__:
            continue
        candidates.append((name, obj))

    if not candidates:
        raise SkillScriptError(
            f"模块 '{module.__name__}' 中未找到任何 public function"
        )

    if len(candidates) == 1:
        name, func = candidates[0]
        logger.debug("自动检测入口函数: %s", name)
        return func

    # 多个候选：优先选 async def
    async_candidates = [
        (n, f) for n, f in candidates if inspect.iscoroutinefunction(f)
    ]
    if len(async_candidates) == 1:
        name, func = async_candidates[0]
        logger.debug("自动检测入口函数（唯一 async）: %s", name)
        return func

    # 无法确定
    candidate_names = [n for n, _ in candidates]
    raise SkillScriptError(
        f"模块 '{module.__name__}' 中存在多个候选函数 {candidate_names}，"
        f"请在 SKILL.md 中通过 entry_function 字段显式指定"
    )


def clear_cache() -> None:
    """清空模块缓存（测试用）。"""
    with _load_lock:
        _loaded_modules.clear()
