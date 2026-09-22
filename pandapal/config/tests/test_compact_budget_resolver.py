"""pandapal/config/tests/test_compact_budget_resolver.py

覆盖用例 ID：RES-1 ~ RES-12（context_window_resolver 优先级 / 缓存 / fallback /
档位区间 / I1 校验 / 防御性回落 / builder kwargs 契约 / describe_table）。
"""

from __future__ import annotations

import io
import logging

import pytest

from pandapal.config.llm import context_window_resolver as cr


# ════════════════════════════════════════════════════════════════
#  共享 fixture / crafted table
# ════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _reset_resolver(monkeypatch):
    """env 与 _TABLE 缓存双隔离：杜绝跨用例残留。"""
    monkeypatch.delenv("PANDAPAL_MODEL_MAX_CONTEXT", raising=False)
    saved = cr._TABLE
    cr._TABLE = None
    yield
    cr._TABLE = saved


def _four_tiers() -> list[dict]:
    """与 model_context_windows.toml 一致的四个档位（数据注入，非 mock 行为）。"""
    return [
        {
            "name": "small",
            "max_context": 160_000,
            "context_window_ratio": 0.80,
            "system_prompt_ratio": 0.172,
            "system_prompt_floor": 22_000,
            "tool_schema_ratio": 0.078,
            "tool_schema_floor": 8_000,
            "recall_ratio": 0.0,
        },
        {
            "name": "medium",
            "max_context": 300_000,
            "context_window_ratio": 0.80,
            "system_prompt_ratio": 0.110,
            "system_prompt_floor": 22_000,
            "tool_schema_ratio": 0.050,
            "tool_schema_floor": 8_000,
            "recall_ratio": 0.0,
        },
        {
            "name": "large",
            "max_context": 600_000,
            "context_window_ratio": 0.80,
            "system_prompt_ratio": 0.060,
            "system_prompt_floor": 22_000,
            "tool_schema_ratio": 0.020,
            "tool_schema_floor": 8_000,
            "recall_ratio": 0.0,
        },
        {
            "name": "huge",
            "max_context": 999_999_999,
            "context_window_ratio": 0.60,
            "system_prompt_ratio": 0.024,
            "system_prompt_floor": 24_000,
            "tool_schema_ratio": 0.008,
            "tool_schema_floor": 8_000,
            "recall_ratio": 0.0,
        },
    ]


def _table(models=None, patterns=None, tiers=None, default=128_000) -> dict:
    return {
        "default": {"value": default},
        "models": models or {},
        "patterns": patterns or {},
        "tiers": tiers or _four_tiers(),
    }


def _four_tier_table() -> dict:
    """model_id → M 精确映射（含边界值），tiers 与 toml 一致。"""
    return _table(
        models={
            "m128k": 128_000,
            "m200k": 200_000,
            "m400k": 400_000,
            "m1m": 1_000_000,
            "m160k": 160_000,
            "m160k1": 160_001,
            "m600k": 600_000,
            "m600k1": 600_001,
        },
    )


class _FakeTomlPath:
    """可计数 / 可注入失败的 _TOML_PATH 替身。"""

    def __init__(self, data: bytes, *, fail: bool = False):
        self._data = data
        self._fail = fail
        self.name = "model_context_windows.toml"
        self.open_calls = 0

    def open(self, mode: str = "rb"):
        self.open_calls += 1
        if self._fail:
            raise OSError("toml unreadable")
        return io.BytesIO(self._data)


_MINIMAL_TOML = b"""
[default]
value = 128000

[models]

[patterns]

[[tiers]]
name = "small"
max_context = 160000
context_window_ratio = 0.80
system_prompt_ratio = 0.172
system_prompt_floor = 22000
tool_schema_ratio = 0.078
tool_schema_floor = 8000
recall_ratio = 0.0
"""


# ════════════════════════════════════════════════════════════════
#  RES-1 / RES-2  env 优先级与非法值忽略
# ════════════════════════════════════════════════════════════════


# R4 [P0] env 覆盖最高优先级
def test_res01_env_override_wins(monkeypatch, caplog):
    monkeypatch.setenv("PANDAPAL_MODEL_MAX_CONTEXT", "1000000")
    with caplog.at_level(logging.WARNING, logger="pandapal.config.llm.context_window_resolver"):
        result = cr.resolve_model_max_context("any-model")
    assert result == (1_000_000, "env:PANDAPAL_MODEL_MAX_CONTEXT", False)
    assert any("使用环境变量" in r.getMessage() for r in caplog.records)


