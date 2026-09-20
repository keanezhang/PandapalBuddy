"""pandapal.local.prompt_fragments 单元测试 + Pool 内容热重载集成测试。

覆盖（风险驱动）：
  - R1 [P0] 内容未变 → 绝不重建/rebind（保 prompt cache 字节稳定）
  - R2 [P0] 内容变化 → 自动重建并 rebind
  - R3 [P1] 模式归属正确（coding 片段不进 office，反之亦然）
  - R4 [P1] 片段文件缺失 → 不注入、不崩溃
  - R5 [P1] 四个规范名大小写不敏感（全小写/全大写/混合均可命中）
  - R6 [P1] 读失败 → 保留旧值（不清空 prompt）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pandapal.local.prompt_fragments import PromptAssembler
from pandapal.scheduler.agent_pool import SessionAgentPool

BASE = {"coding": "CODING_BASE", "office": "OFFICE_BASE"}
ENV = "## 运行环境\nworkspace=/tmp/ws"


def _make(tmp_path: Path) -> PromptAssembler:
    return PromptAssembler(
        base_prompts=BASE, env_block=ENV, work_dir=tmp_path,
    )


# ─── PromptAssembler 单元测试 ────────────────────────────────────────────


def test_build_injects_fragments_by_mode(tmp_path: Path) -> None:
    """R3：coding / office 各自只拿到本模式片段，且 base + 片段标题都在。"""
    (tmp_path / "PANDAPAL.md").write_text("PROJ-GUIDE", encoding="utf-8")
    (tmp_path / "soul.md").write_text("USER-SOUL", encoding="utf-8")
    a = _make(tmp_path)

    coding = a.get("coding")
    office = a.get("office")
    assert coding is not None and office is not None

    assert "CODING_BASE" in coding and "PROJ-GUIDE" in coding
    assert "USER-SOUL" not in coding  # office 专属不进 coding
    assert "## 项目指引" in coding  # 片段标题注入

    assert "OFFICE_BASE" in office and "USER-SOUL" in office
    assert "PROJ-GUIDE" not in office  # coding 专属不进 office
    assert "## 用户性格与偏好" in office


def test_unchanged_content_returns_same_object(tmp_path: Path) -> None:
    """R1：内容未变 → refresh() 返回 False，且 get() 返回同一字符串对象（字节稳定）。"""
    (tmp_path / "soul.md").write_text("SOUL", encoding="utf-8")
    a = _make(tmp_path)

    first = a.get("office")
    assert a.refresh() is False
    assert a.get("office") is first  # 同一对象 → 调用方无需 rebind


def test_content_change_rebuilds(tmp_path: Path) -> None:
    """R2：内容变化 → refresh() 返回 True，prompt 更新。"""
    p = tmp_path / "soul.md"
    p.write_text("SOUL-V1", encoding="utf-8")
    a = _make(tmp_path)
    before = a.get("office")
    assert "SOUL-V1" in before

    p.write_text("SOUL-V2", encoding="utf-8")
    assert a.refresh() is True
    after = a.get("office")
    assert after != before
    assert "SOUL-V2" in after and "SOUL-V1" not in after


def test_missing_fragment_not_injected(tmp_path: Path) -> None:
    """R4：文件缺失 → 不注入标题、不崩溃，base + env 仍在。"""
    a = _make(tmp_path)  # 目录为空
    office = a.get("office")
    assert office is not None
    assert "OFFICE_BASE" in office and ENV in office
    assert "## 用户性格与偏好（soul.md）" not in office  # 缺失片段的标题不出现


@pytest.mark.parametrize(
    ("variant", "mode", "marker"),
    [
        # 四个规范名 × 全小写 / 全大写 / 混合大小写
        ("pandapal.md", "coding", "PANDAPAL-LOWER"),
        ("PANDAPAL.md", "coding", "PANDAPAL-UPPER"),
        ("Pandapal.Md", "coding", "PANDAPAL-MIXED"),
        ("coding_rules.md", "coding", "CODING-LOWER"),
        ("CODING_RULES.md", "coding", "CODING-UPPER"),
        ("CoDiNg_RuLeS.MD", "coding", "CODING-MIXED"),
        ("soul.md", "office", "SOUL-LOWER"),
        ("SOUL.MD", "office", "SOUL-UPPER"),
        ("Soul.Md", "office", "SOUL-MIXED"),
        ("office_rules.md", "office", "OFFICE-LOWER"),
        ("OFFICE_RULES.md", "office", "OFFICE-UPPER"),
        ("OfFiCe_RuLeS.mD", "office", "OFFICE-MIXED"),
    ],
)
def test_fragment_filename_case_insensitive(
    tmp_path: Path, variant: str, mode: str, marker: str
) -> None:
    """R5：四个规范名均大小写不敏感——任意大小写组合都能匹配并注入。"""
    (tmp_path / variant).write_text(marker, encoding="utf-8")
    a = _make(tmp_path)
    assert marker in a.get(mode)


def test_read_failure_keeps_previous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R6：读取抛异常 → 保留上次内容，不清空、不标记变化。"""
    (tmp_path / "soul.md").write_text("SOUL-OK", encoding="utf-8")
    a = _make(tmp_path)
    good = a.get("office")
    assert "SOUL-OK" in good

    def _boom(*_args: object, **_kwargs: object) -> str:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", _boom)

    assert a.refresh() is False  # 读失败不视为变化
    assert a.get("office") is good  # 保留旧内容


