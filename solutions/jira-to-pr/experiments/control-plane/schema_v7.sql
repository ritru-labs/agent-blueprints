-- Phase 1F: readback-checked PR body refresh for each new immutable head.
CREATE TABLE pr_body_update_attempts (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES workflow_runs(id) ON DELETE RESTRICT,
    draft_pr_id uuid NOT NULL,
    head_link_id uuid NOT NULL UNIQUE,
    candidate_id uuid NOT NULL,
    verification_id uuid NOT NULL,
    publication_id uuid NOT NULL,
    head_commit_sha char(40) NOT NULL CHECK (head_commit_sha ~ '^[0-9a-f]{40}$'),
    previous_body_sha256 char(64) NOT NULL CHECK (previous_body_sha256 ~ '^[0-9a-f]{64}$'),
    body_sha256 char(64) NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
    operation_key char(64) NOT NULL UNIQUE CHECK (operation_key ~ '^[0-9a-f]{64}$'),
    state text NOT NULL DEFAULT 'PLANNED'
      CHECK (state IN ('PLANNED', 'OUTCOME_UNKNOWN', 'CONFIRMED')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    confirmed_at timestamptz,
    UNIQUE (run_id, head_commit_sha),
    FOREIGN KEY (head_link_id, run_id, candidate_id, verification_id,
                 publication_id, draft_pr_id, head_commit_sha)
      REFERENCES pr_head_links(id, run_id, candidate_id, verification_id,
                               publication_id, draft_pr_id, head_commit_sha)
      ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK (previous_body_sha256 <> body_sha256),
    CHECK ((state = 'CONFIRMED') = (confirmed_at IS NOT NULL))
);

CREATE FUNCTION guard_pr_body_update_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE run workflow_runs%ROWTYPE;
DECLARE draft draft_pr_attempts%ROWTYPE;
DECLARE head pr_head_links%ROWTYPE;
DECLARE prior_body char(64);
BEGIN
    SELECT * INTO run FROM workflow_runs WHERE id = NEW.run_id FOR UPDATE;
    SELECT * INTO draft FROM draft_pr_attempts WHERE id = NEW.draft_pr_id;
    SELECT * INTO head FROM pr_head_links WHERE id = NEW.head_link_id;
    IF head.ordinal = 2 THEN
        prior_body := draft.body_sha256;
    ELSE
        SELECT body_sha256 INTO prior_body FROM pr_body_update_attempts
          WHERE run_id = NEW.run_id AND head_link_id = (
            SELECT id FROM pr_head_links WHERE run_id = NEW.run_id AND ordinal = head.ordinal - 1)
          AND state = 'CONFIRMED';
    END IF;
    IF run.state IS DISTINCT FROM 'VERIFIED' OR draft.state IS DISTINCT FROM 'CONFIRMED' OR
       head.ordinal IS NULL OR head.ordinal < 2 OR
       head.id IS DISTINCT FROM (SELECT id FROM pr_head_links WHERE run_id = NEW.run_id
                                  ORDER BY ordinal DESC LIMIT 1) OR
       run.candidate_id IS DISTINCT FROM NEW.candidate_id OR
       run.verification_id IS DISTINCT FROM NEW.verification_id OR
       head.publication_id IS DISTINCT FROM NEW.publication_id OR
       head.head_commit_sha IS DISTINCT FROM NEW.head_commit_sha OR
       head.draft_pr_id IS DISTINCT FROM NEW.draft_pr_id OR
       prior_body IS DISTINCT FROM NEW.previous_body_sha256 OR
       NEW.state <> 'PLANNED' OR NEW.confirmed_at IS NOT NULL THEN
        RAISE EXCEPTION 'PR body update requires current verified head and prior body hash';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER pr_body_update_insert_guard BEFORE INSERT ON pr_body_update_attempts
    FOR EACH ROW EXECUTE FUNCTION guard_pr_body_update_insert();

CREATE FUNCTION guard_pr_body_update_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.run_id, NEW.draft_pr_id, NEW.head_link_id, NEW.candidate_id,
        NEW.verification_id, NEW.publication_id, NEW.head_commit_sha,
        NEW.previous_body_sha256, NEW.body_sha256, NEW.operation_key, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.run_id, OLD.draft_pr_id, OLD.head_link_id, OLD.candidate_id,
        OLD.verification_id, OLD.publication_id, OLD.head_commit_sha,
        OLD.previous_body_sha256, OLD.body_sha256, OLD.operation_key, OLD.created_at) OR
       NOT ((OLD.state = 'PLANNED' AND NEW.state = 'OUTCOME_UNKNOWN' AND
             NEW.confirmed_at IS NULL) OR
            (OLD.state = 'OUTCOME_UNKNOWN' AND NEW.state = 'CONFIRMED' AND
             NEW.confirmed_at IS NOT NULL)) THEN
        RAISE EXCEPTION 'PR body update intent is immutable or transition is illegal';
    END IF;
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;
CREATE TRIGGER pr_body_update_change_guard BEFORE UPDATE ON pr_body_update_attempts
    FOR EACH ROW EXECUTE FUNCTION guard_pr_body_update_change();
CREATE TRIGGER pr_body_update_no_delete BEFORE DELETE ON pr_body_update_attempts
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_row();

INSERT INTO schema_migrations(version) VALUES (7);
