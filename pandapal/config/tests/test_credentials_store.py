"""pandapal/config/tests/test_credentials_store.py — 凭据存储不变量与回归防护。

本文件的多数用例对应 PRD 的验收标准（AC-xx），其中两条是**回归防护**，
守的是审查中发现的 🔴 级缺陷，不得复发：

  - AC-07：脱敏 key 不被回写（曾导致用户全部真实 key 被覆写且不可恢复）
  - AC-01：单 provider 多模型（旧主键是 provider，全系统最多 4 个模型）
"""

from __future__ import annotations

import pytest

from pandapal.config.llm.credentials_store import (
    CredentialStore,
    LegacyCredentialFormatError,
    _mask_key,
)

_REAL_KEY = "sk-realkey123456"


@pytest.fixture
def store(tmp_path):
    return CredentialStore(tmp_path)


def _cred(provider: str, model_id: str, **kw):
    """构造一条凭据；默认用系统默认表内的模型，免填单价。"""
    base = {
        "provider": provider,
        "api_key": _REAL_KEY,
        "model_id": model_id,
        "is_default": False,
    }
    base.update(kw)
    return base


# ── AC-01：单 provider 多模型 ────────────────────────────────────────────────


def test_ac01_multiple_models_per_provider(store):
    """同一 provider 下可配多个模型。

    ⚠️ 回归防护：旧实现主键是 provider（拒绝 provider 重复），导致一个 provider
    只能配一个模型、全系统最多 4 个——「model_id 完全用户填、有什么用什么」
    在那个数据模型下根本无法成立。
    """
    store.save_all([
        _cred("dashscope", "qwen-max", is_default=True),
        _cred("dashscope", "qwen-turbo"),
        _cred("dashscope", "qwen-plus"),
    ])
    assert [c["model_id"] for c in store.load_all()] == [
        "qwen-max", "qwen-turbo", "qwen-plus",
    ]


def test_duplicate_primary_key_rejected(store):
    """(provider, model_id) 组合重复 → 拒绝。"""
    with pytest.raises(ValueError, match="重复"):
        store.save_all([
            _cred("dashscope", "qwen-max", is_default=True),
            _cred("dashscope", "qwen-max"),
        ])


def test_ac10_routing_key_collision_rejected(store):
    """AC-10：跨 provider 的 model_id 重名 → 拒绝。

    model_id 同时是 LLMRouter 的路由键，重名会导致「装配了 A 却路由到 B」的
    静默错配——费用记到错误的 provider 账上，预算也守错了额度。
    """
    with pytest.raises(ValueError, match="路由键"):
        store.save_all([
            _cred("openai", "gpt-4o", is_default=True),
            _cred("dashscope", "gpt-4o"),
        ])


# ── AC-07：脱敏 key 不被回写（🔴 回归防护）──────────────────────────────────


def test_ac07_masked_key_writeback_rejected(store):
    """把 load_all() 返回的脱敏值原样提交 → 必须拒绝。

    ⚠️ 回归防护（真实事故）：用户在设置页只改 model_id 点保存，前端把脱敏值
    连同其他字段一起提交，后端仅有的「长度 ≥8」校验放行了 `sk-r***3456`（11 字符），
    结果 toml 中所有 provider 的真实 key 被覆写且**不可恢复**。
    """
    store.save_all([_cred("dashscope", "qwen-max", is_default=True)])
    masked = store.load_all()[0]["api_key"]
    assert _MASK in masked and len(masked) >= 8  # 确认它能骗过长度校验

    with pytest.raises(ValueError, match="脱敏"):
        store.save_all([
            _cred("dashscope", "qwen-max", is_default=True, api_key=masked)
        ])
    # 真实 key 未被破坏
    assert store.load_all_raw()[0]["api_key"] == _REAL_KEY


def test_ac07_omitted_key_preserves_existing(store):
    """省略 api_key（sentinel）→ 沿用旧值，其余字段照常更新。"""
    store.save_all([_cred("dashscope", "qwen-max", is_default=True)])
    store.save_all([
        _cred(
            "dashscope", "qwen-max",
            is_default=True, api_key=None, base_url="https://proxy.example.com/v1",
        )
    ])
    saved = store.load_all_raw()[0]
    assert saved["api_key"] == _REAL_KEY
    assert saved["base_url"] == "https://proxy.example.com/v1"


