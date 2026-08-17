"""pandaren/sub_agent/tests/test_llm_config.py — 子 Agent LLM 配置（model + llm_settings）

覆盖：
  - L1 loader 解析：frontmatter 顶层 model + LLM 参数白名单字段 → SubAgentBlueprint
  - L2 builder merge：父级 settings 为底，蓝图字段逐字段覆盖，model 只覆盖 target_model

设计决策（与用户对齐）：
  - settings 继承语义：子 Agent 默认继承主 Agent 的 llm_settings；蓝图显式字段逐字段覆盖父级
  - model 双来源：顶层 `model:` 字段 → bp.model；编程 API llm_settings.target_model → bp.llm_settings
  - 两者同写 → model 顶层字段优先（覆盖 target_model）
  - frontmatter 顶层展开：model / temperature / max_tokens 等平铺在 YAML 顶层

运行：python -m pytest pandaren/sub_agent/tests/test_llm_config.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pandaren.identity.models import TrustLevel, PERMISSION_ALL
from pandaren.llm.types import ModelSettings
from pandaren.sub_agent.loader import (
    _LLM_SETTINGS_FIELDS,
    _parse_llm_settings,
    load_agent_from_file,
)
from pandaren.sub_agent.models import SubAgentBlueprint, SubAgentSource


# ════════════════════════════════════════════════════
#  测试辅助
# ════════════════════════════════════════════════════

class _FakeClient:
    """假 LLM Client：build_blueprint 不真正调用 LLM，只需非 None。"""

    model_name = "fake-model"


def _write_bp_file(tmp_path: Path, frontmatter_extra: str = "") -> Path:
    """写一个最小 Agent 蓝图文件（可注入额外 frontmatter 行）。"""
    p = tmp_path / "test_agent.md"
    p.write_text(
        "---\n"
        "agent_id: test.agent\n"
        "agent_name: 测试子代理\n"
        "when_to_use: 测试用\n"
        "trust_level: sub_agent\n"
        f"{frontmatter_extra}"
        "---\n\n"
        "你是测试子代理。\n",
        encoding="utf-8",
    )
    return p


def _make_bp(
    *,
    model: str | None = None,
    llm_settings: ModelSettings | None = None,
) -> SubAgentBlueprint:
    return SubAgentBlueprint(
        agent_id="test.agent",
        agent_name="测试子代理",
        when_to_use="测试用",
        system_prompt="你是测试子代理。",
        trust_level=TrustLevel.SUB_AGENT,
        sensitive_permissions=PERMISSION_ALL,
        model=model,
        llm_settings=llm_settings,
    )


def _build_sub_agent_blueprint(bp: SubAgentBlueprint, parent_settings: ModelSettings | None):
    """走真实构建链路：父级 builder（带 settings）→ _build_sub_agent_from_blueprint。"""
    from pandaren.builder import AgentBuilder

    builder = (
        AgentBuilder()
        .identity(
            agent_id="parent.agent",
            agent_name="父代理",
            when_to_use="测试用",
            trust_level=TrustLevel.ORCHESTRATOR,
            sensitive_permissions=PERMISSION_ALL,
        )
        .llm(client=_FakeClient())
    )
    if parent_settings is not None:
        builder._llm_settings = parent_settings
    return builder._build_sub_agent_from_blueprint(
        bp=bp,
        llm_client=_FakeClient(),
        tools_pool=[],
        skills_pool=[],
        audit_log=None,
    )


# ════════════════════════════════════════════════════
#  L1：loader 解析
# ════════════════════════════════════════════════════

class TestLoaderParse:
    def test_model_field(self, tmp_path: Path):
        p = _write_bp_file(tmp_path, "model: deepseek-v4-flash\n")
        bp = load_agent_from_file(p, source=SubAgentSource.DIRECTORY)
        assert bp.model == "deepseek-v4-flash"
        assert bp.llm_settings is None

    def test_llm_settings_scalars(self, tmp_path: Path):
        p = _write_bp_file(tmp_path, "temperature: 0.7\nmax_tokens: 4096\n")
        bp = load_agent_from_file(p, source=SubAgentSource.DIRECTORY)
        assert bp.model is None
        assert bp.llm_settings is not None
        assert bp.llm_settings.temperature == 0.7
        assert bp.llm_settings.max_tokens == 4096
        # 未写字段保持 None（不覆盖 provider 默认）
        assert bp.llm_settings.top_p is None
        assert bp.llm_settings.target_model is None

    def test_llm_settings_nested_fields(self, tmp_path: Path):
        p = _write_bp_file(
            tmp_path,
            "reasoning:\n  effort: low\nstop: ['END', 'STOP']\n",
        )
        bp = load_agent_from_file(p, source=SubAgentSource.DIRECTORY)
        assert bp.llm_settings is not None
        assert bp.llm_settings.reasoning == {"effort": "low"}
        assert bp.llm_settings.stop == ["END", "STOP"]

    def test_no_llm_fields_backward_compatible(self, tmp_path: Path):
        p = _write_bp_file(tmp_path)
        bp = load_agent_from_file(p, source=SubAgentSource.DIRECTORY)
        assert bp.model is None
        assert bp.llm_settings is None

    def test_unknown_keys_ignored(self, tmp_path: Path):
        p = _write_bp_file(tmp_path, "foo: bar\nunknown: 123\n")
        bp = load_agent_from_file(p, source=SubAgentSource.DIRECTORY)
        assert bp.model is None
        assert bp.llm_settings is None
        assert bp.agent_id == "test.agent"

    def test_both_model_and_settings(self, tmp_path: Path):
        p = _write_bp_file(tmp_path, "model: m2\ntemperature: 0.5\n")
        bp = load_agent_from_file(p, source=SubAgentSource.DIRECTORY)
        assert bp.model == "m2"
        assert bp.llm_settings is not None
        assert bp.llm_settings.temperature == 0.5

    def test_whitelist_contains_all_model_settings_fields(self):
        from dataclasses import fields

        expected = {f.name for f in fields(ModelSettings)} - {"target_model"}
        assert set(_LLM_SETTINGS_FIELDS) == expected

    def test_parse_llm_settings_empty_returns_none(self):
        assert _parse_llm_settings({"foo": 1}) is None
        assert _parse_llm_settings({}) is None


# ════════════════════════════════════════════════════
#  L2：builder merge 语义
# ════════════════════════════════════════════════════

class TestBuilderMerge:
    def test_no_blueprint_llm_config_inherits_parent(self):
        """蓝图无任何 LLM 字段 → 完全继承父级 settings。"""
        sub = _build_sub_agent_blueprint(
            _make_bp(),
            parent_settings=ModelSettings(temperature=0.7, max_tokens=4096),
        )
        assert sub.llm_settings is not None
        assert sub.llm_settings.temperature == 0.7
        assert sub.llm_settings.max_tokens == 4096

    def test_blueprint_field_overrides_parent_field(self):
        """蓝图写 temperature → 只覆盖该字段，其余继续继承父级。"""
        sub = _build_sub_agent_blueprint(
            _make_bp(llm_settings=ModelSettings(temperature=0.2)),
            parent_settings=ModelSettings(temperature=0.7, max_tokens=4096),
        )
        assert sub.llm_settings.temperature == 0.2   # 蓝图覆盖
        assert sub.llm_settings.max_tokens == 4096   # 仍继承父级

    def test_model_maps_to_target_model_keeps_inherited(self):
        """蓝图写 model → 只设 target_model，其余字段继续继承父级。"""
        sub = _build_sub_agent_blueprint(
            _make_bp(model="deepseek-v4-flash"),
            parent_settings=ModelSettings(temperature=0.7, max_tokens=4096),
        )
        assert sub.llm_settings.target_model == "deepseek-v4-flash"
        assert sub.llm_settings.temperature == 0.7
        assert sub.llm_settings.max_tokens == 4096

    def test_model_wins_over_settings_target_model(self):
        """model 顶层字段 + settings.target_model 同写 → model 优先。"""
        sub = _build_sub_agent_blueprint(
            _make_bp(
                model="model-from-frontmatter",
                llm_settings=ModelSettings(
                    temperature=0.2, target_model="model-from-settings",
                ),
            ),
            parent_settings=ModelSettings(temperature=0.7, max_tokens=4096),
        )
        assert sub.llm_settings.target_model == "model-from-frontmatter"
        assert sub.llm_settings.temperature == 0.2
        assert sub.llm_settings.max_tokens == 4096

    def test_no_parent_no_blueprint_returns_none(self):
        """父级无 settings + 蓝图无配置 → None（provider 默认，现状保持）。"""
        sub = _build_sub_agent_blueprint(_make_bp(), parent_settings=None)
        assert sub.llm_settings is None

    def test_no_parent_uses_blueprint_settings(self):
        """父级无 settings + 蓝图写 → 用蓝图的。"""
        sub = _build_sub_agent_blueprint(
            _make_bp(llm_settings=ModelSettings(temperature=0.3)),
            parent_settings=None,
        )
        assert sub.llm_settings is not None
        assert sub.llm_settings.temperature == 0.3

    def test_no_parent_model_only(self):
        """父级无 settings + 蓝图只写 model → ModelSettings(target_model=...)。"""
        sub = _build_sub_agent_blueprint(
            _make_bp(model="deepseek-v4-flash"),
            parent_settings=None,
        )
        assert sub.llm_settings is not None
        assert sub.llm_settings.target_model == "deepseek-v4-flash"
        assert sub.llm_settings.temperature is None
