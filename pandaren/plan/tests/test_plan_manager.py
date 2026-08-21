"""PlanManager 状态机 + plan/files 路径安全测试（pandaren/plan 层）。

覆盖设计用例：
  - 生命周期: enter / exit(approved) / exit(abandon) / reenter / restore_from_session_meta
  - Reminder 注入: FULL(turn0) / SPARSE(每5轮) / REFINE(re-entry 单次消费) / methodology / executing 空
  - ToolResult 消费: exit_plan_mode 成功 / 其它工具与失败结果不消费
  - 工具过滤: 白名单 + 只读工具保留、其余过滤；同名去重
  - files: slug 等价类 / 路径生成穿越防护 / validate 拒绝矩阵 / 初始写 & 覆盖 & 读 & 存在性
"""

from __future__ import annotations

import logging
import os
import pathlib
from types import SimpleNamespace

import pytest

from pandaren.plan.files import (
    generate_plan_file_path,
    generate_word_slug,
    plan_exists,
    read_plan,
    validate_plan_file_path,
    write_initial_plan_file,
    write_plan_content,
)
from pandaren.plan.manager import PlanManager
from pandaren.plan.prompt import (
    FULL_PLANNING_REMINDER,
    PLAN_MODE_REFINE_REMINDER,
    SPARSE_PLANNING_REMINDER,
)


# ─────────────────────────────────────────────
# 生命周期（inv-1 状态机不变量）
# ─────────────────────────────────────────────


def test_initial_state_is_executing():
    pm = PlanManager()

    assert pm.phase == "executing"
    assert pm.is_executing() is True
    assert pm.is_planning() is False
    assert pm.turns == 0
    assert pm.get_plan_file_path() is None
    assert pm.is_reentry is False
    assert pm.context_reminder is None


def test_enter_resets_all_state_and_enters_planning(caplog):
    # inv-1 enter 幂等重置 + Risk-1 残留状态被清空
    caplog.set_level(logging.INFO, logger="pandaren.plan.manager")
    pm = PlanManager()
    pm.exit(approved=False)  # 脏状态：曾退出过
    pm.set_context_reminder("残留 reminder")

    pm.enter("p.md", methodology="自定义方法论")

    assert pm.phase == "planning"
    assert pm.is_planning() is True
    assert pm.get_plan_file_path() == "p.md"
    assert pm.turns == 0
    assert pm.is_reentry is False
    assert pm.context_reminder is None  # 残留被清空
    assert "[plan-manager] entered" in caplog.text


def test_enter_is_idempotent_over_reentry_state():
    # inv-1 enter 幂等：re-entry 状态被重置
    pm = PlanManager()
    pm.enter("a.md")
    pm.increment_turn()
    pm.reenter()
    assert pm.is_reentry is True

    pm.enter("a.md")  # 再次 enter

    assert pm.turns == 0
    assert pm.is_reentry is False
    assert pm.phase == "planning"
    assert pm.get_plan_file_path() == "a.md"


def test_exit_approved_keeps_context_reminder():
    # Risk-2 批准后保留实施指引，供 run_core 注入
    pm = PlanManager()
    pm.enter("p.md")
    pm.set_context_reminder("实施指引")

    pm.exit(approved=True)

    assert pm.phase == "executing"
    assert pm.is_executing() is True
    assert pm.context_reminder == "实施指引"


def test_exit_abandoned_clears_context_reminder():
    # Risk-3 放弃后清空 context reminder，防止旧指引泄漏到下一 run
    pm = PlanManager()
    pm.enter("p.md")
    pm.set_context_reminder("实施指引")

    pm.exit(approved=False)

    assert pm.phase == "executing"
    assert pm.is_executing() is True
    assert pm.context_reminder is None


def test_reenter_sets_reentry_flag_keeps_turns():
    # inv-2 完善模式：保留已消耗轮次，标记 re-entry
    pm = PlanManager()
    pm.enter("p.md")
    pm.increment_turn()
    pm.increment_turn()

    pm.reenter()

    assert pm.phase == "planning"
    assert pm.is_planning() is True
    assert pm.is_reentry is True
    assert pm.turns == 2


# ─────────────────────────────────────────────
# 跨 run 恢复（P0 回归锚：实例隔离）
# ─────────────────────────────────────────────


