-- Phase 1E GitHub intent. Existing local publication rows stay local-only.
ALTER TABLE publication_attempts ADD COLUMN remote_kind text NOT NULL DEFAULT 'local_bare'
    CHECK (remote_kind IN ('local_bare', 'github'));
ALTER TABLE publication_attempts ADD CONSTRAINT publication_draft_link UNIQUE
    (id, run_id, candidate_id, verification_id, commit_sha, branch_ref);

CREATE OR REPLACE FUNCTION guard_publication_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE run workflow_runs%ROWTYPE;
DECLARE verified verification_runs%ROWTYPE;
DECLARE policy jsonb;
BEGIN
    SELECT * INTO run FROM workflow_runs WHERE id = NEW.run_id FOR UPDATE;
    SELECT * INTO verified FROM verification_runs WHERE id = NEW.verification_id;
    SELECT document INTO policy FROM policy_snapshots WHERE policy_hash = run.policy_hash;
    IF run.state IS DISTINCT FROM 'VERIFIED' OR
       run.candidate_id IS DISTINCT FROM NEW.candidate_id OR
       run.verification_id IS DISTINCT FROM NEW.verification_id OR
       run.base_commit IS DISTINCT FROM NEW.base_commit OR
       verified.status IS DISTINCT FROM 'PASS' OR
       NEW.branch_ref IS DISTINCT FROM 'refs/heads/agent/' || NEW.run_id::text OR
       NEW.state IS DISTINCT FROM 'PLANNED' OR NEW.confirmed_at IS NOT NULL OR
       NOT COALESCE(((NEW.remote_kind = 'local_bare' AND
             policy->>'external_writes_enabled' = 'false') OR
            (NEW.remote_kind = 'github' AND
             policy->>'external_writes_enabled' = 'true' AND
             policy->'allowed_operation_kinds' ? 'git_branch')), FALSE) THEN
        RAISE EXCEPTION 'publication requires the current exact PASS candidate and allowed remote';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION guard_publication_update() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE run workflow_runs%ROWTYPE;
BEGIN
    IF (NEW.id, NEW.run_id, NEW.candidate_id, NEW.verification_id,
        NEW.archive_sha256, NEW.candidate_tree_sha256, NEW.base_commit,
        NEW.git_tree_sha, NEW.commit_sha, NEW.branch_ref, NEW.remote_id,
        NEW.publisher_policy_hash, NEW.operation_key, NEW.created_at, NEW.remote_kind)
       IS DISTINCT FROM
       (OLD.id, OLD.run_id, OLD.candidate_id, OLD.verification_id,
        OLD.archive_sha256, OLD.candidate_tree_sha256, OLD.base_commit,
        OLD.git_tree_sha, OLD.commit_sha, OLD.branch_ref, OLD.remote_id,
        OLD.publisher_policy_hash, OLD.operation_key, OLD.created_at, OLD.remote_kind) THEN
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

CREATE TABLE draft_pr_attempts (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL UNIQUE REFERENCES workflow_runs(id) ON DELETE RESTRICT,
    publication_id uuid NOT NULL UNIQUE,
    candidate_id uuid NOT NULL,
    verification_id uuid NOT NULL,
    repository text NOT NULL CHECK (repository = 'ritru-labs/agent-blueprints'),
    base_ref text NOT NULL CHECK (length(base_ref) BETWEEN 12 AND 160),
    head_ref text NOT NULL CHECK (length(head_ref) BETWEEN 12 AND 160),
    head_commit_sha char(40) NOT NULL CHECK (head_commit_sha ~ '^[0-9a-f]{40}$'),
    actor_login text NOT NULL CHECK (length(actor_login) BETWEEN 1 AND 100),
    title_sha256 char(64) NOT NULL CHECK (title_sha256 ~ '^[0-9a-f]{64}$'),
    body_sha256 char(64) NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
    operation_key char(64) NOT NULL UNIQUE CHECK (operation_key ~ '^[0-9a-f]{64}$'),
    state text NOT NULL DEFAULT 'PLANNED'
        CHECK (state IN ('PLANNED', 'OUTCOME_UNKNOWN', 'CONFIRMED')),
    pr_number integer CHECK (pr_number > 0),
    pr_url text CHECK (length(pr_url) <= 300),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    confirmed_at timestamptz,
    UNIQUE (repository, pr_number),
    FOREIGN KEY (publication_id, run_id, candidate_id, verification_id,
                 head_commit_sha, head_ref)
        REFERENCES publication_attempts(id, run_id, candidate_id, verification_id,
                                         commit_sha, branch_ref)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK ((state = 'CONFIRMED') =
           (pr_number IS NOT NULL AND pr_url IS NOT NULL AND confirmed_at IS NOT NULL))
);

