-- Phase 1D-B: preserve v1 rows while adding immutable candidate history and repair intent.
DROP TRIGGER workflow_update_guard ON workflow_runs;
DROP FUNCTION guard_workflow_update();
DROP TRIGGER candidate_immutable ON candidate_artifacts;

CREATE TABLE workflow_sessions (
    session_id text PRIMARY KEY CHECK (length(session_id) BETWEEN 1 AND 200),
    run_id uuid NOT NULL REFERENCES workflow_runs(id) ON DELETE RESTRICT,
    predecessor_session_id text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (run_id, session_id),
    FOREIGN KEY (run_id, predecessor_session_id)
        REFERENCES workflow_sessions(run_id, session_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK (predecessor_session_id IS NULL OR predecessor_session_id <> session_id)
);
INSERT INTO workflow_sessions(session_id, run_id)
    SELECT session_id, id FROM workflow_runs;

ALTER TABLE workflow_runs ADD COLUMN current_session_id text;
UPDATE workflow_runs SET current_session_id = session_id;
ALTER TABLE workflow_runs ALTER COLUMN current_session_id SET NOT NULL;
ALTER TABLE workflow_runs ADD CONSTRAINT workflow_current_session_link
    FOREIGN KEY (id, current_session_id)
    REFERENCES workflow_sessions(run_id, session_id) ON UPDATE RESTRICT ON DELETE RESTRICT
    DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE candidate_artifacts ADD COLUMN ordinal integer;
ALTER TABLE candidate_artifacts ADD COLUMN source_session_id text;
ALTER TABLE candidate_artifacts ADD COLUMN source_turn_id text;
ALTER TABLE candidate_artifacts ADD COLUMN repair_attempt_id uuid;
UPDATE candidate_artifacts AS c SET ordinal = 1, source_session_id = r.session_id
    FROM workflow_runs AS r WHERE r.id = c.run_id;
ALTER TABLE candidate_artifacts ALTER COLUMN ordinal SET NOT NULL;
ALTER TABLE candidate_artifacts ALTER COLUMN source_session_id SET NOT NULL;
ALTER TABLE candidate_artifacts ADD CONSTRAINT candidate_ordinal_positive CHECK (ordinal > 0);
ALTER TABLE candidate_artifacts ADD CONSTRAINT candidate_source_ids_bounded CHECK (
    length(source_artifact_id) <= 200 AND
    (source_turn_id IS NULL OR length(source_turn_id) BETWEEN 1 AND 200) AND
    length(storage_path) <= 300
);
ALTER TABLE candidate_artifacts ADD CONSTRAINT candidate_ordinal_unique UNIQUE (run_id, ordinal);
ALTER TABLE candidate_artifacts ADD CONSTRAINT candidate_source_artifact_unique UNIQUE (run_id, source_artifact_id);
ALTER TABLE candidate_artifacts ADD CONSTRAINT candidate_source_turn_unique UNIQUE
    (run_id, source_session_id, source_turn_id);
ALTER TABLE candidate_artifacts ADD CONSTRAINT candidate_session_link
    FOREIGN KEY (run_id, source_session_id) REFERENCES workflow_sessions(run_id, session_id)
    ON UPDATE RESTRICT ON DELETE RESTRICT;

CREATE TABLE repair_attempts (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES workflow_runs(id) ON DELETE RESTRICT,
    ordinal integer NOT NULL CHECK (ordinal > 0),
    failed_candidate_id uuid NOT NULL,
    failed_verification_id uuid NOT NULL,
    session_id text NOT NULL,
    input_key char(64) NOT NULL UNIQUE CHECK (input_key ~ '^[0-9a-f]{64}$'),
    input_sha256 char(64) NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    status text NOT NULL DEFAULT 'PLANNED' CHECK (status IN ('PLANNED', 'UNCERTAIN', 'OBSERVED')),
    message_item_id text,
    result_turn_id text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (id, run_id),
    UNIQUE (run_id, ordinal),
    UNIQUE (failed_verification_id),
    UNIQUE (run_id, session_id, result_turn_id),
    CHECK (message_item_id IS NULL OR length(message_item_id) BETWEEN 1 AND 200),
    CHECK (result_turn_id IS NULL OR length(result_turn_id) BETWEEN 1 AND 200),
    FOREIGN KEY (failed_verification_id, run_id, failed_candidate_id)
        REFERENCES verification_runs(id, run_id, candidate_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (run_id, session_id) REFERENCES workflow_sessions(run_id, session_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT repair_observation_shape CHECK (
        (status <> 'OBSERVED' AND message_item_id IS NULL AND result_turn_id IS NULL) OR
        (status = 'OBSERVED' AND message_item_id IS NOT NULL AND result_turn_id IS NOT NULL)
    )
);
ALTER TABLE candidate_artifacts ADD CONSTRAINT candidate_repair_link
    FOREIGN KEY (repair_attempt_id, run_id) REFERENCES repair_attempts(id, run_id)
    ON UPDATE RESTRICT ON DELETE RESTRICT;
ALTER TABLE verification_runs ADD CONSTRAINT verification_evidence_bounded
    CHECK (octet_length(evidence::text) <= 8192);
CREATE TRIGGER candidate_immutable BEFORE UPDATE OR DELETE ON candidate_artifacts
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_row();

ALTER TABLE workflow_runs DROP CONSTRAINT workflow_runs_state_check;
ALTER TABLE workflow_runs DROP CONSTRAINT workflow_state_shape;
ALTER TABLE workflow_runs ADD CONSTRAINT workflow_runs_state_check CHECK (state IN
    ('RECEIVED', 'CANDIDATE_READY', 'VERIFYING', 'REPAIR_PENDING',
     'REPAIR_INPUT_PLANNED', 'REPAIR_INPUT_UNKNOWN', 'AWAITING_REPAIR_CANDIDATE',
     'VERIFIED', 'NEEDS_HUMAN'));
ALTER TABLE workflow_runs ADD CONSTRAINT workflow_state_shape CHECK (
    (state = 'RECEIVED' AND candidate_id IS NULL AND verification_id IS NULL) OR
    (state IN ('CANDIDATE_READY', 'VERIFYING') AND candidate_id IS NOT NULL AND verification_id IS NULL) OR
    (state IN ('REPAIR_PENDING', 'REPAIR_INPUT_PLANNED', 'REPAIR_INPUT_UNKNOWN',
               'AWAITING_REPAIR_CANDIDATE', 'VERIFIED')
       AND candidate_id IS NOT NULL AND verification_id IS NOT NULL) OR
    state = 'NEEDS_HUMAN'
);

CREATE FUNCTION guard_session_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE run workflow_runs%ROWTYPE;
BEGIN
    SELECT * INTO run FROM workflow_runs WHERE id = NEW.run_id;
    IF NEW.predecessor_session_id IS NULL THEN
        IF NEW.session_id IS DISTINCT FROM run.session_id THEN
            RAISE EXCEPTION 'only the original session can have no predecessor';
        END IF;
    ELSIF NEW.predecessor_session_id IS DISTINCT FROM run.current_session_id OR
          run.state <> 'REPAIR_PENDING' THEN
        RAISE EXCEPTION 'replacement session must extend the current failed session';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER workflow_session_insert_guard BEFORE INSERT ON workflow_sessions
    FOR EACH ROW EXECUTE FUNCTION guard_session_insert();
CREATE TRIGGER workflow_session_immutable BEFORE UPDATE OR DELETE ON workflow_sessions
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_row();

CREATE FUNCTION guard_repair_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE run workflow_runs%ROWTYPE;
DECLARE failed verification_runs%ROWTYPE;
DECLARE repair_limit integer;
BEGIN
    SELECT * INTO run FROM workflow_runs WHERE id = NEW.run_id;
    SELECT * INTO failed FROM verification_runs WHERE id = NEW.failed_verification_id;
    SELECT (document->>'max_repair_attempts')::integer INTO repair_limit
      FROM policy_snapshots WHERE policy_hash = run.policy_hash;
    IF run.state <> 'REPAIR_PENDING' OR run.verification_id IS DISTINCT FROM NEW.failed_verification_id OR
       run.candidate_id IS DISTINCT FROM NEW.failed_candidate_id OR failed.status <> 'FAIL' OR
       run.current_session_id IS DISTINCT FROM NEW.session_id OR NEW.ordinal > repair_limit OR
       NEW.ordinal <> (SELECT count(*) + 1 FROM repair_attempts WHERE run_id = NEW.run_id) OR
       NEW.status <> 'PLANNED' THEN
        RAISE EXCEPTION 'repair attempt violates state, lineage, or budget';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER repair_insert_guard BEFORE INSERT ON repair_attempts
    FOR EACH ROW EXECUTE FUNCTION guard_repair_insert();

CREATE FUNCTION guard_repair_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.run_id, NEW.ordinal, NEW.failed_candidate_id, NEW.failed_verification_id,
        NEW.session_id, NEW.input_key, NEW.input_sha256, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.run_id, OLD.ordinal, OLD.failed_candidate_id, OLD.failed_verification_id,
        OLD.session_id, OLD.input_key, OLD.input_sha256, OLD.created_at) OR
       NOT ((OLD.status = 'PLANNED' AND NEW.status = 'UNCERTAIN' AND
             NEW.message_item_id IS NULL AND NEW.result_turn_id IS NULL) OR
            (OLD.status = 'UNCERTAIN' AND NEW.status = 'OBSERVED' AND
             NEW.message_item_id IS NOT NULL AND NEW.result_turn_id IS NOT NULL)) THEN
        RAISE EXCEPTION 'repair intent is immutable or transition is illegal';
    END IF;
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;
CREATE TRIGGER repair_update_guard BEFORE UPDATE ON repair_attempts
    FOR EACH ROW EXECUTE FUNCTION guard_repair_update();
CREATE TRIGGER repair_no_delete BEFORE DELETE ON repair_attempts
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_row();

CREATE FUNCTION guard_candidate_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE run workflow_runs%ROWTYPE;
DECLARE prior candidate_artifacts%ROWTYPE;
DECLARE attempt repair_attempts%ROWTYPE;
BEGIN
    SELECT * INTO run FROM workflow_runs WHERE id = NEW.run_id;
    IF NEW.source_session_id IS DISTINCT FROM run.current_session_id OR
       NEW.source_turn_id IS NULL OR length(NEW.source_turn_id) = 0 THEN
        RAISE EXCEPTION 'candidate source is not a saved turn in current session';
    END IF;
    IF NEW.ordinal = 1 THEN
        IF run.state <> 'RECEIVED' OR NEW.repair_attempt_id IS NOT NULL THEN
            RAISE EXCEPTION 'first candidate requires a new run';
        END IF;
    ELSE
        SELECT * INTO prior FROM candidate_artifacts WHERE id = run.candidate_id;
        SELECT * INTO attempt FROM repair_attempts WHERE id = NEW.repair_attempt_id;
        IF run.state <> 'AWAITING_REPAIR_CANDIDATE' OR
           NEW.ordinal IS DISTINCT FROM prior.ordinal + 1 OR attempt.status IS DISTINCT FROM 'OBSERVED' OR
           attempt.run_id IS DISTINCT FROM NEW.run_id OR
           attempt.failed_verification_id IS DISTINCT FROM run.verification_id OR
           attempt.session_id IS DISTINCT FROM NEW.source_session_id OR
           attempt.result_turn_id IS DISTINCT FROM NEW.source_turn_id OR
           NEW.source_artifact_id IS NOT DISTINCT FROM prior.source_artifact_id OR
           NEW.archive_sha256 IS NOT DISTINCT FROM prior.archive_sha256 OR
           NEW.tree_sha256 IS NOT DISTINCT FROM prior.tree_sha256 THEN
            RAISE EXCEPTION 'candidate is not linked to the observed repair turn';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER candidate_insert_guard BEFORE INSERT ON candidate_artifacts
    FOR EACH ROW EXECUTE FUNCTION guard_candidate_insert();

CREATE FUNCTION guard_workflow_update() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE linked_status text;
DECLARE linked_candidate candidate_artifacts%ROWTYPE;
DECLARE linked_session workflow_sessions%ROWTYPE;
DECLARE linked_evidence jsonb;
DECLARE repair_limit integer;
DECLARE repair_used integer;
BEGIN
    IF (NEW.id, NEW.task_key, NEW.repository, NEW.base_commit, NEW.session_id, NEW.policy_hash, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.task_key, OLD.repository, OLD.base_commit, OLD.session_id, OLD.policy_hash, OLD.created_at) THEN
        RAISE EXCEPTION 'workflow identity and policy are immutable';
    END IF;
    IF NEW.current_session_id IS DISTINCT FROM OLD.current_session_id THEN
        SELECT * INTO linked_session FROM workflow_sessions WHERE session_id = NEW.current_session_id;
        IF OLD.state <> 'REPAIR_PENDING' OR NEW.state <> OLD.state OR
           linked_session.run_id IS DISTINCT FROM OLD.id OR
           linked_session.predecessor_session_id IS DISTINCT FROM OLD.current_session_id THEN
            RAISE EXCEPTION 'replacement session lineage is invalid';
        END IF;
    END IF;
    IF NEW.candidate_id IS DISTINCT FROM OLD.candidate_id THEN
        SELECT * INTO linked_candidate FROM candidate_artifacts WHERE id = NEW.candidate_id;
        IF NOT COALESCE(((OLD.state = 'RECEIVED' AND NEW.state = 'CANDIDATE_READY' AND
                 OLD.candidate_id IS NULL AND NEW.verification_id IS NULL AND
                 linked_candidate.ordinal = 1) OR
                (OLD.state = 'AWAITING_REPAIR_CANDIDATE' AND NEW.state = 'CANDIDATE_READY' AND
                 NEW.verification_id IS NULL AND linked_candidate.ordinal =
                   (SELECT ordinal + 1 FROM candidate_artifacts WHERE id = OLD.candidate_id) AND
                 EXISTS (SELECT 1 FROM repair_attempts a WHERE a.id = linked_candidate.repair_attempt_id
                         AND a.failed_verification_id = OLD.verification_id AND a.status = 'OBSERVED'))), FALSE) THEN
            RAISE EXCEPTION 'candidate link cannot change without observed repair';
        END IF;
    ELSIF OLD.verification_id IS DISTINCT FROM NEW.verification_id AND
          NOT (OLD.verification_id IS NULL AND OLD.state = 'VERIFYING' AND
               NEW.state IN ('VERIFIED', 'REPAIR_PENDING', 'NEEDS_HUMAN')) THEN
        RAISE EXCEPTION 'verification link cannot change';
    END IF;
    IF NOT (
        (OLD.state = 'RECEIVED' AND NEW.state IN ('CANDIDATE_READY', 'NEEDS_HUMAN')) OR
        (OLD.state = 'CANDIDATE_READY' AND NEW.state IN ('VERIFYING', 'NEEDS_HUMAN')) OR
        (OLD.state = 'VERIFYING' AND NEW.state IN ('VERIFIED', 'REPAIR_PENDING', 'NEEDS_HUMAN')) OR
        (OLD.state = 'REPAIR_PENDING' AND NEW.state IN ('REPAIR_PENDING', 'REPAIR_INPUT_PLANNED', 'NEEDS_HUMAN')) OR
        (OLD.state = 'REPAIR_INPUT_PLANNED' AND NEW.state IN ('REPAIR_INPUT_UNKNOWN', 'NEEDS_HUMAN')) OR
        (OLD.state = 'REPAIR_INPUT_UNKNOWN' AND NEW.state IN ('AWAITING_REPAIR_CANDIDATE', 'NEEDS_HUMAN')) OR
        (OLD.state = 'AWAITING_REPAIR_CANDIDATE' AND NEW.state IN ('CANDIDATE_READY', 'NEEDS_HUMAN')) OR
        (OLD.state = 'VERIFIED' AND NEW.state = 'NEEDS_HUMAN')
    ) THEN
        RAISE EXCEPTION 'illegal workflow transition: % to %', OLD.state, NEW.state;
    END IF;
    IF NEW.state = 'VERIFIED' THEN
        SELECT status INTO linked_status FROM verification_runs WHERE id = NEW.verification_id
            AND run_id = NEW.id AND candidate_id = NEW.candidate_id;
        IF linked_status IS DISTINCT FROM 'PASS' THEN
            RAISE EXCEPTION 'VERIFIED requires a linked PASS verification';
        END IF;
    ELSIF NEW.state IN ('REPAIR_PENDING', 'REPAIR_INPUT_PLANNED', 'REPAIR_INPUT_UNKNOWN',
                        'AWAITING_REPAIR_CANDIDATE') THEN
        SELECT status, evidence INTO linked_status, linked_evidence FROM verification_runs WHERE id = NEW.verification_id
            AND run_id = NEW.id AND candidate_id = NEW.candidate_id;
        IF linked_status IS DISTINCT FROM 'FAIL' THEN
            RAISE EXCEPTION 'repair requires a linked FAIL verification';
        END IF;
        IF OLD.state = 'VERIFYING' AND NEW.state = 'REPAIR_PENDING' THEN
            SELECT (document->>'max_repair_attempts')::integer INTO repair_limit
                FROM policy_snapshots WHERE policy_hash = OLD.policy_hash;
            SELECT count(*) INTO repair_used FROM repair_attempts WHERE run_id = OLD.id;
            IF repair_used >= repair_limit OR
               linked_evidence->'findings' IS DISTINCT FROM jsonb_build_array(jsonb_build_object(
                 'code', 'ADD_ARITHMETIC',
                 'message', 'add(a, b) must return the arithmetic sum for positive, zero, and negative integers.')) THEN
                RAISE EXCEPTION 'repair policy budget or approved finding is absent';
            END IF;
        END IF;
        IF NEW.state = 'REPAIR_INPUT_PLANNED' AND NOT EXISTS (
            SELECT 1 FROM repair_attempts a WHERE a.run_id = NEW.id AND
            a.failed_verification_id = NEW.verification_id AND
            a.session_id = NEW.current_session_id AND a.status = 'PLANNED') THEN
            RAISE EXCEPTION 'repair input lacks planned intent';
        END IF;
        IF NEW.state = 'REPAIR_INPUT_UNKNOWN' AND NOT EXISTS (
            SELECT 1 FROM repair_attempts a WHERE a.run_id = NEW.id AND
            a.failed_verification_id = NEW.verification_id AND a.status = 'UNCERTAIN') THEN
            RAISE EXCEPTION 'repair input lacks uncertain submission record';
        END IF;
        IF NEW.state = 'AWAITING_REPAIR_CANDIDATE' AND NOT EXISTS (
            SELECT 1 FROM repair_attempts a WHERE a.run_id = NEW.id AND
            a.failed_verification_id = NEW.verification_id AND a.status = 'OBSERVED') THEN
            RAISE EXCEPTION 'repair turn has not been reconciled';
        END IF;
    END IF;
    NEW.version := OLD.version + 1;
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;
CREATE TRIGGER workflow_update_guard BEFORE UPDATE ON workflow_runs
    FOR EACH ROW EXECUTE FUNCTION guard_workflow_update();

INSERT INTO schema_migrations(version) VALUES (2);