def test_omitted_key_on_new_credential_rejected(store):
    """新增凭据省略 api_key → 拒绝（绝不写入空 key）。"""
    with pytest.raises(ValueError, match="必须提供 api_key"):
        store.save_all([
            _cred("openai", "gpt-4o", is_default=True, api_key=None)
        ])


# ── AC-05 / AC-06：单价三级回落与「定价表不是白名单」──────────────────────


def test_ac05_model_without_price_source_rejected(store):
    """系统默认表没有、用户也没填 → 拒绝保存。

    绝不放行：放行意味着该模型消费静默计 0、预算守卫对其失效。
    """
    with pytest.raises(ValueError, match="无系统默认单价"):
        store.save_all([
            _cred("openai", "my-finetune-v3", is_default=True)
        ])


def test_ac06_pricing_table_is_not_a_whitelist(store):
    """AC-06 防退化：表外模型只要用户填了价就必须能保存。

    定价表只决定「用户要不要自己填价」，**绝不决定「这个模型能不能用」**。
    """
    store.save_all([
        _cred(
            "openai", "my-finetune-v3", is_default=True,
            input_price_per_1k=0.01, output_price_per_1k=0.04,
        )
    ])
    saved = store.load_all()[0]
    assert saved["model_id"] == "my-finetune-v3"
    assert saved["input_price_per_1k"] == 0.01


def test_half_price_rejected(store):
    with pytest.raises(ValueError, match="同时填写"):
        store.save_all([
            _cred(
                "openai", "my-finetune-v3", is_default=True,
                input_price_per_1k=0.01,
            )
        ])


def test_prices_round_trip(store):
    """单价字段完整往返，不在读写途中丢失。"""
    store.save_all([
        _cred(
            "openai", "custom-x", is_default=True,
            input_price_per_1k=0.011,
            output_price_per_1k=0.044,
            cache_read_price_per_1k=0.0044,
        )
    ])
    c = store.load_all()[0]
    assert (
        c["input_price_per_1k"],
        c["output_price_per_1k"],
        c["cache_read_price_per_1k"],
    ) == (0.011, 0.044, 0.0044)


# ── 默认模型 / provider 白名单 / 旧格式 ────────────────────────────────────


def test_default_is_keyed_by_model_id(store):
    """默认标识落在 model_id 上，同 provider 的另一模型不会被误判为默认。"""
    store.save_all([
        _cred("dashscope", "qwen-max"),
        _cred("dashscope", "qwen-turbo", is_default=True),
    ])
    flags = {c["model_id"]: c["is_default"] for c in store.load_all()}
    assert flags == {"qwen-max": False, "qwen-turbo": True}


def test_exactly_one_default_required(store):
    with pytest.raises(ValueError, match="有且仅有一组"):
        store.save_all([
            _cred("dashscope", "qwen-max", is_default=True),
            _cred("openai", "gpt-4o", is_default=True),
        ])


def test_ac08_provider_outside_catalog_rejected(store):
    """AC-08：provider 超出系统目录 → 拒绝。"""
    with pytest.raises(ValueError, match="不在白名单"):
        store.save_all([_cred("anthropic", "claude-x", is_default=True)])


def test_legacy_format_detected_not_silently_read(store, tmp_path):
    """v1 格式（含 default_provider）→ 明确报错，不做兼容读取。"""
    (tmp_path / "llm_credentials.toml").write_text(
        'default_provider = "dashscope"\n\n'
        '[[credentials]]\nprovider = "dashscope"\n'
        f'api_key = "{_REAL_KEY}"\nmodel_id = "qwen-max"\n',
        encoding="utf-8",
    )
    with pytest.raises(LegacyCredentialFormatError):
        store.load_all()
    # 门禁查询不抛异常，而是报告状态供调用方处置
    assert store.get_status()["legacy_format"] is True


def test_missing_file_returns_empty(store):
    assert store.load_all() == []
    assert store.get_status()["configured"] is False


_MASK = "***"


def test_mask_key_shape():
    assert _mask_key(_REAL_KEY) == "sk-r***3456"
    assert _mask_key("") == ""
