"""pandapal/knowledge_base/tests/test_models.py — KBConfig 校验/兼容 + collection 派生。

覆盖设计文档用例 4 / 6 与新增字段约定：
  - inv-14：validate() 每条判定至少被一条单点用例命中（空名 / 非法名 / 空目录 / 缺 Key /
    缺 Model / 缺 URL / URL 非 http(s) / api_type 非法 / 维度越白名单 / 缺 LLM provider /
    key / model / top_k 越界）
  - from_dict 向后兼容：忽略未知字段；旧 dict 缺 enable_graph 回落默认 False
  - auto_rebuild 默认 True（需求 R3.5）
  - inv-4：collection_name_for 是 name 的纯函数（格式 / 确定性 / 隔离 / 独立对拍）
"""

from __future__ import annotations

import hashlib
import re

import pytest

from pandapal.knowledge_base.models import KBConfig, collection_name_for


def _ok(**overrides) -> KBConfig:
    base = dict(
        name="ok",
        documents_dir="/tmp/kb/documents",
        embedding_api_key="sk-x",
        embedding_model="text-embedding-v3",
        embedding_api_url="https://api.example.com/v1",
        embedding_api_type="text",
        embedding_dimension=1024,
        llm_provider="dashscope",
        llm_api_key="sk-y",
        llm_model="qwen-plus",
        top_k=5,
    )
    base.update(overrides)
    return KBConfig(**base)


# ── inv-14 校验矩阵：每条 raise 至少被单点用例命中（MC-DC）──────────────────


@pytest.mark.parametrize(
    "overrides, exc_substr",
    [
        ({}, None),                                                        # 全合法
        ({"name": ""}, "知识库名称不能为空"),
        ({"name": "a/b"}, "知识库名称只能包含字母、数字、中文、下划线或连字符"),
        ({"documents_dir": ""}, "文档目录不能为空"),
        ({"embedding_api_key": ""}, "必须填写 Embedding API Key"),
        ({"embedding_model": ""}, "必须填写 Embedding 模型名"),
        ({"embedding_api_url": ""}, "必须填写 Embedding API 地址"),
        ({"embedding_api_url": "ftp://x"}, "Embedding API 地址必须以 http:// 或 https:// 开头"),
        ({"embedding_api_type": "xml"}, "Embedding API 类型必须是 text 或 multimodal"),
        ({"embedding_dimension": 999}, "向量维度必须是"),
        ({"llm_provider": ""}, "必须填写抽取 LLM 的 provider"),
        ({"llm_api_key": ""}, "必须填写抽取 LLM 的 API Key"),
        ({"llm_model": ""}, "必须填写抽取 LLM 的模型名"),
        ({"top_k": 0}, "引用来源数量必须在 1~20 之间"),
        ({"top_k": 21}, "引用来源数量必须在 1~20 之间"),
    ],
)
def test_validate_matrix(overrides, exc_substr):
    cfg = _ok(**overrides)
    if exc_substr is None:
        cfg.validate()  # 不抛
        return
    with pytest.raises(ValueError) as exc:
        cfg.validate()
    assert exc_substr in str(exc.value)


def test_validate_multimodal_and_boundary_top_k_pass():
    # 合法边界：multimodal + 白名单维度 + top_k=1/20 + 空 documents_dir 由后端派生
    _ok(embedding_api_type="multimodal", embedding_dimension=2048, top_k=1).validate()
    _ok(top_k=20).validate()


# ── from_dict 向后兼容 ─────────────────────────────────────────────────────


def test_from_dict_ignores_unknown_fields():
    cfg = KBConfig.from_dict({"name": "x", "unknown_field": 1})
    assert cfg.name == "x"
    assert not hasattr(cfg, "unknown_field")


def test_from_dict_roundtrips_enable_graph():
    cfg = KBConfig.from_dict({"name": "x", "enable_graph": True})
    assert cfg.enable_graph is True
    # 落盘（to_dict）→ 回读（from_dict）透传
    assert KBConfig.from_dict(cfg.to_dict()).enable_graph is True
    assert cfg.to_dict()["enable_graph"] is True


def test_from_dict_old_dict_without_enable_graph_defaults_false():
    cfg = KBConfig.from_dict({"name": "x"})
    assert cfg.enable_graph is False


def test_auto_rebuild_defaults_true():
    assert KBConfig(name="x").auto_rebuild is True
    assert KBConfig.from_dict({"name": "x"}).auto_rebuild is True


# ── inv-4 collection_name_for：格式 / 确定性 / 隔离 / 独立对拍 ───────────────


def test_collection_name_for_format_and_determinism():
    value = collection_name_for("公司制度")
    assert re.fullmatch(r"kb_[0-9a-f]{16}", value)
    assert collection_name_for("公司制度") == value  # 确定性


def test_collection_name_for_matches_independent_hash_oracle():
    # Oracle=参考实现：独立用 sha256(name)[:16] 对拍，非抄被测实现
    expected = "kb_" + hashlib.sha256("公司制度".encode("utf-8")).hexdigest()[:16]
    assert collection_name_for("公司制度") == expected


def test_collection_name_for_isolates_distinct_names():
    inputs = ["公司制度", "ascii_kb", "with space", "with-dash", "with_under", "x" * 300, "😀", ""]
    values = [collection_name_for(x) for x in inputs]
    assert len(set(values)) == len(values)  # 不同 name → 不同 collection
    for x in inputs:  # 同名 → 同 collection
        assert collection_name_for(x) == collection_name_for(x)
