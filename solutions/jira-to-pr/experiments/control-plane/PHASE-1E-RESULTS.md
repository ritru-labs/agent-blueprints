# Phase 1E local publisher result

**Status:** Local publisher gate demonstrated. GitHub branch publication and draft PR are still open.

The [sanitized evidence](evidence/phase-1e-local-publication.json) records one saved Phase 1A candidate, one fresh independent Phase 1B Docker `PASS`, one PostgreSQL publication intent, and one commit pushed to a disposable local bare Git remote. The full Git tree was constructed from the pinned base commit plus the allowlisted candidate files. The remote commit and tree IDs matched the intent after readback.

The first publisher process intentionally exited with code `75` after the Git push and before local confirmation, leaving the publication row `OUTCOME_UNKNOWN`. A new process reloaded PostgreSQL, reconstructed the same deterministic commit, observed the exact remote branch, and moved the row to `CONFIRMED`. A repeated resume made no new durable record or branch change. The test suite also rejected a tampered artifact, a competing worker, a colliding or moved branch, a different remote identity, an uncertain absent ref, and direct mutation of the immutable publication intent.

This proof used a local remote only. It consumed no new OpenAI session and made no GitHub workflow branch or PR write. The repaired Phase 1D candidate was not republished because that live run's disposable database and artifact store were removed after its proof. A GitHub adapter still needs scoped authentication, exact remote readback, durable PR intent/reconciliation, and a separate review gate. Jira integration, merge, and deployment remain out of scope.
