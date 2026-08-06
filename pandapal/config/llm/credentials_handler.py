"""pandapal/config/credentials_handler.py — LLM 凭据 IPC 直通 handler。

镜像 BudgetHandler 模式：不走 Router，通过 InboundDispatcher 的直通 handler 注册。
职责：
    - LOAD_CREDENTIALS    → 加载凭据列表（脱敏）→ CREDENTIALS_LIST
    - SAVE_LLM_CREDENTIALS → 校验 → 写入文件 → 注入环境变量 → CREDENTIALS_SAVED
    - VERIFY_CREDENTIALS  → 连通性探测 → CREDENTIALS_VERIFIED
    - GET_CREDENTIALS_STATUS → 门禁查询 → CREDENTIALS_STATUS

事件出口约定（直通路径集中式转发改造）：
- 请求-响应事件（CREDENTIALS_LIST / CREDENTIALS_SAVED / CREDENTIALS_VERIFIED /
  CREDENTIALS_STATUS）：本类只构建并返回，由 Dispatcher 统一 broadcast.send()
  并注入 origin_channel_id；
- CREDENTIALS_LIST_CHANGED（凭据守卫内部自主推送，非请求触发）：豁免路径，
  仍由守卫自广播，不经本 handler。

设计约束：
    - O3：任何异常吞掉并留痕，绝不向 IPC 层抛
    - user_id 权威取自进程（单用户 sidecar），不信任入站 payload
    - 保存采用「校验 → 写入 → 注入」流程，失败不产生半成品文件
    - 校验 URL 从 provider_catalog 取（单一真相源），不再硬编码 _VERIFY_URLS
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from pandapal.config.llm.credentials_store import CredentialStore
from pandapal.config.llm.provider_catalog import get_provider_meta
from pandapal.events.normalized import EventType, NormalizedEvent

logger = logging.getLogger("pandapal.config.credentials_handler")

# 校验超时（秒）
_VERIFY_TIMEOUT = 10.0


class CredentialsHandler:
    """LLM 凭据直通 handler。"""

    def __init__(
        self,
        store: CredentialStore,
        user_id: str,
    ) -> None:
        self._store = store
        self._user_id = user_id

    # ── LOAD_CREDENTIALS → CREDENTIALS_LIST ──────────────────────────

    async def handle_load(self) -> NormalizedEvent | None:
        """加载凭据列表，构建脱敏后的 CREDENTIALS_LIST 事件。"""
        logger.info("[credentials] <<< LOAD_CREDENTIALS (user=%s)", self._user_id)
        try:
            credentials = self._store.load_all()
            logger.info("[credentials] >>> CREDENTIALS_LIST count=%d (user=%s)", len(credentials), self._user_id)
            return NormalizedEvent(
                event_type=EventType.CREDENTIALS_LIST,
                payload={"credentials": credentials},
            )
        except Exception as e:
            logger.exception("handle_load 失败: %s", e)
            return None

    # ── SAVE_LLM_CREDENTIALS → CREDENTIALS_SAVED ────────────────────

    async def handle_save(self, data: dict[str, Any]) -> NormalizedEvent | None:
        """⚠️ 已停用：用户 toml 的写入者唯一由桌面壳（Rust）承担。

        **为什么停用**（PRD·G4 单一写入者）：
            历史上前端有两条写入路径——向导页走 Rust `save_llm_credentials`、
            设置页走本 IPC 让 Python 写，两条各带一套校验。校验一旦漂移就会出事：
            Python 侧当时只校验「api_key 长度 ≥8」，放行了脱敏值 `sk-a***bcd`
            （11 字符），导致用户改一次设置就把全部真实 key 覆写且不可恢复。

        **为什么 owner 只能是 Rust**：
            首次配置时 sidecar 尚未启动（凭据门禁在其之前），Python 不具备写入时机。

        本方法保留并**显式拒绝**，而非直接删除消息类型——旧版前端会收到明确原因，
        而不是「未知消息类型」式的沉默失败。
        """
        logger.warning(
            "[credentials] <<< SAVE_LLM_CREDENTIALS 已停用（user=%s）："
            "写入者唯一为桌面壳 Rust save_llm_credentials",
            self._user_id,
        )
        return NormalizedEvent(
            event_type=EventType.CREDENTIALS_SAVED,
            payload={
                "success": False,
                "error": (
                    "该保存通道已停用：凭据写入统一由桌面端完成。"
                    "请更新客户端后重试。"
                ),
            },
        )

    # ── VERIFY_CREDENTIALS → CREDENTIALS_VERIFIED ────────────────────

    async def handle_verify(self, data: dict[str, Any]) -> NormalizedEvent | None:
        """对每组凭据发起连通性探测，构建逐组结果事件。"""
        credentials = data.get("credentials", [])

        logger.info(
            "[credentials] <<< VERIFY_CREDENTIALS count=%d providers=%s (user=%s)",
            len(credentials) if isinstance(credentials, list) else 0,
            [c.get("provider", "?") for c in credentials if isinstance(c, dict)],
            self._user_id,
        )

        if not isinstance(credentials, list) or len(credentials) == 0:
            return NormalizedEvent(
                event_type=EventType.CREDENTIALS_VERIFIED,
                payload={"success": False, "results": []},
            )

        # ★ sentinel 机制补位：前端对「未编辑的密钥」**不提交** api_key 字段
        #   （credentialStore.toSubmittable），真实 key 永不经过前端。save 路径靠
        #   CredentialStore._merge_preserved_keys 从文件取回旧值，verify 路径必须
        #   做同样的事——否则用户不改任何东西点「校验」，已保存的凭据会全部报
        #   「密钥不能为空」，只有逐个重敲密钥才能验证成功。
        #   注意这里按行补位而非复用 _merge_preserved_keys：后者对「找不到旧值」
        #   抛 ValueError（save 语义正确），而 verify 应逐行报错、不牵连其他行。
        try:
            _existing_keys = {
                (c["provider"], c["model_id"]): c["api_key"]
                for c in self._store.load_all_raw()
            }
        except Exception as e:  # 故障隔离点：读旧文件失败不应让整个校验崩掉
            logger.warning("[credentials] verify: 读取现有凭据失败，省略的 key 无法补位: %s", e)
            _existing_keys = {}

        results: list[dict[str, Any]] = []
        for cred in credentials:
            provider = cred.get("provider", "") if isinstance(cred, dict) else ""
            api_key = cred.get("api_key", "") if isinstance(cred, dict) else ""
            base_url = cred.get("base_url") if isinstance(cred, dict) else None

            if not api_key and isinstance(cred, dict):
                # 省略了 api_key → 取回文件中的旧 key；取不到则留空，
                # 由 _verify_one 对该行单独报「密钥不能为空」（新增行未填 key）
                api_key = _existing_keys.get(
                    (cred.get("provider", ""), cred.get("model_id", "")), ""
                )

            result = await self._verify_one(provider, api_key, base_url)
            logger.info(
                "[credentials] verify: provider=%s success=%s error=%s",
                result.get("provider"), result.get("success"), result.get("error"),
            )
            results.append(result)

        all_passed = all(r.get("success") for r in results)
        logger.info(
            "[credentials] >>> CREDENTIALS_VERIFIED all_passed=%s count=%d",
            all_passed, len(results),
        )
        return NormalizedEvent(
            event_type=EventType.CREDENTIALS_VERIFIED,
            payload={"success": all_passed, "results": results},
        )

    async def _verify_one(
        self, provider: str, api_key: str, base_url: str | None
    ) -> dict[str, Any]:
        """对单个 provider 做连通性探测。

        verify_url 来源（单一真相源 provider_catalog）：
          - 用户填了 base_url → 用 base_url + /models
          - 未填 → 用 catalog 的 meta.verify_url

        Returns:
            {"provider": "...", "success": bool, "error": "..." | None}
        """
        if not provider or not api_key or not api_key.strip():
            return {"provider": provider or "unknown", "success": False, "error": "密钥不能为空"}

        # provider 合法性校验：必须在 catalog 白名单内
        meta = get_provider_meta(provider)
        if meta is None:
            return {"provider": provider, "success": False, "error": f"不支持的服务商：{provider}"}

        # 构造探测 URL：优先用用户自定义 base_url + /models，否则用 catalog 的 verify_url
        custom = str(base_url).strip() if base_url else ""
        if custom:
            verify_url = custom.rstrip("/") + "/models"
        else:
            verify_url = meta.verify_url

        headers = {"Authorization": f"Bearer {api_key.strip()}"}
        try:
            async with httpx.AsyncClient(timeout=_VERIFY_TIMEOUT) as client:
                resp = await client.get(verify_url, headers=headers)

            if resp.status_code in (200, 401, 403):
                # 200 = 鉴权通过，直接可用
                # 401/403 = API 端点可到达，但凭据无效（仍算有效的连通性校验，告知用户 key 不对）
                if resp.status_code == 200:
                    logger.info("凭据校验通过: provider=%s", provider)
                    return {"provider": provider, "success": True}
                else:
                    logger.info("凭据校验失败（鉴权拒绝）: provider=%s status=%d", provider, resp.status_code)
                    return {
                        "provider": provider,
                        "success": False,
                        "error": f"{provider} 密钥无效，请核对后重试",
                    }
            else:
                logger.warning("凭据校验失败: provider=%s status=%d", provider, resp.status_code)
                return {
                    "provider": provider,
                    "success": False,
                    "error": f"验证失败：服务返回异常（HTTP {resp.status_code}）",
                }
        except httpx.TimeoutException:
            return {
                "provider": provider,
                "success": False,
                "error": f"无法连接 {provider}，请检查网络或稍后重试",
            }
        except Exception as e:
            logger.exception("凭据校验异常: provider=%s", provider)
            return {
                "provider": provider,
                "success": False,
                "error": f"验证失败：{e}",
            }

    # ── GET_CREDENTIALS_STATUS → CREDENTIALS_STATUS ──────────────────

    async def handle_status(self) -> NormalizedEvent | None:
        """门禁轻量查询：构建配置状态事件。"""
        logger.info("[credentials] <<< GET_CREDENTIALS_STATUS (user=%s)", self._user_id)
        try:
            status = self._store.get_status()
            logger.info(
                "[credentials] >>> CREDENTIALS_STATUS configured=%s count=%d "
                "default_model=%s legacy=%s",
                status["configured"], status["credential_count"],
                status["default_model_id"], status["legacy_format"],
            )
            return NormalizedEvent(
                event_type=EventType.CREDENTIALS_STATUS,
                payload={
                    "configured": status["configured"],
                    "credential_count": status["credential_count"],
                    "default_model_id": status["default_model_id"],
                    "legacy_format": status["legacy_format"],
                    "default_resolvable": status["default_resolvable"],
                },
            )
        except Exception as e:
            logger.exception("handle_status 失败: %s", e)
            return NormalizedEvent(
                event_type=EventType.CREDENTIALS_STATUS,
                payload={
                    "configured": False,
                    "credential_count": 0,
                    "default_model_id": None,
                    "legacy_format": False,
                },
            )