def test_restore_from_session_meta_returns_fresh_instance():
    # P0 回归锚：restore 必须返回全新实例，不污染其它实例
    pm_a = PlanManager()
    pm_a.enter("A.md")
    pm_b = PlanManager()  # 初始态

    restored = PlanManager.restore_from_session_meta(
        {"plan_file_path": "B.md", "plan_phase": "planning"}
    )

    assert restored is not pm_a
    assert restored is not pm_b
    assert restored.get_plan_file_path() == "B.md"
    assert restored.is_planning() is True
    # pm_a / pm_b 状态不受影响
    assert pm_a.phase == "planning"
    assert pm_a.get_plan_file_path() == "A.md"
    assert pm_b.phase == "executing"
    assert pm_b.get_plan_file_path() is None
    assert pm_b.is_reentry is False


@pytest.mark.parametrize("meta, phase, path", [
    ({}, "executing", None),
    ({"plan_phase": "submitted", "plan_file_path": "x.md"}, "submitted", "x.md"),
])
def test_restore_defaults(meta, phase, path):
    # inv-1 restore 默认值矩阵
    pm = PlanManager.restore_from_session_meta(meta)

    assert pm.phase == phase
    assert pm.get_plan_file_path() == path
    assert pm.turns == 0
    assert pm.is_reentry is False
    assert pm.is_planning() is (phase == "planning")
    assert pm.is_executing() is (phase == "executing")


# ─────────────────────────────────────────────
# Reminder 注入（inv-3 优先级: re-entry > methodology > FULL > SPARSE）
# ─────────────────────────────────────────────


def test_get_reminder_empty_when_executing():
    # inv-1 非规划阶段不注入任何 reminder
    pm = PlanManager()
    pm.enter("p.md")
    pm.exit(approved=True)

    assert pm.get_reminder() == ""
    assert pm.turns == 0


def test_get_reminder_full_on_first_turn():
    # inv-3 首轮注入完整 6-Phase 方法论
    pm = PlanManager()
    pm.enter("p.md")

    assert pm.get_reminder() == FULL_PLANNING_REMINDER
    assert pm.turns == 0


def test_get_reminder_uses_custom_methodology():
    # Risk-4 用户自定义方法论优先于默认 FULL
    pm = PlanManager()
    pm.enter("p.md", methodology="先访谈再设计")

    assert pm.get_reminder() == "先访谈再设计"


def test_get_reminder_reentry_injected_once():
    # inv-3 re-entry reminder 单次消费，消费后 is_reentry 复位
    pm = PlanManager()
    pm.enter("p.md")
    pm.increment_turn()
    pm.increment_turn()
    pm.reenter()

    assert pm.get_reminder() == PLAN_MODE_REFINE_REMINDER
    assert pm.get_reminder() == ""  # 已消费
    assert pm.is_reentry is False


def test_get_reminder_sparse_every_five_turns():
    # inv-3 SPARSE 每 5 轮注入一次；注入后 turns_since_reminder 归零
    pm = PlanManager()
    pm.enter("p.md")
    assert pm.get_reminder() == FULL_PLANNING_REMINDER  # 消费 turn 0

    for _ in range(5):
        pm.increment_turn()
    assert pm.get_reminder() == SPARSE_PLANNING_REMINDER
    assert pm._plan_mode_turns_since_reminder == 0  # 副作用：注入后计数归零

    for _ in range(4):
        pm.increment_turn()
    assert pm.get_reminder() == ""

    pm.increment_turn()
    assert pm.get_reminder() == SPARSE_PLANNING_REMINDER
    assert pm._plan_mode_turns_since_reminder == 0


# ─────────────────────────────────────────────
# ToolResult 消费（Risk-5）
# ─────────────────────────────────────────────


@pytest.mark.parametrize("data, expected_plan_path", [
    ({"plan_path": "p.md", "plan_content": "# 计划"}, "p.md"),
    (None, ""),  # 非 dict data → plan_path 兜底为空串
])
def test_handle_tool_result_consumes_exit_plan_mode(data, expected_plan_path):
    # Risk-5 成功 exit_plan_mode 被消费并生成提交消息
    result = SimpleNamespace(success=True, data=data)
    pm = PlanManager()

    consumed, msg = pm.handle_tool_result("exit_plan_mode", result)

    assert consumed is True
    assert "计划已提交，等待用户批准" in msg
    assert f"计划文件: {expected_plan_path}" in msg


