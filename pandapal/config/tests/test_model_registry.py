"""pandapal/config/tests/test_model_registry.py — 可用模型清单派生不变量。

核心不变量：resolve_available_models 从用户凭据派生可用模型清单。
  - 每组凭据 → 一个 AvailableModel（display_name = model_id，不美化）
  - is_default 凭据对应的模型置于首位
  - 空凭据返回空清单
  - MODEL_LIST 下发体字段与前端 types/api.ts 契约一致（无 enabled 字段）

注：旧版基于静态白名单（_DECLARED_MODELS）的「声明∩凭证」逻辑已删除（规则 2：
系统不内置模型），本测试改为验证「从凭据派生」的新语义。
"""
from __future__ import annotations

from pandapal.config.llm import model_registry as mr


def _cred(
    provider: str,
    model_id: str,
    is_default: bool = False,
    api_key: str = "sk-test1234",
    base_url: str | None = None,
) -> dict:
    """构造一个凭据 dict（模拟 CredentialStore.load_all() 的输出，api_key 脱敏无妨）。"""
    return {
        "provider": provider,
        "api_key": api_key,
        "model_id": model_id,
        "base_url": base_url,
        "is_default": is_default,
    }


def test_resolve_from_credentials():
    """resolve_available_models 从凭据派生，每组凭据一个 AvailableModel。"""
    creds = [
        _cred("dashscope", "qwen-plus", is_default=True),
        _cred("openai", "gpt-4o"),
    ]
    avail = mr.resolve_available_models(creds)
    assert len(avail) == 2
    assert {m.model_id for m in avail} == {"qwen-plus", "gpt-4o"}


def test_default_always_first():
    """is_default 凭据对应的模型置于首位（无论其在凭据列表中的顺序）。"""
    creds = [
        _cred("openai", "gpt-4o"),
        _cred("dashscope", "qwen-plus", is_default=True),
    ]
    avail = mr.resolve_available_models(creds)
    assert avail[0].model_id == "qwen-plus"


def test_display_name_equals_model_id():
    """display_name = model_id（规则 2：系统不内置模型数据，不做美化映射）。"""
    creds = [_cred("deepseek", "deepseek-chat", is_default=True)]
    avail = mr.resolve_available_models(creds)
    assert avail[0].display_name == "deepseek-chat"


def test_empty_credentials_returns_empty():
    """空凭据 / None 返回空清单。"""
    assert mr.resolve_available_models([]) == []


def test_skip_invalid_credentials():
    """缺 model_id 或 provider 的凭据被跳过（不派生为可用模型）。"""
    creds = [
        _cred("dashscope", "", is_default=True),  # 无 model_id
        _cred("", "gpt-4o"),  # 无 provider
        _cred("openai", "gpt-4o-mini"),  # 有效
    ]
    avail = mr.resolve_available_models(creds)
    assert len(avail) == 1
    assert avail[0].model_id == "gpt-4o-mini"


def test_is_available_and_find_available():
    """is_available / find_available 在给定清单内查找（替代旧 find_declared）。"""
    creds = [
        _cred("dashscope", "qwen-plus", is_default=True),
        _cred("openai", "gpt-4o"),
    ]
    avail = mr.resolve_available_models(creds)
    assert mr.is_available("qwen-plus", avail) is True
    assert mr.is_available("gpt-4o", avail) is True
    assert mr.is_available("__nonexistent__", avail) is False
    found = mr.find_available("gpt-4o", avail)
    assert found is not None
    assert found.provider == "openai"
    assert mr.find_available("__nonexistent__", avail) is None


def test_get_default_model_id():
    """get_default_model_id 取清单首项（即 is_default 凭据的 model_id）。"""
    creds = [
        _cred("openai", "gpt-4o"),
        _cred("dashscope", "qwen-plus", is_default=True),
    ]
    avail = mr.resolve_available_models(creds)
    assert mr.get_default_model_id(avail) == "qwen-plus"
    assert mr.get_default_model_id([]) == ""


def test_model_list_payload_shape():
    """MODEL_LIST 下发体字段与前端 types/api.ts ModelListMsg 契约一致（无 enabled）。"""
    creds = [
        _cred("dashscope", "qwen-plus", is_default=True),
        _cred("openai", "gpt-4o"),
    ]
    avail = mr.resolve_available_models(creds)
    payload = mr.to_model_list_payload(avail, "qwen-plus")
    assert set(payload.keys()) == {"models", "default_model_id"}
    assert payload["default_model_id"] == "qwen-plus"
    for item in payload["models"]:
        assert set(item.keys()) == {
            "model_id", "display_name", "provider", "price_source",
        }
