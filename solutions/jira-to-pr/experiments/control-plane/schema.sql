-- Phase 1C synthetic control plane. Apply to a dedicated PostgreSQL database.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE policy_snapshots (
    policy_hash char(64) PRIMARY KEY CHECK (policy_hash ~ '^[0-9a-f]{64}$'),
    document jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE workflow_runs (
    id uuid PRIMARY KEY,
    task_key text NOT NULL UNIQUE CHECK (length(task_key) BETWEEN 1 AND 120),
    repository text NOT NULL,
    base_commit char(40) NOT NULL CHECK (base_commit ~ '^[0-9a-f]{40}$'),
    session_id text NOT NULL,
    policy_hash char(64) NOT NULL REFERENCES policy_snapshots(policy_hash),
    state text NOT NULL DEFAULT 'RECEIVED' CHECK (state IN
        ('RECEIVED', 'CANDIDATE_READY', 'VERIFYING', 'VERIFIED', 'NEEDS_HUMAN')),
    candidate_id uuid,
    verification_id uuid,
    version bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT workflow_state_shape CHECK (
        (state = 'RECEIVED' AND candidate_id IS NULL AND verification_id IS NULL) OR
        (state IN ('CANDIDATE_READY', 'VERIFYING') AND candidate_id IS NOT NULL AND verification_id IS NULL) OR
        (state = 'VERIFIED' AND candidate_id IS NOT NULL AND verification_id IS NOT NULL) OR
        state = 'NEEDS_HUMAN'
    ),
    UNIQUE (id, candidate_id)
);

CREATE TABLE candidate_artifacts (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES workflow_runs(id) ON DELETE RESTRICT,
    source_artifact_id text NOT NULL CHECK (length(source_artifact_id) > 0),
    archive_sha256 char(64) NOT NULL CHECK (archive_sha256 ~ '^[0-9a-f]{64}$'),
    tree_sha256 char(64) NOT NULL CHECK (tree_sha256 ~ '^[0-9a-f]{64}$'),
    base_commit char(40) NOT NULL CHECK (base_commit ~ '^[0-9a-f]{40}$'),
    storage_path text NOT NULL CHECK (length(storage_path) > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (id, run_id),
    UNIQUE (id, run_id, archive_sha256, tree_sha256, base_commit),
    UNIQUE (run_id, archive_sha256, tree_sha256)
);

CREATE TABLE verification_runs (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    candidate_id uuid NOT NULL,
    archive_sha256 char(64) NOT NULL CHECK (archive_sha256 ~ '^[0-9a-f]{64}$'),
    tree_sha256 char(64) NOT NULL CHECK (tree_sha256 ~ '^[0-9a-f]{64}$'),
    base_commit char(40) NOT NULL CHECK (base_commit ~ '^[0-9a-f]{40}$'),
    status text NOT NULL CHECK (status IN ('PASS', 'FAIL')),
    verifier_image_id text NOT NULL,
    evidence jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (id, run_id, candidate_id),
    UNIQUE (candidate_id),
    FOREIGN KEY (candidate_id, run_id, archive_sha256, tree_sha256, base_commit)
      REFERENCES candidate_artifacts(id, run_id, archive_sha256, tree_sha256, base_commit)
      ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT verification_evidence_link CHECK ((
        jsonb_typeof(evidence) = 'object' AND
        evidence ?& ARRAY['status', 'artifact_sha256', 'candidate_tree_sha256',
                          'base_commit', 'verifier_image_id'] AND
        evidence->>'status' = status AND
        evidence->>'artifact_sha256' = archive_sha256 AND
        evidence->>'candidate_tree_sha256' = tree_sha256 AND
        evidence->>'base_commit' = base_commit AND
        evidence->>'verifier_image_id' = verifier_image_id
    ) IS TRUE)
);

ALTER TABLE workflow_runs ADD CONSTRAINT workflow_candidate_link
    FOREIGN KEY (candidate_id, id) REFERENCES candidate_artifacts(id, run_id)
    ON UPDATE RESTRICT ON DELETE RESTRICT;
ALTER TABLE workflow_runs ADD CONSTRAINT workflow_verification_link
    FOREIGN KEY (verification_id, id, candidate_id)
    REFERENCES verification_runs(id, run_id, candidate_id)
    ON UPDATE RESTRICT ON DELETE RESTRICT;

CREATE TABLE workflow_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES workflow_runs(id) ON DELETE RESTRICT,
    from_state text,
    to_state text NOT NULL,
    reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 200),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE external_operations (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES workflow_runs(id) ON DELETE RESTRICT,
    operation_key text NOT NULL UNIQUE CHECK (length(operation_key) BETWEEN 1 AND 200),
    kind text NOT NULL CHECK (kind = 'synthetic_notice'),
    payload_sha256 char(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    state text NOT NULL DEFAULT 'PLANNED' CHECK (state IN ('PLANNED', 'SUCCEEDED', 'OUTCOME_UNKNOWN')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE FUNCTION reject_immutable_row() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME;
END $$;

CREATE TRIGGER policy_immutable BEFORE UPDATE OR DELETE ON policy_snapshots
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_row();
CREATE TRIGGER candidate_immutable BEFORE UPDATE OR DELETE ON candidate_artifacts
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_row();
CREATE TRIGGER verification_immutable BEFORE UPDATE OR DELETE ON verification_runs
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_row();
CREATE TRIGGER event_immutable BEFORE UPDATE OR DELETE ON workflow_events
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_row();
CREATE TRIGGER workflow_no_delete BEFORE DELETE ON workflow_runs
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_row();
CREATE TRIGGER external_operation_no_delete BEFORE DELETE ON external_operations
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_row();

CREATE FUNCTION guard_workflow_update() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE linked_status text;
BEGIN
    IF (NEW.id, NEW.task_key, NEW.repository, NEW.base_commit, NEW.session_id, NEW.policy_hash, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.task_key, OLD.repository, OLD.base_commit, OLD.session_id, OLD.policy_hash, OLD.created_at) THEN
        RAISE EXCEPTION 'workflow identity and policy are immutable';
    END IF;
    IF NEW.candidate_id IS DISTINCT FROM OLD.candidate_id AND
       NOT (OLD.candidate_id IS NULL AND OLD.state = 'RECEIVED' AND
            NEW.state = 'CANDIDATE_READY') THEN
        RAISE EXCEPTION 'candidate link can only be set at intake';
    END IF;
    IF NEW.verification_id IS DISTINCT FROM OLD.verification_id AND
       NOT (OLD.verification_id IS NULL AND OLD.state = 'VERIFYING' AND
            NEW.state IN ('VERIFIED', 'NEEDS_HUMAN')) THEN
        RAISE EXCEPTION 'verification link can only be set at result commit';
    END IF;
    IF NOT (
        (OLD.state = 'RECEIVED' AND NEW.state IN ('CANDIDATE_READY', 'NEEDS_HUMAN')) OR
        (OLD.state = 'CANDIDATE_READY' AND NEW.state IN ('VERIFYING', 'NEEDS_HUMAN')) OR
        (OLD.state = 'VERIFYING' AND NEW.state IN ('VERIFIED', 'NEEDS_HUMAN')) OR
        (OLD.state = 'VERIFIED' AND NEW.state = 'NEEDS_HUMAN')
    ) THEN
        RAISE EXCEPTION 'illegal workflow transition: % to %', OLD.state, NEW.state;
    END IF;
    IF NEW.state = 'VERIFIED' THEN
        SELECT status INTO linked_status FROM verification_runs
        WHERE id = NEW.verification_id AND run_id = NEW.id AND candidate_id = NEW.candidate_id;
        IF linked_status IS DISTINCT FROM 'PASS' THEN
            RAISE EXCEPTION 'VERIFIED requires a linked PASS verification';
        END IF;
    END IF;
    NEW.version := OLD.version + 1;
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;
CREATE TRIGGER workflow_update_guard BEFORE UPDATE ON workflow_runs
    FOR EACH ROW EXECUTE FUNCTION guard_workflow_update();

CREATE FUNCTION guard_external_operation_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.run_id, NEW.operation_key, NEW.kind, NEW.payload_sha256, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.run_id, OLD.operation_key, OLD.kind, OLD.payload_sha256, OLD.created_at) THEN
        RAISE EXCEPTION 'external operation intent is immutable';
    END IF;
    IF OLD.state <> 'PLANNED' OR NEW.state NOT IN ('SUCCEEDED', 'OUTCOME_UNKNOWN') THEN
        RAISE EXCEPTION 'illegal external operation transition';
    END IF;
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;
CREATE TRIGGER external_operation_update_guard BEFORE UPDATE ON external_operations
    FOR EACH ROW EXECUTE FUNCTION guard_external_operation_update();

INSERT INTO schema_migrations(version) VALUES (1);
