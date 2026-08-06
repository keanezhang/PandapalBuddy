"""pandapal_relay — 云端 Relay Server（部署在服务器上）

独立部署，职责：
- 接收企微回调 → 通过 WebSocket 转发给本地 Agent
- 接收 Agent 回复 → 通过企微 API 发送给用户

启动：python -m pandapal_relay
"""
