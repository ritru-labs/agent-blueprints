-- Durable intent precedes the first Agents API session create request.
CREATE TABLE initial_session_intents (
    task_key text PRIMARY KEY CHECK (length(task_key) BETWEEN 1 AND 120),
    policy_hash char(64) NOT NULL REFERENCES policy_snapshots(policy_hash) ON DELETE RESTRICT,
    base_commit char(40) NOT NULL CHECK (base_commit ~ '^[0-9a-f]{40}$'),
    request_key char(64) NOT NULL UNIQUE CHECK (request_key ~ '^[0-9a-f]{64}$'),
    state text NOT NULL DEFAULT 'PLANNED'
        CHECK (state IN ('PLANNED', 'OUTCOME_UNKNOWN', 'CONFIRMED')),
    session_id text UNIQUE CHECK (length(session_id) BETWEEN 1 AND 200),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((state = 'CONFIRMED') = (session_id IS NOT NULL))
);
CREATE FUNCTION guard_initial_session_intent_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.task_key, NEW.policy_hash, NEW.base_commit, NEW.request_key, NEW.created_at)
       IS DISTINCT FROM
       (OLD.task_key, OLD.policy_hash, OLD.base_commit, OLD.request_key, OLD.created_at) OR
       NOT ((OLD.state = 'PLANNED' AND NEW.state = 'OUTCOME_UNKNOWN' AND
             NEW.session_id IS NULL) OR
            (OLD.state = 'OUTCOME_UNKNOWN' AND NEW.state = 'CONFIRMED' AND
             NEW.session_id IS NOT NULL)) THEN
        RAISE EXCEPTION 'initial session intent is immutable or transition is illegal';
    END IF;
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;
CREATE TRIGGER initial_session_intent_update_guard BEFORE UPDATE ON initial_session_intents
    FOR EACH ROW EXECUTE FUNCTION guard_initial_session_intent_update();
CREATE TRIGGER initial_session_intent_no_delete BEFORE DELETE ON initial_session_intents
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_row();

INSERT INTO schema_migrations(version) VALUES (10);
