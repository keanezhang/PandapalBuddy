"""pandapal_relay/config.py — Relay Server 配置"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class RelayConfig:
    """Relay Server 配置，从环境变量读取。"""

    # ── WeCom ──
    wecom_corp_id: str = ""
    wecom_agent_id: str = ""
    wecom_app_secret: str = ""
    wecom_token: str = ""
    wecom_aes_key: str = ""
    # D6 白名单（fail-closed）：None（未配置）或 [] 都拒绝所有人，仅非空白名单放行。
    wecom_allowed_userids: list[str] | None = None
    # 回包路由 user_id（必填，零兜底）：出站帧 payload 意外缺 user_id 时的回包目标，
    # 必须显式配置且为白名单成员，否则 run_relay 启动 fail-fast。
    # 这是 user_id 路由配置，与 session_id 无关（SESSION_ID 契约只约束 session_id）。
    wecom_default_user_id: str = ""

    # ── XiaoZhi ──
    xiaozhi_enabled: bool = False
    xiaozhi_asr_provider: str = "mock"  # mock / dashscope / whisper
    xiaozhi_asr_api_key: str = ""
    xiaozhi_tts_provider: str = "mock"  # mock / edge_tts / dashscope
    xiaozhi_tts_api_key: str = ""

    # ── Auth ──
    auth_jwt_secret: str = ""
    auth_db_path: str = "relay_auth.db"

    # ── Server ──
    port: int = 8090

    @property
    def wecom_enabled(self) -> bool:
        """WeCom 渠道是否启用：以显式配置 WECOM_CORP_ID 为判据（未配置即禁用企微）。"""
        return bool(self.wecom_corp_id)

    @classmethod
    def from_env(cls) -> "RelayConfig":
        raw_userids = os.getenv("WECOM_APP_MESSAGE_USERIDS", "")
        allowed = [u.strip() for u in raw_userids.split(",") if u.strip()] if raw_userids else None
        return cls(
            wecom_corp_id=os.getenv("WECOM_CORP_ID", ""),
            wecom_agent_id=os.getenv("WECOM_AGENT_ID", ""),
            wecom_app_secret=os.getenv("WECOM_APP_SECRET", ""),
            wecom_token=os.getenv("WECOM_TOKEN", ""),
            wecom_aes_key=os.getenv("WECOM_AES_KEY", ""),
            wecom_allowed_userids=allowed,
            wecom_default_user_id=os.getenv("WECOM_DEFAULT_USER_ID", "").strip(),
            xiaozhi_enabled=os.getenv("XIAOZHI_ENABLED", "false").lower() in ("true", "1", "yes"),
            xiaozhi_asr_provider=os.getenv("XIAOZHI_ASR_PROVIDER", "mock"),
            xiaozhi_asr_api_key=os.getenv("XIAOZHI_ASR_API_KEY", ""),
            xiaozhi_tts_provider=os.getenv("XIAOZHI_TTS_PROVIDER", "mock"),
            xiaozhi_tts_api_key=os.getenv("XIAOZHI_TTS_API_KEY", ""),
            auth_jwt_secret=os.getenv("AUTH_JWT_SECRET", ""),
            auth_db_path=os.getenv("AUTH_DB_PATH", "relay_auth.db"),
            port=int(os.getenv("RELAY_PORT", "8090")),
        )

    def validate(self) -> list[str]:
        """返回缺失的必填字段名列表。"""
        missing = []
        # Auth：jwt_secret 是安全关键配置，缺失则必须 Fail-Fast（设计文档失败 7）
        if not self.auth_jwt_secret:
            missing.append("AUTH_JWT_SECRET")
        # WeCom（可选渠道）：未配置 WECOM_CORP_ID 视为禁用企微，跳过校验；
        # 一旦配置则其余字段必须齐全（fail-closed，防半配置静默失效）。
        if self.wecom_corp_id:
            if not self.wecom_agent_id:
                missing.append("WECOM_AGENT_ID")
            if not self.wecom_app_secret:
                missing.append("WECOM_APP_SECRET")
            if not self.wecom_token:
                missing.append("WECOM_TOKEN")
            if not self.wecom_aes_key:
                missing.append("WECOM_AES_KEY")
        return missing