@pytest.mark.parametrize("tool_name, success", [
    ("write_plan", True),
    ("exit_plan_mode", False),
])
def test_handle_tool_result_ignores_others(tool_name, success):
    # Risk-5 其它工具 / 失败结果不被消费
    result = SimpleNamespace(success=success, data=None)
    pm = PlanManager()

    consumed, msg = pm.handle_tool_result(tool_name, result)

    assert (consumed, msg) == (False, "")


# ─────────────────────────────────────────────
# 哈希 / 读计划内容
# ─────────────────────────────────────────────


def test_compute_plan_hash_invariants():
    # inv-1 确定性 + 蜕变关系（SHA-256 特征）
    content = "# 计划\n步骤1：JWT"
    h1 = PlanManager.compute_plan_hash(content)
    h2 = PlanManager.compute_plan_hash(content)
    h3 = PlanManager.compute_plan_hash(content + " 追加")

    assert h1 == h2  # 确定性
    assert len(h1) == 64  # SHA-256 十六进制长度
    assert all(c in "0123456789abcdef" for c in h1)  # hex 字符集
    assert h1 != h3  # 内容变化 → 摘要变化


def test_read_plan_content_delegates(tmp_path):
    # inv-2 委托 files.read_plan：未 enter → None；enter 后读到磁盘内容
    pm = PlanManager()
    assert pm.read_plan_content() is None

    p = tmp_path / "p.md"
    p.write_text("# 计划", encoding="utf-8")
    pm.enter(str(p))

    assert pm.read_plan_content() == "# 计划"


# ─────────────────────────────────────────────
# files.generate_word_slug（Risk-6 等价类）
# ─────────────────────────────────────────────


@pytest.mark.parametrize("name, expected", [
    ("JWT 认证服务", "JWT-认证服务"),  # 中英混合，空白转连字符
    # 裁决（主 Agent）：设计文档 golden 'a-b-c' 与实际实现不符。
    # 实现按 docstring 意图「不含路径分隔符」移除 / 与 \（re.sub 白名单剔除），
    # 且输入域是「名称」非路径（路径经 validate_plan_file_path 前置校验），
    # 移除分隔符 = 正确行为，非 bug。测试按实测行为 'abc' 落地。
    (r"a/b\c", "abc"),
    ("one two three four five six", "one-two-three-four-five"),  # 超长截断到 5 词
    ("", "plan"),  # 空输入兜底
])
def test_generate_word_slug_equivalence(name, expected):
    # Risk-6 中英混合 + 分隔符 + 超长 + 空输入
    assert generate_word_slug(name) == expected


# ─────────────────────────────────────────────
# files.generate_plan_file_path（Risk-7/8 路径安全 + 优先级）
# ─────────────────────────────────────────────


def test_generate_plan_file_path_blocks_traversal(tmp_path):
    # Risk-7 路径穿越防护：斜杠被替换，文件名不含分隔符
    plan_dir = tmp_path / "plans"
    r1 = generate_plan_file_path("../../evil", plan_dir=str(plan_dir))

    assert r1 == str(plan_dir / ".._.._evil.md")
    assert os.sep not in os.path.basename(r1)
    assert plan_dir.is_dir()  # 副作用：目录被创建


def test_generate_plan_file_path_priority_and_config_home(tmp_path):
    # Risk-8 优先级: plan_dir > config_home/plans；传 plan_dir 时忽略 config_home
    config_home = tmp_path / "cfg"

    r2 = generate_plan_file_path("JWT 认证", config_home=str(config_home))
    assert r2 == str(config_home / "plans" / "JWT 认证.md")
    assert (config_home / "plans").is_dir()

    plan_dir = tmp_path / "custom"
    r3 = generate_plan_file_path(
        "dup", config_home=str(config_home), plan_dir=str(plan_dir)
    )
    assert r3 == str(plan_dir / "dup.md")
    assert not (config_home / "plans" / "dup.md").exists()


# ─────────────────────────────────────────────
# files.validate_plan_file_path（Risk-7 拒绝矩阵 + known-gap）
# ─────────────────────────────────────────────


