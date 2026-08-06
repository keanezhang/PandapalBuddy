-- pandapal Storage Migration v002 — AgentTask Verification
-- Adds verify_hint / verified / verify_evidence columns to agent_tasks.
-- Date: 2026-07-07

ALTER TABLE agent_tasks ADD COLUMN verify_hint TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_tasks ADD COLUMN verified INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_tasks ADD COLUMN verify_evidence TEXT NOT NULL DEFAULT '';
