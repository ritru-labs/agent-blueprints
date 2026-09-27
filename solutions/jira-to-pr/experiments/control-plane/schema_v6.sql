-- Phase 1F: immutable CI repair lineage and multiple publications on one PR branch.
ALTER TABLE publication_attempts DROP CONSTRAINT publication_attempts_run_id_key;
ALTER TABLE publication_attempts DROP CONSTRAINT publication_attempts_remote_id_branch_ref_key;
ALTER TABLE publication_attempts ADD CONSTRAINT publication_candidate_unique UNIQUE (run_id, candidate_id);
ALTER TABLE publication_attempts ADD CONSTRAINT publication_commit_unique UNIQUE
    (remote_id, branch_ref, commit_sha);

ALTER TABLE pr_observation_batches ADD CONSTRAINT observation_repair_link UNIQUE
    (id, head_link_id, run_id, candidate_id, verification_id);

CREATE TABLE ci_repair_intents (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES workflow_runs(id) ON DELETE RESTRICT,
    head_link_id uuid NOT NULL UNIQUE REFERENCES pr_head_links(id) ON DELETE RESTRICT,
    observation_id uuid NOT NULL UNIQUE,
    failed_candidate_id uuid NOT NULL,
    failed_verification_id uuid NOT NULL,
    session_id text NOT NULL,
    input_key char(64) NOT NULL UNIQUE CHECK (input_key ~ '^[0-9a-f]{64}$'),
    input_sha256 char(64) NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    status text NOT NULL DEFAULT 'PLANNED' CHECK (status IN ('PLANNED', 'UNCERTAIN', 'OBSERVED')),
    message_item_id text CHECK (message_item_id IS NULL OR length(message_item_id) BETWEEN 1 AND 200),
    result_turn_id text CHECK (result_turn_id IS NULL OR length(result_turn_id) BETWEEN 1 AND 200),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (id, run_id),
    FOREIGN KEY (observation_id, head_link_id, run_id, failed_candidate_id,
                 failed_verification_id)
        REFERENCES pr_observation_batches(id, head_link_id, run_id, candidate_id, verification_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (failed_verification_id, run_id, failed_candidate_id)
        REFERENCES verification_runs(id, run_id, candidate_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (run_id, session_id) REFERENCES workflow_sessions(run_id, session_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK ((status = 'OBSERVED') = (message_item_id IS NOT NULL AND result_turn_id IS NOT NULL))
);
ALTER TABLE candidate_artifacts ADD COLUMN ci_repair_id uuid;
ALTER TABLE candidate_artifacts ADD CONSTRAINT candidate_ci_repair_link
    FOREIGN KEY (ci_repair_id, run_id) REFERENCES ci_repair_intents(id, run_id)
    ON UPDATE RESTRICT ON DELETE RESTRICT;
ALTER TABLE candidate_artifacts ADD CONSTRAINT candidate_one_repair_source
    CHECK (repair_attempt_id IS NULL OR ci_repair_id IS NULL);

CREATE FUNCTION guard_ci_repair_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE run workflow_runs%ROWTYPE;
DECLARE batch pr_observation_batches%ROWTYPE;
DECLARE latest pr_head_links%ROWTYPE;
DECLARE repair_limit integer;
DECLARE repair_used integer;
BEGIN
    SELECT * INTO run FROM workflow_runs WHERE id = NEW.run_id FOR UPDATE;
    SELECT * INTO batch FROM pr_observation_batches WHERE id = NEW.observation_id;
    SELECT * INTO latest FROM pr_head_links WHERE run_id = NEW.run_id
      ORDER BY ordinal DESC LIMIT 1;
    SELECT (document->>'max_repair_attempts')::integer INTO repair_limit
      FROM policy_snapshots WHERE policy_hash = run.policy_hash;
    SELECT (SELECT count(*) FROM repair_attempts WHERE run_id = NEW.run_id) +
           (SELECT count(*) FROM ci_repair_intents WHERE run_id = NEW.run_id)
      INTO repair_used;
    IF run.state IS DISTINCT FROM 'VERIFIED' OR
       run.candidate_id IS DISTINCT FROM NEW.failed_candidate_id OR
       run.verification_id IS DISTINCT FROM NEW.failed_verification_id OR
       run.current_session_id IS DISTINCT FROM NEW.session_id OR
       batch.id IS NULL OR batch.gate IS DISTINCT FROM 'FAIL' OR
       batch.head_link_id IS DISTINCT FROM latest.id OR
       NEW.head_link_id IS DISTINCT FROM latest.id OR
       batch.id IS DISTINCT FROM (SELECT id FROM pr_observation_batches
           WHERE head_link_id = latest.id ORDER BY observed_at DESC, id DESC LIMIT 1) OR
       batch.candidate_id IS DISTINCT FROM run.candidate_id OR
       batch.verification_id IS DISTINCT FROM run.verification_id OR
       repair_used >= repair_limit OR NEW.status <> 'PLANNED' OR
       NEW.message_item_id IS NOT NULL OR NEW.result_turn_id IS NOT NULL OR
       NOT EXISTS (SELECT 1 FROM jsonb_array_elements(batch.findings) finding
         WHERE finding->>'code' = 'ADD_ARITHMETIC' OR
               (finding->>'code' = 'REQUIRED_CHECK_FAILED' AND
                finding->>'conclusion' = 'failure')) OR
       EXISTS (SELECT 1 FROM jsonb_array_elements(batch.findings) finding
         WHERE finding->>'code' NOT IN ('REQUIRED_CHECK_FAILED', 'ADD_ARITHMETIC') OR
               (finding->>'code' = 'REQUIRED_CHECK_FAILED' AND
                finding->>'conclusion' <> 'failure')) THEN
        RAISE EXCEPTION 'CI repair requires current exact-head approved failure and budget';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER ci_repair_insert_guard BEFORE INSERT ON ci_repair_intents
    FOR EACH ROW EXECUTE FUNCTION guard_ci_repair_insert();

CREATE FUNCTION guard_ci_repair_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.run_id, NEW.head_link_id, NEW.observation_id,
        NEW.failed_candidate_id, NEW.failed_verification_id, NEW.session_id,
        NEW.input_key, NEW.input_sha256, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.run_id, OLD.head_link_id, OLD.observation_id,
        OLD.failed_candidate_id, OLD.failed_verification_id, OLD.session_id,
        OLD.input_key, OLD.input_sha256, OLD.created_at) OR
       NOT ((OLD.status = 'PLANNED' AND NEW.status = 'UNCERTAIN' AND
             NEW.message_item_id IS NULL AND NEW.result_turn_id IS NULL) OR
            (OLD.status = 'UNCERTAIN' AND NEW.status = 'OBSERVED' AND
             NEW.message_item_id IS NOT NULL AND NEW.result_turn_id IS NOT NULL)) THEN
        RAISE EXCEPTION 'CI repair intent is immutable or transition is illegal';
    END IF;
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;
CREATE TRIGGER ci_repair_update_guard BEFORE UPDATE ON ci_repair_intents
    FOR EACH ROW EXECUTE FUNCTION guard_ci_repair_update();
CREATE TRIGGER ci_repair_no_delete BEFORE DELETE ON ci_repair_intents
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_row();

CREATE OR REPLACE FUNCTION guard_repair_insert() RETURNS trigger LANGUAGE plpgsql AS $$
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
       run.current_session_id IS DISTINCT FROM NEW.session_id OR (SELECT count(*) FROM repair_attempts WHERE run_id = NEW.run_id) +
       (SELECT count(*) FROM ci_repair_intents WHERE run_id = NEW.run_id) >= repair_limit OR
       NEW.ordinal > repair_limit OR
       NEW.ordinal <> (SELECT count(*) + 1 FROM repair_attempts WHERE run_id = NEW.run_id) OR
       NEW.status <> 'PLANNED' THEN
        RAISE EXCEPTION 'repair attempt violates state, lineage, or budget';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION guard_candidate_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE run workflow_runs%ROWTYPE;
DECLARE prior candidate_artifacts%ROWTYPE;
DECLARE attempt repair_attempts%ROWTYPE;
DECLARE ci ci_repair_intents%ROWTYPE;
BEGIN
    SELECT * INTO run FROM workflow_runs WHERE id = NEW.run_id;
    IF NEW.source_session_id IS DISTINCT FROM run.current_session_id OR
       NEW.source_turn_id IS NULL OR length(NEW.source_turn_id) = 0 THEN
        RAISE EXCEPTION 'candidate source is not a saved turn in current session';
    END IF;
    IF NEW.ordinal = 1 THEN
        IF run.state <> 'RECEIVED' OR NEW.repair_attempt_id IS NOT NULL OR
           NEW.ci_repair_id IS NOT NULL THEN
            RAISE EXCEPTION 'first candidate requires a new run';
        END IF;
    ELSE
        SELECT * INTO prior FROM candidate_artifacts WHERE id = run.candidate_id;
        SELECT * INTO attempt FROM repair_attempts WHERE id = NEW.repair_attempt_id;
        SELECT * INTO ci FROM ci_repair_intents WHERE id = NEW.ci_repair_id;
        IF run.state = 'VERIFIED' THEN
            IF NEW.repair_attempt_id IS NOT NULL OR ci.status IS DISTINCT FROM 'OBSERVED' OR
               ci.run_id IS DISTINCT FROM NEW.run_id OR
               ci.failed_candidate_id IS DISTINCT FROM run.candidate_id OR
               ci.failed_verification_id IS DISTINCT FROM run.verification_id OR
               ci.session_id IS DISTINCT FROM NEW.source_session_id OR
               ci.result_turn_id IS DISTINCT FROM NEW.source_turn_id OR
               NEW.ordinal IS DISTINCT FROM prior.ordinal + 1 OR
               NEW.source_artifact_id IS NOT DISTINCT FROM prior.source_artifact_id OR
               NEW.archive_sha256 IS NOT DISTINCT FROM prior.archive_sha256 OR
               NEW.tree_sha256 IS NOT DISTINCT FROM prior.tree_sha256 THEN
                RAISE EXCEPTION 'candidate is not linked to observed CI repair';
            END IF;
            RETURN NEW;
        END IF;
        IF NEW.ci_repair_id IS NOT NULL OR run.state <> 'AWAITING_REPAIR_CANDIDATE' OR
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

CREATE OR REPLACE FUNCTION guard_workflow_update() RETURNS trigger LANGUAGE plpgsql AS $$
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
                (OLD.state = 'VERIFIED' AND NEW.state = 'CANDIDATE_READY' AND
                 NEW.verification_id IS NULL AND linked_candidate.ordinal =
                   (SELECT ordinal + 1 FROM candidate_artifacts WHERE id = OLD.candidate_id) AND
                 EXISTS (SELECT 1 FROM ci_repair_intents a WHERE a.id = linked_candidate.ci_repair_id
                         AND a.failed_candidate_id = OLD.candidate_id AND
                             a.failed_verification_id = OLD.verification_id AND a.status = 'OBSERVED')) OR
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
        (OLD.state = 'VERIFIED' AND NEW.state IN ('CANDIDATE_READY', 'NEEDS_HUMAN'))
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
            SELECT (SELECT count(*) FROM repair_attempts WHERE run_id = OLD.id) +
                   (SELECT count(*) FROM ci_repair_intents WHERE run_id = OLD.id)
              INTO repair_used;
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

INSERT INTO schema_migrations(version) VALUES (6);