# R4 [P0] 非法 env 忽略后回落查表
@pytest.mark.parametrize("bad", ["abc", "-1", "0"])
def test_res02_invalid_env_ignored(monkeypatch, caplog, bad):
    monkeypatch.setenv("PANDAPAL_MODEL_MAX_CONTEXT", bad)
    with caplog.at_level(logging.WARNING, logger="pandapal.config.llm.context_window_resolver"):
        result = cr.resolve_model_max_context("claude-sonnet-x")
    assert result == (200_000, "pattern:claude-sonnet*", False)
    assert any("不是正整数，已忽略" in r.getMessage() for r in caplog.records)


# ════════════════════════════════════════════════════════════════
#  RES-3 ~ RES-6  优先级链：exact > pattern > default / 空 model
# ════════════════════════════════════════════════════════════════


# R4 [P0] [models] 精确优先于 [patterns] 通配
def test_res03_exact_beats_pattern(monkeypatch):
    crafted = _table(
        models={"deepseek-v4-chat": 1_000_000},
        patterns={"deepseek-v4*": 131_072},
    )
    monkeypatch.setattr(cr, "_load_table", lambda: crafted)
    assert cr.resolve_model_max_context("deepseek-v4-chat") == (
        1_000_000, "exact:deepseek-v4-chat", False,
    )


# R4 [P0] [patterns] fnmatch 大小写不敏感
def test_res04_pattern_case_insensitive(monkeypatch):
    crafted = _table(patterns={"*1m*": 1_000_000})
    monkeypatch.setattr(cr, "_load_table", lambda: crafted)
    assert cr.resolve_model_max_context("VENDOR-1M-FLAGSHIP") == (
        1_000_000, "pattern:*1m*", False,
    )


# R4 [P0] [default] 兜底 + fell_back=True + WARNING
def test_res05_default_fallback_with_warning(monkeypatch, caplog):
    crafted = _table(default=128_000)
    monkeypatch.setattr(cr, "_load_table", lambda: crafted)
    with caplog.at_level(logging.WARNING, logger="pandapal.config.llm.context_window_resolver"):
        result = cr.resolve_model_max_context("unlisted-model")
    assert result == (128_000, "default", True)
    assert any("回落默认上限" in r.getMessage() for r in caplog.records)


# R4 [P1] model_id 空 → default
@pytest.mark.parametrize("model_id", ["", None])
def test_res06_empty_model_id_goes_default(monkeypatch, model_id):
    monkeypatch.setattr(cr, "_load_table", lambda: _table(default=128_000))
    assert cr.resolve_model_max_context(model_id) == (128_000, "default", True)


# ════════════════════════════════════════════════════════════════
#  RES-7  _load_table 缓存 + fallback table
# ════════════════════════════════════════════════════════════════


# R5 [P1] 缓存早退：二次加载同一对象且不再读文件
def test_res07_load_table_cache_returns_same_object(monkeypatch):
    fake = _FakeTomlPath(_MINIMAL_TOML)
    monkeypatch.setattr(cr, "_TOML_PATH", fake)
    cr._TABLE = None
    first = cr._load_table()
    second = cr._load_table()
    assert first is second
    assert fake.open_calls == 1


# R5 [P1] toml 不可用 → 回落内置 fallback tier + ERROR 留痕
def test_res07_load_table_fallback_on_toml_unavailable(monkeypatch, caplog):
    monkeypatch.setattr(cr, "_TOML_PATH", _FakeTomlPath(b"", fail=True))
    cr._TABLE = None
    with caplog.at_level(logging.ERROR, logger="pandapal.config.llm.context_window_resolver"):
        table = cr._load_table()
    assert table["default"]["value"] == 128_000
    assert table["tiers"] == [dict(cr._FALLBACK_TIER)]
    assert any("回落内置默认档位" in r.getMessage() for r in caplog.records)


# ════════════════════════════════════════════════════════════════
#  RES-8  档位区间（四档 golden + 边界）
# ════════════════════════════════════════════════════════════════