CREATE FUNCTION guard_draft_pr_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE run workflow_runs%ROWTYPE;
DECLARE publication publication_attempts%ROWTYPE;
DECLARE policy jsonb;
BEGIN
    SELECT * INTO run FROM workflow_runs WHERE id = NEW.run_id FOR UPDATE;
    SELECT * INTO publication FROM publication_attempts WHERE id = NEW.publication_id;
    SELECT document INTO policy FROM policy_snapshots WHERE policy_hash = run.policy_hash;
    IF run.state IS DISTINCT FROM 'VERIFIED' OR
       run.candidate_id IS DISTINCT FROM NEW.candidate_id OR
       run.verification_id IS DISTINCT FROM NEW.verification_id OR
       publication.state IS DISTINCT FROM 'CONFIRMED' OR
       publication.remote_kind IS DISTINCT FROM 'github' OR
       NEW.head_ref IS DISTINCT FROM publication.branch_ref OR
       NEW.head_commit_sha IS DISTINCT FROM publication.commit_sha OR
       NEW.base_ref IS DISTINCT FROM policy->>'github_target_base' OR
       NEW.actor_login IS DISTINCT FROM policy->>'github_actor_login' OR
       NEW.repository IS DISTINCT FROM policy->>'repository' OR
       policy->>'external_writes_enabled' IS DISTINCT FROM 'true' OR
       (policy->'allowed_operation_kinds' ? 'draft_pr') IS DISTINCT FROM TRUE OR
       NEW.state IS DISTINCT FROM 'PLANNED' OR NEW.pr_number IS NOT NULL OR
       NEW.pr_url IS NOT NULL OR NEW.confirmed_at IS NOT NULL THEN
        RAISE EXCEPTION 'draft PR requires the current confirmed GitHub publication';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER draft_pr_insert_guard BEFORE INSERT ON draft_pr_attempts
    FOR EACH ROW EXECUTE FUNCTION guard_draft_pr_insert();

CREATE FUNCTION guard_draft_pr_update() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE run workflow_runs%ROWTYPE;
DECLARE publication publication_attempts%ROWTYPE;
BEGIN
    IF (NEW.id, NEW.run_id, NEW.publication_id, NEW.candidate_id,
        NEW.verification_id, NEW.repository, NEW.base_ref, NEW.head_ref,
        NEW.head_commit_sha, NEW.actor_login, NEW.title_sha256, NEW.body_sha256,
        NEW.operation_key, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.run_id, OLD.publication_id, OLD.candidate_id,
        OLD.verification_id, OLD.repository, OLD.base_ref, OLD.head_ref,
        OLD.head_commit_sha, OLD.actor_login, OLD.title_sha256, OLD.body_sha256,
        OLD.operation_key, OLD.created_at) THEN
        RAISE EXCEPTION 'draft PR intent is immutable';
    END IF;
    SELECT * INTO run FROM workflow_runs WHERE id = NEW.run_id FOR UPDATE;
    SELECT * INTO publication FROM publication_attempts WHERE id = NEW.publication_id;
    IF run.state IS DISTINCT FROM 'VERIFIED' OR
       publication.state IS DISTINCT FROM 'CONFIRMED' OR
       NOT ((OLD.state = 'PLANNED' AND NEW.state = 'OUTCOME_UNKNOWN' AND
             NEW.pr_number IS NULL AND NEW.pr_url IS NULL AND NEW.confirmed_at IS NULL) OR
            (OLD.state = 'OUTCOME_UNKNOWN' AND NEW.state = 'CONFIRMED' AND
             NEW.pr_number > 0 AND
             NEW.pr_url = 'https://github.com/' || NEW.repository || '/pull/' || NEW.pr_number::text AND
             NEW.confirmed_at IS NOT NULL)) THEN
        RAISE EXCEPTION 'draft PR transition requires read-back of the exact GitHub PR';
    END IF;
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;
CREATE TRIGGER draft_pr_update_guard BEFORE UPDATE ON draft_pr_attempts
    FOR EACH ROW EXECUTE FUNCTION guard_draft_pr_update();
CREATE TRIGGER draft_pr_no_delete BEFORE DELETE ON draft_pr_attempts
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_row();

INSERT INTO schema_migrations(version) VALUES (4);
