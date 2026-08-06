# PandaPal Relay — 多渠道接入中继服务器

> Relay Server 是 PandaPal 的多渠道接入层，负责将企微、XiaoZhi 等外部渠道的消息转发给本地 Agent，并将 Agent 的回复推送回对应渠道。

## 功能特性

- 🔌 **WebSocket 接入**：Agent 通过 WebSocket 连接到 Relay Server
- 📡 **企微回调**：接收企业微信消息回调（POST /assistant/wecom/callback）
- 🎤 **XiaoZhi 接入**：支持 XiaoZhi 智能音箱设备 WebSocket 连接
- 🔄 **消息中继**：双向转发消息（渠道 → Agent → 渠道）
- ✅ **HITL 桥接**：将企微作为 HITL 审批渠道
- 💓 **健康检查**：GET /health 端点
- 📊 **渠道追踪**：GET /relay/channels 查看活跃渠道

## 架构概览

```
┌─────────────┐     WebSocket      ┌─────────────┐
│   Agent     │ ◄──────────────►  │   Relay     │
│  (本地)     │   /relay/ws       │   Server    │
└─────────────┘                   │  (FastAPI)  │
                                 └─────────────┘
                                        │
                      ┌─────────────────┼─────────────────┐
                      │                 │                 │
               POST /assistant/    WS /xiaozhi/ws    GET /health
               /wecom/callback        │
                      │                 │
               ┌──────────┐    ┌──────────────┐
               │  企微     │    │  XiaoZhi     │
               │  服务器   │    │  智能音箱     │
               └──────────┘    └──────────────┘
```

## 项目结构

```
pandapal_relay/
├── run_relay.py           启动入口（python -m pandapal_relay）
├── server.py              FastAPI 应用 + WebSocket 端点
├── config.py              配置管理（从环境变量加载）
├── message_types.py       消息类型契约（与 pandapal/messages/types.py 同步的本地副本）
├── normalized_events.py   NormalizedEvent 模型（本地副本）
├── router_models.py       InboundMessage 模型（本地副本）
├── transport_protocol.py  Transport 协议（本地副本）
├── wecom_bridge.py        企微桥接器（回调解密、消息转发）
├── wecom_transport.py     企微消息推送（WeComRestTransport）
├── wecom/                 企微底层支持
│   ├── crypto.py          消息加解密
│   └── sender.py          主动发送消息
├── xiaozhi_bridge.py      XiaoZhi 桥接器
├── xiaozhi/               XiaoZhi 协议封装
│   ├── asr.py             语音识别
│   ├── tts.py             语音合成
│   └── models.py          设备会话模型
├── auth/                  账号认证系统（注册/登录/JWT）
│   ├── service.py
│   ├── router.py
│   └── models.py
└── tests/                 单元测试
```

## 快速开始

### 安装依赖

```bash
cd pandapal_relay
pip install -e ..     # 从仓库根 editable 安装（依赖见根 pyproject.toml）
```

依赖项（声明于仓库根 `pyproject.toml` 的 `[project.dependencies]`）：
- fastapi
- uvicorn
- websockets
- pyyaml
- python-dotenv
- cryptography（企微消息加密）
- aiosqlite（账号系统持久化）
- bcrypt / PyJWT（账号认证）

### 配置

创建 `.env` 文件：

```bash
# .env

# 服务端口
RELAY_PORT=8090

# ── 认证（必填）──
# JWT 签名密钥：缺失即拒绝启动（fail-fast）
AUTH_JWT_SECRET=your_jwt_secret

# ── 企微配置（可选：未配置 WECOM_CORP_ID 即禁用企微，服务器照常启动）──
# 启用则以下全部必填（fail-closed，防半配置静默失效）
WECOM_CORP_ID=your_corp_id
WECOM_AGENT_ID=your_agent_id
WECOM_APP_SECRET=your_app_secret
WECOM_TOKEN=your_wecom_token
WECOM_AES_KEY=your_wecom_encoding_aes_key
WECOM_APP_MESSAGE_USERIDS=YourUserId     # 白名单（fail-closed）
WECOM_DEFAULT_USER_ID=YourUserId         # 回包路由 user_id（必填，须在白名单内）

# ── XiaoZhi 配置（可选：XIAOZHI_ENABLED=true 时启用）──
XIAOZHI_ENABLED=false
XIAOZHI_ASR_PROVIDER=your_asr_provider
XIAOZHI_ASR_API_KEY=your_asr_api_key
XIAOZHI_TTS_PROVIDER=your_tts_provider
XIAOZHI_TTS_API_KEY=your_tts_api_key
```

> 最少配置：仅 `AUTH_JWT_SECRET` 必填（`RELAY_PORT` 默认 8090）。
> 不配置企微 / 小智时，服务器以 Auth + Agent 通道模式正常运行。

### 启动 Relay Server

```bash
# 方式 1：作为模块运行（推荐）
python -m pandapal_relay

# 方式 2：直接运行脚本
python pandapal_relay/run_relay.py
```

### 验证运行

```bash
# 健康检查
curl http://localhost:8090/health

# 预期返回：
# {"status": "ok", "agent_connected": false, "auth_enabled": true, "xiaozhi_enabled": false}
```

## 使用说明

### 1. Agent 连接

Agent 作为 WebSocket 客户端连接到 Relay Server：