def test_invalid_mode_returns_none(tmp_path: Path) -> None:
    """mode 非法 / None → None（pool 据此保持当前绑定）。"""
    a = _make(tmp_path)
    assert a.get(None) is None
    assert a.get("bogus") is None


# ─── Pool 内容热重载集成测试 ─────────────────────────────────────────────


class _RecAgent:
    """记录 rebind 调用的 Agent 桩。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def rebind_system_prompt(self, prompt: str) -> None:
        self.prompts.append(prompt)

    def cancel(self) -> None:  # noqa: D401 - 桩
        ...

    async def aclose(self) -> None:
        ...


class _Blueprint:
    def __init__(self, system_prompt: str = "") -> None:
        # 真实 AgentBlueprint 必填字段：materialize 出的 Agent Memory「已烤入」的 prompt
        # （生产见 run_local: agent_builder.system_prompt(assembler.get(DEFAULT_MODE))）。
        # Pool 以其为初始 bound_prompt 判据，故桩须如实携带「构造期烤入值」。
        self.system_prompt = system_prompt
        self.n = 0

    def materialize(self) -> _RecAgent:
        self.n += 1
        return _RecAgent()


class _Broadcast:
    async def send(self, event: object, origin_channel_id: str | None = None) -> None:
        ...


@pytest.mark.asyncio
async def test_pool_rebinds_only_on_content_change(tmp_path: Path) -> None:
    """R1+R2：同内容多次 acquire 不 rebind；内容变更后 acquire 触发一次 rebind。"""
    soul = tmp_path / "soul.md"
    soul.write_text("V1", encoding="utf-8")

    assembler = _make(tmp_path)
    pool = SessionAgentPool(
        blueprint=_Blueprint(system_prompt=assembler.get("office")),
        broadcast=_Broadcast(),
        max_concurrent=2,
        prompt_assembler=assembler,
        default_mode="office",
    )
    await pool.start()
    try:
        async with pool.acquire("s1", "u", mode="office") as agent:
            pass
        # 初始 bound_prompt 已 = default_mode 的 prompt → 首次一致即跳过，无 rebind
        assert agent.prompts == []

        async with pool.acquire("s1", "u", mode="office") as agent:
            pass
        assert agent.prompts == []  # 内容未变 → 仍不 rebind

        soul.write_text("V2", encoding="utf-8")
        async with pool.acquire("s1", "u", mode="office") as agent:
            pass
        assert len(agent.prompts) == 1  # 内容变 → rebind 一次
        assert "V2" in agent.prompts[-1]
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_fragment_added_after_blueprint_build_is_bound(tmp_path: Path) -> None:
    """回归：片段文件在 blueprint 构建之后、session materialize 之前才出现时，仍须被注入。

    真实的坑（run_local 启动序列）：
      t0 应用启动 → agent_builder.system_prompt(assembler.get(DEFAULT_MODE)) 把「当时」的
                    office prompt 烤入 blueprint（此刻 soul.md 尚未存在）；
      t1 用户（或另一个进程）创建 soul.md（片段「热」出现）；
      t2 用户首次发消息 → pool 为新 session materialize 一个 Agent。

    此时 Agent 的 Memory 里烤入的是 t0 的旧 prompt（无片段），而 assembler 现在能解析出
    带片段的新 prompt。若 Pool 的初始 bound_prompt 取「此刻重新解析」的值，就会与 Agent
    实际 prompt 不相等、却恰好等于新内容，被 _apply_mode 判为「无变化」而跳过 rebind，
    片段永不注入。正确做法：初始 bound_prompt 取 blueprint.system_prompt（已烤入值），
    从而检测到差异并 rebind 恰好一次。
    """
    assembler = _make(tmp_path)  # 目录为空 → 构建期 office prompt 不含片段
    baked = assembler.get("office")
    assert baked is not None
    assert "USER-SOUL" not in baked  # 构建期确实没烤入片段

    # blueprint 构建之后，才把 office 专属片段放进来
    (tmp_path / "soul.md").write_text("USER-SOUL", encoding="utf-8")

    pool = SessionAgentPool(
        blueprint=_Blueprint(system_prompt=baked),  # 模拟「t0 烤入的旧 prompt」
        broadcast=_Broadcast(),
        max_concurrent=2,
        prompt_assembler=assembler,
        default_mode="office",
    )
    await pool.start()
    try:
        async with pool.acquire("s1", "u", mode="office") as agent:
            pass
        # 关键断言：检测到「烤入值 ≠ 当前解析值」→ 恰好一次 rebind，且新 prompt 含片段
        assert len(agent.prompts) == 1
        assert "USER-SOUL" in agent.prompts[-1]
    finally:
        await pool.stop()