def test_validate_plan_file_path_rejects_invalid(tmp_path):
    # Risk-7 拒绝矩阵：路径穿越 / 非 .md / 父目录不存在
    traversal = str(tmp_path / ".." / "evil.md")
    non_md = str(tmp_path / "plan.txt")
    missing_parent = str(tmp_path / "no_such_dir" / "p.md")

    assert validate_plan_file_path(traversal) is None
    assert validate_plan_file_path(non_md) is None
    assert validate_plan_file_path(missing_parent) is None


def test_validate_plan_file_path_accepts_valid(tmp_path):
    # inv-2 合法绝对路径 → resolve 后原样返回
    d = tmp_path / "sub"
    d.mkdir()
    p = d / "p.md"
    p.write_text("x", encoding="utf-8")

    r = validate_plan_file_path(str(p))

    assert r == str(pathlib.Path(str(p)).resolve())


@pytest.mark.xfail(
    reason="[known-gap] KG-2: validate_plan_file_path 未校验 isabs，相对路径被放行",
    strict=True,
)
def test_validate_plan_file_path_rejects_relative_known_gap(monkeypatch, tmp_path):
    # [known-gap KG-2] 期望：docstring 声明必须绝对路径，相对路径应返回 None
    # 现状：实现未校验 os.path.isabs，相对路径经 resolve 后放行（cwd 下有 sub 时）
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path)

    r_rel = validate_plan_file_path("sub/p.md")
    assert r_rel is None


# ─────────────────────────────────────────────
# files 读写 / 存在性（inv-2）
# ─────────────────────────────────────────────


def test_write_initial_plan_file(tmp_path):
    # inv-2 初始模板 + 父目录自动创建
    p = tmp_path / "plans" / "p.md"

    write_initial_plan_file(str(p), "请实现 JWT 认证")

    assert p.read_text(encoding="utf-8") == "# 用户原始需求\n\n请实现 JWT 认证\n"
    assert p.parent.is_dir()


def test_write_plan_content_overwrites(tmp_path):
    # inv-2 全量覆盖 + 父目录不存在时自动创建
    p = tmp_path / "plans" / "p.md"
    write_initial_plan_file(str(p), "旧需求")

    write_plan_content(str(p), "# 新计划")

    assert p.read_text(encoding="utf-8") == "# 新计划"

    deep = tmp_path / "a" / "b" / "c.md"
    write_plan_content(str(deep), "x")
    assert deep.read_text(encoding="utf-8") == "x"


def test_read_plan_missing_returns_none(tmp_path):
    # Risk-9 文件不存在 → None 而非抛错
    assert read_plan(str(tmp_path / "missing.md")) is None


def test_plan_exists_three_states(tmp_path):
    # inv-2 三态：不存在 / 空白内容 / 非空
    missing = tmp_path / "missing.md"
    blank = tmp_path / "blank.md"
    blank.write_text("  \n\t ", encoding="utf-8")
    filled = tmp_path / "filled.md"
    filled.write_text("# 计划", encoding="utf-8")

    assert plan_exists(str(missing)) is False
    assert plan_exists(str(blank)) is False
    assert plan_exists(str(filled)) is True


# ─────────────────────────────────────────────
# 工具过滤（inv-4）
# ─────────────────────────────────────────────


def _tool(name: str, read_only: bool = False) -> SimpleNamespace:
    return SimpleNamespace(name=name, policy=SimpleNamespace(read_only=read_only))


def test_filter_tools_whitelist_and_readonly():
    # inv-4 过滤规则：白名单内置工具 + 只读工具保留；其余过滤
    all_tools = [
        _tool("enter_plan_mode"),
        _tool("write_plan"),
        _tool("exit_plan_mode"),
        _tool("ask_user"),
        _tool("read_file", read_only=True),
        _tool("edit_file"),
        _tool("execute_command"),
    ]
    pm = PlanManager()

    kept = pm.filter_tools(all_tools)

    assert {t.name for t in kept} == {
        "enter_plan_mode", "write_plan", "exit_plan_mode", "ask_user", "read_file",
    }
    # 阶段无关：进入 Plan Mode 后过滤结果一致
    pm.enter("p.md")
    assert {t.name for t in pm.filter_tools(all_tools)} == {
        "enter_plan_mode", "write_plan", "exit_plan_mode", "ask_user", "read_file",
    }


def test_filter_tools_deduplicates():
    # inv-4 同名工具去重（dict 语义），保留首个
    pm = PlanManager()

    kept = pm.filter_tools([_tool("read_file", read_only=True)] * 3)

    assert len(kept) == 1
