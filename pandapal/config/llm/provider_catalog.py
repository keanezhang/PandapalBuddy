"""系统预置 provider 元信息（单一真相源，只读）。

数据来源：同目录下的 ``provider_catalog.toml``，由 Python 与 Rust 共读，
杜绝 provider 白名单 / 环境前缀 / 校验 URL 等在前后端各处散落重复。

设计约束（与 PRD §模型管理 三条规则一致）：
    1. provider 固定：只支持 :data:`BUILTIN_PROVIDERS` 内的 provider，前端下拉选取，
       不能手填新的 provider。
    2. model_id / api_key 完全用户填：本模块不含任何模型数据，用户填什么存什么、用什么。
    3. 本模块为系统配置（只读，随软件发布）；用户凭据在
       ``{data_dir}/users/{user_id}/llm_credentials.toml``（可读写），两者不重合。

本模块替代旧散落定义：
    - ``llm_config.py`` SUPPORTED_PROVIDERS / _PROVIDER_ENV_PREFIX
    - ``credentials_store.py`` _ALL_PROVIDERS / _PROVIDER_ENV_PREFIX
    - ``credentials_handler.py`` _VERIFY_URLS
    - ``llm_pricing.py`` _KNOWN_PROVIDERS
    - 前端 ``credentialStore.ts`` PROVIDER_META（改为从后端拉取）
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "ProviderMeta",
    "BUILTIN_PROVIDERS",
    "PROVIDER_CATALOG",
    "get_provider_meta",
    "is_builtin_provider",
    "resolve_base_url",
    "catalog_payload",
]


@dataclass(frozen=True)
class ProviderMeta:
    """单个系统预置 provider 的元信息。

    所有字段均来自 ``provider_catalog.toml``，运行时只读。
    """

    id: str
    display_name: str
    guide_url: str
    default_base_url: str
    env_prefix: str
    verify_url: str

    def to_public_payload(self) -> dict[str, str]:
        """下发给前端的公开字段（不含 env_prefix / verify_url 等后端专用字段）。"""
        return {
            "id": self.id,
            "display_name": self.display_name,
            "guide_url": self.guide_url,
            "default_base_url": self.default_base_url,
        }


# ── 加载与校验 ───────────────────────────────────────────────────────────────

_CATALOG_PATH = Path(__file__).resolve().parent / "provider_catalog.toml"


def _load_catalog() -> tuple[ProviderMeta, ...]:
    """从 toml 加载并校验 provider 元信息。

    校验规则：
        - providers 列表非空
        - 每个 provider 的 id 唯一
        - 必填字段齐全（id / display_name / guide_url / default_base_url /
          env_prefix / verify_url）
    """
    with _CATALOG_PATH.open("rb") as f:
        raw = tomllib.load(f)

    providers_raw = raw.get("providers", [])
    if not providers_raw:
        raise ValueError("provider_catalog.toml: providers 列表为空")

    providers: list[ProviderMeta] = []
    seen_ids: set[str] = set()
    required_fields = (
        "id",
        "display_name",
        "guide_url",
        "default_base_url",
        "env_prefix",
        "verify_url",
    )
    for p in providers_raw:
        for field_name in required_fields:
            if field_name not in p or not p[field_name]:
                raise ValueError(
                    f"provider_catalog.toml: provider 缺少必填字段 {field_name!r}: {p}"
                )
        pid = p["id"]
        if pid in seen_ids:
            raise ValueError(f"provider_catalog.toml: provider id 重复: {pid}")
        seen_ids.add(pid)
        providers.append(
            ProviderMeta(
                id=pid,
                display_name=p["display_name"],
                guide_url=p["guide_url"],
                default_base_url=p["default_base_url"],
                env_prefix=p["env_prefix"],
                verify_url=p["verify_url"],
            )
        )

    return tuple(providers)


_PROVIDERS: tuple[ProviderMeta, ...] = _load_catalog()


# ── 公开常量（替代旧 SUPPORTED_PROVIDERS / _ALL_PROVIDERS / _KNOWN_PROVIDERS）─

#: 系统预置 provider id 元组（前端下拉源、凭据校验白名单）。
#: 与底层 SDK ``OpenAICompatibleClient._PROVIDER_FACTORIES`` 同源——
#: 此处列出的每个 provider 必须在 SDK 有对应 for_*() 工厂方法。
BUILTIN_PROVIDERS: tuple[str, ...] = tuple(p.id for p in _PROVIDERS)

#: provider id → ProviderMeta 的字典。
PROVIDER_CATALOG: dict[str, ProviderMeta] = {p.id: p for p in _PROVIDERS}


# ── Helper 函数 ──────────────────────────────────────────────────────────────


def get_provider_meta(provider: str) -> ProviderMeta | None:
    """获取 provider 元信息；非预置 provider 返回 None。"""
    return PROVIDER_CATALOG.get(provider)


def is_builtin_provider(provider: str) -> bool:
    """判断 provider id 是否在系统预置白名单内。"""
    return provider in PROVIDER_CATALOG


def resolve_base_url(provider: str, user_base_url: str | None) -> str:
    """解析 provider 的实际 base_url。

    规则（与 PRD 一致）：先给默认值，如果用户改了就优先用用户传入的。

    Args:
        provider: provider id（必须在 :data:`BUILTIN_PROVIDERS` 内）
        user_base_url: 用户在凭据表单填写的 base_url，可为空字符串 / None

    Returns:
        实际使用的 base_url

    Raises:
        ValueError: provider 不在预置白名单内
    """
    meta = PROVIDER_CATALOG.get(provider)
    if meta is None:
        raise ValueError(
            f"Unsupported provider: {provider!r}. "
            f"Supported: {list(BUILTIN_PROVIDERS)}"
        )
    if user_base_url and user_base_url.strip():
        return user_base_url.strip()
    return meta.default_base_url


def catalog_payload() -> dict[str, Any]:
    """生成下发给前端的 PROVIDER_CATALOG 消息体。

    前端启动时通过 ``PROVIDER_CATALOG_REQUEST`` 拉取此 payload，替代旧的
    硬编码 ``PROVIDER_META`` 常量。只含前端需要的公开字段，不含
    env_prefix / verify_url 等后端专用字段。
    """
    return {
        "providers": [p.to_public_payload() for p in _PROVIDERS],
    }
