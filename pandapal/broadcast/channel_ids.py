"""Channel ID constants — single source of truth for all channel identifiers."""

# ── 远程渠道固定 ID ──────────────────────────────────────────────────────────

# 企微渠道：固定精确 ID（不含 user_id，user_id 通过 payload 传递）
WECOM_CHANNEL_ID = "wecom"

# 小智渠道：前缀 + device_id，允许 xiaozhi:{device_id} 形式
XIAOZHI_CHANNEL_PREFIX = "xiaozhi:"  # 前缀白名单条目（以 ":" 结尾表示前缀匹配）

# ── 本地渠道 ID（不参与远程白名单）─────────────────────────────────────────

LOCAL_DESKTOP_IPC_CHANNEL_ID = "__desktop_ipc__"
LOCAL_HITL_CHANNEL_ID = "__hitl_bridge__"
LOCAL_SCHEDULER_CHANNEL_ID = "__scheduler__"  # 任务调度内部通信（task_instruction / task_result）
LOCAL_AGENT_TASK_CHANNEL_ID = "__agent_task__"  # AgentTask 任务面板推送渠道

# ── 远程渠道白名单（注入 ChannelRegistry）──────────────────────────────────
#
# 规则：
#   - 精确匹配：字符串不以 ":" 结尾，如 "wecom"
#   - 前缀匹配：字符串以 ":" 结尾，如 "xiaozhi:"（允许 "xiaozhi:{任意后缀}"）
#
ALLOWED_REMOTE_CHANNEL_PATTERNS: frozenset[str] = frozenset({
    WECOM_CHANNEL_ID,
    XIAOZHI_CHANNEL_PREFIX,
})
