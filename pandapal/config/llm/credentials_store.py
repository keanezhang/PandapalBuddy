"""pandapal/config/llm/credentials_store.py — LLM 凭据文件读写（TOML 格式）。

文件路径：
    {data_dir}/users/{user_id}/credentials/llm_credentials.toml

TOML 格式（v2，**不兼容旧格式**）：
    # LLM credentials for PandaPal (user-managed)

    default_model_id = "qwen-max"

    [[credentials]]
    provider = "dashscope"
    api_key = "sk-xxx"
    model_id = "qwen-max"
    base_url = ""                      # 可选，空则用 catalog 的 default_base_url
    input_price_per_1k = 0.0112        # 可选，CNY；留空则回落系统默认表
    output_price_per_1k = 0.0448       # 可选，CNY；与输入价必须同时填或同时留空
    cache_read_price_per_1k = 0.00448  # 可选，CNY；留空则取生效的输入价

    [[credentials]]
    provider = "dashscope"             # ← 同一 provider 可配多个模型
    api_key = "sk-xxx"
    model_id = "qwen-turbo"

设计约束：
  - **主键 = (provider, model_id)**。历史上主键是 provider（拒绝 provider 重复），
    导致一个 provider 只能配一个模型、全系统最多 4 个——「model_id 完全用户填、
    有什么用什么」在那个数据模型下根本无法成立。这是本次重构最深的一处改动。
  - **默认标识 = default_model_id**（而非 default_provider）：切换的粒度是模型。
  - **model_id 全局唯一**：它同时是 LLMRouter 的路由键，跨 provider 重名会导致
    「装配了 A 却路由到 B」的静默错配（费用记到错误的 provider 账上）。
  - 单价三级回落在 :func:`resolve_effective_price` 实现；无任何单价来源的模型
    **拒绝保存**（§九：金额类字段缺失即失败，绝不静默计 0）。
  - **脱敏值禁止回写**：``load_all()`` 返回的 api_key 是脱敏的，若原样提交回来会
    覆盖真实 key 且不可恢复。两道防线：① 提交体可省略 api_key 表示「保持不变」
    （按 (provider, model_id) 合并旧值）；② 显式拒绝形如 ``sk-a***bcd`` 的值。
  - 读用 Python 3.12+ 内置 tomllib，写用手动拼接（零额外依赖）
  - save_all 采用「写临时文件 → rename」保证原子性

关于写入权：
    面向用户的写入路径**唯一**由桌面壳（Rust ``save_llm_credentials``）承担——
    首次配置时 sidecar 尚未启动，Python 不具备写入时机。本模块的 :meth:`save_all`
    仅供内部 / 测试使用，**不对前端暴露 IPC 入口**。历史上前端有两条写入路径
    （Rust 向导页 + Python 设置页）各带不同校验，正是脱敏 key 覆盖真 key 的土壤。
"""

from __future__ import annotations

import logging
import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Dict, List

from pandapal.config.llm.model_prices import resolve_effective_price
from pandapal.config.llm.provider_catalog import BUILTIN_PROVIDERS, PROVIDER_CATALOG

logger = logging.getLogger(__name__)

# 支持的所有 provider（从 catalog 派生，单一真相源）
_ALL_PROVIDERS: frozenset[str] = frozenset(BUILTIN_PROVIDERS)

# 凭据文件名
_CREDENTIALS_FILENAME = "llm_credentials.toml"

# api_key 最小长度（低于此值几乎必然是误填）
_MIN_API_KEY_LENGTH = 8

# 脱敏标记：_mask_key 产出的中缀。提交体中出现该标记即判定为「脱敏值回写」。
_MASK_MARKER = "***"

# 单价字段（CNY / 每 1k token）。后三项为高峰价（可选，缺省回落对应单档价）
_PRICE_FIELDS = (
    "input_price_per_1k",
    "output_price_per_1k",
    "cache_read_price_per_1k",
    "peak_input_price_per_1k",
    "peak_output_price_per_1k",
    "peak_cache_read_price_per_1k",
)

