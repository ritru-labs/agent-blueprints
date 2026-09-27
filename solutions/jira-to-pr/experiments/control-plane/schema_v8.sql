-- Phase 1F: require one exact trusted check and latest-review approval.
CREATE OR REPLACE FUNCTION guard_pr_observation_pass() RETURNS trigger LANGUAGE plpgsql AS $$
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

INSERT INTO schema_migrations(version) VALUES (8);
