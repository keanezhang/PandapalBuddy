"""Config 层数据模型。

SystemConfig: 从 agent.yaml 解析的系统配置（frozen）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SystemConfig:
    """系统配置（从 agent.yaml 解析，不可变）。

    I4: 返回给调用方的是 frozen 对象，防止缓存污染。
    """

    relay_url: str
    relay_auth_token: str
    data_dir: str = ""  # 从环境变量 PANDAPAL_DATA_DIR 读取
    session_timeout_minutes: int = 60  # 代码默认值，不需要环境变量配置
    hitl_timeout_seconds: int = 600  # 代码默认值，不需要环境变量配置
    screen_control_enabled: bool = False  # 代码默认值，不需要环境变量配置
    # 存储模式（PANDAPAL_STORAGE_MODE）：统一驱动**会话/对话数据**与**四大观测支柱**
    # 的落盘方式，一个开关切换整机形态。
    #   "markdown" — 人类可读 .md 文件（默认，调试/轻量）
    #   "sqlite"   — SQLite 数据库（正式生产：pandapal.db 会话库 + observability.db 观测库）
    storage_mode: str = "markdown"
