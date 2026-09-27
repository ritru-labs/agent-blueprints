-- Harden v10 initial-session confirmation for databases that already applied it.
-- The nullable session_id leaves PLANNED/OUTCOME_UNKNOWN intents unbound until
-- confirmation; the CHECK in v10 requires a session_id for CONFIRMED rows.
ALTER TABLE workflow_runs ADD CONSTRAINT workflow_initial_session_identity_unique
    UNIQUE (task_key, session_id, policy_hash, base_commit);
ALTER TABLE initial_session_intents ADD CONSTRAINT initial_session_run_identity_fk
    FOREIGN KEY (task_key, session_id, policy_hash, base_commit)
    REFERENCES workflow_runs(task_key, session_id, policy_hash, base_commit)
    ON UPDATE RESTRICT ON DELETE RESTRICT;

INSERT INTO schema_migrations(version) VALUES (11);
