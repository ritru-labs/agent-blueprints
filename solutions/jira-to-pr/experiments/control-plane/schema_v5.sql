-- Phase 1F: exact-head CI/review observations. No merge operation exists.
CREATE TABLE pr_observation_policies (
    run_id uuid PRIMARY KEY REFERENCES workflow_runs(id) ON DELETE RESTRICT,
    policy_sha256 char(64) NOT NULL CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
    document jsonb NOT NULL CHECK (octet_length(document::text) <= 4096),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER pr_observation_policy_immutable BEFORE UPDATE OR DELETE ON pr_observation_policies
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_row();

ALTER TABLE draft_pr_attempts ADD CONSTRAINT draft_pr_run_link UNIQUE (id, run_id);
ALTER TABLE publication_attempts ADD CONSTRAINT publication_head_link UNIQUE
    (id, run_id, candidate_id, verification_id, commit_sha);

CREATE TABLE pr_head_links (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES workflow_runs(id) ON DELETE RESTRICT,
    draft_pr_id uuid NOT NULL,
    publication_id uuid NOT NULL UNIQUE,
    candidate_id uuid NOT NULL,
    verification_id uuid NOT NULL,
    head_commit_sha char(40) NOT NULL CHECK (head_commit_sha ~ '^[0-9a-f]{40}$'),
    ordinal integer NOT NULL CHECK (ordinal > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (run_id, ordinal),
    UNIQUE (draft_pr_id, head_commit_sha),
    UNIQUE (id, run_id, candidate_id, verification_id, publication_id, draft_pr_id, head_commit_sha),
    FOREIGN KEY (draft_pr_id, run_id) REFERENCES draft_pr_attempts(id, run_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (publication_id, run_id, candidate_id, verification_id, head_commit_sha)
        REFERENCES publication_attempts(id, run_id, candidate_id, verification_id, commit_sha)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);
CREATE FUNCTION guard_pr_head_link() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE draft draft_pr_attempts%ROWTYPE;
DECLARE publication publication_attempts%ROWTYPE;
DECLARE prior pr_head_links%ROWTYPE;
BEGIN
    SELECT * INTO draft FROM draft_pr_attempts WHERE id = NEW.draft_pr_id;
    SELECT * INTO publication FROM publication_attempts WHERE id = NEW.publication_id;
    IF draft.state IS DISTINCT FROM 'CONFIRMED' OR publication.state IS DISTINCT FROM 'CONFIRMED' OR
       publication.remote_kind IS DISTINCT FROM 'github' OR
       draft.run_id IS DISTINCT FROM NEW.run_id OR
       publication.run_id IS DISTINCT FROM NEW.run_id OR
       publication.branch_ref IS DISTINCT FROM draft.head_ref OR
       publication.candidate_id IS DISTINCT FROM NEW.candidate_id OR
       publication.verification_id IS DISTINCT FROM NEW.verification_id OR
       publication.commit_sha IS DISTINCT FROM NEW.head_commit_sha THEN
        RAISE EXCEPTION 'PR head requires a confirmed exact publication on the draft branch';
    END IF;
    IF NEW.ordinal = 1 THEN
        IF draft.publication_id IS DISTINCT FROM NEW.publication_id OR
           draft.head_commit_sha IS DISTINCT FROM NEW.head_commit_sha THEN
            RAISE EXCEPTION 'first PR head must equal the draft creation head';
        END IF;
    ELSE
        SELECT * INTO prior FROM pr_head_links
          WHERE run_id = NEW.run_id AND ordinal = NEW.ordinal - 1;
        IF prior.id IS NULL OR prior.draft_pr_id IS DISTINCT FROM NEW.draft_pr_id OR
           prior.head_commit_sha IS NOT DISTINCT FROM NEW.head_commit_sha THEN
            RAISE EXCEPTION 'PR head lineage is invalid';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER pr_head_link_guard BEFORE INSERT ON pr_head_links
    FOR EACH ROW EXECUTE FUNCTION guard_pr_head_link();
CREATE TRIGGER pr_head_link_immutable BEFORE UPDATE OR DELETE ON pr_head_links
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_row();

CREATE TABLE pr_observation_batches (
    id uuid PRIMARY KEY,
    head_link_id uuid NOT NULL,
    run_id uuid NOT NULL,
    candidate_id uuid NOT NULL,
    verification_id uuid NOT NULL,
    publication_id uuid NOT NULL,
    draft_pr_id uuid NOT NULL,
    head_commit_sha char(40) NOT NULL,
    policy_sha256 char(64) NOT NULL,
    payload_sha256 char(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    gate text NOT NULL CHECK (gate IN ('PASS', 'PENDING', 'FAIL', 'NEEDS_HUMAN')),
    findings jsonb NOT NULL CHECK (octet_length(findings::text) <= 4096),
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (head_link_id, run_id, candidate_id, verification_id,
                 publication_id, draft_pr_id, head_commit_sha)
        REFERENCES pr_head_links(id, run_id, candidate_id, verification_id,
                                 publication_id, draft_pr_id, head_commit_sha)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (run_id) REFERENCES pr_observation_policies(run_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);
CREATE FUNCTION guard_pr_observation_batch() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE policy_hash char(64);
DECLARE policy_doc jsonb;
DECLARE finding jsonb;
BEGIN
    SELECT policy_sha256, document INTO policy_hash, policy_doc
      FROM pr_observation_policies WHERE run_id = NEW.run_id;
    IF NEW.policy_sha256 IS DISTINCT FROM policy_hash OR
       jsonb_typeof(NEW.findings) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'CI/review observation policy or findings are invalid';
    END IF;
    IF jsonb_array_length(NEW.findings) > 30 THEN
        RAISE EXCEPTION 'CI/review findings exceed the bounded count';
    END IF;
    FOR finding IN SELECT value FROM jsonb_array_elements(NEW.findings) LOOP
        IF jsonb_typeof(finding) IS DISTINCT FROM 'object' OR
           finding - ARRAY['code', 'check', 'conclusion', 'repair_code', 'review_id', 'comment_id']
             <> '{}'::jsonb OR
           finding->>'code' IS NULL OR
           finding->>'code' NOT IN ('REQUIRED_CHECK_MISSING', 'REQUIRED_CHECK_RUNNING',
               'REQUIRED_CHECK_FAILED', 'REQUIRED_APPROVAL_MISSING', 'UNSAFE_REVIEW',
               'ADD_ARITHMETIC') OR
           (finding ? 'check' AND NOT EXISTS (
             SELECT 1 FROM jsonb_array_elements(policy_doc->'required_checks') item
              WHERE item->>'name' = finding->>'check')) OR
           (finding ? 'repair_code' AND NOT EXISTS (
             SELECT 1 FROM jsonb_array_elements(policy_doc->'required_checks') item
              WHERE item->>'name' = finding->>'check' AND
                    item->>'repair_code' = finding->>'repair_code' AND
                    finding->>'conclusion' = 'failure')) THEN
            RAISE EXCEPTION 'CI/review finding is outside the trusted allowlist';
        END IF;
    END LOOP;
    RETURN NEW;
END $$;
CREATE TRIGGER pr_observation_batch_guard BEFORE INSERT ON pr_observation_batches
    FOR EACH ROW EXECUTE FUNCTION guard_pr_observation_batch();
CREATE TABLE pr_check_observations (
    id uuid PRIMARY KEY,
    batch_id uuid NOT NULL REFERENCES pr_observation_batches(id) ON DELETE RESTRICT,
    check_name text NOT NULL CHECK (length(check_name) BETWEEN 1 AND 120),
    app_slug text NOT NULL CHECK (length(app_slug) BETWEEN 1 AND 80),
    check_id bigint NOT NULL CHECK (check_id > 0),
    run_attempt integer CHECK (run_attempt > 0),
    check_suite_id bigint CHECK (check_suite_id > 0),
    head_commit_sha char(40) NOT NULL CHECK (head_commit_sha ~ '^[0-9a-f]{40}$'),
    status text NOT NULL CHECK (status IN ('queued', 'in_progress', 'completed')),
    conclusion text CHECK (conclusion IN ('success', 'failure', 'cancelled', 'timed_out',
                                         'action_required', 'neutral', 'skipped', 'stale')),
    started_at timestamptz,
    completed_at timestamptz,
    UNIQUE (batch_id, check_name, app_slug, check_id),
    CHECK ((status = 'completed') = (conclusion IS NOT NULL))
);
CREATE TABLE pr_review_observations (
    id uuid PRIMARY KEY,
    batch_id uuid NOT NULL REFERENCES pr_observation_batches(id) ON DELETE RESTRICT,
    review_id bigint NOT NULL CHECK (review_id > 0),
    reviewer text NOT NULL CHECK (length(reviewer) BETWEEN 1 AND 100),
    review_head_sha char(40) NOT NULL CHECK (review_head_sha ~ '^[0-9a-f]{40}$'),
    state text NOT NULL CHECK (state IN ('APPROVED', 'CHANGES_REQUESTED', 'COMMENTED', 'DISMISSED')),
    finding_code text CHECK (finding_code IN ('ADD_ARITHMETIC', 'UNSAFE_REVIEW')),
    body_sha256 char(64) NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
    submitted_at timestamptz,
    UNIQUE (batch_id, review_id)
);
CREATE TABLE pr_review_comment_observations (
    id uuid PRIMARY KEY,
    batch_id uuid NOT NULL REFERENCES pr_observation_batches(id) ON DELETE RESTRICT,
    comment_id bigint NOT NULL CHECK (comment_id > 0),
    reviewer text NOT NULL CHECK (length(reviewer) BETWEEN 1 AND 100),
    review_head_sha char(40) NOT NULL CHECK (review_head_sha ~ '^[0-9a-f]{40}$'),
    finding_code text CHECK (finding_code IN ('ADD_ARITHMETIC', 'UNSAFE_REVIEW')),
    body_sha256 char(64) NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
    created_at timestamptz,
    UNIQUE (batch_id, comment_id)
);
CREATE TRIGGER pr_review_comment_immutable BEFORE UPDATE OR DELETE ON pr_review_comment_observations
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_row();
CREATE FUNCTION guard_pr_check_head() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE batch_head char(40);
BEGIN
    SELECT head_commit_sha INTO batch_head FROM pr_observation_batches WHERE id = NEW.batch_id;
    IF NEW.head_commit_sha IS DISTINCT FROM batch_head THEN
        RAISE EXCEPTION 'check run belongs to a stale PR head';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER pr_check_head_guard BEFORE INSERT ON pr_check_observations
    FOR EACH ROW EXECUTE FUNCTION guard_pr_check_head();
CREATE TRIGGER pr_observation_batch_immutable BEFORE UPDATE OR DELETE ON pr_observation_batches
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_row();
CREATE TRIGGER pr_check_observation_immutable BEFORE UPDATE OR DELETE ON pr_check_observations
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_row();
CREATE TRIGGER pr_review_observation_immutable BEFORE UPDATE OR DELETE ON pr_review_observations
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_row();

CREATE FUNCTION guard_pr_observation_pass() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE required jsonb;
DECLARE item jsonb;
DECLARE approval_limit integer;
DECLARE approvals integer;
BEGIN
    IF NEW.gate <> 'PASS' THEN
        RETURN NULL;
    END IF;
    SELECT document INTO required FROM pr_observation_policies WHERE run_id = NEW.run_id;
    IF NEW.findings <> '[]'::jsonb THEN
        RAISE EXCEPTION 'PASS observation cannot include findings';
    END IF;
    FOR item IN SELECT value FROM jsonb_array_elements(required->'required_checks') LOOP
        IF (SELECT count(*) FROM pr_check_observations c
            WHERE c.batch_id = NEW.id AND c.check_name = item->>'name' AND
                  c.app_slug = item->>'app_slug') <> 1 OR
           NOT EXISTS (SELECT 1 FROM pr_check_observations c
            WHERE c.batch_id = NEW.id AND c.check_name = item->>'name' AND
                  c.app_slug = item->>'app_slug' AND c.head_commit_sha = NEW.head_commit_sha AND
                  c.status = 'completed' AND c.conclusion = 'success') THEN
            RAISE EXCEPTION 'PASS requires every exact-head trusted check';
        END IF;
    END LOOP;
    approval_limit := (required->>'required_approvals')::integer;
    SELECT count(*) INTO approvals FROM (
      SELECT DISTINCT ON (reviewer) reviewer, state, finding_code
      FROM pr_review_observations r WHERE r.batch_id = NEW.id AND
        r.review_head_sha = NEW.head_commit_sha AND required->'review_actors' ? r.reviewer
      ORDER BY reviewer, review_id DESC
    ) latest WHERE latest.state = 'APPROVED' AND latest.finding_code IS NULL;
    IF approvals < approval_limit OR EXISTS (
        SELECT 1 FROM pr_review_observations r WHERE r.batch_id = NEW.id AND
          r.review_head_sha = NEW.head_commit_sha AND
          (r.finding_code IS NOT NULL OR r.state = 'CHANGES_REQUESTED')) THEN
        RAISE EXCEPTION 'PASS requires exact-head trusted review clearance';
    END IF;
    IF EXISTS (SELECT 1 FROM pr_review_comment_observations c
               WHERE c.batch_id = NEW.id AND c.review_head_sha = NEW.head_commit_sha
                 AND c.finding_code IS NOT NULL) THEN
        RAISE EXCEPTION 'PASS cannot ignore exact-head review comments';
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER pr_observation_pass_guard
    AFTER INSERT ON pr_observation_batches DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION guard_pr_observation_pass();

INSERT INTO schema_migrations(version) VALUES (5);
