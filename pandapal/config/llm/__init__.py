"""pandapal.config.llm — 模型配置 + 模型切换。

子模块：
    - provider_catalog:   provider 元信息（单一真相源，provider_catalog.toml）
    - credentials_store:  用户凭据 TOML 读写（llm_credentials.toml）
    - credentials_handler:凭据 IPC 直通 handler（LOAD/SAVE/VERIFY/STATUS）
    - llm_config:         激活凭证（从环境变量读取）
    - model_registry:     可用模型清单（从凭据派生，模型切换真相源）

场景：
    1. 模型配置 — provider_catalog + credentials_store + credentials_handler + llm_config
    2. 模型切换 — model_registry（从已配置凭据派生可用模型清单）
"""
from pandapal.config.llm.credentials_handler import CredentialsHandler
from pandapal.config.llm.credentials_store import CredentialStore
from pandapal.config.llm.model_registry import (
    AvailableModel,
    find_available,
    get_default_model_id,
    is_available,
    resolve_available_models,
    to_model_list_payload,
)
from pandapal.config.llm.provider_catalog import (
    BUILTIN_PROVIDERS,
    PROVIDER_CATALOG,
    catalog_payload,
    get_provider_meta,
    is_builtin_provider,
    resolve_base_url,
)

__all__ = [
    # 模型配置
    "BUILTIN_PROVIDERS",
    "PROVIDER_CATALOG",
    "catalog_payload",
    "get_provider_meta",
    "is_builtin_provider",
    "resolve_base_url",
    "CredentialStore",
    "CredentialsHandler",
    # 模型切换
    "AvailableModel",
    "resolve_available_models",
    "get_default_model_id",
    "is_available",
    "find_available",
    "to_model_list_payload",
]
