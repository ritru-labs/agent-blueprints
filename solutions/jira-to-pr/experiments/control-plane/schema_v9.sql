-- A -> B -> A on one PR head is a new observation, while consecutive A reads
-- still reconcile to the same immutable batch in application logic.
ALTER TABLE pr_observation_batches
    DROP CONSTRAINT IF EXISTS pr_observation_batches_head_link_id_payload_sha256_key;

INSERT INTO schema_migrations(version) VALUES (9);
