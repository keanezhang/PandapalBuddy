-- pandapal Storage Schema — 完整建表（单文件合并版）
-- 12 tables + indexes，一次建全，幂等（CREATE TABLE IF NOT EXISTS）。
-- 合并自：v001_initial_schema / v002_add_task_verification /
--          v003_session_list / v004_group_members。
--
-- ★ is_empty 语义：0=非空（默认，安全侧），1=空会话（显式设置，启动时清除）。
--   默认值取 0：避免裸 INSERT / 旧行迁移时被误标为「空」而被 hard_delete_empty_sessions 硬删。

-- ──────────────────────────────────────────────
-- Schema Version Tracking (single-row)
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL DEFAULT 0,
    applied_at TEXT NOT NULL
);

-- ──────────────────────────────────────────────
-- Sessions（含 UI 会话列表元数据）
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    last_active TEXT NOT NULL,
    created_at TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    preview TEXT NOT NULL DEFAULT '',
    message_count INTEGER NOT NULL DEFAULT 0,
    is_empty INTEGER NOT NULL DEFAULT 0,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT '',
    group_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_visible_updated
    ON sessions(user_id, is_empty, is_deleted, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_group
    ON sessions(user_id, group_id, is_empty, is_deleted, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_empty_flag
    ON sessions(user_id, is_empty);

-- ──────────────────────────────────────────────
-- Task Definitions
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS task_definitions (
    task_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    trigger_rule_json TEXT NOT NULL,
    task_prompt TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    sensitivity TEXT NOT NULL DEFAULT 'medium',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_definitions_user_id ON task_definitions(user_id);

-- ──────────────────────────────────────────────
-- Task Executions
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS task_executions (
    execution_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    started_at TEXT,
    completed_at TEXT,
    result_json TEXT,
    source_channel_id TEXT,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_task_executions_task_id ON task_executions(task_id);
CREATE INDEX IF NOT EXISTS idx_task_executions_user_id ON task_executions(user_id);

-- ──────────────────────────────────────────────
-- Device Registrations
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS device_registrations (
    device_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    channel_type TEXT NOT NULL,
    is_online INTEGER NOT NULL DEFAULT 0,
    registered_at TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_device_registrations_user_id ON device_registrations(user_id);
CREATE INDEX IF NOT EXISTS idx_device_registrations_user_online ON device_registrations(user_id, is_online);

-- ──────────────────────────────────────────────
-- Approval Requests (HITL Bridge)
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS approval_requests (
    approval_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tool_args_summary TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    decision TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    decision_user_id TEXT,
    session_id TEXT,
    source_channel_id TEXT NOT NULL DEFAULT '',
    reply_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_approval_requests_user_status ON approval_requests(user_id, status);

-- ──────────────────────────────────────────────
-- Avatar Configs
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS avatar_configs (
    user_id TEXT PRIMARY KEY,
    character_name TEXT NOT NULL,
    animation_list_json TEXT NOT NULL DEFAULT '[]',
    state_animation_map_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

-- ──────────────────────────────────────────────
-- Run States (HITL pause/resume)
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS run_states (
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    serialized_state BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, run_id)
);

-- ──────────────────────────────────────────────
-- Raw Log (pandaren SDK RawLogBackend)
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS raw_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    entry_type TEXT NOT NULL DEFAULT 'message',
    content_json TEXT NOT NULL,
    turn_index INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    run_id TEXT,
    step INTEGER
);

CREATE INDEX IF NOT EXISTS idx_raw_log_session_id ON raw_log(session_id);
CREATE INDEX IF NOT EXISTS idx_raw_log_user_id ON raw_log(user_id);

-- ──────────────────────────────────────────────
-- Session Summaries (pandaren SDK SummaryBackend)
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS session_summaries (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_session_summaries_session_id ON session_summaries(session_id);
CREATE INDEX IF NOT EXISTS idx_session_summaries_user_id ON session_summaries(user_id);

-- ──────────────────────────────────────────────
-- Agent Tasks（AI 会话内的步骤拆解与进度追踪）
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS agent_tasks (
    task_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    active_form TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    blocks_json TEXT NOT NULL DEFAULT '[]',
    blocked_by_json TEXT NOT NULL DEFAULT '[]',
    "order" INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    verify_hint TEXT NOT NULL DEFAULT '',
    verified INTEGER NOT NULL DEFAULT 0,
    verify_evidence TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_agent_tasks_session_id ON agent_tasks(session_id);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_session_status ON agent_tasks(session_id, status);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_user_id ON agent_tasks(user_id);

-- ──────────────────────────────────────────────
-- Session Groups（用户自定义会话分组）
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS session_groups (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    session_ids TEXT NOT NULL DEFAULT '[]'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_session_groups_user_name
    ON session_groups(user_id, name);

CREATE INDEX IF NOT EXISTS idx_session_groups_user_id
    ON session_groups(user_id);
