-- pandapal Storage Schema v003 - Session List (UI 会话列表)
-- Adds UI-layer metadata columns to sessions table and creates session_groups table.
-- Date: 2026-07-11
--
-- Semantic note:
--   Existing columns belong to SessionManager (message-session lifecycle).
--   New columns below belong to SessionListManager (UI-session metadata).
--   Two managers share the same table.
--
-- Compatibility:
--   All new columns have DEFAULT values so existing SessionRepository queries stay valid.
--   updated_at is written in sync with last_active by SessionRepository writes.

-- Sessions: UI metadata columns
ALTER TABLE sessions ADD COLUMN title TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN preview TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN message_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN is_empty INTEGER NOT NULL DEFAULT 1;
ALTER TABLE sessions ADD COLUMN is_favorite INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN updated_at TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN group_id TEXT;

-- Backfill updated_at from last_active for legacy rows
UPDATE sessions SET updated_at = last_active WHERE updated_at = '';

-- Session groups (user-defined categorization for sessions)
CREATE TABLE IF NOT EXISTS session_groups (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_session_groups_user_name
    ON session_groups(user_id, name);

CREATE INDEX IF NOT EXISTS idx_session_groups_user_id
    ON session_groups(user_id);

-- Indexes for hot-path queries on sessions
CREATE INDEX IF NOT EXISTS idx_sessions_visible_updated
    ON sessions(user_id, is_empty, is_deleted, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_sessions_group
    ON sessions(user_id, group_id, is_empty, is_deleted, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_sessions_empty_flag
    ON sessions(user_id, is_empty);
