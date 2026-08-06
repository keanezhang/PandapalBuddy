"""pandapal.local — 本地 Agent 端（运行在你的电脑上）

独立运行，职责：
- 通过 WebSocket 连接远端 Relay Server
- 接收消息 → 调用 pandaren Agent 处理 → 回复发回 Relay

启动：python -m pandapal.local
"""