```python
import websockets

async def connect_agent():
    async with websockets.connect("ws://localhost:8090/relay/ws") as ws:
        # 接收消息
        async for msg in ws:
            frame = json.loads(msg)
            print(f"Received: {frame}")
            # 发送 ACK
            await ws.send(json.dumps({"type": "ack", "msg_id": frame["msg_id"]}))
```

PandaPal 的 `pandapal/local/run_local.py` 会自动完成此连接。

### 2. 企微接入

在企微管理后台配置消息回调 URL：

```
http://your-domain:8090/assistant/wecom/callback
```

Relay Server 会自动处理：
- URL 验证（GET 请求）
- 消息解密（POST 请求）
- 将消息转发给 Agent
- 接收 Agent 回复并加密发送到企微

### 3. XiaoZhi 接入

XiaoZhi 设备连接到 Relay Server：

```
WebSocket: ws://your-domain:8090/xiaozhi/ws
```

Relay Server 会：
- 处理 ASR（语音识别）音频帧
- 将识别文本转发给 Agent
- 接收 Agent 回复，通过 TTS（语音合成）返回音频

### 4. HITL 审批

当 Agent 触发 HITL 暂停时，Relay Server 会：
1. 将审批请求通过企微发送给用户
2. 用户在企业微信中点击「批准」或「拒绝」
3. Relay Server 将决策转发给 Agent
4. Agent 继续执行或终止

## API 端点

### WebSocket

| 端点 | 说明 |
|------|------|
| `WS /relay/ws` | Agent 连接端点 |
| `WS /xiaozhi/ws` | XiaoZhi 设备连接端点（需启用） |

### HTTP

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| GET | `/relay/channels` | 获取活跃渠道列表 |
| POST | `/assistant/wecom/callback` | 企微消息回调（启用 WeCom 时注册） |
| GET | `/assistant/wecom/callback` | 企微 URL 验证（启用 WeCom 时注册） |

## 消息帧格式

### 渠道 → Agent（转发消息）

```json
{
  "type": "message",
  "msg_id": "uuid",
  "payload": {
    "message_type": "user_instruction",
    "user_id": "user-123",
    "session_id": "session-001",
    "content": "帮我查一下天气",
    "content_type": "text",
    "source_channel_id": "wecom:user-123"
  }
}
```

> `session_id` 必填（发起方创建，空值即 fail-fast）；`source_channel_id` 位于 `payload` 内。

### Agent → 渠道（回复消息）

```json
{
  "type": "message",
  "msg_id": "uuid",
  "event_type": "agent_reply",
  "payload": {
    "content": "今天天气晴朗...",
    "session_id": "session-001"
  },
  "target_channel_ids": ["wecom:user-123"]
}
```

> Agent 侧帧使用 `event_type`（`EventType` 枚举值，如 `agent_reply` / `hitl_request`），
> 目标渠道用数组 `target_channel_ids`。

### HITL 请求

```json
{
  "type": "message",
  "msg_id": "uuid",
  "event_type": "hitl_request",
  "payload": {
    "approval_id": "req-123",
    "tool_name": "send_email",
    "tool_args_summary": "收件人: ...",
    "session_id": "session-001"
  },
  "target_channel_ids": ["wecom:user-123"]
}
```

### HITL 响应

```json
{
  "type": "message",
  "msg_id": "uuid",
  "payload": {
    "message_type": "approval_response",
    "user_id": "user-123",
    "content": {
      "approval_id": "req-123",
      "decision": "approved"
    },
    "session_id": "session-001",
    "source_channel_id": "wecom:user-123"
  }
}
```

> `decision` 取值 `"approved"` / `"rejected"`（`HITLDecision` 常量，勿用动词原形 `approve` / `reject`）。

## 部署

### 使用 nginx 反向代理

```nginx
# /etc/nginx/sites-available/pandapal_relay
server {
    listen 80;
    server_name your-domain.com;

    location /relay/ {
        proxy_pass http://localhost:8090;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
    }

    location /assistant/ {
        proxy_pass http://localhost:8090;
        proxy_http_version 1.1;
    }
}
```

### 使用 systemd 管理进程

```ini
# /etc/systemd/system/pandapal-relay.service
[Unit]
Description=PandaPal Relay Server
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/pandapal_relay
ExecStart=/usr/bin/python -m pandapal_relay
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
# 启动并设置开机自启
sudo systemctl enable pandapal-relay
sudo systemctl start pandapal-relay
sudo systemctl status pandapal-relay

# 修改 unit 文件或配置后重载
sudo nano /etc/systemd/system/pandapal-relay.service
sudo systemctl daemon-reload
sudo systemctl restart pandapal-relay

# 健康检查
curl http://localhost:8090/health
# 预期返回：
# {"status":"ok","agent_connected":false,"auth_enabled":true,"xiaozhi_enabled":false}

# 查看实时日志
sudo journalctl -u pandapal-relay.service -f
```

## 故障排查

### Agent 无法连接

1. 确认 Relay Server 已启动：`curl http://localhost:8090/health`
2. 检查防火墙设置：确保端口 8090 开放
3. 查看 Relay Server 日志

### 企微消息无法接收

1. 确认回调 URL 配置正确
2. 确认 Token 和 AES Key 配置正确
3. 查看 Relay Server 日志：`grep "WeCom" <log_file>`

### XiaoZhi 设备无法连接

1. 确认 `XIAOZHI_ENABLED=true`
2. 确认 ASR/TTS 配置正确
3. 检查 WebSocket 连接：`wscat -c ws://localhost:8090/xiaozhi/ws`

## 许可证

MIT
