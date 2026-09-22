"""模型事实解析器用例：RES-01..RES-10。

旧版本测的是「模型 → 档位 → 各字段配额（比例 × M）」全链路；
档位表已删除，本文件只覆盖**模型事实**解析 + 配置表结构护栏 + I1 校验落点。
"""

from __future__ import annotations

import logging
import tomllib

import pytest

from pandapal.config.llm import context_window_resolver as cr
from pandapal.config.llm.context_budget import (
    check_i1,
    output_reserve_hard_limit,
)


@pytest.fixture(autouse=True)
def _reset_table(monkeypatch):
    monkeypatch.delenv("PANDAPAL_MODEL_MAX_CONTEXT", raising=False)
    saved = cr._TABLE
    cr._TABLE = None
    yield
    cr._TABLE = saved


def _fake_table() -> dict:
    return {
        "default": {"max_context": 128_000, "max_output": 32_000},
        "models": {"m1m": {"max_context": 1_000_000, "max_output": 64_000}},
        "patterns": [
            {"glob": "*1m*", "max_context": 1_000_000},
            {"glob": "*128k*", "max_context": 128_000},
            {"glob": "claude-*", "max_context": 200_000, "max_output": 8_192},
        ],
    }


# ─────────────────────────────────────────────────────────────
# RES-01/02：环境变量覆盖（最高优先级）
# ─────────────────────────────────────────────────────────────


def test_res01_env_override_wins(monkeypatch, caplog):
    monkeypatch.setenv("PANDAPAL_MODEL_MAX_CONTEXT", "2000000")
    with caplog.at_level(logging.WARNING, logger=cr.__name__):
        assert cr.resolve_model_max_context("claude-sonnet-4") == (2_000_000, None)
    assert any("环境变量" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("bad", ["", "not-a-number", "0", "-5"])
def test_res02_invalid_env_ignored(monkeypatch, caplog, bad):
    monkeypatch.setattr(cr, "_load_table", _fake_table)
    monkeypatch.setenv("PANDAPAL_MODEL_MAX_CONTEXT", bad)
    with caplog.at_level(logging.WARNING, logger=cr.__name__):
        assert cr.resolve_model_max_context("claude-anything")[0] == 200_000


# ─────────────────────────────────────────────────────────────
# RES-03/04：优先级——精确 > 通配（大小写不敏感）
# ─────────────────────────────────────────────────────────────


def test_res03_exact_beats_pattern(monkeypatch):
    monkeypatch.setattr(cr, "_load_table", _fake_table)
    assert cr.resolve_model_max_context("m1m") == (1_000_000, 64_000)


def test_res04_pattern_case_insensitive(monkeypatch):
    monkeypatch.setattr(cr, "_load_table", _fake_table)
    assert cr.resolve_model_max_context("Claude-Sonnet-4")[0] == 200_000


# ─────────────────────────────────────────────────────────────
# RES-05/06：兜底
# ─────────────────────────────────────────────────────────────


def test_res05_default_fallback_with_warning(monkeypatch, caplog):
    monkeypatch.setattr(cr, "_load_table", _fake_table)
    with caplog.at_level(logging.WARNING, logger=cr.__name__):
        assert cr.resolve_model_max_context("totally-unknown") == (128_000, 32_000)
    assert any("未在" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("model_id", ["", "   "])
def test_res06_empty_model_id_goes_default(monkeypatch, model_id):
    monkeypatch.setattr(cr, "_load_table", _fake_table)
    assert cr.resolve_model_max_context(model_id) == (128_000, 32_000)


# ─────────────────────────────────────────────────────────────
# RES-07/08：表加载缓存与容错
# ─────────────────────────────────────────────────────────────


def test_res07_load_table_cache_returns_same_object(monkeypatch):
    first = cr._load_table()
    assert cr._load_table() is first


def test_res08_toml_unavailable_falls_back(monkeypatch, caplog):
    monkeypatch.setattr(cr, "_TOML_PATH", cr._TOML_PATH.with_name("__no_such__.toml"))
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger=cr.__name__):
        table = cr._load_table()
    assert table["default"]["max_context"] == 128_000
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


# ─────────────────────────────────────────────────────────────
# RES-09：真实配置文件的结构护栏（验收项：[[tiers]] 不复存在）
# ─────────────────────────────────────────────────────────────


def test_res09_real_toml_has_no_tiers_and_declares_max_output():
    data = tomllib.loads(cr._TOML_PATH.read_text(encoding="utf-8"))
    assert "tiers" not in data, "档位表已删除，不应再出现 [[tiers]]"
    assert data["default"]["max_context"] == 128_000
    assert data["default"]["max_output"] == 32_000
    # 通配段是数组表（承载 max_output），不是 dict
    assert isinstance(data["patterns"], list)


# ─────────────────────────────────────────────────────────────
# RES-10：I1 校验（固定比例 0.80 下等价于「输出预留 ≤ 8% × M」）
# ─────────────────────────────────────────────────────────────


def test_res10_i1_output_reserve_limit():
    assert output_reserve_hard_limit(1_000_000) == 80_000
    assert output_reserve_hard_limit(200_000) == 16_000


@pytest.mark.parametrize("m, reserve, ok", [
    (1_000_000, 32_000, True),
    (400_000, 32_000, True),    # 恰好临界
    (200_000, 32_000, False),   # 超 2x
    (128_000, 32_000, False),   # 超 3x
])
def test_res10_check_i1(m, reserve, ok, caplog):
    with caplog.at_level(logging.WARNING, logger="pandapal.config.llm.context_budget"):
        assert check_i1(m, reserve) is ok
    assert (not ok) == any(r.levelno == logging.WARNING for r in caplog.records)