# 旧格式标记字段：出现即判定为 v1 格式，不做兼容读取（见模块 docstring）
_LEGACY_MARKER = "default_provider"


class LegacyCredentialFormatError(Exception):
    """检测到 v1 版凭据文件格式。

    v2 结构与 v1 不兼容（主键 provider → (provider, model_id)、
    default_provider → default_model_id），且刻意**不做**兼容读取——
    兼容层会让两套语义长期并存。调用方应备份原文件并引导用户重新配置。
    """


class CredentialStore:
    """LLM 凭据持久化存储（TOML 格式）。

    使用方式:
        store = CredentialStore("/path/to/users/alice")
        creds = store.load_all()          # api_key 脱敏后返回（供前端展示）
        raw = store.load_all_raw()        # 真实 api_key（供装配 LLM client）
        status = store.get_status()       # 门禁轻量查询
    """

    def __init__(self, credentials_dir: str | Path) -> None:
        self._dir = Path(credentials_dir)
        self._file = self._dir / _CREDENTIALS_FILENAME

    # ── Public API ──────────────────────────────────────────────────────

    def load_all(self) -> List[Dict[str, Any]]:
        """加载全部凭据，api_key **已脱敏**（供前端展示 / 状态查询）。

        ⚠️ 返回值中的 api_key 不可用于调用 LLM，也不可原样回写——
        回写会覆盖真实 key 且不可恢复。装配请用 :meth:`load_all_raw`。

        文件不存在或无法解析时返回空列表（不抛异常）。

        Raises:
            LegacyCredentialFormatError: 检测到 v1 格式文件
        """
        logger.debug("[cred-store] loading: %s", self._file)
        return self._load(mask=True)

    def load_all_raw(self) -> List[Dict[str, Any]]:
        """加载全部凭据，返回**真实 api_key**（仅装配用，不脱敏）。

        与 :meth:`load_all` 的唯一差异是 api_key 保留真实值。

        Raises:
            LegacyCredentialFormatError: 检测到 v1 格式文件
        """
        return self._load(mask=False)

    def get_status(self) -> Dict[str, Any]:
        """返回轻量配置状态（门禁查询用）。"""
        data = self._read_toml()
        if data is None:
            return {
                "configured": False,
                "credential_count": 0,
                "default_model_id": None,
                "legacy_format": False,
                "default_resolvable": False,
            }

        if _LEGACY_MARKER in data:
            # 门禁查询不抛异常——它的职责是「报告状态」，由调用方决定如何处置。
            logger.warning("[cred-store] 检测到 v1 格式凭据文件: %s", self._file)
            return {
                "configured": False,
                "credential_count": 0,
                "default_model_id": None,
                "legacy_format": True,
                "default_resolvable": False,
            }

        usable = [
            c
            for c in data.get("credentials", [])
            if isinstance(c, dict)
            and c.get("provider")
            and c.get("api_key")
            and c.get("model_id")
        ]
        count = len(usable)
        default_model_id = data.get("default_model_id") or None

        # configured 是**门禁判定**（决策类，§九 fail-closed）：必须与 sidecar
        # 的启动前提一致，否则前端放行、sidecar 每次 exit(1)，用户无自愈路径。
        # run_local 要求「存在 is_default 凭据」，即 default_model_id 必须存在
        # 且指向清单中真实存在的模型——只数条目数是不够的。
        default_resolvable = bool(default_model_id) and any(
            c.get("model_id") == default_model_id for c in usable
        )

        return {
            "configured": count >= 1 and default_resolvable,
            "credential_count": count,
            "default_model_id": default_model_id,
            "legacy_format": False,
            # 供前端区分「没配过」与「配了但默认模型失效」两种未就绪状态，
            # 后者应引导去设默认，而不是重走首次配置向导。
            "default_resolvable": default_resolvable,
        }

    def save_all(self, credentials: List[Dict[str, Any]]) -> None:
        """保存凭据列表到文件（原子写入）。

        ⚠️ **仅供内部 / 测试使用**，不对前端暴露 IPC 入口——面向用户的写入路径
        唯一由 Rust 承担（见模块 docstring）。

        **api_key 合并语义**：某条凭据若不含 ``api_key`` 键（或值为 None），
        表示「保持原值不变」，将按 (provider, model_id) 从现有文件中取回旧值。
        找不到旧值则报错——绝不写入空 key。

        Raises:
            ValueError: 数据不合法（含脱敏值回写、无单价来源等）
            OSError: 磁盘写入失败
        """
        merged = self._merge_preserved_keys(credentials)
        logger.info(
            "[cred-store] saving %d credentials (models=%s) to %s",
            len(merged),
            [c.get("model_id", "?") for c in merged],
            self._file,
        )
        self._validate(merged)
        content = self._build_toml_content(merged)
        self._write_atomic(content)
        logger.info("[cred-store] saved OK: %s (%d bytes)", self._file, len(content))

    # ── Private: 加载 ───────────────────────────────────────────────────

    def _load(self, *, mask: bool) -> List[Dict[str, Any]]:
        """load_all / load_all_raw 的共同实现，避免两份几乎相同的解析逻辑。"""
        data = self._read_toml()
        if data is None:
            return []

        if _LEGACY_MARKER in data:
            raise LegacyCredentialFormatError(
                f"{self._file} 为 v1 格式（含 {_LEGACY_MARKER}），"
                f"v2 不做兼容读取，请备份后重新配置"
            )

        default_model_id = data.get("default_model_id", "")
        credentials: List[Dict[str, Any]] = []

        for entry in data.get("credentials", []):
            if not isinstance(entry, dict):
                continue
            provider = entry.get("provider", "")
            api_key = entry.get("api_key", "")
            model_id = entry.get("model_id", "")
            if not provider or not api_key or not model_id:
                continue

            cred: Dict[str, Any] = {
                "provider": provider,
                "api_key": _mask_key(api_key) if mask else api_key,
                "model_id": model_id,
                "base_url": entry.get("base_url") or None,
                "is_default": model_id == default_model_id,
            }
            for field_name in _PRICE_FIELDS:
                cred[field_name] = entry.get(field_name)
            credentials.append(cred)

        return credentials

    def _merge_preserved_keys(
        self, credentials: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """把「省略 api_key」的凭据补上现有文件中的旧 key（sentinel 机制）。

        这是「脱敏值不被回写」的第一道防线：前端对未编辑的密钥不提交该字段，
        后端在此按 (provider, model_id) 取回旧值，用户的真实 key 永不经过前端。
        """
        needs_merge = any(
            isinstance(c, dict) and c.get("api_key") is None for c in credentials
        )
        if not needs_merge:
            return list(credentials)

        try:
            existing = {
                (c["provider"], c["model_id"]): c["api_key"]
                for c in self.load_all_raw()
            }
        except LegacyCredentialFormatError:
            existing = {}

        merged: List[Dict[str, Any]] = []
        for i, cred in enumerate(credentials):
            if not isinstance(cred, dict):
                raise ValueError(f"credentials[{i}] 必须是字典")
            if cred.get("api_key") is not None:
                merged.append(dict(cred))
                continue

            key = (cred.get("provider", ""), cred.get("model_id", ""))
            old = existing.get(key)
            if not old:
                # 绝不写入空 key：找不到旧值说明这是新增凭据，必须提供 api_key
                raise ValueError(
                    f"credentials[{i}]({key[0]}/{key[1]}): 省略了 api_key 但"
                    f"现有配置中找不到该模型，新增凭据必须提供 api_key"
                )
            new_cred = dict(cred)
            new_cred["api_key"] = old
            merged.append(new_cred)

        return merged

    # ── Private: 文件 I/O ───────────────────────────────────────────────

    def _read_toml(self) -> Dict[str, Any] | None:
        """读取 TOML 文件，返回解析后的 dict。文件不存在 / 解析失败返回 None。"""
        if not self._file.exists():
            return None
        try:
            with open(self._file, "rb") as f:
                return tomllib.load(f)  # type: ignore[no-any-return]
        except Exception as e:
            logger.warning("无法解析凭据文件 %s: %s", self._file, e)
            return None

    def _write_atomic(self, content: str) -> None:
        """原子写入：写临时文件 → rename，保证不产生半成品。"""
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            suffix=".toml",
            prefix=".llm_credentials_tmp_",
            dir=str(self._dir),
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, str(self._file))
            logger.info("凭据已保存到 %s", self._file)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ── Private: TOML 内容生成 ───────────────────────────────────────────

    def _build_toml_content(self, credentials: List[Dict[str, Any]]) -> str:
        """根据凭据列表生成 TOML 文件内容（调用前须已通过 _validate）。"""
        lines: List[str] = [
            "# LLM credentials for PandaPal (user-managed)",
            "# This file is auto-generated. Do not edit manually.",
            "",
        ]

        default_model_id = next(
            (c["model_id"] for c in credentials if c.get("is_default")), ""
        )
        if default_model_id:
            lines.append(f"default_model_id = {_toml_str(default_model_id)}")
            lines.append("")

        for cred in credentials:
            lines.append("[[credentials]]")
            lines.append(f"provider = {_toml_str(cred['provider'])}")
            lines.append(f"api_key = {_toml_str(str(cred['api_key']).strip())}")
            lines.append(f"model_id = {_toml_str(str(cred['model_id']).strip())}")

            base_url = cred.get("base_url")
            if base_url and str(base_url).strip():
                lines.append(f"base_url = {_toml_str(str(base_url).strip())}")

            for field_name in _PRICE_FIELDS:
                value = cred.get(field_name)
                if value is not None:
                    lines.append(f"{field_name} = {float(value)!r}")
            lines.append("")

        return "\n".join(lines) + "\n"

    # ── Private: 校验 ───────────────────────────────────────────────────

    def _validate(self, credentials: List[Dict[str, Any]]) -> None:
        """校验凭据数据合法性。

        Raises:
            ValueError: 校验不通过
        """
        if not isinstance(credentials, list):
            raise ValueError("credentials 必须是列表")

        seen_keys: set[tuple[str, str]] = set()
        seen_model_ids: set[str] = set()
        default_count = 0

        for i, cred in enumerate(credentials):
            if not isinstance(cred, dict):
                raise ValueError(f"credentials[{i}] 必须是字典")

            provider = cred.get("provider", "")
            if provider not in _ALL_PROVIDERS:
                raise ValueError(
                    f"credentials[{i}]: provider={provider!r} 不在白名单 "
                    f"{sorted(_ALL_PROVIDERS)}"
                )

            model_id = str(cred.get("model_id", "")).strip()
            if not model_id:
                raise ValueError(f"credentials[{i}]({provider}): model_id 缺失")

            # 主键唯一：(provider, model_id)
            if (provider, model_id) in seen_keys:
                raise ValueError(
                    f"credentials[{i}]: (provider={provider!r}, "
                    f"model_id={model_id!r}) 重复"
                )
            seen_keys.add((provider, model_id))

            # 路由键唯一：model_id 是 LLMRouter 的键，跨 provider 重名会导致
            # 「装配了 A 却路由到 B」的静默错配（费用记到错误的 provider 账上）。
            if model_id in seen_model_ids:
                raise ValueError(
                    f"credentials[{i}]: model_id={model_id!r} 已被其他 provider 使用；"
                    f"model_id 同时是路由键，必须全局唯一"
                )
            seen_model_ids.add(model_id)

            self._validate_api_key(i, provider, model_id, cred.get("api_key"))
            self._validate_base_url(i, provider, cred.get("base_url"))
            self._validate_price(i, provider, model_id, cred)

            if cred.get("is_default"):
                default_count += 1

        # ⚠️ 不加 `credentials and` 前置条件：空列表会短路掉唯一默认校验，
        #    使 save_all([]) 一路通过并写出「只有文件头」的空凭据文件——
        #    静默清空用户全部配置。写入路径必须有下限（Rust 侧已拒绝空列表）。
        if default_count != 1:
            raise ValueError(
                f"必须有且仅有一组凭据设为默认，当前有 {default_count} 组"
                + ("（凭据列表为空）" if not credentials else "")
            )

    @staticmethod
    def _validate_api_key(
        i: int, provider: str, model_id: str, api_key: Any
    ) -> None:
        """校验 api_key，并拦截「脱敏值回写」。"""
        key = str(api_key or "").strip()
        if not key or len(key) < _MIN_API_KEY_LENGTH:
            raise ValueError(
                f"credentials[{i}]({provider}/{model_id}): api_key 缺失或长度不足"
            )
        # 第二道防线：即便前端 sentinel 机制失效（或提交体被篡改），也绝不让
        # 脱敏值覆盖真实 key。历史事故：用户只改 model_id，全部真 key 被
        # 覆写为 sk-a***bcd 且不可恢复——它能通过旧版仅有的「长度 ≥8」校验。
        if _MASK_MARKER in key:
            raise ValueError(
                f"credentials[{i}]({provider}/{model_id}): api_key 疑似脱敏值"
                f"（含 {_MASK_MARKER!r}），拒绝写入。未修改密钥时请省略该字段"
            )

    @staticmethod
    def _validate_base_url(i: int, provider: str, base_url: Any) -> None:
        if base_url and isinstance(base_url, str) and base_url.strip():
            url = base_url.strip()
            if not url.startswith(("http://", "https://")):
                raise ValueError(
                    f"credentials[{i}]({provider}): "
                    f"base_url={url!r} 必须以 http:// 或 https:// 开头"
                )

    @staticmethod
    def _validate_price(
        i: int, provider: str, model_id: str, cred: Dict[str, Any]
    ) -> None:
        """校验单价三级回落有确定结果（第③级 → 拒绝保存）。

        高峰价（``peak_*``）为可选增强：缺省回落对应单档价，故不参与
        「有无单价来源」的判定；负值等金额类非法值由 resolve_effective_price
        抛出 ValueError。
        """
        try:
            price = resolve_effective_price(
                model_id,
                cred.get("input_price_per_1k"),
                cred.get("output_price_per_1k"),
                cred.get("cache_read_price_per_1k"),
                user_peak_input_price=cred.get("peak_input_price_per_1k"),
                user_peak_output_price=cred.get("peak_output_price_per_1k"),
                user_peak_cache_price=cred.get("peak_cache_read_price_per_1k"),
            )
        except ValueError as e:
            raise ValueError(f"credentials[{i}]({provider}/{model_id}): {e}") from e

        if price is None:
            # 三级回落第③级：无任何单价来源。绝不放行——放行意味着该模型的消费
            # 静默计 0、预算守卫对其失效（§九：金额类字段缺失即失败）。
            raise ValueError(
                f"credentials[{i}]({provider}/{model_id}): 该模型无系统默认单价，"
                f"请填写 input_price_per_1k 与 output_price_per_1k（单位 CNY/1k token）"
            )


# ── 工具函数 ────────────────────────────────────────────────────────────

def _mask_key(key: str) -> str:
    """API Key 脱敏：仅露首 4 尾 4 位。"""
    if not key:
        return ""
    if len(key) <= _MIN_API_KEY_LENGTH:
        return key[:2] + _MASK_MARKER + key[-2:]
    return key[:4] + _MASK_MARKER + key[-4:]


def _toml_str(s: str) -> str:
    """将字符串转为 TOML 双引号字符串，处理转义。"""
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def get_provider_env_prefix(provider: str) -> str:
    """查询 provider 对应的环境变量前缀（从 provider_catalog 派生）。"""
    meta = PROVIDER_CATALOG.get(provider)
    return meta.env_prefix if meta else provider.upper()
