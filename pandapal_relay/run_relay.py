"""pandapal_relay/run_relay.py — Relay Server 启动入口

Usage（服务器上运行）：
    python -m pandapal_relay

或者：
    python pandapal_relay/run_relay.py

单进程、单端口（默认 8090），包含：
- POST /auth/register            — 用户注册（注册即登录）
- POST /auth/login               — 用户登录（JWT 签发）
- PUT  /auth/password            — 修改密码（需 Authorization: Bearer JWT）
- POST /assistant/wecom/callback — 企微消息接收（启用 WeCom 时注册）
- GET  /assistant/wecom/callback — 企微 URL 验证（启用 WeCom 时注册）
- WS   /relay/ws                 — Agent 连接
- WS   /xiaozhi/ws               — XiaoZhi 设备连接
- GET  /health                   — 健康检查

环境变量：
- AUTH_JWT_SECRET（必填）— JWT 签名密钥
- AUTH_DB_PATH（可选）— 认证数据库路径，默认 relay_auth.db
- WECOM_*（可选）— 企业微信渠道；配置 WECOM_CORP_ID 即启用，未配置则禁用企微
- XIAOZHI_*（可选）— 小智硬件渠道；XIAOZHI_ENABLED=true 时启用
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from .config import RelayConfig
from .server import (
    router as relay_ws_router,
    get_agent_connected,
    init_relay_server,
    register_reply_handler,
    forward_to_agent,
)
from .wecom_bridge import router as wecom_router, init_wecom_bridge
from .wecom.crypto import WeComCrypto
from .wecom.sender import WeComSender


logger = logging.getLogger("pandapal_relay")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )


def _load_env() -> None:
    """加载 relay/.env 文件。"""
    try:
        from dotenv import load_dotenv
        # 尝试多个位置
        env_path = Path(__file__).parent / ".env"
        if env_path.exists():
            load_dotenv(env_path)
            logger.info("[Config] Loaded %s", env_path)
        else:
            # fallback: pandapal_relay/.env.development
            env_dev = Path(__file__).parent / ".env.development"
            if env_dev.exists():
                load_dotenv(env_dev)
                logger.info("[Config] Loaded %s", env_dev)
    except ImportError:
        pass


def _init_xiaozhi_bridge(config: RelayConfig) -> None:
    """初始化 XiaoZhi Bridge（如果启用）。"""
    from .xiaozhi_bridge import (
        xiaozhi_router,
        init_xiaozhi_bridge,
        handle_agent_reply_for_xiaozhi,
    )

    init_xiaozhi_bridge(
        asr_provider_name=config.xiaozhi_asr_provider,
        asr_api_key=config.xiaozhi_asr_api_key,
        tts_provider_name=config.xiaozhi_tts_provider,
        tts_api_key=config.xiaozhi_tts_api_key,
        forward_to_agent=forward_to_agent,
    )

    # 注册 XiaoZhi 渠道回复处理器
    register_reply_handler(handle_agent_reply_for_xiaozhi)

    return xiaozhi_router


async def _init_auth(config: RelayConfig):
    """初始化 Auth 服务（在 Relay 端提供 HTTP 认证接口）。

    Returns:
        已初始化的 AuthService 实例。
    """
    from pandapal_relay.auth.service import AuthService
    from pandapal_relay.auth.models import AuthConfig
    from pandapal_relay.auth.router import init_auth_router

    auth_service = AuthService(
        config=AuthConfig(
            jwt_secret=config.auth_jwt_secret,
            db_path=config.auth_db_path,
        ),
    )
    await auth_service.initialize()
    init_auth_router(auth_service)
    logger.info("[Auth] Auth service initialized (db=%s)", config.auth_db_path)
    return auth_service


async def run_relay() -> None:
    """启动 Relay Server。"""
    _setup_logging()
    logger.info("=" * 50)
    logger.info("PandaPal Relay Server — Starting...")
    logger.info("=" * 50)

    _load_env()

    # ── 配置 ──
    config = RelayConfig.from_env()
    missing = config.validate()
    if missing:
        # 失败 7（Fail-Fast）：缺失任意必填项（含 AUTH_JWT_SECRET）即拒绝启动
        logger.error("[Config] Missing env vars: %s", ", ".join(missing))
        sys.exit(1)

    logger.info("[Config] OK — port=%d", config.port)

    # ── 初始化 Auth 服务 ──
    auth_service = await _init_auth(config)

    # ── 注入 AuthService 到 Relay Server（用于 WSS JWT 验签）──
    init_relay_server(auth_service)

    # ── WeCom 组件（可选渠道：未配置 WECOM_CORP_ID 即禁用，服务器照常启动）──
    wecom_transport = None
    wecom_sender = None
    if config.wecom_enabled:
        crypto = WeComCrypto(
            token=config.wecom_token,
            encoding_aes_key=config.wecom_aes_key,
            corp_id=config.wecom_corp_id,
        )
        wecom_sender = WeComSender(
            corp_id=config.wecom_corp_id,
            agent_id=config.wecom_agent_id,
            app_secret=config.wecom_app_secret,
        )

        # 回包路由 user_id 零兜底（健壮性与降级契约 §九：ID 类缺失即报错）——
        #   仅取显式配置的 WECOM_DEFAULT_USER_ID，且必须在白名单内；
        #   缺失或不在白名单 → fail-fast 拒绝启动（error 提示），绝不从白名单推导/猜测。
        #   出站帧 payload 缺 user_id 时 wecom_bridge 拒绝发送 + error 留痕。
        wecom_default_user_id = config.wecom_default_user_id
        if not wecom_default_user_id:
            logger.error(
                "[Config] WECOM_DEFAULT_USER_ID 未配置 — user_id 零兜底，拒绝启动。"
                "请显式配置回包路由用户（必须是 WECOM_APP_MESSAGE_USERIDS 白名单成员）"
            )
            sys.exit(1)
        if not config.wecom_allowed_userids:
            logger.error(
                "[Config] WECOM_APP_MESSAGE_USERIDS 未配置或为空 — 白名单 fail-closed "
                "（未配置≠不限制），拒绝启动"
            )
            sys.exit(1)
        if wecom_default_user_id not in config.wecom_allowed_userids:
            logger.error(
                "[Config] WECOM_DEFAULT_USER_ID='%s' 不在白名单 WECOM_APP_MESSAGE_USERIDS "
                "内，拒绝启动",
                wecom_default_user_id,
            )
            sys.exit(1)
        wecom_transport = init_wecom_bridge(
            crypto=crypto,
            sender=wecom_sender,
            user_id=wecom_default_user_id,
            allowed_userids=config.wecom_allowed_userids,
        )
        logger.info(
            "[Relay] startup self-check: wecom 回包路由用户 %s（已显式配置且在白名单内）",
            wecom_default_user_id,
        )
        try:
            await wecom_transport.start()
            logger.info("[WeComBridge] Transport started (access_token verified)")
        except Exception as e:
            # 启动失败不阻塞服务（HC3 Fail-Safe）——记 warning，进入降级模式
            logger.warning("[WeComBridge] Transport start failed (continuing offline): %s", e)

        # ── 启动自检：报告 transport 真实状态（observability 诚实化）──
        wecom_lifecycle = {
            "channel": "wecom",
            "transport": type(wecom_transport).__name__,
            "is_started": wecom_transport.is_started,
        }
        if not wecom_transport.is_started:
            logger.warning(
                "[Relay] startup self-check: wecom transport NOT started — "
                "企微消息发送可能失败"
            )
        logger.info(
            "[Relay] startup self-check: channel=%s transport=%s is_started=%s",
            wecom_lifecycle["channel"],
            wecom_lifecycle["transport"],
            wecom_lifecycle["is_started"],
        )
    else:
        logger.info(
            "[Config] WECOM_CORP_ID 未配置 — WeCom 渠道禁用（仅 Auth + Agent 通道可用）"
        )

    # ── 创建 FastAPI app ──
    from pandapal_relay.auth.router import auth_router
    from fastapi.middleware.cors import CORSMiddleware

    app = FastAPI(title="PandaPal Relay", version="0.1.0")

    # CORS: 允许桌面前端跨域调用 auth 接口
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # 桌面端使用 tauri://localhost 或 http://localhost
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth_router)
    if config.wecom_enabled:
        app.include_router(wecom_router)
    app.include_router(relay_ws_router)

    # ── 初始化 XiaoZhi Bridge（如果启用） ──
    if config.xiaozhi_enabled:
        xiaozhi_router = _init_xiaozhi_bridge(config)
        app.include_router(xiaozhi_router)
        logger.info("[Config] XiaoZhi Bridge enabled (asr=%s, tts=%s)",
                    config.xiaozhi_asr_provider, config.xiaozhi_tts_provider)

    @app.get("/health")
    async def health():
        result = {
            "status": "ok",
            "agent_connected": get_agent_connected(),
            "auth_enabled": True,
            "wecom_enabled": config.wecom_enabled,
            "xiaozhi_enabled": config.xiaozhi_enabled,
        }
        if config.wecom_enabled and wecom_transport is not None:
            result["wecom_transport_started"] = wecom_transport.is_started
        if config.xiaozhi_enabled:
            from .xiaozhi_bridge import get_device_count
            result["xiaozhi_devices"] = get_device_count()
        return result

    # ── 启动 ──
    uv_config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=config.port,
        log_level="info",
        # WebSocket 协议级 Ping/Pong 配置：
        # 禁用 uvicorn 服务端主动 ping（默认 20s），
        # 改为由 Gateway 客户端主动发 ping，uvicorn 被动回 pong。
        # 这样避免两端都发 ping 浪费带宽，也防止 uvicorn ping 超时误杀长连接。
        ws_ping_interval=None,
        ws_ping_timeout=None,
    )
    server = uvicorn.Server(uv_config)

    def _signal_handler(sig, frame):
        logger.info("[Relay] Signal %s received, shutting down...", sig)
        server.should_exit = True

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    logger.info("[Relay] Listening on 0.0.0.0:%d", config.port)
    logger.info("[Relay] Routes:")
    logger.info("[Relay]   POST /auth/register")
    logger.info("[Relay]   POST /auth/login")
    logger.info("[Relay]   PUT  /auth/password  (Bearer JWT)")
    if config.wecom_enabled:
        logger.info("[Relay]   POST /assistant/wecom/callback")
    logger.info("[Relay]   WS   /relay/ws")
    if config.xiaozhi_enabled:
        logger.info("[Relay]   WS   /xiaozhi/ws")
    logger.info("[Relay]   GET  /health")

    try:
        await server.serve()
    finally:
        # 关闭 wecom transport（与 start() 配对，仅 WeCom 启用时）
        if wecom_transport is not None:
            try:
                await wecom_transport.stop()
            except Exception as e:
                logger.warning("[WeComBridge] Transport stop error: %s", e)
        if wecom_sender is not None:
            await wecom_sender.close()
        await auth_service.shutdown()
        logger.info("[Relay] Shutdown complete.")


def main() -> None:
    asyncio.run(run_relay())


if __name__ == "__main__":
    main()