# R4/R5 档位落入正确区间（golden 手算）
@pytest.mark.parametrize(
    "model_id, tier, cw, sys_t, tool_t, conv_t",
    [
        ("m128k", "small", 102_400, 22_016, 9_984, 70_400),
        ("m200k", "medium", 160_000, 22_000, 10_000, 128_000),
        ("m400k", "large", 320_000, 24_000, 8_000, 288_000),
        ("m1m", "huge", 600_000, 24_000, 8_000, 568_000),
    ],
)
def test_res08_tier_ranges_golden(monkeypatch, model_id, tier, cw, sys_t, tool_t, conv_t):
    monkeypatch.setattr(cr, "_load_table", lambda: _four_tier_table())
    b = cr.resolve_budget(model_id)
    assert b.tier == tier
    assert b.context_window == cw
    assert b.system_prompt_tokens == sys_t
    assert b.tool_schema_tokens == tool_t
    assert b.conversation_tokens == conv_t


# R4/R5 边界值：首个 `<= max_context` 命中 / 越界取末档
@pytest.mark.parametrize(
    "model_id, tier",
    [
        ("m160k", "small"),
        ("m160k1", "medium"),
        ("m600k", "large"),
        ("m600k1", "huge"),
    ],
)
def test_res08_tier_boundaries(monkeypatch, model_id, tier):
    monkeypatch.setattr(cr, "_load_table", lambda: _four_tier_table())
    assert cr.resolve_budget(model_id).tier == tier


# ════════════════════════════════════════════════════════════════
#  RES-9 / RES-10  I1 校验 / 防御性回落分支
# ════════════════════════════════════════════════════════════════


# R5 [P1] I1 校验：CW + reserved > 0.9×M → WARNING，不拒绝启动
def test_res09_i1_warning_when_over_safety_limit(monkeypatch, caplog):
    monkeypatch.setattr(
        cr, "_load_table",
        lambda: _table(models={"m128k": 128_000, "m1m": 1_000_000}),
    )
    with caplog.at_level(logging.WARNING, logger="pandapal.config.llm.context_window_resolver"):
        over = cr.resolve_budget("m128k", reserved_output_tokens=200_000)
        ok = cr.resolve_budget("m1m", reserved_output_tokens=8_000)
    assert over.context_window == 102_400
    assert ok.context_window == 600_000
    messages = [r.getMessage() for r in caplog.records]
    assert any("I1 校验失败" in m for m in messages)


# R5 [P1] 防御性回落：固定槽位过大 → 按 CW 比例回落（不拒绝启动）
def test_res10_defensive_ratio_fallback(monkeypatch, caplog):
    pathological_tier = {
        "name": "pathological",
        "max_context": 160_000,
        "context_window_ratio": 0.80,
        "system_prompt_ratio": 0.9,
        "system_prompt_floor": 0,
        "tool_schema_ratio": 0.5,
        "tool_schema_floor": 0,
        "recall_ratio": 0.0,
    }
    monkeypatch.setattr(
        cr, "_load_table",
        lambda: _table(models={"m128k": 128_000}, tiers=[pathological_tier]),
    )
    with caplog.at_level(logging.WARNING, logger="pandapal.config.llm.context_window_resolver"):
        b = cr.resolve_budget("m128k")
    assert b.system_prompt_tokens == 15_360   # floor(102_400 × 0.15)
    assert b.tool_schema_tokens == 10_240     # floor(102_400 × 0.10)
    assert b.recall_tokens == 0
    assert b.conversation_tokens == 76_800    # 102_400 − 15_360 − 10_240
    assert any("已按 CW 比例回落" in r.getMessage() for r in caplog.records)


# ════════════════════════════════════════════════════════════════
#  RES-11 / RES-12  builder kwargs 契约 / describe_table smoke
# ════════════════════════════════════════════════════════════════


# R1 [P0] + inv-1 前提：to_builder_kwargs 四键，可直接喂给 AgentBuilder
def test_res11_to_builder_kwargs_contract(monkeypatch):
    from pandaren.builder import AgentBuilder

    monkeypatch.setattr(
        cr, "_load_table",
        lambda: _table(models={"m1m": 1_000_000}),
    )
    b = cr.resolve_budget("m1m")
    kwargs = b.to_builder_kwargs()
    assert kwargs == {
        "context_window": 600_000,
        "system_prompt_tokens_abs": 24_000,
        "tool_schema_tokens_abs": 8_000,
        "recall_ratio": 0.0,
    }
    built = AgentBuilder().context_budget(**kwargs)
    assert isinstance(built, AgentBuilder)


# R5 [P2] describe_table smoke：四档 + header 渲染
def test_res12_describe_table_smoke():
    text = cr.describe_table()
    for tier in ("small", "medium", "large", "huge"):
        assert tier in text
    assert "档位" in text.splitlines()[0]
