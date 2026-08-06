"""pandapal/config/tests/test_provider_catalog.py — provider_catalog.toml 完整性守卫。

校验系统预置 provider 元信息（单一真相源）的不变量：
  - toml 能正常加载（语法 / 结构正确）
  - providers 列表非空
  - 每个 provider 必填字段齐全（id / display_name / guide_url /
    default_base_url / env_prefix / verify_url）
  - provider id 唯一
  - BUILTIN_PROVIDERS 与底层 SDK ``_PROVIDER_FACTORIES`` 同源（每个 catalog
    provider 必须在 SDK 有对应 for_*() 工厂方法，否则 for_provider() 会抛错）
  - resolve_base_url 语义：user_base_url 非空用用户的，空用 default_base_url
  - catalog_payload 只下发公开字段（不含 env_prefix / verify_url）

设计约束（与 PRD §模型管理 三条规则一致）：
  1. provider 固定：只支持 BUILTIN_PROVIDERS 内的 provider
  2. model_id 完全用户填：本模块不含任何模型数据
  3. 系统配置 toml（只读）与用户配置 toml 分开
"""
from __future__ import annotations

import pandapal.config.llm.provider_catalog as pc


# ── 加载与结构 ────────────────────────────────────────────────────────────


def test_catalog_loads_non_empty():
    """toml 能加载，且 providers 列表非空。"""
    assert len(pc.BUILTIN_PROVIDERS) > 0, "provider_catalog.toml: providers 列表为空"
    assert len(pc.PROVIDER_CATALOG) == len(pc.BUILTIN_PROVIDERS)


def test_required_fields_present():
    """每个 provider 必填字段齐全且非空。"""
    required = ("id", "display_name", "guide_url", "default_base_url",
                "env_prefix", "verify_url")
    for pid in pc.BUILTIN_PROVIDERS:
        meta = pc.PROVIDER_CATALOG[pid]
        for field_name in required:
            val = getattr(meta, field_name)
            assert isinstance(val, str) and val.strip(), (
                f"provider {pid!r}: 字段 {field_name!r} 缺失或为空"
            )


def test_provider_ids_unique():
    """provider id 唯一（_load_catalog 已校验，此处再断言 catalog 字典键无重复）。"""
    ids = [p.id for p in pc.PROVIDER_CATALOG.values()]
    assert len(ids) == len(set(ids)), f"provider id 重复: {ids}"


# ── 与底层 SDK 同源（约束 §4.2）────────────────────────────────────────────


def test_catalog_providers_match_sdk_factories():
    """catalog 的每个 provider 必须在 SDK ``_PROVIDER_FACTORIES`` 内有对应工厂。

    规则 1：provider 固定 = SDK ``_PROVIDER_FACTORIES`` 实际定义的集合。
    若 catalog 列了 SDK 不支持的 provider，for_provider() 装配时会抛 ValueError。
    """
    try:
        from pandaren.llm.client import OpenAICompatibleClient
        sdk_providers = set(OpenAICompatibleClient._PROVIDER_FACTORIES.keys())
    except Exception:
        # SDK 不可导入时跳过（不阻塞单测，CI 环境再校验）
        return
    catalog_providers = set(pc.BUILTIN_PROVIDERS)
    extra = catalog_providers - sdk_providers
    assert not extra, (
        f"catalog 列了 SDK 不支持的 provider: {extra}; "
        f"SDK _PROVIDER_FACTORIES={sorted(sdk_providers)}"
    )


# ── 4 个已知 provider ──────────────────────────────────────────────────────


def test_known_providers_present():
    """当前已知 4 个 provider 必须都在 catalog 内（dashscope/volcengine/openai/deepseek）。

    与底层 SDK ``_PROVIDER_FACTORIES`` 一致（PRD §11.1 已确认）。
    """
    expected = {"dashscope", "volcengine", "openai", "deepseek"}
    assert expected.issubset(set(pc.BUILTIN_PROVIDERS)), (
        f"缺少预期 provider: {expected - set(pc.BUILTIN_PROVIDERS)}"
    )


# ── Helper 函数语义 ─────────────────────────────────────────────────────────


def test_get_provider_meta():
    """get_provider_meta 命中预置 provider，非预置返回 None。"""
    for pid in pc.BUILTIN_PROVIDERS:
        meta = pc.get_provider_meta(pid)
        assert meta is not None
        assert meta.id == pid
    assert pc.get_provider_meta("__nonexistent__") is None


def test_is_builtin_provider():
    """is_builtin_provider 判定 provider 是否在白名单内。"""
    for pid in pc.BUILTIN_PROVIDERS:
        assert pc.is_builtin_provider(pid) is True
    assert pc.is_builtin_provider("__nonexistent__") is False


def test_resolve_base_url_uses_user_when_non_empty():
    """resolve_base_url：user_base_url 非空 → 用用户的（规则 7）。"""
    pid = pc.BUILTIN_PROVIDERS[0]
    meta = pc.PROVIDER_CATALOG[pid]
    custom = "https://my-proxy.example.com/v1"
    assert pc.resolve_base_url(pid, custom) == custom
    assert pc.resolve_base_url(pid, "  " + custom + "  ") == custom  # strip


def test_resolve_base_url_falls_back_to_default():
    """resolve_base_url：user_base_url 空 / None → 用 catalog 的 default_base_url。"""
    pid = pc.BUILTIN_PROVIDERS[0]
    meta = pc.PROVIDER_CATALOG[pid]
    assert pc.resolve_base_url(pid, "") == meta.default_base_url
    assert pc.resolve_base_url(pid, None) == meta.default_base_url
    assert pc.resolve_base_url(pid, "   ") == meta.default_base_url  # 纯空白


def test_resolve_base_url_rejects_unknown_provider():
    """resolve_base_url：未知 provider 抛 ValueError（规则 1：provider 固定）。"""
    try:
        pc.resolve_base_url("__nonexistent__", None)
    except ValueError as e:
        assert "Unsupported provider" in str(e)
    else:
        raise AssertionError("未知 provider 应抛 ValueError")


# ── catalog_payload（IPC 下发体）────────────────────────────────────────────


def test_catalog_payload_shape():
    """catalog_payload 下发体字段与前端 types/api.ts ProviderCatalogMsg 契约一致。

    只含公开字段（id / display_name / guide_url / default_base_url），
    不含 env_prefix / verify_url（后端专用字段不下发，§7.1 注）。
    """
    payload = pc.catalog_payload()
    assert set(payload.keys()) == {"providers"}
    assert isinstance(payload["providers"], list)
    assert len(payload["providers"]) == len(pc.BUILTIN_PROVIDERS)

    allowed_fields = {"id", "display_name", "guide_url", "default_base_url"}
    for item in payload["providers"]:
        assert set(item.keys()) == allowed_fields, (
            f"下发字段越界: {set(item.keys()) - allowed_fields}"
        )
        # 不应含后端专用字段
        assert "env_prefix" not in item
        assert "verify_url" not in item
