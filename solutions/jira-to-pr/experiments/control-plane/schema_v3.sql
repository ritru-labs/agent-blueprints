-- Phase 1E: one immutable publication intent for the exact verified candidate.
-- The first adapter is a disposable local bare Git remote. No GitHub API write
-- is enabled by this migration.
CREATE TABLE publication_attempts (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL UNIQUE REFERENCES workflow_runs(id) ON DELETE RESTRICT,
    candidate_id uuid NOT NULL,
    verification_id uuid NOT NULL,
    archive_sha256 char(64) NOT NULL CHECK (archive_sha256 ~ '^[0-9a-f]{64}$'),
    candidate_tree_sha256 char(64) NOT NULL CHECK (candidate_tree_sha256 ~ '^[0-9a-f]{64}$'),
    base_commit char(40) NOT NULL CHECK (base_commit ~ '^[0-9a-f]{40}$'),
    git_tree_sha char(40) NOT NULL CHECK (git_tree_sha ~ '^[0-9a-f]{40}$'),
    commit_sha char(40) NOT NULL CHECK (commit_sha ~ '^[0-9a-f]{40}$'),
    branch_ref text NOT NULL CHECK (length(branch_ref) BETWEEN 1 AND 160),
    remote_id char(64) NOT NULL CHECK (remote_id ~ '^[0-9a-f]{64}$'),
    publisher_policy_hash char(64) NOT NULL CHECK (publisher_policy_hash ~ '^[0-9a-f]{64}$'),
    operation_key char(64) NOT NULL UNIQUE CHECK (operation_key ~ '^[0-9a-f]{64}$'),
    state text NOT NULL DEFAULT 'PLANNED'
        CHECK (state IN ('PLANNED', 'OUTCOME_UNKNOWN', 'CONFIRMED')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    confirmed_at timestamptz,
    UNIQUE (remote_id, branch_ref),
    FOREIGN KEY (verification_id, run_id, candidate_id)
        REFERENCES verification_runs(id, run_id, candidate_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (candidate_id, run_id, archive_sha256, candidate_tree_sha256, base_commit)
        REFERENCES candidate_artifacts(id, run_id, archive_sha256, tree_sha256, base_commit)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK ((state = 'CONFIRMED') = (confirmed_at IS NOT NULL))
);

CREATE FUNCTION guard_publication_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE run workflow_runs%ROWTYPE;
DECLARE verified verification_runs%ROWTYPE;
BEGIN
    SELECT * INTO run FROM workflow_runs WHERE id = NEW.run_id FOR UPDATE;
    SELECT * INTO verified FROM verification_runs WHERE id = NEW.verification_id;
    IF run.state IS DISTINCT FROM 'VERIFIED' OR
       run.candidate_id IS DISTINCT FROM NEW.candidate_id OR
       run.verification_id IS DISTINCT FROM NEW.verification_id OR
       run.base_commit IS DISTINCT FROM NEW.base_commit OR
       verified.status IS DISTINCT FROM 'PASS' OR
       NEW.branch_ref IS DISTINCT FROM 'refs/heads/agent/' || NEW.run_id::text OR
       NEW.state IS DISTINCT FROM 'PLANNED' OR NEW.confirmed_at IS NOT NULL THEN
        RAISE EXCEPTION 'publication requires the current exact PASS candidate';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER publication_insert_guard BEFORE INSERT ON publication_attempts
    FOR EACH ROW EXECUTE FUNCTION guard_publication_insert();

CREATE FUNCTION guard_publication_update() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE run workflow_runs%ROWTYPE;
BEGIN
    IF (NEW.id, NEW.run_id, NEW.candidate_id, NEW.verification_id,
        NEW.archive_sha256, NEW.candidate_tree_sha256, NEW.base_commit,
        NEW.git_tree_sha, NEW.commit_sha, NEW.branch_ref, NEW.remote_id,
        NEW.publisher_policy_hash, NEW.operation_key, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.run_id, OLD.candidate_id, OLD.verification_id,
        OLD.archive_sha256, OLD.candidate_tree_sha256, OLD.base_commit,
        OLD.git_tree_sha, OLD.commit_sha, OLD.branch_ref, OLD.remote_id,
        OLD.publisher_policy_hash, OLD.operation_key, OLD.created_at) THEN
        RAISE EXCEPTION 'publication intent is immutable';
    END IF;
    SELECT * INTO run FROM workflow_runs WHERE id = NEW.run_id FOR UPDATE;
    IF run.state IS DISTINCT FROM 'VERIFIED' OR
       run.candidate_id IS DISTINCT FROM NEW.candidate_id OR
       run.verification_id IS DISTINCT FROM NEW.verification_id OR
       NOT ((OLD.state = 'PLANNED' AND NEW.state = 'OUTCOME_UNKNOWN' AND
             NEW.confirmed_at IS NULL) OR
            (OLD.state = 'OUTCOME_UNKNOWN' AND NEW.state = 'CONFIRMED' AND
             NEW.confirmed_at IS NOT NULL)) THEN
        RAISE EXCEPTION 'publication transition requires the current verified candidate';
    END IF;
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;
CREATE TRIGGER publication_update_guard BEFORE UPDATE ON publication_attempts
    FOR EACH ROW EXECUTE FUNCTION guard_publication_update();
CREATE TRIGGER publication_no_delete BEFORE DELETE ON publication_attempts
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_row();

INSERT INTO schema_migrations(version) VALUES (3);
